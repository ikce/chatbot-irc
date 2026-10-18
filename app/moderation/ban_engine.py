"""Ban engine adaptive (Phase 4).

Le bot pose un ban IRC selon son statut sur le chan :
    - Si op → MODE +b direct (via pydle `client.rawmsg("MODE", chan, "+b", mask)`)
    - Sinon → /msg <chanserv_nick> BAN #chan mask reason (Undernet X par défaut)

Coverage bidirectionnel : si on a IP + host distinct, on pose 2 masks (*!*@ip et
*!*@host) pour attraper les reconnexions avec cloak vs IP directe.

Safeguards :
    - Dry-run par chan (`policy.dry_run`) : record BannedEntity avec active=False,
      log dans IncidentHistory (incident_type='dry_run_ban'), pas d'ordre IRC émis.
    - Dedup fenêtre : si un mask identique a été banni dans les N dernières secondes
      (`BanEngineConfig.duplicate_dedupe_window_sec`), on skip.
    - Rate limit global : max `max_bans_per_hour` bans/heure toutes sources
      confondues (safeguard runaway).

Entrées possibles :
    - `emit_from_verdict(client, verdict, channel, nick, ident, host, policy)` :
      appelé depuis _reputation_check_and_notify (P2) quand verdict.should_ban.
    - `emit_from_pipeline(client, pattern_matches, llm_verdict, channel, nick,
      ident, host, ip, policy)` : appelé depuis message_pipeline (P3) quand
      is_severe.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..db import (
    BanReasonTemplate,
    ChannelPolicy,
    count_recent_bans,
    find_recent_ban,
    get_ban_engine_config,
    get_ban_template,
    list_ban_templates,
    record_ban,
    record_incident,
)
from ..event_bus import Event, bus

log = logging.getLogger("chatbot.ban_engine")


@dataclass
class BanContext:
    """Toutes les infos nécessaires pour émettre un ban et interpoler la raison."""
    channel: str
    nick: str
    ident: str
    host: str
    ip: Optional[str] = None
    resolved_host: Optional[str] = None
    source: str = "manual"              # spamhaus_drop | abuseipdb | llm | pattern | local_incidents | manual
    score: int = 0
    verdict: str = ""                   # verdict LLM textuel si applicable
    pattern_kinds: str = ""             # csv des kinds patterns matchés
    cidr: str = ""                      # CIDR Spamhaus si applicable
    reference: str = ""                 # SBL ref ou ID quelconque
    trigger_context: str = ""           # message ayant déclenché
    template_override: Optional[str] = None  # raison textuelle pré-composée (bypass template)


def _is_public_ip(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
        return ip.is_global and not ip.is_multicast and not ip.is_reserved
    except ValueError:
        return False


def _build_masks(ctx: BanContext) -> list[str]:
    """Construit la liste des masks à bannir pour couvrir IP + host.

    Règles :
        - Si IP publique : *!*@<ip>
        - Si host connu et différent de l'IP : *!*@<host>
        - Sinon fallback sur *!*@<host> seul (host peut être une cloak)

    On dédupe pour éviter de poser 2× le même mask si host == ip textuellement.
    """
    masks: list[str] = []
    seen: set[str] = set()

    def _add(target: str) -> None:
        m = f"*!*@{target}"
        if m not in seen:
            seen.add(m)
            masks.append(m)

    if ctx.ip and _is_public_ip(ctx.ip):
        _add(ctx.ip)
    if ctx.host and ctx.host != ctx.ip:
        # Ne pas bannir des hostnames vides ou "localhost" ou IPs privées
        if ctx.host and ctx.host.lower() not in ("localhost", "127.0.0.1", "::1"):
            _add(ctx.host)
    if not masks and ctx.host:
        # Dernier recours : même si host semblait privé, on essaie
        _add(ctx.host)
    return masks


def _pick_template(policy: ChannelPolicy) -> Optional[BanReasonTemplate]:
    """Retourne le template raison à utiliser : policy > engine default > premier actif."""
    if policy.ban_reason_template_id:
        t = get_ban_template(policy.ban_reason_template_id)
        if t and t.active:
            return t
    engine_cfg = get_ban_engine_config()
    if engine_cfg.default_template_id:
        t = get_ban_template(engine_cfg.default_template_id)
        if t and t.active:
            return t
    # fallback : premier actif dispo
    actives = list_ban_templates(active_only=True)
    return actives[0] if actives else None


def _render_reason(
    template: Optional[BanReasonTemplate],
    ctx: BanContext,
    bot_nick: str,
) -> str:
    """Interpole la raison avec le contexte ban. Placeholders supportés :
        {nick} {ip} {host} {source} {score} {chan} {channel} {verdict}
        {pattern_kinds} {cidr} {ref} {bot}
    """
    if ctx.template_override:
        return ctx.template_override[:400]
    if template is None:
        return f"Automated moderation action by {bot_nick}."
    subs = {
        "nick": ctx.nick or "?",
        "ip": ctx.ip or "?",
        "host": ctx.host or "?",
        "source": ctx.source or "auto",
        "score": str(ctx.score) if ctx.score else "?",
        "chan": ctx.channel,
        "channel": ctx.channel,
        "verdict": ctx.verdict or "?",
        "pattern_kinds": ctx.pattern_kinds or "?",
        "cidr": ctx.cidr or "?",
        "ref": ctx.reference or "?",
        "bot": bot_nick,
    }
    try:
        return template.template.format(**subs)[:400]
    except (KeyError, IndexError, ValueError) as e:
        log.warning("Template '%s' interpolation failed: %s — using literal", template.name, e)
        return template.template[:400]


def is_bot_op(client, channel: str) -> bool:
    """Détecte si le bot a le +o sur ce chan (inspecte pydle channels state).

    pydle expose `client.channels[chan]['modes']['o']` = liste des ops.
    Certaines versions utilisent des sets. On teste avec fallback.
    """
    try:
        chan_info = getattr(client, "channels", {}).get(channel)
        if not chan_info:
            return False
        modes = chan_info.get("modes", {}) or {}
        ops = modes.get("o") or modes.get("+o") or []
        # ops peut être list, set, tuple ou dict
        if isinstance(ops, dict):
            ops = list(ops.keys())
        return client.nickname in ops
    except Exception:
        log.debug("is_bot_op check failed for %s", channel, exc_info=True)
        return False


async def _send_mode_ban(client, channel: str, mask: str) -> bool:
    """Émet MODE +b via pydle. Retourne True si envoyé sans exception."""
    try:
        # pydle expose set_mode(target, *modes)
        await client.rawmsg("MODE", channel, "+b", mask)
        return True
    except Exception:
        log.exception("MODE +b failed for %s %s", channel, mask)
        return False


async def _send_chanserv_ban(
    client, channel: str, mask: str, reason: str, duration_sec: int
) -> bool:
    """Émet /msg <chanserv> BAN via template interpolé."""
    cfg = get_ban_engine_config()
    duration_str = "0" if duration_sec <= 0 else str(duration_sec)
    try:
        cmd = cfg.chanserv_ban_cmd_template.format(
            channel=channel,
            mask=mask,
            duration=duration_str,
            reason=reason[:200],
        )
    except (KeyError, IndexError, ValueError) as e:
        log.warning("chanserv template broken: %s — fallback", e)
        cmd = f"BAN {channel} {mask} {duration_str} :{reason[:200]}"
    try:
        await client.message(cfg.chanserv_nick, cmd)
        return True
    except Exception:
        log.exception("chanserv ban send failed for %s %s", channel, mask)
        return False


async def _send_kick(client, channel: str, nick: str, message: str) -> None:
    try:
        await client.rawmsg("KICK", channel, nick, message[:200])
    except Exception:
        log.debug("KICK failed (probably not op) for %s %s", channel, nick, exc_info=True)


async def emit_ban(
    client,
    ctx: BanContext,
    policy: ChannelPolicy,
) -> dict:
    """Point d'entrée principal. Retourne un dict de résultat.

    Respect strict de `policy.auto_ban_enabled` et `policy.dry_run` :
        - `auto_ban_enabled=False` → no-op, retourne skipped=True
        - `dry_run=True` → record BannedEntity(active=False) + log incident,
          jamais d'ordre IRC.

    Le caller est responsable d'avoir vérifié la sévérité — ce module n'a pas
    de logique de sévérité, juste l'exécution mécanique du ban.
    """
    result = {
        "skipped": False,
        "dry_run": False,
        "masks": [],
        "method": "",
        "reason": "",
        "errors": [],
    }

    if not policy.auto_ban_enabled:
        result["skipped"] = True
        result["errors"].append("auto_ban_disabled")
        return result

    engine_cfg = get_ban_engine_config()

    # Rate limit global (safeguard runaway)
    recent = count_recent_bans(since_seconds=3600)
    if recent >= engine_cfg.max_bans_per_hour:
        log.warning(
            "Ban engine rate-limited: %d bans in last hour >= %d",
            recent, engine_cfg.max_bans_per_hour,
        )
        result["skipped"] = True
        result["errors"].append(f"rate_limited({recent}/{engine_cfg.max_bans_per_hour})")
        await bus.publish(Event(type="error", payload={
            "message": (
                f"[BAN-ENGINE] SKIP {ctx.channel} {ctx.nick} — "
                f"rate limit hit ({recent}/{engine_cfg.max_bans_per_hour}/h)"
            )
        }))
        return result

    masks = _build_masks(ctx)
    if not masks:
        result["skipped"] = True
        result["errors"].append("no_mask_computable")
        return result

    # Dedup : skip masks bannis récemment
    filtered: list[str] = []
    for m in masks:
        existing = find_recent_ban(
            ctx.channel, m, engine_cfg.duplicate_dedupe_window_sec,
        )
        if existing is None:
            filtered.append(m)
        else:
            result["errors"].append(f"dedup_skip:{m}")
    if not filtered:
        result["skipped"] = True
        return result

    result["masks"] = filtered

    # Compose raison
    template = _pick_template(policy)
    bot_nick = getattr(client, "nickname", "bot") if client else "bot"
    reason = _render_reason(template, ctx, bot_nick)
    result["reason"] = reason

    # DRY-RUN : log + broadcast, pas d'ordre IRC
    if policy.dry_run:
        result["dry_run"] = True
        result["method"] = "dry_run"
        for m in filtered:
            record_ban(
                channel=ctx.channel, mask=m, ip=ctx.ip, host=ctx.host,
                reason=reason, source=ctx.source,
                banned_by_nick=f"{bot_nick} (dry-run)",
                original_nick=ctx.nick,
                trigger_context=ctx.trigger_context,
                active=False,  # dry-run : jamais actif
            )
        record_incident(
            ip=ctx.ip, host=ctx.host, nick=ctx.nick, channel=ctx.channel,
            incident_type="dry_run_ban",
            severity=min(10, max(1, ctx.score // 10 or 5)),
            detail=f"[DRY-RUN] masks={filtered} source={ctx.source} reason={reason}"[:1000],
        )
        line = (
            f"[BAN-ENGINE DRY-RUN] {ctx.channel} {ctx.nick} → "
            f"masks={filtered} source={ctx.source} :: {reason[:200]}"
        )
        await bus.publish(Event(type="system", payload={"message": line}))
        return result

    # LIVE : émission réelle
    if client is None:
        result["skipped"] = True
        result["errors"].append("no_client")
        return result

    duration = max(0, policy.default_ban_duration_sec)
    op = is_bot_op(client, ctx.channel)
    method = "mode" if (op and not engine_cfg.prefer_chanserv) else "chanserv"
    result["method"] = method

    successes: list[str] = []
    for m in filtered:
        if method == "mode":
            ok = await _send_mode_ban(client, ctx.channel, m)
        else:
            ok = await _send_chanserv_ban(client, ctx.channel, m, reason, duration)
        if ok:
            successes.append(m)
            record_ban(
                channel=ctx.channel, mask=m, ip=ctx.ip, host=ctx.host,
                reason=reason, source=ctx.source,
                banned_by_nick=bot_nick,
                original_nick=ctx.nick,
                trigger_context=ctx.trigger_context,
                active=True,
            )
        else:
            result["errors"].append(f"send_failed:{m}")

    # KICK optionnel (utile pour éjecter immédiatement, sinon le user reste en chan
    # jusqu'à sa prochaine action serveur — le ban empêche juste de re-JOIN)
    if successes and engine_cfg.kick_before_ban and (op or engine_cfg.prefer_chanserv is False):
        kick_msg = engine_cfg.kick_message_template.format(reason=reason)
        await _send_kick(client, ctx.channel, ctx.nick, kick_msg)

    # Log incident central + broadcast console
    record_incident(
        ip=ctx.ip, host=ctx.host, nick=ctx.nick, channel=ctx.channel,
        incident_type="ban",
        severity=min(10, max(1, ctx.score // 10 or 5)),
        detail=f"masks={successes} method={method} source={ctx.source} reason={reason}"[:1000],
    )
    line = (
        f"[BAN-ENGINE {method.upper()}] {ctx.channel} {ctx.nick} → "
        f"masks={successes} source={ctx.source} :: {reason[:200]}"
    )
    await bus.publish(Event(type="system", payload={"message": line}))

    # Notifications policy
    if policy.notify_channel:
        try:
            await client.message(
                policy.notify_channel,
                f"[BAN {ctx.channel}] {ctx.nick} ({ctx.host}) — {reason[:200]}",
            )
        except Exception:
            log.exception("notify_channel send failed")
    if policy.notify_nick:
        try:
            await client.message(
                policy.notify_nick,
                f"[BAN {ctx.channel}] {ctx.nick} → masks={successes} :: {reason[:200]}",
            )
        except Exception:
            log.exception("notify_nick send failed")

    return result


async def unban_mask(client, channel: str, mask: str) -> bool:
    """MODE -b <mask>. Utilisé par la WebUI pour un unban manuel."""
    if client is None:
        return False
    try:
        await client.rawmsg("MODE", channel, "-b", mask)
        return True
    except Exception:
        log.exception("MODE -b failed for %s %s", channel, mask)
        return False


# =============================================================================
# Adapters pour les 2 sources d'appel (reputation P2 + message pipeline P3)
# =============================================================================


async def emit_from_verdict(
    client,
    verdict,   # ReputationVerdict
    channel: str,
    nick: str,
    ident: str,
    host: str,
    policy: ChannelPolicy,
) -> dict:
    """Adapter appelé depuis _reputation_check_and_notify quand verdict.should_ban."""
    ctx = BanContext(
        channel=channel, nick=nick, ident=ident, host=host,
        ip=verdict.ip, resolved_host=verdict.resolved_host,
        source=verdict.ban_source or (verdict.sources[0] if verdict.sources else "reputation"),
        score=verdict.score,
    )
    if verdict.spamhaus_match:
        ctx.cidr = verdict.spamhaus_match.get("cidr", "")
        ctx.reference = verdict.spamhaus_match.get("ref", "")
    return await emit_ban(client, ctx, policy)


async def emit_from_pipeline(
    client,
    channel: str,
    nick: str,
    ident: str,
    host: str,
    ip: Optional[str],
    pattern_matches: list,
    llm_verdict,
    policy: ChannelPolicy,
    trigger_message: str = "",
) -> dict:
    """Adapter appelé depuis message_pipeline quand is_severe."""
    # Prioriser la source : LLM > pattern (LLM confirme le contexte)
    if llm_verdict is not None and llm_verdict.is_severe:
        source = "llm"
        verdict_txt = llm_verdict.verdict
        score = int(round(llm_verdict.confidence * 100))
    elif pattern_matches:
        source = "pattern"
        verdict_txt = ""
        score = int(max((m.severity for m in pattern_matches), default=5)) * 10
    else:
        source = "content"
        verdict_txt = ""
        score = 50

    ctx = BanContext(
        channel=channel, nick=nick, ident=ident, host=host,
        ip=ip, source=source, score=score, verdict=verdict_txt,
        pattern_kinds=",".join(sorted({m.kind for m in pattern_matches})) if pattern_matches else "",
        trigger_context=trigger_message,
    )
    return await emit_ban(client, ctx, policy)
