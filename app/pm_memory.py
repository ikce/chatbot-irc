"""Mémoire PV persistante pour conversations avec le bot en PM.

Toggle par user (nick, case-insensitive) : `!pv on` active, `!pv off` désactive
(sans détruire l'historique), `!pv clear` efface l'historique, `!pv reset`
efface + désactive, `!pv status` info.

Quand actif ET en PM :
    - TOUS les messages PM sont traités par le LLM (bypass du trigger keyword)
    - Le message user est stocké dans PmMessage
    - Les N derniers messages (config LlmConfig.pm_memory_window, default 30)
    sont injectés comme history dans generate_reply
    - La réponse assistant est stockée aussi

Design decisions :
    - Clé = nick.lower() plutôt que mask complet : ident/host changent (VPN,
      NAT, dual-boot), le nick est stable dans la pratique IRC.
    - ACL : gate par allow_llm_chat (grandfathered si aucun UserAcl matche).
    - Ne fait rien en channel (target != source) : `try_handle` return False.
    - `!pv` commands sont traitées EN PREMIER même quand memory est off, pour
      permettre `!pv on` sans autre pré-requis.
"""
from __future__ import annotations

import logging

from .db import (
    add_pm_message,
    count_pm_messages,
    delete_pm_messages,
    get_irc_config,
    get_llm_config,
    get_pm_memory,
    list_pm_messages,
    resolve_user_acl,
    set_pm_memory_enabled,
    touch_pm_memory,
    user_acl_check,
)
from .event_bus import Event, bus
from .llm import generate_reply, tool_ids_for
from . import ignore, memory
from .throttle import throttle


log = logging.getLogger("chatbot.pm_memory")

_VALID_SUBS = {"on", "off", "clear", "reset", "status"}
_FALLBACK_PREFIXES = ("!", ".", "+", "~")


def _nick_key(nick: str) -> str:
    return nick.lower()


def _prefix() -> str:
    try:
        return get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        return "!"


def _match_pv_command(stripped: str, prefix: str) -> str | None:
    """Retourne le sous-commande si `stripped` = `<prefix>pv [sub]`, sinon None.

    Accepte aussi les préfixes fallback pour la fenêtre transition post-changement.
    Retourne '' (chaîne vide) si `!pv` sans argument.
    """
    for p in (prefix, *_FALLBACK_PREFIXES):
        head = p + "pv"
        if stripped == head:
            return ""
        if stripped.startswith(head + " ") or stripped.startswith(head + "\t"):
            return stripped[len(head):].strip()
    return None


