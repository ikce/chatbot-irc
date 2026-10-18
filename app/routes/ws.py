from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..event_bus import Event, bus
from ..irc_client import supervisor

router = APIRouter()
log = logging.getLogger("chatbot.ws")


@router.websocket("/ws")
async def ws_console(ws: WebSocket) -> None:
    await ws.accept()
    q = bus.subscribe()

    async def pump_outbound() -> None:
        try:
            while True:
                ev: Event = await q.get()
                await ws.send_text(json.dumps({
                    "type": ev.type,
                    "payload": ev.payload,
                }))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("ws pump crash: %s", e)

    pump_task = asyncio.create_task(pump_outbound())

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = msg.get("action")
            if action == "say":
                channel = msg.get("channel", "").strip()
                text = msg.get("message", "").strip()
                if not channel or not text:
                    continue
                client = supervisor.get_client()
                if client is None or not client.connected:
                    await bus.publish(Event(type="error", payload={
                        "message": "IRC not connected — cannot say"
                    }))
                    continue
                await client.say_manual(channel, text)
            elif action == "ping":
                await ws.send_text(json.dumps({"type": "pong", "payload": {}}))
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        bus.unsubscribe(q)
