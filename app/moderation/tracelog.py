"""Écriture asynchrone du tracelog IRC.

Chaque observation (msg, join, part, quit, nick_change) est queue-ée puis
écrite en background : ne bloque JAMAIS le handler IRC. La résolution DNS
bidirectionnelle enrichit chaque row (IP+host toujours quand possible).

Un worker asyncio consomme la queue. Si la queue déborde (>2000), on drop
les plus vieux (log avant de dropper — signal de saturation).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..db import record_trace
from .dns_resolver import resolve_both

log = logging.getLogger("chatbot.tracelog")

_QUEUE_MAX = 2000
_queue: asyncio.Queue["_TraceItem"] = asyncio.Queue(maxsize=_QUEUE_MAX)
_worker_task: Optional[asyncio.Task] = None


@dataclass
class _TraceItem:
    event_type: str
    nick: str
    ident: str
    host: str
    channel: str = ""
    context: str = ""
    ts: datetime = field(default_factory=datetime.utcnow)


async def record(
    event_type: str,
    nick: str,
    ident: str,
    host: str,
    channel: str = "",
    context: str = "",
) -> None:
    """Fire-and-forget : enfile une observation, ne bloque jamais l'IRC.

    Enrichissement DNS + écriture DB se font dans le worker background.
    """
    item = _TraceItem(
        event_type=event_type,
        nick=nick,
        ident=ident,
        host=host,
        channel=channel,
        context=context,
    )
    try:
        _queue.put_nowait(item)
    except asyncio.QueueFull:
        # Drop le plus vieux pour faire de la place — mieux perdre 1 event que bloquer
        try:
            _queue.get_nowait()
            _queue.put_nowait(item)
            log.warning("tracelog queue saturée (>%d), 1 event droppé", _QUEUE_MAX)
        except asyncio.QueueEmpty:
            pass


async def _worker() -> None:
    """Boucle infinie : consomme la queue, résout DNS, écrit en DB."""
    log.info("tracelog worker started")
    while True:
        try:
            item = await _queue.get()
        except asyncio.CancelledError:
            log.info("tracelog worker cancelled")
            break

        try:
            ip, resolved_host = await resolve_both(item.host)
            # Si le host original == resolved_host, on ne stocke pas de doublon
            resolved = resolved_host if resolved_host and resolved_host != item.host else None
            record_trace(
                event_type=item.event_type,
                nick=item.nick,
                ident=item.ident,
                host=item.host,
                ip=ip,
                resolved_host=resolved,
                channel=item.channel,
                context=item.context,
            )
        except Exception as e:  # noqa: BLE001
            # On mange l'exception — le worker ne doit JAMAIS mourir
            log.exception("tracelog worker failed on item %r: %s", item, e)
        finally:
            _queue.task_done()


def start_worker() -> None:
    """À appeler au lifespan startup de FastAPI."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker(), name="tracelog-worker")


async def stop_worker() -> None:
    """À appeler au lifespan shutdown de FastAPI."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None


def queue_stats() -> dict:
    return {
        "queued": _queue.qsize(),
        "max": _QUEUE_MAX,
        "worker_running": _worker_task is not None and not _worker_task.done(),
    }
