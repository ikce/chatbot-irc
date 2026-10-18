"""Détection d'URL dans les messages IRC + preview (titre, description, métadonnées).

Fire-and-forget async : spawn depuis irc_client.on_message via asyncio.create_task,
non-bloquant pour le pipeline chat/commandes.

Design safety-first :
    - **SSRF guard hardcodé** : refuse toute IP privée (RFC1918), loopback,
      link-local, multicast, reserved. Résout le hostname AVANT chaque requête
      — y compris chaque hop de redirection (event hook httpx), sinon un
      domaine public pourrait rediriger vers http://192.168.x.x.
    - **Content-type gate** : ignore tout ce qui n'est pas HTML (pas de
      téléchargements de PDFs de 200 MB déclenchés par un lien collé).
    - **Body cap** : lecture en streaming, coupée à `max_bytes_html` bytes.
      Exception YouTube : cap relevé à `_YOUTUBE_MAX_BYTES` car le JSON
      `ytInitialPlayerResponse` est vers ~750 KB dans la page watch.
    - **Cache mémoire TTL** : évite de re-fetcher la même URL en boucle
      (défaut 30min). LRU-ish : purge de la moitié la plus vieille au-dessus
      de `_CACHE_MAX`.
    - **Cap URL/message** : anti-flood si quelqu'un colle 10 liens d'un coup.
    - **ACL** : guardé par `allow_url_fetch` sur UserAcl, admins compris
      (décocher le flag coupe la preview pour un admin aussi).

Format posté : gabarits éditables dans la WebUI (page LLM, carte « Aperçu des
liens » → UrlIntelConfig.link_format/desc_format/yt_format/yt_desc_format).
Variables : {title} {site} {description} {domain} {url} et, pour YouTube,
{channel} {duration} {views} {likes} {date}. Un bloc `[[ ... ]]` n'est affiché
que si toutes ses variables sont non vides. Gabarit vide = ligne non postée.
Par défaut :
    ↳ [Titre] — Site
    » Description (og:description / meta description, tronquée)

    ↳ ▶ [Titre vidéo] · Chaîne · 3:34 · 1,8 Md vues · 👍 19,5 M · 2009-10-24
    » Début de la description de la vidéo (liens retirés)

YouTube : videoDetails + microformat de `ytInitialPlayerResponse`, fallback
sur les <meta> / itemprop de la page, puis sur l'oEmbed (titre + chaîne) si la
page est bloquée (mur de consentement, bot check).
"""
from __future__ import annotations

import asyncio
import html as html_mod
import ipaddress
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx

from .db import get_url_intel_config, user_acl_check
from .event_bus import Event, bus


log = logging.getLogger("chatbot.url_intel")


# Match http(s) URLs. Exclut caractères IRC courants qui coupent une URL.
URL_RE = re.compile(r'https?://[^\s\'"<>`\|,]+', re.IGNORECASE)

# Ponctuation susceptible d'être collée à la fin d'une URL par erreur.
_TRAILING_STRIP = ".,;:!?)]}>"

_HEADERS = {"User-Agent": "chatbot-irc/1.0 (+URL preview)"}

_TITLE_MAX_CHARS = 240
# Ligne de description : IRC coupe vers ~510 bytes avec le préfixe, et les
# accents/emojis prennent 2-4 bytes en UTF-8 — on garde de la marge.
_DESC_MAX_CHARS = 280

# YouTube : videoDetails/microformat sont vers 750-780 KB dans la page watch.
_YOUTUBE_MAX_BYTES = 1_572_864  # 1.5 MB
# Cookies de consentement : évite l'interstitiel "Avant d'accéder à YouTube"
# servi à certaines IPs (EU, datacenters).
_YOUTUBE_COOKIES = {"SOCS": "CAI", "CONSENT": "YES+cb"}
_YT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
_YT_PLAYER_RESPONSE_RE = re.compile(r'ytInitialPlayerResponse\s*=\s*\{')
_ISO_DURATION_RE = re.compile(r'^PT?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$')

