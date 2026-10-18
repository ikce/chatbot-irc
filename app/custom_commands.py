"""Commandes IRC custom : mapping user-défini (regex → recette read-only).

Design safety-first :
    - **Pas de SQL arbitraire**. Le user configure quel *recette* (fonction
      Python read-only pré-approuvée) sa commande appelle. La liste des
      recettes vit ici (`RECIPES`). Ajouter une recette = éditer ce fichier.
    - **Regex compilée côté Python**, jamais interprétée en DB.
    - **Args = groupes nommés** de la regex → passés en kwargs à la recette.
    - **Output = template str.format**, `{clé}` interpolé depuis le dict
      renvoyé par la recette. Vide → dump texte compact automatique.
    - **ACL** : réutilise `BibleAcl` (une seule liste de masks à maintenir).
    - **Throttle** : partagé par le handler, indépendant du LLM classique.

Wiring : `try_handle()` est appelé par irc_client.on_message() AVANT le
handler Bible et AVANT la logique trigger LLM. Si consommé → return.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

import httpx

from . import ip_intel
from .db import (
    active_ban_for_target,
    count_incidents_for_target,
    get_irc_config,
    get_llm_config,
    list_custom_commands,
    log_message,
    lookup_ips_for_nick,
    lookup_nicks_for_host,
    lookup_nicks_for_ip,
    seen_nick_summary,
    top_flaggers_for_channel,
    user_acl_check,
)
from .event_bus import Event, bus
from .moderation.mask_utils import mask_matches

# Noms des commandes seed intel — check allow_intel plutôt que allow_custom_commands
INTEL_SEED_NAMES = frozenset({"flag", "ip", "seen", "host", "topflag"})

log = logging.getLogger("chatbot.custom_commands")


# =============================================================================
# Recipe registry — SEULES ces fonctions sont invocables via une CustomCommand
# =============================================================================


def _recipe_ip_flag_summary(target: str, **_) -> dict:
    """Résumé rapide flag pour une IP ou un host : nb incidents, types, ban."""
    ip = target if _looks_like_ip(target) else None
    host = None if ip else target
    inc = count_incidents_for_target(ip=ip, host=host, since_days=90)
    ban = active_ban_for_target(ip=ip, host=host)
    from .db import lookup_ip_annotations
    ann_list = lookup_ip_annotations(ip=ip, host=host)
    ann_str = "aucune"
    if ann_list:
        ann_str = ", ".join(f"[{a.label}] {a.note or '(sans note)'}" for a in ann_list[:2])
    types_str = ", ".join(f"{k}={v}" for k, v in (inc.get("by_type") or {}).items()) or "aucun"
    return {
        "target": target,
        "incident_count": inc.get("total", 0),
        "incident_types": types_str,
        "last_incident": _fmt_ts(inc.get("last_ts")),
        "severity_max": inc.get("severity_max", 0),
        "ban_status": _ban_str(ban),
        "annotation": ann_str,
    }


def _recipe_ip_full_intel(ip: str, **_) -> dict:
    """Dossier complet : agrège via ip_intel.summarize_ip, renvoie le texte prêt LLM."""
    summary = ip_intel.summarize_ip(ip)
    return {
        "target": summary["target"],
        "verdict": summary["verdict_hint"],
        "text": ip_intel.format_for_llm(summary),
        # Champs individuels pour usage éventuel dans output_template
        "annotation_count": len(summary.get("annotations") or []),
        "abuseipdb_score": (summary.get("abuseipdb") or {}).get("score", 0),
        "spamhaus_listed": bool(summary.get("spamhaus")),
        "incident_count": (summary.get("incidents") or {}).get("total", 0),
        "banned": bool(summary.get("active_ban")),
    }


def _recipe_nick_history(nick: str, **_) -> dict:
    """Historique tracelog d'un nick : first/last seen, chans, IPs distinctes."""
    s = seen_nick_summary(nick)
    if not s:
        return {
            "nick": nick, "events": 0, "first_seen": "jamais",
            "last_seen": "jamais", "channels": "aucun", "ip_count": 0,
        }
    ips = lookup_ips_for_nick(nick, limit=10)
    chans = ", ".join(s.get("channels") or []) or "aucun"
    return {
        "nick": s["nick"],
        "events": s["events"],
        "first_seen": _fmt_ts(s["first_seen"]),
        "last_seen": _fmt_ts(s["last_seen"]),
        "channels": chans,
        "ip_count": len(ips),
        "ips_preview": ", ".join(
            f"{r.get('ip') or r.get('host') or '?'}" for r in ips[:5]
        ),
    }


