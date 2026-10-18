"""Génération d'image via OpenWebUI + share sur Immich self-hosted.

Deux surfaces d'entrée qui appellent le même moteur `generate_and_share()` :

    - **Command** : `!img <prompt>` — prompt RAW, l'user assume sa formulation.
    - **Natural** : "hey bot, fais-moi une image de X" — via query_intent
      pipeline, le LLM traduit FR→EN et enrichit le prompt pour SDXL/Flux avant
      d'appeler generate_and_share().

Pipeline interne :
    1. Semaphore acquire (max_concurrent globalement)
    2. POST OpenWebUI /api/v1/images/generations (reuse llm_url + llm_api_key)
    3. Récupère l'image (URL retournée par OWU OU base64 inline)
    4. POST Immich /api/assets (multipart) → assetId
    5. POST Immich /api/shared-links → share URL
    6. Post l'URL dans le chan IRC

Semaphore + throttle protègent :
    - Le GPU derrière OpenWebUI (image gen = HEAVY)
    - Un troll qui spam `!img` sans limite
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Optional

import httpx

from .db import (
    get_image_gen_config,
    get_irc_config,
    get_llm_config,
    user_acl_check,
)
from .event_bus import Event, bus


log = logging.getLogger("chatbot.image_gen")

# Semaphore global — recréé lazy pour prendre en compte les changements de config
_SEMA: Optional[asyncio.Semaphore] = None
_SEMA_SIZE = 0

# Throttle par nick (dernière génération lancée) — mémoire in-process
_LAST_BY_NICK: dict[str, float] = {}

_FALLBACK_PREFIXES = ("!", ".", "+", "~")


def _get_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    """Semaphore global. Recréé si `max_concurrent` a changé côté config."""
    global _SEMA, _SEMA_SIZE
    if _SEMA is None or _SEMA_SIZE != max_concurrent:
        _SEMA = asyncio.Semaphore(max(1, max_concurrent))
        _SEMA_SIZE = max(1, max_concurrent)
    return _SEMA


def _throttled(nick: str, per_nick_sec: float) -> bool:
    now = time.monotonic()
    last = _LAST_BY_NICK.get(nick.lower(), 0.0)
    return (now - last) < per_nick_sec


def _mark_throttle(nick: str) -> None:
    _LAST_BY_NICK[nick.lower()] = time.monotonic()


# --------------------------------------------------------------------------
# Command handler : !img <prompt>
# --------------------------------------------------------------------------

async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """Traite `!img <prompt>`. True = consommé (même si refusé silencieusement)."""
    stripped = message.strip()
    if not stripped:
        return False

    try:
        prefix = get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        prefix = "!"

    # Match `<prefix>img <prompt>` avec fallback prefixes
    prompt: str | None = None
    for p in (prefix, *_FALLBACK_PREFIXES):
        head = p + "img"
        if stripped.startswith(head + " ") or stripped.startswith(head + "\t"):
            prompt = stripped[len(head):].strip()
            break
        if stripped == head:
            await client.message(channel, f"{source}: usage — {prefix}img <description>")
            return True
    if prompt is None:
        return False
    if not prompt:
        await client.message(channel, f"{source}: usage — {prefix}img <description>")
        return True

    cfg = get_image_gen_config()
    if not cfg.enabled:
        await client.message(channel, f"{source}: image gen désactivée (config /images).")
        return True

    # ACL guard : deny-by-default. Feature coûteuse (GPU + API OpenWebUI),
    # jamais grandfathered — un user sans ACL allow_image_gen (ou is_admin)
    # est refusé silencieusement. Log admin only via event bus.
    is_admin = user_acl_check(source, ident, host, "is_admin")
    if not is_admin and not user_acl_check(source, ident, host, "allow_image_gen"):
        await bus.publish(Event(type="system", payload={
            "message": f"[image_gen] refusé (pas d'ACL allow_image_gen) — {source}"
        }))
        return True  # consommé silencieusement

    if _throttled(source, cfg.throttle_per_nick_sec):
        await client.message(
            channel,
            f"{source}: doucement — attends {cfg.throttle_per_nick_sec:.0f}s "
            f"entre 2 images.",
        )
        return True
    _mark_throttle(source)

    # Fire-and-forget : ack immédiat, gen en background, URL postée à la fin
    await client.message(channel, f"🎨 {source}: génération en cours...")
    asyncio.create_task(
        _run_and_post(client, source, channel, prompt, cfg),
        name=f"image_gen:{source}:{int(time.time())}",
    )
    return True


async def _run_and_post(client, source: str, channel: str,
                        prompt: str, cfg) -> None:
    """Task background : gen + upload + share + post URL. Jamais raise."""
    sema = _get_semaphore(cfg.max_concurrent)
    async with sema:
        try:
            url = await generate_and_share(prompt=prompt, requester=source, cfg=cfg)
        except Exception as e:  # noqa: BLE001
            log.exception("image_gen failed for %s", source)
            await bus.publish(Event(type="error", payload={
                "message": f"image_gen: {e}"
            }))
            try:
                await client.message(channel, f"{source}: ❌ échec de la génération ({e})")
            except Exception:  # noqa: BLE001
                pass
            return

    if not url:
        try:
            await client.message(channel, f"{source}: ❌ aucune image générée.")
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        await client.message(channel, f"🖼 {source}: {url}")
        await bus.publish(Event(type="system", payload={
            "message": f"[image_gen] {source} → {url} (prompt: {prompt[:80]})"
        }))
    except Exception:  # noqa: BLE001
        log.exception("post image url failed")


# --------------------------------------------------------------------------
# Core : gen via OpenWebUI + upload Immich + share
# --------------------------------------------------------------------------

async def generate_and_share(prompt: str, requester: str,
                             cfg=None) -> Optional[str]:
    """Génère une image via OpenWebUI, l'upload sur Immich, retourne share URL.

    Peut aussi être appelé depuis un autre chemin (ex natural language via
    query_intent). Le semaphore + throttle ne sont PAS ré-appliqués ici — c'est
    au caller de les gérer (try_handle le fait pour le command mode).
    """
    if cfg is None:
        cfg = get_image_gen_config()
    if not cfg.enabled:
        raise RuntimeError("image_gen désactivée")
    if not cfg.immich_url or not cfg.immich_api_key:
        raise RuntimeError("Immich pas configuré (voir /images)")

    llm_cfg = get_llm_config()
    if not llm_cfg.llm_url:
        raise RuntimeError("LLM URL pas configurée")

    # 1. Génération OpenWebUI
    image_bytes = await _openwebui_generate(prompt, llm_cfg, cfg)
    if not image_bytes:
        raise RuntimeError("OpenWebUI n'a retourné aucune image")

    # 2. Upload Immich
    asset_id = await _immich_upload(image_bytes, requester, prompt, cfg)
    if not asset_id:
        raise RuntimeError("Upload Immich a échoué")

    # 3. Share link
    share_url = await _immich_share(asset_id, cfg)
    return share_url


async def _openwebui_generate(prompt: str, llm_cfg, cfg) -> Optional[bytes]:
    """POST /api/v1/images/generations. Retourne les bytes de la 1re image."""
    headers = {"Content-Type": "application/json"}
    if llm_cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {llm_cfg.llm_api_key}"

    payload: dict = {
        "prompt": prompt,
        "n": max(1, cfg.n),
        "size": cfg.size or "1024x1024",
    }
    if cfg.model:
        payload["model"] = cfg.model

    url = f"{llm_cfg.llm_url.rstrip('/')}/api/v1/images/generations"
    timeout = httpx.Timeout(cfg.fetch_timeout_sec, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
        r = await c.post(url, json=payload)
        if r.status_code >= 400:
            body_snippet = (r.text or "")[:500].replace("\n", " ")
            raise RuntimeError(f"OWU {r.status_code}: {body_snippet}")
        data = r.json()

    # Deux shapes vues côté OpenWebUI :
    #   - OpenAI-compat strict : {"data": [{"url": "..."} | {"b64_json": "..."}]}
    #   - ComfyUI proxy direct : [{"url": "..."}] (list top-level)
    #   - Certains renvoient aussi {"images": [...]}
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("data") or data.get("images") or []
    else:
        raise RuntimeError(f"OWU shape inattendue: {type(data).__name__}")
    if not entries:
        raise RuntimeError(f"OWU response vide: {str(data)[:300]}")
    first = entries[0]
    # Si c'est une string, c'est probablement directement une URL
    if isinstance(first, str):
        first = {"url": first}
    if not isinstance(first, dict):
        raise RuntimeError(f"OWU entry shape inattendue: {type(first).__name__}")

    # Cherche du base64 (multiples clés selon backend)
    for b64_key in ("b64_json", "b64", "image", "data"):
        val = first.get(b64_key)
        if val and isinstance(val, str) and len(val) > 100 \
                and not val.startswith(("http://", "https://", "/")):
            try:
                return base64.b64decode(val)
            except Exception:  # noqa: BLE001
                continue  # essaie la clé suivante

    # Cherche une URL (multiples clés selon backend)
    for url_key in ("url", "image_url", "src"):
        val = first.get(url_key)
        if val and isinstance(val, str) and (val.startswith(("http://", "https://", "/"))):
            img_url = val
            if img_url.startswith("/"):
                img_url = llm_cfg.llm_url.rstrip("/") + img_url
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
                rr = await c.get(img_url)
                rr.raise_for_status()
                return rr.content

    raise RuntimeError(f"OWU entry sans image reconnaissable: keys={list(first.keys())}")


async def _immich_upload(image_bytes: bytes, requester: str,
                         prompt: str, cfg) -> Optional[str]:
    """POST /api/assets (multipart). Retourne l'assetId."""
    from datetime import datetime, timezone

    headers = {
        "x-api-key": cfg.immich_api_key,
        "Accept": "application/json",
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    # Truncate prompt in filename to keep it sane
    prompt_slug = "".join(c if c.isalnum() else "_" for c in prompt[:40]).strip("_")
    filename = f"chatbot-{requester}-{prompt_slug}.png"

    files = {
        "assetData": (filename, image_bytes, "image/png"),
    }
    form = {
        "deviceAssetId": f"chatbot-irc-{int(time.time())}-{requester}",
        "deviceId": "chatbot-irc",
        "fileCreatedAt": now_iso,
        "fileModifiedAt": now_iso,
    }

    url = f"{cfg.immich_url.rstrip('/')}/api/assets"
    timeout = httpx.Timeout(60.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
        r = await c.post(url, data=form, files=files)
        if r.status_code >= 400:
            body_snippet = (r.text or "")[:500].replace("\n", " ")
            raise RuntimeError(f"Immich upload {r.status_code}: {body_snippet}")
        data = r.json()

    return data.get("id")


async def _immich_share(asset_id: str, cfg) -> Optional[str]:
    """POST /api/shared-links. Retourne l'URL publique du share."""
    from datetime import datetime, timedelta, timezone

    headers = {
        "x-api-key": cfg.immich_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: dict = {
        "type": "INDIVIDUAL",
        "assetIds": [asset_id],
        "allowDownload": True,
        "allowUpload": False,
        "showMetadata": False,
    }
    if cfg.share_expiry_hours and cfg.share_expiry_hours > 0:
        exp = datetime.now(timezone.utc) + timedelta(hours=cfg.share_expiry_hours)
        payload["expiresAt"] = exp.isoformat()

    url = f"{cfg.immich_url.rstrip('/')}/api/shared-links"
    timeout = httpx.Timeout(30.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
        r = await c.post(url, json=payload)
        if r.status_code >= 400:
            body_snippet = (r.text or "")[:500].replace("\n", " ")
            raise RuntimeError(f"Immich share {r.status_code}: {body_snippet}")
        data = r.json()

    # Immich retourne {"key": "abc123", ...} — l'URL publique = <immich_url>/share/<key>
    key = data.get("key")
    if not key:
        return None
    return f"{cfg.immich_url.rstrip('/')}/share/{key}"
