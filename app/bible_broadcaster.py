"""Scheduler broadcast récurrent — verset biblique posté dans un channel.

Task asyncio unique lancée au boot par main.py (comme IrcSupervisor). Elle
tick toutes les 30s, itère les BibleBroadcast actifs, et pour chacun :
  - vérifie que interval_minutes est écoulé depuis last_run_at
  - pick un mode selon les flags cochés (random / playlist / VOTD)
  - fetch le passage, post dans le channel, update last_run_at

Si aucun flag coché → skip silencieux (broadcast désactivé).
Si plusieurs flags → round-robin entre les modes actifs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from . import bible as bible_mod
from . import bible_handler
from .db import (
    get_bible_config,
    list_bible_broadcasts,
    log_message,
    update_bible_broadcast,
)
from .event_bus import Event, bus

log = logging.getLogger("chatbot.bible.broadcaster")

_TICK_SEC = 30.0


class BibleBroadcaster:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._supervisor = None  # set via bind()

    def bind(self, supervisor) -> None:
        """Passe une référence au supervisor IRC pour récupérer le client à chaque tick."""
        self._supervisor = supervisor

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="bible_broadcaster")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("broadcaster tick crashed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_TICK_SEC)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        if self._supervisor is None:
            return
        client = self._supervisor.get_client()
        if client is None or not getattr(client, "connected", False):
            return  # attend que l'IRC soit up
        now = datetime.utcnow()
        for bc in list_bible_broadcasts(active_only=True):
            if bc.last_run_at is not None:
                elapsed = now - bc.last_run_at
                if elapsed < timedelta(minutes=max(1, bc.interval_minutes)):
                    continue
            # Vérifier que le bot est bien sur ce chan avant de tenter
            chans = list(getattr(client, "channels", {}).keys())
            if bc.channel not in chans:
                log.debug("broadcast skip %s: bot pas sur le chan", bc.channel)
                continue
            try:
                await self._run_one(client, bc)
            except Exception:  # noqa: BLE001
                log.exception("broadcast %s failed", bc.channel)

    async def _run_one(self, client, bc) -> None:
        modes_active = self._active_modes(bc)
        if not modes_active:
            return  # tous flags off → dodo
        # Round-robin : sélectionne un mode selon rr_cursor
        cursor = bc.rr_cursor or 0
        mode = modes_active[cursor % len(modes_active)]
        next_cursor = (cursor + 1) % len(modes_active)

        cfg = get_bible_config()
        versions = self._pick_versions(bc, cfg)

        result = None
        new_playlist_cursor = bc.playlist_cursor
        if mode == "random":
            books = self._parse_json_list(bc.books_filter)
            ref = await bible_mod.random_reference(books_filter=books or None)
            passages = await bible_mod.fetch_all_versions(versions, ref)
            result = (ref, passages)
        elif mode == "playlist":
            playlist = self._parse_json_list(bc.playlist)
            if not playlist:
                log.info("broadcast %s: playlist vide, skip mode", bc.channel)
                update_bible_broadcast(bc.id, rr_cursor=next_cursor)
                return
            idx = bc.playlist_cursor % len(playlist)
            ref_text = playlist[idx]
            ref = bible_mod.parse_reference(ref_text)
            new_playlist_cursor = (idx + 1) % len(playlist)
            if ref is None:
                log.warning("broadcast %s: ref invalide dans playlist idx=%d '%s'",
                            bc.channel, idx, ref_text)
                update_bible_broadcast(bc.id,
                                       rr_cursor=next_cursor,
                                       playlist_cursor=new_playlist_cursor)
                return
            passages = await bible_mod.fetch_all_versions(versions, ref)
            result = (ref, passages)
        elif mode == "votd":
            ref, passages = await bible_mod.fetch_votd(versions)
            result = (ref, passages)

        if not result:
            update_bible_broadcast(bc.id, rr_cursor=next_cursor,
                                   playlist_cursor=new_playlist_cursor)
            return

        ref, passages = result
        rendered_lines: list[str] = []
        for p in passages:
            line = p.render()
            rendered_lines.append(line)
            for sub in _chunk(line, 400):
                try:
                    await client.message(bc.channel, sub)
                    log_message(channel=bc.channel, nick=client.nickname,
                                message=sub, direction="out")
                    await bus.publish(Event(type="bot_reply", payload={
                        "channel": bc.channel, "nick": client.nickname, "message": sub,
                    }))
                except Exception:  # noqa: BLE001
                    log.exception("broadcast send failed on %s", bc.channel)

        # Commentaire LLM optionnel sur broadcast
        if cfg.llm_commentary_for_broadcast and any(p.ok for p in passages):
            try:
                commentary = await bible_handler._llm_commentary(
                    "\n".join(rendered_lines), cfg.default_mode,
                )
            except Exception:  # noqa: BLE001
                log.exception("broadcast LLM commentary failed")
                commentary = ""
            if commentary:
                for sub in _chunk(commentary, 400):
                    try:
                        await client.message(bc.channel, sub)
                        log_message(channel=bc.channel, nick=client.nickname,
                                    message=sub, direction="out")
                        await bus.publish(Event(type="bot_reply", payload={
                            "channel": bc.channel, "nick": client.nickname, "message": sub,
                        }))
                    except Exception:  # noqa: BLE001
                        log.exception("broadcast commentary send failed on %s", bc.channel)

        update_bible_broadcast(
            bc.id,
            last_run_at=datetime.utcnow(),
            rr_cursor=next_cursor,
            playlist_cursor=new_playlist_cursor,
        )
        await bus.publish(Event(type="system", payload={
            "message": f"[bible-broadcast] {bc.channel} mode={mode} ref={ref.human()}"
        }))

    @staticmethod
    def _active_modes(bc) -> list[str]:
        out: list[str] = []
        if bc.mode_random:
            out.append("random")
        if bc.mode_playlist:
            out.append("playlist")
        if bc.mode_votd:
            out.append("votd")
        return out

    @staticmethod
    def _pick_versions(bc, cfg) -> list[str]:
        override = bible_mod.parse_versions_json(bc.versions_override)
        if override:
            return override
        vers = bible_mod.parse_versions_json(cfg.versions_enabled)
        return vers or ["en-kjv"]

    @staticmethod
    def _parse_json_list(raw: str) -> list[str]:
        if not raw:
            return []
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                return [str(v).strip() for v in val if str(v).strip()]
        except (ValueError, TypeError):
            pass
        return []


def _chunk(text: str, size: int = 400) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


broadcaster = BibleBroadcaster()