async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """Traite le message : cmd `!pv` OU chat PV avec mémoire. True = consommé.

    En channel : no-op immédiat (return False), on ne touche jamais aux chans.
    """
    is_pm = channel == source
    if not is_pm:
        return False

    stripped = message.strip()
    if not stripped:
        return False

    prefix = _prefix()

    # !pv command dispatch — géré AVANT le check enabled, pour permettre `!pv on`.
    sub_or_none = _match_pv_command(stripped, prefix)
    if sub_or_none is not None:
        return await _handle_pv_command(
            client, source, ident, host, sub_or_none, prefix,
        )

    # Chat avec mémoire — check enabled
    key = _nick_key(source)
    mem = get_pm_memory(key)
    if mem is None or not mem.enabled:
        return False  # pas opt-in → laisse le pipeline continuer (trigger classique)

    # UserAcl guard : deny-by-default (feature coûteuse LLM + DB).
    # Résout l'UserAcl une fois pour récupérer is_admin, allow_llm_chat ET
    # allow_web_search dans la foulée.
    acl = resolve_user_acl(source, ident, host)
    is_admin = bool(acl and acl.is_admin)
    allow_chat = bool(acl and acl.allow_llm_chat)
    if not is_admin and not allow_chat:
        await bus.publish(Event(type="system", payload={
            "message": f"[pm_memory] refusé (pas d'ACL allow_llm_chat) — {source}"
        }))
        return False  # laisse le pipeline continuer (mais le guard trigger le bloquera aussi)

    # Throttle (partage la même mécanique que le chat classique)
    llm_cfg = get_llm_config()
    if not throttle.can_respond(
        source, llm_cfg.throttle_global_sec, llm_cfg.throttle_per_nick_sec
    ):
        await bus.publish(Event(type="system", payload={
            "message": f"[pm_memory] throttled — {source}"
        }))
        return True  # consommé silencieusement
    throttle.mark(source)

    if ignore.is_ignored(source, ident, host):
        return True  # ignoré : silence
    if ignore.is_repetition(source, message):
        minutes = ignore.ignore(source, ident, host)
        if minutes:
            await client.message(source, f"Tu tournes en rond, je t'ignore pendant {minutes} min. 🙄")
            await bus.publish(Event(type="system", payload={
                "message": f"[ignore] {source} ignoré {minutes} min (répétition, PV)"
            }))
            return True

    # Build history (N derniers messages, oldest first)
    window = getattr(llm_cfg, "pm_memory_window", 30) or 30
    history_rows = list_pm_messages(key, limit=window)
    history = [{"role": r.role, "content": r.content} for r in history_rows]

    # Store user turn AVANT génération (visible même si LLM crash)
    add_pm_message(key, role="user", content=message, window=max(window * 2, 100))
    touch_pm_memory(key)

    # web_search effectif = kill-switch global AND opt-in per-user
    effective_ws = bool(
        llm_cfg.web_search_enabled and acl and acl.allow_web_search
    )
    # Faits long terme (memory.py). Pas de recent_lines : `history` couvre déjà
    # la conversation PV.
    try:
        identity = memory.resolve(source, ident, host, memory.account_of(client, source))
    except Exception:  # noqa: BLE001
        log.exception("memory.resolve failed")
        identity = None
    ctx = memory.build_context(identity, source, source, is_pm=True)
    try:
        reply = await generate_reply(
            message, source, history=history, web_search=effective_ws,
            memory_facts=ctx.facts, aliases=ctx.aliases, is_pm=True,
            tool_ids=tool_ids_for(message, bool(acl and acl.allow_url_fetch)),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("PM memory LLM failed for %s", source)
        await bus.publish(Event(type="error", payload={
            "message": f"pm_memory LLM: {e}"
        }))
        return True

    reply, wants_ignore = ignore.split_tag(reply)
    if not reply:
        return True

    # Store assistant turn
    add_pm_message(key, role="assistant", content=reply, window=max(window * 2, 100))
    memory.record_exchange(identity, source, message, reply, is_pm=True)
    if wants_ignore:
        minutes = ignore.ignore(source, ident, host)
        if minutes:
            await client.message(source, f"(je t'ignore pendant {minutes} min)")
            await bus.publish(Event(type="system", payload={
                "message": f"[ignore] {source} ignoré {minutes} min (décision du LLM, PV)"
            }))

    # Send PM back
    for line in _chunk(reply, 400):
        try:
            await client.message(source, line)
        except Exception:  # noqa: BLE001
            log.exception("send PM failed for %s", source)
            break

    return True


async def _handle_pv_command(
    client, source: str, ident: str, host: str, rest: str, prefix: str,
) -> bool:
    """Traite `!pv [on|off|clear|reset|status]`. Toujours consommé (return True)."""
    sub = (rest.split()[0].lower() if rest else "status")
    if sub not in _VALID_SUBS:
        await client.message(
            source,
            f"Usage: {prefix}pv [on|off|clear|reset|status]",
        )
        return True

    key = _nick_key(source)

    if sub == "on":
        # Guard : allow_llm_chat requis (deny-by-default).
        is_admin = user_acl_check(source, ident, host, "is_admin")
        if not is_admin \
                and not user_acl_check(source, ident, host, "allow_llm_chat"):
            await client.message(
                source,
                "❌ Tu n'as pas allow_llm_chat activé — la mémoire PV serait "
                "sans effet. Fais activer par un admin (/users).",
            )
            return True
        set_pm_memory_enabled(key, True)
        await bus.publish(Event(type="system", payload={
            "message": f"[pm_memory] {source} ON"
        }))
        await client.message(
            source,
            "✅ Mémoire PV ON. Je vais suivre notre conversation. "
            f"({prefix}pv off pour désactiver, {prefix}pv clear pour effacer)",
        )
        return True

    if sub == "off":
        set_pm_memory_enabled(key, False)
        await bus.publish(Event(type="system", payload={
            "message": f"[pm_memory] {source} OFF"
        }))
        await client.message(
            source,
            "⏸️ Mémoire PV OFF. L'historique est gardé — "
            f"{prefix}pv on pour reprendre où on en était.",
        )
        return True

    if sub == "clear":
        n = delete_pm_messages(key)
        await bus.publish(Event(type="system", payload={
            "message": f"[pm_memory] {source} clear ({n} msgs)"
        }))
        await client.message(
            source,
            f"🗑 {n} messages effacés. Toggle inchangé.",
        )
        return True

    if sub == "reset":
        n = delete_pm_messages(key)
        set_pm_memory_enabled(key, False)
        await bus.publish(Event(type="system", payload={
            "message": f"[pm_memory] {source} reset ({n} msgs)"
        }))
        await client.message(
            source,
            f"♻ Reset complet : {n} messages effacés, mémoire OFF.",
        )
        return True

    # status
    mem = get_pm_memory(key)
    n = count_pm_messages(key)
    if mem is None:
        await client.message(
            source,
            f"Mémoire PV : jamais initialisée. Tape {prefix}pv on pour activer.",
        )
    else:
        state = "ON" if mem.enabled else "OFF"
        last = (mem.last_active.strftime('%Y-%m-%d %H:%M')
                if mem.last_active else "?")
        await client.message(
            source,
            f"Mémoire PV: {state} · {n} messages en historique · "
            f"dernière activité {last}",
        )
    return True


def _chunk(text: str, size: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    return [text[i: i + size] for i in range(0, len(text), size)]
