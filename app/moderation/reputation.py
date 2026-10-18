"""Orchestrateur de vérification de réputation.

Consomme les 3 sources (Spamhaus, AbuseIPDB, IncidentHistory locale) et rend un
verdict qui alimente les décisions de modération.

Ordre d'évaluation :
    1. Whitelist → si match, verdict.suspect = False (short-circuit, jamais banni)
    2. Résolution IP (via dns_resolver si host, direct si IP)
    3. Spamhaus (lookup O(n) en mémoire, très rapide)
    4. IncidentHistory locale (query DB, count sur fenêtre glissante)
    5. AbuseIPDB (cache DB puis HTTP)

En Phase 2 : verdict = signal, aucune action de ban. Les incidents sont
enregistrés dans IncidentHistory pour usage par le seuil `local_incident_threshold`
au prochain check.
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..db import (
    ChannelPolicy,
    get_channel_policy,
    get_reputation_config,
    record_incident,
    count_incidents,
)
from . import abuseipdb, spamhaus
from .dns_resolver import resolve_both
from .whitelist import is_whitelisted

log = logging.getLogger("chatbot.reputation")


@dataclass
class ReputationVerdict:
    suspect: bool = False
    reasons: list[str] = field(default_factory=list)  # human-readable list
    sources: list[str] = field(default_factory=list)  # "spamhaus_drop" | "abuseipdb" | "local_incidents"
    score: int = 0                                     # 0-100 max des signaux quantifiables
    ip: Optional[str] = None
    resolved_host: Optional[str] = None
    whitelisted: bool = False
    whitelist_mask: Optional[str] = None
    should_ban: bool = False                           # True si un seuil est franchi (Phase 4 lira ça)
    ban_source: str = ""                               # source qui a poussé le should_ban
    spamhaus_match: Optional[dict] = None
    abuseipdb_result: Optional[dict] = None
    local_incident_count: int = 0

    def summary(self) -> str:
        """Résumé court pour log/notification."""
        if self.whitelisted:
            return f"WHITELISTED ({self.whitelist_mask})"
        if not self.suspect:
            return "clean"
        return f"SUSPECT [{', '.join(self.sources)}] score={self.score}"


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


async def check_user(
    nick: str,
    ident: str,
    host: str,
    channel: str,
    policy: Optional[ChannelPolicy] = None,
) -> ReputationVerdict:
    """Vérifie la réputation d'un user vu sur un chan.

    Ne bloque jamais l'IRC (à appeler via asyncio.create_task depuis le handler).
    Enregistre un IncidentHistory si suspect (alimente le seuil local pour le
    prochain check).
    """
    verdict = ReputationVerdict()

    if policy is None:
        policy = get_channel_policy(channel)
    if policy is None or not policy.reputation_check_enabled:
        return verdict

    # 1. Whitelist first — court-circuite tout
    user_mask = f"{nick}!{ident}@{host}"
    wl = is_whitelisted(user_mask, channel=channel)
    if wl.whitelisted:
        verdict.whitelisted = True
        verdict.whitelist_mask = wl.matched_mask
        return verdict

    # 2. Résolution IP / host
    if _is_ip(host):
        verdict.ip = host
        _, resolved = await resolve_both(host)
        verdict.resolved_host = resolved
    else:
        ip, _ = await resolve_both(host)
        verdict.ip = ip
        verdict.resolved_host = host

    # 3. Spamhaus (in-memory, très rapide)
    if verdict.ip:
        sh = spamhaus.check_ip(verdict.ip)
        if sh:
            verdict.spamhaus_match = sh
            verdict.suspect = True
            verdict.sources.append(sh["source"])
            verdict.reasons.append(f"Spamhaus {sh['source']} ({sh['cidr']} {sh['ref']})")
            verdict.score = max(verdict.score, 100)  # Spamhaus = confiance max
            verdict.should_ban = True
            verdict.ban_source = sh["source"]

    # 4. IncidentHistory locale
    cfg = get_reputation_config()
    lookback = max(1, cfg.incident_lookback_days)
    if verdict.ip or verdict.resolved_host or host:
        local = count_incidents(
            ip=verdict.ip,
            host=verdict.resolved_host or (host if not _is_ip(host) else None),
            since_days=lookback,
        )
        verdict.local_incident_count = local
        if local >= policy.local_incident_threshold:
            verdict.suspect = True
            if "local_incidents" not in verdict.sources:
                verdict.sources.append("local_incidents")
            verdict.reasons.append(
                f"{local} incidents locaux sur {lookback}j (seuil {policy.local_incident_threshold})"
            )
            if not verdict.should_ban:
                verdict.should_ban = True
                verdict.ban_source = "local_incidents"

    # 5. AbuseIPDB (cache first, HTTP si stale/miss)
    if verdict.ip:
        try:
            aip = await abuseipdb.check_ip(verdict.ip)
        except Exception as e:
            log.warning("AbuseIPDB exception pour %s: %s", verdict.ip, e)
            aip = None
        if aip:
            verdict.abuseipdb_result = aip
            score = aip["score"]
            verdict.score = max(verdict.score, score)
            if score >= policy.abuseipdb_threshold:
                verdict.suspect = True
                if "abuseipdb" not in verdict.sources:
                    verdict.sources.append("abuseipdb")
                verdict.reasons.append(
                    f"AbuseIPDB {score}/100 ({aip.get('total_reports', 0)} reports"
                    f", {aip.get('country_code', '')})"
                )
                if not verdict.should_ban:
                    verdict.should_ban = True
                    verdict.ban_source = "abuseipdb"

    # Log de l'incident si suspect (alimente le compteur local pour le prochain check)
    if verdict.suspect:
        detail = " | ".join(verdict.reasons)
        try:
            record_incident(
                ip=verdict.ip,
                host=verdict.resolved_host or host,
                nick=nick,
                channel=channel,
                incident_type="flag_reputation",
                severity=min(10, max(1, verdict.score // 10)),
                detail=detail[:1000],
            )
        except Exception as e:
            log.warning("record_incident failed for %s: %s", nick, e)
        log.info("REPUTATION %s@%s [%s] -> %s", nick, host, channel, verdict.summary())

    return verdict
