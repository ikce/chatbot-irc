"""Downloader et matcher pour les listes Spamhaus DROP/EDROP.

Les listes sont téléchargées au boot et rafraîchies périodiquement, stockées en
mémoire pour un lookup O(log n) par CIDR et snapshottées en DB pour audit et
survie au restart (chargement en mémoire au boot depuis la DB si le download
échoue au démarrage).

Format Spamhaus :
    ; Spamhaus DROP List
    1.2.3.0/24 ; SBL12345
    4.5.6.0/24 ; SBL67890
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Optional

import httpx

from ..db import (
    get_reputation_config,
    list_reputation_entries,
    replace_reputation_list,
    update_reputation_config,
)

log = logging.getLogger("chatbot.spamhaus")

DROP_URL = "https://www.spamhaus.org/drop/drop.txt"
EDROP_URL = "https://www.spamhaus.org/drop/edrop.txt"

# Structures en mémoire pour lookup rapide.
# Séparer v4 et v6 est overkill ici (Spamhaus DROP est majoritairement v4), on
# garde une liste unique de (network, source, ref).
_NETS: list[tuple[ipaddress._BaseNetwork, str, str]] = []
_LOCK = asyncio.Lock()
_TASK: Optional[asyncio.Task] = None


def _parse_spamhaus_body(text: str) -> list[tuple[str, str]]:
    """Parse le body d'une liste Spamhaus DROP. Retourne [(cidr, ref), ...]."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        # Format: "1.2.3.0/24 ; SBL12345"
        parts = line.split(";", 1)
        cidr = parts[0].strip()
        ref = parts[1].strip() if len(parts) > 1 else ""
        if not cidr:
            continue
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            log.warning("Spamhaus: CIDR invalide ignoré: %r", cidr)
            continue
        out.append((cidr, ref))
    return out


async def _download(url: str, timeout: float = 20.0) -> str:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "chatbot-irc-moderation/1.0"})
        r.raise_for_status()
        return r.text


async def refresh_now() -> dict:
    """Télécharge DROP + EDROP, snapshotte en DB, recharge le cache mémoire.

    Retourne un dict {drop: int, edrop: int, error: str}. En cas d'erreur sur
    une source, l'autre est quand même appliquée si possible.
    """
    result = {"drop": 0, "edrop": 0, "error": ""}
    errors: list[str] = []

    for url, source in [(DROP_URL, "spamhaus_drop"), (EDROP_URL, "spamhaus_edrop")]:
        try:
            body = await _download(url)
            entries = _parse_spamhaus_body(body)
            replace_reputation_list(source, entries)
            key = "drop" if source == "spamhaus_drop" else "edrop"
            result[key] = len(entries)
            log.info("Spamhaus %s: %d CIDR", source, len(entries))
        except Exception as e:
            msg = f"{source}: {e.__class__.__name__} {e}"
            errors.append(msg)
            log.warning("Spamhaus refresh failed for %s: %s", source, e)

    if errors:
        result["error"] = "; ".join(errors)

    # Update métadonnées (même en erreur partielle on note ce qu'on a)
    from datetime import datetime as _dt
    update_reputation_config(
        spamhaus_last_update=_dt.utcnow(),
        spamhaus_drop_count=result["drop"],
        spamhaus_edrop_count=result["edrop"],
        spamhaus_last_error=result["error"],
    )

    await load_from_db()
    return result


async def load_from_db() -> int:
    """Recharge le cache mémoire depuis le snapshot DB. Retourne le nb chargé."""
    global _NETS
    rows = list_reputation_entries()  # toutes sources confondues
    new_nets: list[tuple[ipaddress._BaseNetwork, str, str]] = []
    for r in rows:
        try:
            net = ipaddress.ip_network(r.cidr, strict=False)
            new_nets.append((net, r.source, r.ref))
        except ValueError:
            continue
    async with _LOCK:
        _NETS = new_nets
    log.info("Spamhaus: %d CIDR chargés en mémoire", len(new_nets))
    return len(new_nets)


def check_ip(ip: str) -> Optional[dict]:
    """Test si une IP est dans une liste Spamhaus. Sync, appelable depuis handler.

    Retourne None si non listée, sinon {source, cidr, ref}.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net, source, ref in _NETS:
        if addr.version != net.version:
            continue
        if addr in net:
            return {"source": source, "cidr": str(net), "ref": ref}
    return None


def stats() -> dict:
    """Snapshot des compteurs en mémoire pour la WebUI."""
    by_source: dict[str, int] = {}
    for _, source, _ in _NETS:
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "total_in_memory": len(_NETS),
        "by_source": by_source,
    }


async def _refresh_loop() -> None:
    """Boucle : refresh au boot, puis toutes les N secondes selon la config."""
    # Boot : load from DB en premier (dispo immédiate si download lent/fail)
    try:
        await load_from_db()
    except Exception as e:
        log.warning("Spamhaus load_from_db initial failed: %s", e)

    while True:
        cfg = get_reputation_config()
        if not cfg.spamhaus_enabled:
            log.info("Spamhaus disabled, sleeping 1h before recheck")
            await asyncio.sleep(3600)
            continue
        try:
            await refresh_now()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Spamhaus refresh loop error: %s", e)
        interval = max(600, cfg.spamhaus_refresh_interval_sec)  # min 10 min
        await asyncio.sleep(interval)


def start_worker() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_refresh_loop(), name="spamhaus_refresh")
    log.info("Spamhaus refresh worker started")


async def stop_worker() -> None:
    global _TASK
    if _TASK is None:
        return
    _TASK.cancel()
    try:
        await _TASK
    except (asyncio.CancelledError, Exception):
        pass
    _TASK = None
    log.info("Spamhaus refresh worker stopped")