def _recipe_host_activity(host: str, **_) -> dict:
    """Activité d'un host (accept wildcards SQL % ou pattern texte)."""
    pat = host if "%" in host else f"%{host}%"
    rows = lookup_nicks_for_host(pat, limit=20)
    if not rows:
        return {
            "host": host, "events": 0, "nick_count": 0, "ip_count": 0,
            "last_seen": "jamais",
        }
    events = sum(r["count"] for r in rows)
    nicks = {r["nick"] for r in rows}
    ips = {r.get("ip") for r in rows if r.get("ip")}
    last = max((r["last_seen"] for r in rows if r.get("last_seen")), default=None)
    return {
        "host": host,
        "events": events,
        "nick_count": len(nicks),
        "ip_count": len(ips),
        "last_seen": _fmt_ts(last),
    }


def _recipe_ban_status(target: str, **_) -> dict:
    """Ban actif matché sur ip ou host ?"""
    ip = target if _looks_like_ip(target) else None
    host = None if ip else target
    ban = active_ban_for_target(ip=ip, host=host)
    return {
        "target": target,
        "banned": bool(ban),
        "status": _ban_str(ban),
    }


def _recipe_chan_top_flaggers(channel: Optional[str] = None, **_) -> dict:
    """Top 5 nicks avec le plus d'incidents (30j), tous chans ou 1 seul."""
    rows = top_flaggers_for_channel(channel=channel, limit=5, since_days=30)
    scope = f"sur {channel}" if channel else "globalement"
    if not rows:
        return {"scope": scope, "summary": f"Aucun flaggeur {scope} (30j).", "count": 0}
    parts = []
    for r in rows:
        types = ", ".join(f"{k}:{v}" for k, v in r["types"].items())
        parts.append(f"{r['nick']} ({r['count']}: {types})")
    return {
        "scope": scope,
        "count": len(rows),
        "summary": f"Top flaggeurs {scope} (30j): " + " | ".join(parts),
    }


RECIPES: dict[str, Callable[..., dict]] = {
    "ip_flag_summary": _recipe_ip_flag_summary,
    "ip_full_intel": _recipe_ip_full_intel,
    "nick_history": _recipe_nick_history,
    "host_activity": _recipe_host_activity,
    "ban_status": _recipe_ban_status,
    "chan_top_flaggers": _recipe_chan_top_flaggers,
}


# =============================================================================
# Throttle mémoire (partagé avec Bible ? non — pool séparé, ces commandes
# sont plus légères que fetch verset)
# =============================================================================

_last_by_mask: dict[str, float] = {}
_last_global: float = 0.0
_PER_MASK_SEC = 5.0
_GLOBAL_SEC = 1.0


def _throttled(user_mask: str) -> bool:
    global _last_global
    now = time.monotonic()
    if now - _last_global < _GLOBAL_SEC:
        return True
    if now - _last_by_mask.get(user_mask, 0.0) < _PER_MASK_SEC:
        return True
    return False


def _mark(user_mask: str) -> None:
    global _last_global
    now = time.monotonic()
    _last_global = now
    _last_by_mask[user_mask] = now


# =============================================================================
# Handler principal
# =============================================================================


_compiled_cache: dict[int, tuple[str, str, str, re.Pattern]] = {}
# Valeur = (prefix, pattern_mode, trigger_src, compiled). On invalide si un
# des 3 premiers change (prefix modifié dans WebUI, ou trigger édité).


_SIMPLE_PLACEHOLDER = re.compile(r"\\\{(\w+)(\\\.\\\.\\\.)?\\\}")