_META_TAG_RE = re.compile(r'<meta\s[^>]*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_TITLE_RE = re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE | re.DOTALL)
_URL_IN_TEXT_RE = re.compile(r'https?://\S+')


DEFAULT_LINK_FORMAT = "↳ [{title}][[ — {site}]]"
DEFAULT_DESC_FORMAT = "» {description}"
DEFAULT_YT_FORMAT = ("↳ ▶ [{title}][[ · {channel}]][[ · {duration}]][[ · {views} vues]]"
                     "[[ · 👍 {likes}]][[ · {date}]]")
DEFAULT_YT_DESC_FORMAT = "» {description}"

_OPT_BLOCK_RE = re.compile(r"\[\[(.*?)\]\]", re.S)
_VAR_RE = re.compile(r"\{(\w+)\}")

# Cache in-process : url → (champs de preview, expires_at_monotonic). On cache
# les champs, pas les lignes : un gabarit modifié s'applique tout de suite.
_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_MAX = 500  # au-delà, purge de la moitié la plus vieille


def extract_urls(text: str) -> list[str]:
    """Retourne les URLs uniques dans l'ordre d'apparition."""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(_TRAILING_STRIP)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


async def _resolve_and_check(host: str) -> bool:
    """True si le host résout vers une IP publique. False si privée/loopback/etc.

    Résout via getaddrinfo (respecte /etc/hosts + DNS). Refuse aussi si
    résolution échoue — mieux vaut skip qu'accidentellement leaker.
    """
    try:
        # IP littérale directe ?
        ip = ipaddress.ip_address(host)
        return _is_public_ip(ip)
    except ValueError:
        pass

    try:
        loop = asyncio.get_event_loop()
        addrs = await loop.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001
        return False

    for family, _, _, _, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if not _is_public_ip(ip):
            return False
    return True


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _ssrf_hook(request: httpx.Request) -> None:
    """Event hook httpx : appelé pour la requête initiale ET chaque redirection."""
    host = request.url.host
    if not await _resolve_and_check(host):
        log.info("SSRF guard: refused url=%s (host=%s)", request.url, host)
        raise httpx.RequestError(f"SSRF guard refused host {host}", request=request)


def _client(timeout: float, cookies: Optional[dict] = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=_HEADERS,
        cookies=cookies,
        event_hooks={"request": [_ssrf_hook]},
    )


async def _fetch_html(url: str, *, max_bytes: int, timeout: float,
                      cookies: Optional[dict] = None) -> Optional[str]:
    """GET streaming, coupé à max_bytes. None si pas HTML / erreur / SSRF."""
    try:
        async with _client(timeout, cookies) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").lower()
                if "html" not in ctype and "xml" not in ctype:
                    return None
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= max_bytes:
                        break
    except Exception as e:  # noqa: BLE001
        log.info("fetch failed url=%s err=%s", url, e)
        return None
    return bytes(buf[:max_bytes]).decode("utf-8", errors="replace")


def _clean_text(text: str, max_len: int) -> str:
    text = html_mod.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_len:
        text = text[: max_len - 3].rstrip() + "..."
    return text


def _parse_meta(html: str) -> dict[str, str]:
    """Map property/name/itemprop → content de toutes les balises <meta>.

    Ordre des attributs indifférent, quotes simples ou doubles. En cas de
    doublon le premier gagne (le <head> passe avant le body).
    """
    meta: dict[str, str] = {}
    for tag in _META_TAG_RE.finditer(html):
        attrs = {k.lower(): dq or sq for k, dq, sq in _ATTR_RE.findall(tag.group(0))}
        content = attrs.get("content")
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        if key and content:
            meta.setdefault(key.lower(), content)
    return meta


def _description(desc: Optional[str], title: str) -> str:
    if not desc:
        return ""
    desc = _URL_IN_TEXT_RE.sub("", desc)
    desc = _clean_text(desc, _DESC_MAX_CHARS)
    if not desc or desc.lower() == title.lower():
        return ""
    return desc


def render(template: str, fields: dict) -> str:
    """Applique un gabarit. `[[...]]` disparaît si une de ses variables est vide ;
    une variable inconnue est rendue vide."""
    def val(name: str) -> str:
        return str(fields.get(name) or "")

    def opt(m: re.Match) -> str:
        block = m.group(1)
        if all(val(n) for n in _VAR_RE.findall(block)):
            return _VAR_RE.sub(lambda v: val(v.group(1)), block)
        return ""

    out = _OPT_BLOCK_RE.sub(opt, template or "")
    out = _VAR_RE.sub(lambda v: val(v.group(1)), out)
    return re.sub(r"\s{2,}", " ", out).strip()


def format_preview(fields: dict, cfg=None) -> list[str]:
    """Lignes IRC pour des champs de preview, selon les gabarits de la config."""
    cfg = cfg or get_url_intel_config()
    yt = fields.get("kind") == "youtube"
    if yt:
        head_t = getattr(cfg, "yt_format", None)
        desc_t = getattr(cfg, "yt_desc_format", None)
        head_t = DEFAULT_YT_FORMAT if head_t is None else head_t
        desc_t = DEFAULT_YT_DESC_FORMAT if desc_t is None else desc_t
    else:
        head_t = getattr(cfg, "link_format", None)
        desc_t = getattr(cfg, "desc_format", None)
        head_t = DEFAULT_LINK_FORMAT if head_t is None else head_t
        desc_t = DEFAULT_DESC_FORMAT if desc_t is None else desc_t
    lines = []
    head = render(head_t, fields) if head_t.strip() else ""
    if head:
        lines.append(head[:450])
    if desc_t.strip() and fields.get("description"):
        desc = render(desc_t, fields)
        if desc:
            lines.append(desc[:450])
    return lines


def _preview_generic(html: str) -> dict:
    meta = _parse_meta(html)
    title = meta.get("og:title") or meta.get("twitter:title")
    if not title:
        m = _TITLE_RE.search(html)
        title = m.group(1) if m else None
    if not title:
        return {}
    title = _clean_text(title, _TITLE_MAX_CHARS)
    if not title:
        return {}

    site = _clean_text(meta.get("og:site_name", ""), 60)
    if site and site.lower() in title.lower():
        site = ""
    return {
        "kind": "generic",
        "title": title,
        "site": site,
        "description": _description(
            meta.get("og:description")
            or meta.get("description")
            or meta.get("twitter:description"),
            title,
        ),
    }


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def _youtube_video_id(url: str) -> Optional[str]:
    """ID vidéo pour watch?v=, youtu.be/, /shorts/, /live/, /embed/. None sinon
    (chaînes, playlists → preview générique)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]

    vid: Optional[str] = None
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        parts = p.path.strip("/").split("/")
        if parts[0] == "watch":
            vid = parse_qs(p.query).get("v", [""])[0]
        elif parts[0] in ("shorts", "live", "embed", "v") and len(parts) > 1:
            vid = parts[1]
    return vid if vid and _YT_ID_RE.match(vid) else None


def _yt_player_response(html: str) -> dict:
    m = _YT_PLAYER_RESPONSE_RE.search(html)
    if not m:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end() - 1)
    except ValueError:  # JSON tronqué par le cap ou format changé
        return {}
    return obj if isinstance(obj, dict) else {}


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_duration_sec(value: str) -> Optional[int]:
    m = _ISO_DURATION_RE.match(value or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def _fmt_duration(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_count(n: int) -> str:
    """1821987565 → '1,8 Md' ; 19452977 → '19,5 M' ; 4321 → '4,3 k'."""
    for div, suffix in ((1_000_000_000, "Md"), (1_000_000, "M"), (1_000, "k")):
        if n >= div:
            v = n / div
            s = f"{v:.1f}" if v < 100 else f"{v:.0f}"
            return s.replace(".", ",").removesuffix(",0") + f" {suffix}"
    return str(n)


def _preview_youtube(html: str) -> dict:
    pr = _yt_player_response(html)
    details = pr.get("videoDetails") or {}
    micro = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    meta = _parse_meta(html)

    title = details.get("title") or meta.get("og:title")
    if not title:
        return {}
    title = _clean_text(title, _TITLE_MAX_CHARS)

    author = details.get("author") or micro.get("ownerChannelName") or ""
    if details.get("isLive") or (micro.get("liveBroadcastDetails") or {}).get("isLiveNow"):
        duration = "🔴 EN DIRECT"
    else:
        sec = _to_int(details.get("lengthSeconds")) or _iso_duration_sec(meta.get("duration", ""))
        duration = _fmt_duration(sec) if sec else ""
    views = _to_int(details.get("viewCount"))
    likes = _to_int(micro.get("likeCount"))
    published = micro.get("publishDate") or meta.get("datepublished") or ""
    return {
        "kind": "youtube",
        "title": title,
        "site": "YouTube",
        "channel": _clean_text(author, 60) if author else "",
        "duration": duration,
        "views": _fmt_count(views) if views is not None else "",
        "likes": _fmt_count(likes) if likes else "",
        "date": published[:10],
        "description": _description(
            details.get("shortDescription") or meta.get("og:description"), title,
        ),
    }


async def _youtube_oembed(video_id: str, timeout: float) -> dict:
    """Fallback minimal (titre + chaîne) quand la page watch est inexploitable."""
    watch = f"https://www.youtube.com/watch?v={video_id}"
    url = f"https://www.youtube.com/oembed?format=json&url={quote(watch, safe='')}"
    try:
        async with _client(timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.info("oembed failed video=%s err=%s", video_id, e)
        return {}
    title = data.get("title") if isinstance(data, dict) else None
    if not title:
        return {}
    return {
        "kind": "youtube",
        "title": _clean_text(title, _TITLE_MAX_CHARS),
        "site": "YouTube",
        "channel": _clean_text(data.get("author_name") or "", 60),
    }


async def _fetch_youtube_preview(video_id: str, cfg) -> dict:
    # Toujours la page watch canonique : /shorts/ et youtu.be y mènent aussi,
    # et c'est la seule dont la structure est stable.
    html = await _fetch_html(
        f"https://www.youtube.com/watch?v={video_id}",
        max_bytes=max(cfg.max_bytes_html, _YOUTUBE_MAX_BYTES),
        timeout=cfg.fetch_timeout_sec,
        cookies=_YOUTUBE_COOKIES,
    )
    fields = _preview_youtube(html) if html else {}
    return fields or await _youtube_oembed(video_id, cfg.fetch_timeout_sec)


# ---------------------------------------------------------------------------
# Cache + orchestration
# ---------------------------------------------------------------------------

def _cache_get(url: str) -> Optional[dict]:
    entry = _CACHE.get(url)
    if entry is None:
        return None
    lines, expires_at = entry
    if expires_at < time.monotonic():
        _CACHE.pop(url, None)
        return None
    return lines


def _cache_set(url: str, lines: dict, ttl_sec: int) -> None:
    if len(_CACHE) > _CACHE_MAX:
        # Purge de la moitié la plus vieille (approx LRU basé sur expires_at)
        items = sorted(_CACHE.items(), key=lambda kv: kv[1][1])
        _CACHE.clear()
        _CACHE.update(dict(items[len(items) // 2:]))
    _CACHE[url] = (lines, time.monotonic() + ttl_sec)


async def fetch_fields(url: str) -> dict:
    """Champs de preview pour l'URL. {} si skip/échec/pas HTML/SSRF/etc."""
    cfg = get_url_intel_config()
    if not cfg.enabled:
        return {}

    cached = _cache_get(url)
    if cached is not None:
        return cached

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {}

    video_id = _youtube_video_id(url)
    if video_id:
        fields = await _fetch_youtube_preview(video_id, cfg)
    else:
        html = await _fetch_html(
            url, max_bytes=cfg.max_bytes_html, timeout=cfg.fetch_timeout_sec,
        )
        fields = _preview_generic(html) if html else {}

    if fields:
        host = parsed.hostname.lower()
        fields.update(url=url, domain=host[4:] if host.startswith("www.") else host)
        _cache_set(url, fields, cfg.cache_ttl_sec)
    return fields


async def fetch_preview(url: str) -> list[str]:
    """Lignes IRC de preview pour l'URL, selon les gabarits configurés."""
    fields = await fetch_fields(url)
    return format_preview(fields) if fields else []


async def process_message(client, source: str, ident: str, host: str,
                          channel: str, message: str) -> None:
    """Fire-and-forget : extraire URLs du message et poster leur preview."""
    cfg = get_url_intel_config()
    if not cfg.enabled:
        return

    urls = extract_urls(message)
    if not urls:
        return

    # ACL guard : deny-by-default. Feature = fetch URL externe (bande passante
    # + SSRF potentiel). Requiert allow_url_fetch — y compris pour les admins,
    # qui coupent ainsi la preview sur leurs propres liens en décochant le flag.
    if not user_acl_check(source, ident, host, "allow_url_fetch"):
        await bus.publish(Event(type="system", payload={
            "message": f"[url_intel] skip (pas d'ACL allow_url_fetch) — {source}!{ident}@{host}"
        }))
        return

    for url in urls[: cfg.max_urls_per_message]:
        lines = await fetch_preview(url)
        for line in lines:
            try:
                await client.message(channel, line)
            except Exception:  # noqa: BLE001
                log.exception("send preview failed for %s", url)
                break
