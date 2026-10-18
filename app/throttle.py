from __future__ import annotations

import time


class Throttle:
    """Cooldown global + par nick. Non thread-safe, mono-loop asyncio."""

    def __init__(self) -> None:
        self._last_global: float = 0.0
        self._last_per_nick: dict[str, float] = {}

    def can_respond(self, nick: str, global_sec: float, per_nick_sec: float) -> bool:
        now = time.monotonic()
        if now - self._last_global < global_sec:
            return False
        if now - self._last_per_nick.get(nick, 0.0) < per_nick_sec:
            return False
        return True

    def mark(self, nick: str) -> None:
        now = time.monotonic()
        self._last_global = now
        self._last_per_nick[nick] = now


throttle = Throttle()
