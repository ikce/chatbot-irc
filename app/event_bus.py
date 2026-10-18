from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Pub-sub asyncio simple. Chaque subscriber reçoit une Queue dédiée."""

    def __init__(self, maxsize: int = 500) -> None:
        self._subs: list[asyncio.Queue[Event]] = []
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._subs:
            self._subs.remove(q)

    async def publish(self, event: Event) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # drop l'event pour ce subscriber lent (pas critique en lab)
                pass


bus = EventBus()