def _compile_simple_pattern(pattern: str, prefix: str) -> str:
    """Convertit un pattern lisible en regex avec named groups.

    Syntaxe :
        `flag {ip}`        → `^!flag\\s+(?P<ip>\\S+)$`
        `topflag {chan}`   → `^!topflag\\s+(?P<chan>\\S+)$`
        `say {rest...}`    → `^!say\\s+(?P<rest>.+)$`   (catch-all reste de ligne)

    Le préfixe est prepend automatiquement. Les espaces = `\\s+`.
    Les placeholders `{name}` matchent un token non-whitespace, `{name...}`
    matche le reste de la ligne. Tout le reste est literal (escaped).
    """
    escaped = re.escape(pattern)
    # re.escape transforme { → \{ et } → \} — restaurer les placeholders
    def _repl(m: re.Match) -> str:
        name = m.group(1)
        rest_syntax = m.group(2)  # \.\.\. (le "..." escapé)
        if rest_syntax:
            return f"(?P<{name}>.+)"
        return f"(?P<{name}>\\S+)"
    escaped = _SIMPLE_PLACEHOLDER.sub(_repl, escaped)
    # Espaces literaux (\ suivi d'espace) → \s+
    escaped = re.sub(r"(\\\s)+", r"\\s+", escaped)
    return f"^{re.escape(prefix)}{escaped}\\s*$"


def _apply_prefix_to_regex(trigger: str, prefix: str) -> str:
    """Prepend le préfixe à un trigger regex brut (mode 'regex').

    Si le trigger commence déjà par `^`, on insère le préfixe juste après.
    Sinon on préfixe avec `^{prefix}`. Idempotent : re-apply avec le même
    prefix ne double pas.
    """
    esc_prefix = re.escape(prefix)
    # Détecte si le trigger commence déjà par ^ + notre préfixe échappé
    if trigger.startswith(f"^{esc_prefix}"):
        return trigger
    if trigger.startswith("^"):
        return f"^{esc_prefix}{trigger[1:]}"
    return f"^{esc_prefix}{trigger}"


def _get_compiled(cmd) -> Optional[re.Pattern]:
    """Cache la regex compilée par (prefix, pattern_mode, trigger_src).

    Le préfixe est lu depuis IrcConfig.command_prefix à chaque appel, mais
    la fetch est cheap (SELECT id=1). On pourrait cacher plus agressivement
    mais l'invalidation deviendrait trickier — keep it simple.
    """
    try:
        prefix = get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        prefix = "!"
    mode = getattr(cmd, "pattern_mode", "regex") or "regex"
    src = cmd.trigger or ""

    cached = _compiled_cache.get(cmd.id)
    if cached is not None:
        c_prefix, c_mode, c_src, c_pat = cached
        if c_prefix == prefix and c_mode == mode and c_src == src:
            return c_pat

    try:
        if mode == "simple":
            regex_str = _compile_simple_pattern(src, prefix)
        else:
            regex_str = _apply_prefix_to_regex(src, prefix)
        p = re.compile(regex_str, re.IGNORECASE)
    except re.error as e:
        log.warning("regex invalide pour cmd id=%s (%s) [mode=%s]: %s",
                    cmd.id, cmd.name, mode, e)
        _compiled_cache.pop(cmd.id, None)
        return None
    _compiled_cache[cmd.id] = (prefix, mode, src, p)
    return p


def _acl_check(nick: str, ident: str, host: str, cmd_name: str) -> bool:
    """ACL par catégorie via UserAcl. Les commandes seed intel utilisent
    allow_intel ; les autres commandes custom utilisent allow_custom_commands.
    is_admin bypass tout (un admin peut tout invoquer).
    """
    if user_acl_check(nick, ident, host, "is_admin"):
        return True
    flag = "allow_intel" if cmd_name in INTEL_SEED_NAMES else "allow_custom_commands"
    return user_acl_check(nick, ident, host, flag)


def _format_output(cmd, data: dict) -> str:
    """Interpole output_template. Si vide, dump compact des clés string-friendly.

    Le champ spécial 'text' (utilisé par ip_full_intel) est utilisé tel quel
    quand le template est vide, pour laisser le LLM humaniser un blob multi-ligne.
    """
    if cmd.output_template:
        try:
            return cmd.output_template.format(**data)
        except KeyError as e:
            log.warning("output_template pour %s manque la clé %s: %s", cmd.name, e, data)
            # Fallback : dump ce qu'on a
            return _compact_dump(data)
    # Pas de template → si 'text' présent, l'utiliser (recettes qui produisent du texte pré-formatté)
    if isinstance(data.get("text"), str) and data["text"].strip():
        return data["text"]
    return _compact_dump(data)


def _compact_dump(data: dict) -> str:
    parts = []
    for k, v in data.items():
        if k in ("text",):
            continue
        parts.append(f"{k}={v}")
    return " | ".join(parts)


