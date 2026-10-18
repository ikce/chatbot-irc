"""Évaluation des déclencheurs de réponse LLM — pilotée par UserAcl.

Un user (UserAcl) est éligible à trigger le bot si :
  1. Son mask matche (fnmatch case-insensitive sur nick!ident@host)
  2. Son `keyword`, s'il est rempli, apparaît en substring case-insensitive
     dans le message. Si `keyword` est vide, le bot réagit quand :
       - le message arrive en PV (is_pm=True), OU
       - le nick du bot est mentionné dans le message.

L'autorisation finale (allow_llm_chat / is_admin) reste à la charge du caller —
ce module ne fait que trouver le user pertinent.
"""
from __future__ import annotations

from typing import Optional

from .db import UserAcl, list_user_acls
from .moderation.mask_utils import mask_matches


def evaluate(
    nick: str,
    ident: str,
    host: str,
    message: str,
    bot_nick: str,
    is_pm: bool,
) -> Optional[UserAcl]:
    """Retourne le premier UserAcl actif qui devrait déclencher le bot, ou None.

    Ordre = insertion (id ASC). Les patterns permissifs (catch-all `*!*@*`)
    tendent à être seed en premier — mets tes règles spécifiques avec un mask
    plus fin AVANT le catch-all si tu veux les prioriser (drop puis re-add).
    """
    user_mask = f"{nick}!{ident}@{host}"
    msg_lower = message.lower()
    bot_nick_lower = bot_nick.lower() if bot_nick else ""

    for u in list_user_acls(active_only=True):
        if not u.mask:
            continue
        if not mask_matches(user_mask, u.mask):
            continue
        kw = (u.keyword or "").strip()
        if kw:
            if kw.lower() in msg_lower:
                return u
        else:
            # Keyword vide = mention du nick du bot ou PV
            if is_pm or (bot_nick_lower and bot_nick_lower in msg_lower):
                return u
    return None
