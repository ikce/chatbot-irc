"""Handler pour les commandes !bible / !bible-apo / !bible-meme.

Séparé de l'irc_client pour garder ce dernier lisible. Appelé depuis
ChatterBot.on_message avant la logique trigger classique.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import bible as bible_mod
from .db import (
    get_bible_config,
    get_irc_config,
    get_llm_config,
    log_message,
    user_acl_check,
)
from .event_bus import Event, bus

log = logging.getLogger("chatbot.bible.handler")


# Throttle mémoire par mask + global — indépendant du throttle LLM classique
_last_by_mask: dict[str, float] = {}
_last_global: float = 0.0


def _acl_check(nick: str, ident: str, host: str) -> bool:
    """True si le user a `allow_bible` sur son UserAcl (ou est admin)."""
    if user_acl_check(nick, ident, host, "is_admin"):
        return True
    return user_acl_check(nick, ident, host, "allow_bible")


def _throttled(user_mask: str, per_mask_sec: float, global_sec: float) -> bool:
    global _last_global
    now = time.monotonic()
    if now - _last_global < global_sec:
        return True
    if now - _last_by_mask.get(user_mask, 0.0) < per_mask_sec:
        return True
    return False


def _mark_throttle(user_mask: str) -> None:
    global _last_global
    now = time.monotonic()
    _last_global = now
    _last_by_mask[user_mask] = now


def _parse_command(prefix: str, message: str) -> tuple[str, str] | None:
    """Retourne (mode, ref_text) si le message est une commande bible, sinon None.

    mode ∈ {"default", "apologist", "meme"}. La sous-commande (-apo/-meme) force
    le mode ; !bible tout court prend le default de la config.
    """
    if not message.startswith(prefix):
        return None
    body = message[len(prefix):].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if cmd == "bible":
        return "default", rest
    if cmd in ("bible-apo", "bible-apologist"):
        return "apologist", rest
    if cmd in ("bible-meme", "bible-shitpost"):
        return "meme", rest
    return None


async def _llm_commentary(passages_render: str, mode: str) -> str:
    """Appelle le LLM pour un commentaire du passage. mode = apologist | meme."""
    cfg = get_bible_config()
    llm_cfg = get_llm_config()
    system = cfg.prompt_meme if mode == "meme" else cfg.prompt_apologist
    headers = {"Content-Type": "application/json"}
    if llm_cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {llm_cfg.llm_api_key}"
    payload = {
        "model": llm_cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Passage biblique à commenter :\n{passages_render}"},
        ],
        "max_tokens": llm_cfg.max_tokens,
        "temperature": llm_cfg.temperature,
        "stream": False,
    }
    timeout = httpx.Timeout(120.0, connect=10.0)
    url = f"{llm_cfg.llm_url.rstrip('/')}/api/chat/completions"
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        log.warning("LLM shape inattendue pour commentary: %r", data)
        return ""
    return " ".join(text.splitlines()).strip()


def _chunk(text: str, size: int = 400) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """Traite le message si c'est une commande bible. Retourne True si consommé.

    Consommé = on skip TOUT le reste du pipeline chat classique.
    """
    cfg = get_bible_config()
    # Préfixe global depuis IrcConfig (BibleConfig.command_prefix est legacy)
    prefix = get_irc_config().command_prefix or cfg.command_prefix or "!"
    parsed = _parse_command(prefix, message)
    if parsed is None:
        return False

    mode_asked, ref_text = parsed
    user_mask = f"{source}!{ident}@{host}"
    is_pm = channel == source

    # Guard PM : si allow_pm=False, refuser en contexte PM. Pas consommé —
    # laisse le pipeline continuer (le user peut discuter en PM normalement).
    if is_pm and not getattr(cfg, "allow_pm", True):
        await bus.publish(Event(type="system", payload={
            "message": f"[bible] PM ignoré (allow_pm=False) — {user_mask}"
        }))
        return False

    if not _acl_check(source, ident, host):
        # Silence radio par défaut — évite de leak la surface aux non-autorisés.
        await bus.publish(Event(type="system", payload={
            "message": f"[bible] refusé (ACL) — {user_mask} sur {channel}"
        }))
        return True

    if not ref_text:
        try:
            await client.message(channel, f"{source}: usage — !bible Jean 3:16 (ou !bible-meme, !bible-apo)")
        except Exception:  # noqa: BLE001
            log.exception("send usage failed")
        return True

    if _throttled(user_mask, cfg.throttle_per_mask_sec, cfg.throttle_global_sec):
        await bus.publish(Event(type="system", payload={
            "message": f"[bible] throttled — {user_mask}"
        }))
        return True

    ref = bible_mod.parse_reference(ref_text)
    if ref is None:
        try:
            await client.message(channel, f"{source}: référence non reconnue — essaie 'Livre Chapitre:Verset' (ex: Jean 3:16)")
        except Exception:  # noqa: BLE001
            log.exception("send parse-fail failed")
        return True

    _mark_throttle(user_mask)

    versions = bible_mod.parse_versions_json(cfg.versions_enabled)
    if not versions:
        versions = ["en-kjv"]

    try:
        passages = await bible_mod.fetch_all_versions(versions, ref)
    except Exception as e:  # noqa: BLE001
        log.exception("fetch_all_versions failed")
        await bus.publish(Event(type="error", payload={"message": f"[bible] fetch error: {e}"}))
        return True

    # Post chaque version
    rendered_lines: list[str] = []
    for p in passages:
        line = p.render()
        rendered_lines.append(line)
        for sub in _chunk(line, 400):
            try:
                await client.message(channel, sub)
                log_message(channel=channel, nick=client.nickname, message=sub, direction="out")
                await bus.publish(Event(type="bot_reply", payload={
                    "channel": channel, "nick": client.nickname, "message": sub,
                }))
            except Exception:  # noqa: BLE001
                log.exception("send passage failed")

    # Commentaire LLM (facultatif)
    if cfg.llm_commentary_enabled and any(p.ok for p in passages):
        mode = cfg.default_mode if mode_asked == "default" else mode_asked
        try:
            commentary = await _llm_commentary("\n".join(rendered_lines), mode)
        except Exception as e:  # noqa: BLE001
            log.exception("LLM commentary failed")
            await bus.publish(Event(type="error", payload={"message": f"[bible] LLM: {e}"}))
            commentary = ""
        if commentary:
            for sub in _chunk(commentary, 400):
                try:
                    await client.message(channel, sub)
                    log_message(channel=channel, nick=client.nickname, message=sub, direction="out")
                    await bus.publish(Event(type="bot_reply", payload={
                        "channel": channel, "nick": client.nickname, "message": sub,
                    }))
                except Exception:  # noqa: BLE001
                    log.exception("send commentary failed")

    return True