async def _humanize(text: str, cmd, source: str) -> str:
    """Appel LLM #2 pour reformuler la sortie recette avec la persona du bot."""
    cfg = get_llm_config()
    persona = cfg.humanization_prompt or cfg.system_prompt or (
        "Tu es un chatter bot IRC concis. Réponds en 1 à 3 phrases."
    )
    user_content = (
        f"Question de {source} (via commande custom '{cmd.name}').\n\n"
        f"Données brutes de la DB de modération:\n{text}\n\n"
        f"Reformule pour IRC en 1 à 3 phrases, factuel, sans inventer. "
        f"Si les données montrent un pattern suspect (annotation bad, Spamhaus, "
        f"score AbuseIPDB élevé, ban actif), signale-le en priorité."
    )
    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key}"
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": persona},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "stream": False,
    }
    url = f"{cfg.llm_url.rstrip('/')}/api/chat/completions"
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    out = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    return " ".join(out.splitlines()).strip() or text


def _chunk(text: str, size: int = 400) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """Traite le message si une commande custom match. True = consommé."""
    stripped = message.strip()
    if not stripped:
        return False
    # Short-circuit sur le préfixe courant + accepter aussi les préfixes
    # populaires (!, ., +, ~) pour laisser passer les patterns simples qui
    # ne se seraient pas encore recompilés — safety net cheap.
    try:
        prefix = get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        prefix = "!"
    if not stripped.startswith((prefix, "!", ".", "+", "~")):
        return False

    is_pm = channel == source  # target == nick de l'émetteur en PM
    user_mask = f"{source}!{ident}@{host}"

    for cmd in list_custom_commands(active_only=True):
        # Guard PM : si allow_pm=False, ignorer en contexte PM
        if is_pm and not getattr(cmd, "allow_pm", True):
            continue

        pat = _get_compiled(cmd)
        if pat is None:
            continue
        m = pat.match(stripped)
        if not m:
            continue

        # Match ! ACL check (catégorie basée sur le nom de la commande)
        if cmd.acl_required and not _acl_check(source, ident, host, cmd.name):
            await bus.publish(Event(type="system", payload={
                "message": f"[cmd/{cmd.name}] refusé (ACL) — {user_mask}"
            }))
            return True  # consommé silencieusement

        if _throttled(user_mask):
            await bus.publish(Event(type="system", payload={
                "message": f"[cmd/{cmd.name}] throttled — {user_mask}"
            }))
            return True

        # Lookup recette
        recipe = RECIPES.get(cmd.recipe)
        if recipe is None:
            log.warning("recipe inconnue: %s (cmd %s)", cmd.recipe, cmd.name)
            try:
                await client.message(channel, f"{source}: commande '{cmd.name}' cassée (recette introuvable).")
            except Exception:  # noqa: BLE001
                log.exception("send recipe-missing failed")
            return True

        _mark(user_mask)

        kwargs = m.groupdict()
        try:
            data = recipe(**kwargs)
        except Exception as e:  # noqa: BLE001
            log.exception("recipe %s crashed", cmd.recipe)
            await bus.publish(Event(type="error", payload={
                "message": f"[cmd/{cmd.name}] recipe error: {e}"
            }))
            try:
                await client.message(channel, f"{source}: erreur interne sur !{cmd.name}.")
            except Exception:  # noqa: BLE001
                log.exception("send error failed")
            return True

        text_out = _format_output(cmd, data)

        if cmd.humanize_with_llm and text_out:
            try:
                text_out = await _humanize(text_out, cmd, source)
            except Exception as e:  # noqa: BLE001
                log.warning("humanize failed pour %s, envoie brut: %s", cmd.name, e)
                # on garde text_out tel quel

        if not text_out:
            text_out = f"{source}: rien à afficher."

        for sub in _chunk(text_out, 400):
            try:
                await client.message(channel, sub)
                log_message(channel=channel, nick=client.nickname, message=sub, direction="out")
                await bus.publish(Event(type="bot_reply", payload={
                    "channel": channel, "nick": client.nickname, "message": sub,
                }))
            except Exception:  # noqa: BLE001
                log.exception("send output failed")
        return True

    return False


# =============================================================================
# Utils
# =============================================================================


def _looks_like_ip(s: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    if ts is None:
        return "jamais"
    return str(ts)


def _ban_str(ban) -> str:
    if ban is None:
        return "aucun"
    return f"{ban.mask} sur {ban.channel} (source: {ban.source})"
