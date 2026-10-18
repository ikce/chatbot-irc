"""Commandes IRC admin pour gérer les UserAcl depuis le chat.

Trois commandes, admin-only (via `user_acl_check(..., 'is_admin')`) :

    {prefix}add user <mask> [note...]
    {prefix}del user <mask>
    {prefix}list users

Wiring : `try_handle()` est appelé par `irc_client.on_message()` AVANT
`custom_commands.try_handle()` — on veut la précédence maximale pour éviter
qu'un custom command nommé "add" ou "list" ne shadow l'admin.

Design :
    - Réponses TOUJOURS en PM à l'émetteur (pas de spam dans un chan même si
      la commande y a été tapée) — les infos users sont sensibles.
    - Ajout : `add_user_acl()` seed tous flags à False (opt-in via /users).
    - Suppression : match EXACT sur mask (pas fnmatch) via
      `find_user_acl_by_mask()`. Refuse de supprimer un admin (garde-fou :
      il faut d'abord retirer le flag admin via /users).
    - Refus silencieux si pas admin — on consomme quand même (return True)
      pour ne PAS faire tomber dans custom_commands.
"""
from __future__ import annotations

import logging
import re

from .db import (
    USER_ACL_FLAGS,
    add_user_acl,
    delete_user_acl,
    find_user_acl_by_mask,
    get_irc_config,
    list_user_acls,
    user_acl_check,
)
from .event_bus import Event, bus
from .moderation.mask_utils import normalize_pattern


log = logging.getLogger(__name__)

# Verbes qu'on gère. Le pluriel "users" est toléré partout (add/del/list users).
_CMD_RE = re.compile(
    r"^(?P<verb>add|del|list)\s+users?(?:\s+(?P<rest>.*))?$",
    re.IGNORECASE,
)

# Préfixes de fallback acceptés en plus du préfixe courant, pour couvrir la
# fenêtre de transition juste après un changement de préfixe dans /
_FALLBACK_PREFIXES = ("!", ".", "+", "~")


async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """Traite le message si c'est une commande admin. True = consommé."""
    stripped = message.strip()
    if not stripped:
        return False

    try:
        prefix = get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        prefix = "!"

    body: str | None = None
    for p in (prefix, *_FALLBACK_PREFIXES):
        if stripped.startswith(p):
            body = stripped[len(p):].strip()
            break
    if body is None:
        return False

    m = _CMD_RE.match(body)
    if not m:
        return False

    verb = m.group("verb").lower()
    rest = (m.group("rest") or "").strip()
    user_mask = f"{source}!{ident}@{host}"

    # Gate admin. Consommé même en refus (silencieux) pour bloquer la fall-through.
    if not user_acl_check(source, ident, host, "is_admin"):
        await bus.publish(Event(type="system", payload={
            "message": f"[user_admin/{verb}] refusé (not admin) — {user_mask}"
        }))
        return True

    reply_to = source  # PM à l'émetteur, jamais dans le chan

    if verb == "add":
        return await _handle_add(client, reply_to, source, rest, prefix)
    if verb == "del":
        return await _handle_del(client, reply_to, rest, prefix)
    if verb == "list":
        return await _handle_list(client, reply_to)

    return False


async def _handle_add(client, reply_to: str, source: str,
                      rest: str, prefix: str) -> bool:
    if not rest:
        await client.message(reply_to, f"Usage: {prefix}add user <mask> [note]")
        return True
    parts = rest.split(None, 1)
    mask = normalize_pattern(parts[0])
    note = parts[1].strip() if len(parts) > 1 else ""

    existing = find_user_acl_by_mask(mask)
    if existing:
        await client.message(
            reply_to,
            f"⚠ {mask} déjà présent (id={existing.id}). Édite via /users.",
        )
        return True

    row = add_user_acl(mask=mask, note=note, added_by=f"irc:{source}")
    await bus.publish(Event(type="system", payload={
        "message": f"[user_admin/add] id={row.id} mask={mask} by={source}"
    }))
    await client.message(
        reply_to,
        f"✅ Ajouté id={row.id}: {mask} — tous flags OFF. Coche dans /users.",
    )
    return True


async def _handle_del(client, reply_to: str, rest: str, prefix: str) -> bool:
    if not rest:
        await client.message(reply_to, f"Usage: {prefix}del user <mask>")
        return True
    mask = normalize_pattern(rest.split()[0])
    row = find_user_acl_by_mask(mask)
    if row is None:
        await client.message(reply_to, f"❌ {mask} introuvable (match exact requis).")
        return True
    if row.is_admin:
        await client.message(
            reply_to,
            f"⛔ {mask} est admin — retire d'abord le flag admin via /users.",
        )
        return True

    delete_user_acl(row.id)
    await bus.publish(Event(type="system", payload={
        "message": f"[user_admin/del] id={row.id} mask={mask}"
    }))
    await client.message(reply_to, f"🗑 Supprimé id={row.id}: {mask}")
    return True


async def _handle_list(client, reply_to: str) -> bool:
    rows = list_user_acls()
    if not rows:
        await client.message(reply_to, "Aucun user enregistré.")
        return True

    await client.message(reply_to, f"📋 {len(rows)} users:")
    for r in rows:
        flags = []
        if not r.active:
            flags.append("OFF")
        # is_admin d'abord (label court en majuscules), puis les allow_*
        if r.is_admin:
            flags.append("ADMIN")
        for f in USER_ACL_FLAGS:
            if f == "is_admin":
                continue  # déjà traité au-dessus
            if getattr(r, f, False):
                flags.append(f.replace("allow_", ""))
        flag_str = ",".join(flags) if flags else "no-flags"
        kw = f" kw={r.keyword!r}" if r.keyword else ""
        note = f" — {r.note}" if r.note else ""
        await client.message(reply_to, f"  #{r.id} {r.mask} [{flag_str}]{kw}{note}")
    return True
