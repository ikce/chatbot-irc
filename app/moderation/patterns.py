"""Pattern detection (regex) pour la Phase 3.

Cache compilé en mémoire, invalidation manuelle après CRUD via refresh().
Le scan est un pass linéaire sur tous les patterns actifs — ok pour <100 règles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..db import list_pattern_rules

log = logging.getLogger("chatbot.patterns")


@dataclass
class CompiledPattern:
    rule_id: int
    name: str
    kind: str
    regex: re.Pattern
    severity: int
    description: str


@dataclass
class PatternMatch:
    rule_id: int
    name: str
    kind: str
    severity: int
    snippet: str      # extrait du match (max 100 chars)
    span: tuple[int, int]


_COMPILED: list[CompiledPattern] = []
_COMPILE_ERRORS: list[dict] = []  # {name, error} — visible dans la WebUI


def refresh() -> None:
    """Recompile tous les patterns actifs depuis la DB."""
    global _COMPILED, _COMPILE_ERRORS
    compiled: list[CompiledPattern] = []
    errors: list[dict] = []
    for rule in list_pattern_rules(active_only=True):
        try:
            rx = re.compile(rule.regex, re.IGNORECASE | re.UNICODE)
        except re.error as e:
            errors.append({"name": rule.name, "error": str(e)})
            log.warning("Pattern %r invalide: %s", rule.name, e)
            continue
        compiled.append(CompiledPattern(
            rule_id=rule.id,
            name=rule.name,
            kind=rule.kind,
            regex=rx,
            severity=rule.severity,
            description=rule.description,
        ))
    _COMPILED = compiled
    _COMPILE_ERRORS = errors
    log.info("Patterns: %d compilés, %d erreurs", len(compiled), len(errors))


def scan_message(text: str) -> list[PatternMatch]:
    """Scan un message contre tous les patterns actifs. Retourne les matches.

    Un même pattern peut matcher plusieurs fois — on ne garde que la première
    occurrence par (rule_id) pour éviter le bruit dans les logs.
    """
    if not text or not _COMPILED:
        return []
    seen: set[int] = set()
    matches: list[PatternMatch] = []
    for cp in _COMPILED:
        if cp.rule_id in seen:
            continue
        m = cp.regex.search(text)
        if m:
            seen.add(cp.rule_id)
            snippet = text[max(0, m.start() - 10) : min(len(text), m.end() + 10)]
            matches.append(PatternMatch(
                rule_id=cp.rule_id,
                name=cp.name,
                kind=cp.kind,
                severity=cp.severity,
                snippet=snippet[:100],
                span=(m.start(), m.end()),
            ))
    return matches


def stats() -> dict:
    """Snapshot pour la WebUI."""
    by_kind: dict[str, int] = {}
    for cp in _COMPILED:
        by_kind[cp.kind] = by_kind.get(cp.kind, 0) + 1
    return {
        "total_active": len(_COMPILED),
        "by_kind": by_kind,
        "errors": list(_COMPILE_ERRORS),
    }


def test_regex(regex: str, sample: str) -> Optional[dict]:
    """Test un regex contre un échantillon. Retourne match info ou None.

    Utilisé par la WebUI pour valider un pattern avant enregistrement.
    """
    try:
        rx = re.compile(regex, re.IGNORECASE | re.UNICODE)
    except re.error as e:
        return {"error": str(e)}
    m = rx.search(sample)
    if not m:
        return None
    return {
        "match": m.group(0),
        "start": m.start(),
        "end": m.end(),
        "groups": list(m.groups()),
    }
