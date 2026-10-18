"""Matching whitelist : un user IRC est-il protégé de la modération auto ?

Une entrée whitelist est un mask (`nick!ident@host`) avec wildcards. Peut
être globale (channel=NULL) ou spécifique à un chan. Les entrées globales
matchent partout, les entrées par chan matchent uniquement dans leur chan.

Cache mémoire des entrées actives, rechargé à la demande via `refresh()`
(appelé par les routes CRUD après add/delete).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..db import list_whitelists
from .mask_utils import mask_matches

log = logging.getLogger("chatbot.whitelist")

# Cache : { channel_or_none: [(mask, reason), ...] }
_cache: dict[Optional[str], list[tuple[str, str]]] = {}
_cache_loaded: bool = False


@dataclass(frozen=True)
class WhitelistDecision:
    whitelisted: bool
    matched_mask: str = ""
    reason: str = ""


def _load_cache() -> None:
    """Recharge le cache depuis la DB. Segmentation par channel pour lookup rapide."""
    global _cache_loaded
    _cache.clear()
    for row in list_whitelists(channel=None, active_only=True):
        _cache.setdefault(row.channel, []).append((row.mask, row.reason))
    _cache_loaded = True
    log.debug(
        "whitelist cache reload: %d chans (dont global=%d)",
        len(_cache),
        len(_cache.get(None, [])),
    )


def refresh() -> None:
    """À appeler après tout CRUD sur la table Whitelist."""
    _load_cache()


def is_whitelisted(user_mask: str, channel: Optional[str] = None) -> WhitelistDecision:
    """Test si `user_mask` est whitelisté pour `channel`.

    Ordre de check :
      1. Whitelists spécifiques au chan
      2. Whitelists globales (channel=NULL)

    Return premier match trouvé. Aucun match = non whitelisté.
    """
    if not _cache_loaded:
        _load_cache()

    # 1. Chan spécifique
    if channel is not None:
        for mask, reason in _cache.get(channel, []):
            if mask_matches(user_mask, mask):
                return WhitelistDecision(True, matched_mask=mask, reason=reason)

    # 2. Global
    for mask, reason in _cache.get(None, []):
        if mask_matches(user_mask, mask):
            return WhitelistDecision(True, matched_mask=mask, reason=reason)

    return WhitelistDecision(False)


def preview_matches(pattern: str, sample_masks: list[str]) -> list[str]:
    """Utilitaire WebUI : donne un pattern, retourne les masks de l'échantillon
    qui matcheraient. Sert à valider une whitelist AVANT de la commit.
    """
    return [m for m in sample_masks if mask_matches(m, pattern)]
