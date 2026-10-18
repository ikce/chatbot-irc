"""Ignore automatique : le bot « se tanne » et ignore quelqu'un pendant un temps.

Deux déclencheurs, actifs si `LlmConfig.auto_ignore_minutes` > 0 :
    - répétition : la même question (à ~85 % près) posée REPEAT_COUNT fois au
      bot en REPEAT_WINDOW_SEC → ignore direct, sans appel LLM ;
    - décision du LLM : le prompt l'autorise à finir sa réponse par `[IGNORE]`
      quand on insiste sur un refus ou qu'on le harcèle (cf. llm.generate_reply).

L'ignore vise le nick ET le ident@host (changer de nick ne suffit pas). Les
admins ne sont jamais ignorés. Tout est en mémoire : un redémarrage du bot
lève les ignores en cours.
"""
from __future__ import annotations

import difflib
import re
import time
from collections import deque
from typing import Optional

from .db import get_llm_config, user_acl_check

IGNORE_TAG = "[IGNORE]"
REPEAT_COUNT = 3
REPEAT_WINDOW_SEC = 600
SIMILARITY = 0.85

_until: dict[str, float] = {}                 # clé → fin de l'ignore (monotonic)
_recent: dict[str, deque] = {}                # nick → (ts, texte normalisé)


def _minutes() -> int:
    try:
        return int(getattr(get_llm_config(), "auto_ignore_minutes", 30) or 0)
    except (TypeError, ValueError):
        return 0


def enabled() -> bool:
    return _minutes() > 0


def _keys(nick: str, ident: str, host: str) -> list[str]:
    keys = [f"nick:{nick.lower()}"]
    if host:
        keys.append(f"mask:{(ident or '').lstrip('~').lower()}@{host.lower()}")
    return keys


def remaining(nick: str, ident: str, host: str) -> int:
    """Secondes d'ignore restantes (0 = pas ignoré)."""
    now = time.monotonic()
    left = max((_until.get(k, 0) - now for k in _keys(nick, ident, host)), default=0)
    return int(left) if left > 0 else 0


def is_ignored(nick: str, ident: str, host: str) -> bool:
    return remaining(nick, ident, host) > 0


def ignore(nick: str, ident: str, host: str) -> Optional[int]:
    """Ignore ce user pendant `auto_ignore_minutes`. Retourne les minutes, ou
    None si désactivé ou si c'est un admin."""
    minutes = _minutes()
    if minutes <= 0 or user_acl_check(nick, ident, host, "is_admin"):
        return None
    end = time.monotonic() + minutes * 60
    for k in _keys(nick, ident, host):
        _until[k] = end
    _recent.pop(nick.lower(), None)
    return minutes


def unignore(nick: str) -> bool:
    """Lève l'ignore d'un nick (et des masques ignorés en même temps)."""
    key = f"nick:{nick.lower()}"
    end = _until.pop(key, None)
    if end is None:
        return False
    for k in [k for k, v in _until.items() if v == end]:
        _until.pop(k, None)
    return True


async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """`!unignore <nick>` (admins) : lève un ignore automatique. True = consommé."""
    from .db import get_irc_config
    try:
        prefix = get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        prefix = "!"
    parts = message.strip().split()
    if not parts or parts[0].lower() not in (prefix + "unignore", "!unignore"):
        return False
    reply_to = source if channel == source else channel
    if not user_acl_check(source, ident, host, "is_admin"):
        await client.message(reply_to, f"{source}: réservé aux admins.")
        return True
    if len(parts) < 2:
        await client.message(reply_to, f"Usage: {prefix}unignore <nick>")
        return True
    ok = unignore(parts[1])
    await client.message(reply_to, (
        f"✅ {parts[1]} n'est plus ignoré." if ok else f"{parts[1]} n'était pas ignoré."))
    return True


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def is_repetition(nick: str, message: str) -> bool:
    """Enregistre `message` et dit s'il répète en boucle la même chose."""
    if not enabled():
        return False
    now = time.monotonic()
    q = _recent.setdefault(nick.lower(), deque(maxlen=10))
    while q and now - q[0][0] > REPEAT_WINDOW_SEC:
        q.popleft()
    norm = _norm(message)
    similar = sum(
        1 for _, old in q
        if norm and difflib.SequenceMatcher(None, old, norm).ratio() >= SIMILARITY
    )
    q.append((now, norm))
    return similar + 1 >= REPEAT_COUNT


def split_tag(reply: str) -> tuple[str, bool]:
    """Retire `[IGNORE]` de la réponse du LLM. (texte, le LLM veut ignorer ?)"""
    if not reply or IGNORE_TAG.lower() not in reply.lower():
        return reply, False
    cleaned = re.sub(re.escape(IGNORE_TAG), "", reply, flags=re.I).strip()
    return cleaned, True
