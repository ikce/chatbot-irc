"""Parse et matching de masks IRC nick!ident@host avec wildcards * et ?.

Le matching suit la sémantique IRC standard :
- `*` = 0..N caractères
- `?` = exactement 1 caractère
- nick + host = case-insensitive (RFC 1459)
- ident = case-sensitive côté serveur mais on relâche à CI aussi (pratique)
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ParsedMask:
    nick: str
    ident: str
    host: str

    def as_string(self) -> str:
        return f"{self.nick}!{self.ident}@{self.host}"


_MASK_RE = re.compile(r"^([^!@]+)!([^@]+)@(.+)$")


def parse_mask(mask: str) -> Optional[ParsedMask]:
    """Extrait (nick, ident, host) d'un mask IRC.

    Retourne None si le format est invalide. Un `~` en tête d'ident (typique
    quand pas d'identd) est conservé — c'est un signal en soi.
    """
    if not mask:
        return None
    m = _MASK_RE.match(mask.strip())
    if not m:
        return None
    return ParsedMask(nick=m.group(1), ident=m.group(2), host=m.group(3))


def mask_matches(user_mask: str, pattern: str) -> bool:
    """Test si un mask user (nick!ident@host complet) matche un pattern à wildcards.

    Le pattern peut aussi être un mask complet avec `*` et `?`. Case-insensitive.
    Utilise fnmatch (comme Ubu/Undernet), donc :
      - `*!*@*.isp.com` matche `bob!bob@pool-1.isp.com`
      - `Bob*!*@*` matche `Bob_afk!bob@anywhere`
      - `?ick!*@*` matche `Nick!x@y` mais pas `Alice!x@y`
    """
    if not user_mask or not pattern:
        return False
    # fnmatch fait case-insensitive sur POSIX seulement, on force en lower
    return fnmatch.fnmatchcase(user_mask.lower(), pattern.lower())


def mask_matches_parts(nick: str, ident: str, host: str, pattern: str) -> bool:
    """Variante quand on a déjà les 3 parts extraites."""
    return mask_matches(f"{nick}!{ident}@{host}", pattern)


def normalize_pattern(pattern: str) -> str:
    """Complète un pattern partiel en mask complet.

    - `Bob`             → `Bob!*@*`         (juste un nick)
    - `Bob!*@*`         → tel quel
    - `*@1.2.3.4`       → `*!*@1.2.3.4`     (juste host)
    - `bob@host`        → `*!bob@host`      (ident@host sans nick)
    """
    p = pattern.strip()
    if not p:
        return p
    has_bang = "!" in p
    has_at = "@" in p
    if has_bang and has_at:
        return p
    if not has_bang and not has_at:
        # juste un nick
        return f"{p}!*@*"
    if not has_bang and has_at:
        # ident@host ou @host
        if p.startswith("@"):
            return f"*!*{p}"
        return f"*!{p}"
    # has_bang, not has_at → nick!ident sans host
    return f"{p}@*"
