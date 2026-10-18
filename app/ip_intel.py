"""Agrégation d'intelligence sur une IP.

Consolide toutes les sources locales pour donner au LLM (ou à un op via
commande custom) un dossier compact et lisible :

- **Annotations manuelles** (IpAnnotation) — la source de vérité de l'op
  ("cet IP est un proxy connu", "résidentiel de X, safe").
- **AbuseIPDB** (ReputationCache) — score externe si l'API key est set.
- **Spamhaus** (ReputationList) — CIDR DROP/EDROP match.
- **Incidents locaux** (IncidentHistory) — tout ce que la modération a flaggé.
- **Ban actif** (BannedEntity) — le mask est-il déjà banni ?
- **Tracelog** — combien de nicks distincts vus depuis cet IP ?

Pas d'appel réseau, pas d'écriture, purement read-only.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime
from typing import Optional

from .db import (
    active_ban_for_target,
    count_incidents_for_target,
    get_reputation_cache,
    list_reputation_entries,
    lookup_ip_annotations,
    lookup_nicks_for_ip,
    lookup_nicks_for_host,
)

log = logging.getLogger("chatbot.ip_intel")


def summarize_ip(ip_or_host: str, since_days: int = 90) -> dict:
    """Retourne un dict compact prêt pour affichage/humanization.

    Le string en entrée peut être une IP ou un host. On tente d'inférer.
    """
    target = (ip_or_host or "").strip()
    if not target:
        return _empty(target)

    ip = target if _is_ip(target) else None
    host = None if ip else target

    out: dict = {
        "target": target,
        "ip": ip,
        "host": host,
        "annotations": [],           # [{label, note, added_by, added_at}]
        "abuseipdb": None,           # {score, country, isp, usage_type, checked_at, total_reports}
        "spamhaus": [],              # [{source, cidr, ref, snapshot_at}]
        "incidents": {               # {total, by_type, last_ts, severity_max}
            "total": 0, "by_type": {}, "last_ts": None, "severity_max": 0,
        },
        "active_ban": None,          # {mask, source, reason, banned_at}
        "nicks_seen": [],            # [{nick, count, last_seen}]
        "verdict_hint": "unknown",   # good | bad | suspect | unknown
        "verdict_reasons": [],
    }

    # 1. Annotations manuelles (source de vérité op)
    for ann in lookup_ip_annotations(ip=ip, host=host):
        out["annotations"].append({
            "label": ann.label,
            "note": ann.note,
            "target": ann.target,
            "added_by": ann.added_by,
            "added_at": ann.added_at,
        })

    # 2. AbuseIPDB (seulement si target est une IP)
    if ip:
        cache = get_reputation_cache(ip, source="abuseipdb")
        if cache is not None:
            out["abuseipdb"] = {
                "score": cache.score,
                "country": cache.country_code,
                "isp": cache.isp,
                "domain": cache.domain,
                "usage_type": cache.usage_type,
                "total_reports": cache.total_reports,
                "checked_at": cache.checked_at,
            }

    # 3. Spamhaus DROP/EDROP (CIDR match)
    if ip:
        try:
            ip_obj = ipaddress.ip_address(ip)
            # On charge les entries récentes des deux sources et on filtre en Python
            for src in ("spamhaus_drop", "spamhaus_edrop"):
                for entry in list_reputation_entries(source=src):
                    try:
                        if ip_obj in ipaddress.ip_network(entry.cidr, strict=False):
                            out["spamhaus"].append({
                                "source": src,
                                "cidr": entry.cidr,
                                "ref": entry.ref,
                                "snapshot_at": entry.snapshot_at,
                            })
                            break  # une entrée par source suffit
                    except ValueError:
                        continue
        except ValueError:
            pass

    # 4. Incidents locaux
    out["incidents"] = count_incidents_for_target(ip=ip, host=host, since_days=since_days)

    # 5. Ban actif ?
    ban = active_ban_for_target(ip=ip, host=host)
    if ban is not None:
        out["active_ban"] = {
            "channel": ban.channel,
            "mask": ban.mask,
            "source": ban.source,
            "reason": ban.reason,
            "banned_at": ban.banned_at,
            "expires_at": ban.expires_at,
        }

    # 6. Nicks vus (tracelog)
    if ip:
        rows = lookup_nicks_for_ip(ip, limit=8)
    elif host:
        # `%host%` permissif pour ratisser
        pat = host if "%" in host else f"%{host}%"
        rows = lookup_nicks_for_host(pat, limit=8)
    else:
        rows = []
    out["nicks_seen"] = [
        {"nick": r["nick"], "count": r["count"], "last_seen": r["last_seen"]}
        for r in rows
    ]

    # 7. Verdict heuristique (l'op reste souverain — c'est un hint)
    out["verdict_hint"], out["verdict_reasons"] = _compute_verdict(out)
    return out


def format_for_llm(summary: dict) -> str:
    """Rend un texte compact prêt à passer au LLM en contexte user."""
    lines: list[str] = []
    tgt = summary.get("target") or "?"
    lines.append(f"Dossier IP/host: {tgt} (verdict heuristique: {summary.get('verdict_hint', 'unknown')})")

    reasons = summary.get("verdict_reasons") or []
    if reasons:
        lines.append("Raisons: " + " | ".join(reasons))

    anns = summary.get("annotations") or []
    if anns:
        lines.append("Annotations op:")
        for a in anns:
            note = (a.get("note") or "").replace("\n", " ")
            lines.append(
                f"  - [{a['label']}] {note or '(sans note)'} "
                f"(match: {a['target']}, par {a['added_by']})"
            )

    ab = summary.get("abuseipdb")
    if ab:
        lines.append(
            f"AbuseIPDB: score={ab['score']}/100, {ab['total_reports']} reports, "
            f"pays={ab['country']}, ISP={ab['isp']}, usage={ab['usage_type']}"
        )

    sh = summary.get("spamhaus") or []
    if sh:
        parts = [f"{e['source']}:{e['cidr']}" for e in sh]
        lines.append("Spamhaus: LISTÉ — " + ", ".join(parts))

    inc = summary.get("incidents") or {}
    if inc.get("total", 0) > 0:
        types = ", ".join(f"{k}={v}" for k, v in (inc.get("by_type") or {}).items())
        lines.append(
            f"Incidents locaux: {inc['total']} au total ({types}), "
            f"sévérité max: {inc.get('severity_max')}, "
            f"dernier: {_fmt_ts(inc.get('last_ts'))}"
        )
    else:
        lines.append("Incidents locaux: aucun.")

    ban = summary.get("active_ban")
    if ban:
        lines.append(
            f"BAN ACTIF sur {ban['channel']}: mask={ban['mask']}, "
            f"source={ban['source']}, raison={ban['reason']}"
        )

    nicks = summary.get("nicks_seen") or []
    if nicks:
        parts = [f"{n['nick']} ({n['count']}x)" for n in nicks[:5]]
        lines.append(f"Nicks vus ({len(nicks)}): " + ", ".join(parts))

    return "\n".join(lines)


def _compute_verdict(s: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    # 1. Annotation op est prioritaire absolue
    anns = s.get("annotations") or []
    labels = [a["label"] for a in anns]
    if "bad" in labels:
        reasons.append(f"annotation op = bad")
        return "bad", reasons
    if "good" in labels:
        reasons.append(f"annotation op = good")
        return "good", reasons
    if "suspect" in labels:
        reasons.append(f"annotation op = suspect")
        return "suspect", reasons

    # 2. Spamhaus listing = bad direct
    if s.get("spamhaus"):
        reasons.append("Spamhaus DROP/EDROP listed")
        return "bad", reasons

    # 3. AbuseIPDB score
    ab = s.get("abuseipdb") or {}
    score = ab.get("score", 0) or 0
    if score >= 75:
        reasons.append(f"AbuseIPDB score {score}/100")
        return "bad", reasons
    if score >= 25:
        reasons.append(f"AbuseIPDB score {score}/100")
        return "suspect", reasons

    # 4. Incidents locaux
    total = (s.get("incidents") or {}).get("total", 0)
    sev_max = (s.get("incidents") or {}).get("severity_max", 0)
    if total >= 5 or sev_max >= 7:
        reasons.append(f"{total} incidents locaux (sévérité max {sev_max})")
        return "suspect", reasons

    # 5. Ban actif
    if s.get("active_ban"):
        reasons.append("ban actif enregistré")
        return "bad", reasons

    return "unknown", reasons


def _is_ip(s: str) -> bool:
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


def _empty(target: str) -> dict:
    return {
        "target": target, "ip": None, "host": None,
        "annotations": [], "abuseipdb": None, "spamhaus": [],
        "incidents": {"total": 0, "by_type": {}, "last_ts": None, "severity_max": 0},
        "active_ban": None, "nicks_seen": [],
        "verdict_hint": "unknown", "verdict_reasons": ["empty input"],
    }
