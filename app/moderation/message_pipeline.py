"""Pipeline de modération par message (Phase 3).

Séquence pour un message reçu :
    1. Whitelist → si match, skip complet
    2. Pattern scan (regex) — rapide, sync
    3. Si match ou échantillon → LLM classify
    4. Log LlmModerationLog (systématique si LLM invoqué)
    5. Si verdict sévère → record_incident (alimente compteur pour Phase 2 réputation)
    6. Notification console + notify_channel/notify_nick si sévère

Phase 3 = dry-run côté bans. Le champ `action_taken` du LlmModerationLog note
"dry_run_ban" quand un ban aurait été émis en P4.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from ..db import (
    ChannelPolicy,
    get_channel_policy,
    get_llm_moderation_config,
    record_incident,
    record_llm_moderation,
)
from ..event_bus import Event, bus
from . import ban_engine, llm_mod, patterns
from .whitelist import is_whitelisted

log = logging.getLogger("chatbot.mod_pipeline")

# Throttles en mémoire (par nick, global) — sont perdus au restart, c'est ok.
_LAST_CHECK_PER_NICK: dict[str, float] = {}
_LAST_CHECK_GLOBAL: float = 0.0
_LOCK = asyncio.Lock()


async def _throttle_ok(nick: str) -> bool:
    """True si on peut lancer un check LLM maintenant, False sinon."""
    cfg = get_llm_moderation_config()
    now = time.monotonic()
    global _LAST_CHECK_GLOBAL
    async with _LOCK:
        # Global
        if now - _LAST_CHECK_GLOBAL < cfg.throttle_global_sec:
            return False
        # Per nick
        last = _LAST_CHECK_PER_NICK.get(nick, 0.0)
        if now - last < cfg.throttle_per_nick_sec:
            return False
        # Réserver le slot
        _LAST_CHECK_GLOBAL = now
        _LAST_CHECK_PER_NICK[nick] = now
        return True


async def process_message(
    nick: str,
    ident: str,
    host: str,
    channel: str,
    message: str,
    ip: Optional[str] = None,
    client=None,  # ChatterBot instance pour envoyer notifs IRC
    policy: Optional[ChannelPolicy] = None,
) -> None:
    """Point d'entrée depuis irc_client.on_message.

    N'est appelé QUE si la policy a llm_moderation_enabled OU pattern_detection_enabled.
    Jamais bloquant : swallow toutes les exceptions.
    """
    try:
        await _process(nick, ident, host, channel, message, ip, client, policy)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Pipeline modération : exception non gérée")


async def _process(
    nick: str,
    ident: str,
    host: str,
    channel: str,
    message: str,
    ip: Optional[str],
    client,
    policy: Optional[ChannelPolicy],
) -> None:
    if policy is None:
        policy = get_channel_policy(channel)
    if policy is None:
        return

    # 1. Whitelist court-circuit
    mask = f"{nick}!{ident}@{host}"
    wl = is_whitelisted(mask, channel=channel)
    if wl.whitelisted:
        return

    # 2. Config
    cfg = get_llm_moderation_config()
    if len(message) < cfg.min_message_len:
        return

    # 3. Pattern scan
    pattern_matches = []
    if policy.pattern_detection_enabled:
        pattern_matches = patterns.scan_message(message)

    # 4. Décide si on appelle le LLM
    should_call_llm = False
    if policy.llm_moderation_enabled:
        if pattern_matches:
            # match regex → toujours vérifier avec LLM (réduit faux positifs)
            should_call_llm = True
        elif cfg.sample_rate > 0.0 and random.random() < cfg.sample_rate:
            # échantillonnage passif
            should_call_llm = True

    # Si aucun signal ni LLM à appeler, rien à faire
    if not pattern_matches and not should_call_llm:
        return

    # 5. Throttle avant appel LLM (protège l'endpoint)
    verdict = None
    if should_call_llm:
        if not await _throttle_ok(nick):
            # LLM throttled — on ne l'appelle pas, mais on continue avec les patterns
            log.debug("LLM throttled pour %s/%s", nick, channel)
        else:
            hints = [f"{m.kind}:{m.name}" for m in pattern_matches]
            verdict = await llm_mod.classify_message(
                message=message,
                nick=nick,
                channel=channel,
                model_override=policy.llm_model_for_moderation or None,
                pattern_hints=hints,
            )

    # 6. Décide de l'action
    action_taken = "none"
    is_severe = False
    reasons: list[str] = []

    if verdict is not None:
        if verdict.error:
            log.warning("LLM error [%s/%s]: %s", channel, nick, verdict.error)
        if verdict.is_severe and verdict.confidence >= 0.5:
            is_severe = True
            reasons.append(f"LLM:{verdict.verdict}({verdict.confidence:.2f})")
        # Log systématique du passage LLM
        record_llm_moderation(
            channel=channel,
            nick=nick,
            host=host,
            ip=ip,
            message=message,
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            model_used=verdict.model_used,
            raw_llm_response=verdict.raw_response or verdict.error,
            action_taken=action_taken,  # updated ci-dessous si severe
        )

    # RÈGLE : n'importe quel pattern match = incident sévère à documenter.
    # Ban reste séparé : `strong_patterns` (sev >= 7) OU LLM sévère autorisent le
    # ban engine à tirer. Documentation ≠ ban.
    strong_patterns = [m for m in pattern_matches if m.severity >= 7]
    if pattern_matches and not is_severe:
        is_severe = True
        reasons.append(
            "PATTERNS:" + ",".join(
                f"{m.name}({m.kind},sev{m.severity})" for m in pattern_matches[:5]
            )
        )

    # Seuil ban distinct de la sévérité de documentation.
    ban_worthy = bool(strong_patterns) or (
        verdict is not None and verdict.is_severe and verdict.confidence >= 0.5
    )

    # ------------------------------------------------------------------
    # Trace TOUTES les alertes (sévères ou faibles) dans IncidentHistory.
    # Le user doit pouvoir consulter /moderation/alerts même si aucun ban.
    # ------------------------------------------------------------------
    trace_reasons: list[str] = []
    trace_severity: int = 0
    trace_type: str = ""

    if pattern_matches:
        trace_reasons.append(
            "PATTERNS:" + ",".join(
                f"{m.name}({m.kind},sev{m.severity})" for m in pattern_matches[:5]
            )
        )
        trace_severity = max(trace_severity, max(m.severity for m in pattern_matches))
        # Toute occurrence de pattern → incident sévère à documenter.
        trace_type = "flag_pattern"

    if verdict is not None and verdict.verdict and verdict.verdict != "normal":
        trace_reasons.append(f"LLM:{verdict.verdict}({verdict.confidence:.2f})")
        trace_severity = max(trace_severity, int(verdict.confidence * 10))
        if verdict.is_severe and verdict.confidence >= 0.5:
            trace_type = "flag_llm"
        elif not trace_type:
            trace_type = "llm_hint"

    if trace_reasons:
        try:
            record_incident(
                ip=ip, host=host, nick=nick, channel=channel,
                incident_type=trace_type or "signal",
                severity=max(1, trace_severity),
                detail=" | ".join(trace_reasons)[:1000],
            )
        except Exception:
            log.exception("record_incident failed")

        # Broadcast console pour signaux faibles (les sévères sont traités plus bas)
        if not is_severe:
            try:
                await bus.publish(Event(type="system", payload={
                    "message": (
                        f"[SIGNAL] {channel} {nick} ({trace_type}, sev {trace_severity}) "
                        f":: {' | '.join(trace_reasons)}"
                    ),
                }))
            except Exception:
                log.exception("bus.publish failed (weak signal)")

    if is_severe:
        # P4 : ban_engine décide selon auto_ban_enabled + dry_run
        if policy.auto_ban_enabled:
            action_taken = "dry_run_ban" if policy.dry_run else "ban"
        else:
            action_taken = "flag"

        # Broadcast console + notif IRC
        dry_tag = ""
        if policy.auto_ban_enabled and policy.dry_run:
            dry_tag = " [DRY-RUN — ban simulé]"
        elif not policy.auto_ban_enabled:
            dry_tag = " [flag — auto_ban_enabled=OFF]"
        summary_line = (
            f"[MOD] {channel} {nick}!{ident}@{host} → "
            f"{'/'.join([r.split(':')[0] for r in reasons]) or 'flag'} "
            f":: {' | '.join(reasons)}{dry_tag}"
        )
        try:
            await bus.publish(Event(type="system", payload={"message": summary_line}))
        except Exception:
            log.exception("bus.publish failed")

        # P4 : ban_engine appelé UNIQUEMENT si ban_worthy (sev >= 7 ou LLM sévère).
        # Le ban_engine respecte lui-même auto_ban_enabled + dry_run.
        if client is not None and policy.auto_ban_enabled and ban_worthy:
            try:
                await ban_engine.emit_from_pipeline(
                    client=client, channel=channel, nick=nick, ident=ident,
                    host=host, ip=ip,
                    pattern_matches=strong_patterns or pattern_matches,
                    llm_verdict=verdict,
                    policy=policy,
                    trigger_message=message,
                )
            except Exception:
                log.exception("ban_engine.emit_from_pipeline failed")

        # Notif IRC pour TOUS les incidents sévères (n'importe quel pattern match).
        # Le user veut la trace + la notif immédiate en cas de diffamation.
        if client is not None:
            if policy.notify_channel:
                try:
                    await client.message(
                        policy.notify_channel,
                        f"[MOD-ALERT {channel}] {nick}!{ident}@{host} → {' | '.join(reasons)[:300]}",
                    )
                except Exception:
                    log.exception("notify_channel send failed")
            if policy.notify_nick:
                try:
                    await client.message(
                        policy.notify_nick,
                        f"[MOD {channel}] {nick}!{ident}@{host} → {' | '.join(reasons)[:300]}",
                    )
                except Exception:
                    log.exception("notify_nick send failed")
