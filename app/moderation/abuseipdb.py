"""Client AbuseIPDB avec cache DB.

Le tier gratuit AbuseIPDB accepte 1000 checks/jour. Le cache TTL (24h par
défaut) est essentiel pour ne pas cramer le quota sur des chans actifs.

Doc: https://docs.abuseipdb.com/#check-endpoint
"""
from __future__ import annotations

import ipaddress
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from ..db import (
    get_reputation_cache,
    get_reputation_config,
    upsert_reputation_cache,
)

log = logging.getLogger("chatbot.abuseipdb")

API_URL = "https://api.abuseipdb.com/api/v2/check"


def _is_public_ip(ip: str) -> bool:
    """Retourne True si l'IP est publique (routable), False sinon (RFC1918, LB, etc)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved
                or addr.is_link_local or addr.is_multicast)


async def check_ip(ip: str, force_refresh: bool = False) -> Optional[dict]:
    """Query AbuseIPDB pour une IP.

    Retourne None si :
    - IP privée / non routable
    - API désactivée ou clé manquante
    - Erreur non récupérable (loggée)

    Sinon retourne un dict:
        {
          score: int (0-100),
          total_reports: int,
          country_code: str,
          isp: str,
          domain: str,
          usage_type: str,
          from_cache: bool,
          checked_at: datetime,
        }
    """
    if not _is_public_ip(ip):
        return None

    cfg = get_reputation_config()
    if not cfg.abuseipdb_enabled or not cfg.abuseipdb_api_key:
        return None

    # Cache d'abord (sauf force_refresh)
    if not force_refresh:
        cached = get_reputation_cache(ip, source="abuseipdb")
        if cached is not None:
            age = (datetime.utcnow() - cached.checked_at).total_seconds()
            if age < cfg.abuseipdb_cache_ttl_sec:
                return {
                    "score": cached.score,
                    "total_reports": cached.total_reports,
                    "country_code": cached.country_code,
                    "isp": cached.isp,
                    "domain": cached.domain,
                    "usage_type": cached.usage_type,
                    "from_cache": True,
                    "checked_at": cached.checked_at,
                }

    # HTTP call
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                API_URL,
                params={
                    "ipAddress": ip,
                    "maxAgeInDays": cfg.abuseipdb_max_age_days,
                    "verbose": "",
                },
                headers={
                    "Key": cfg.abuseipdb_api_key,
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as e:
        log.warning("AbuseIPDB HTTP error for %s: %s", ip, e)
        return None

    if r.status_code == 429:
        log.warning("AbuseIPDB rate-limited (429) — check quota")
        return None
    if r.status_code == 401:
        log.error("AbuseIPDB 401 unauthorized — check API key")
        return None
    if r.status_code >= 400:
        log.warning("AbuseIPDB HTTP %d for %s: %s", r.status_code, ip, r.text[:200])
        return None

    try:
        payload = r.json()
        data = payload.get("data", {})
    except (ValueError, KeyError):
        log.warning("AbuseIPDB non-JSON response for %s", ip)
        return None

    score = int(data.get("abuseConfidenceScore", 0))
    total_reports = int(data.get("totalReports", 0))
    country_code = data.get("countryCode") or ""
    isp = data.get("isp") or ""
    domain = data.get("domain") or ""
    usage_type = data.get("usageType") or ""
    is_public = bool(data.get("isPublic", True))

    upsert_reputation_cache(
        ip=ip,
        source="abuseipdb",
        score=score,
        total_reports=total_reports,
        country_code=country_code,
        isp=isp,
        domain=domain,
        usage_type=usage_type,
        is_public=is_public,
        raw_response=json.dumps(data)[:4000],  # cap pour éviter les gros dumps
    )

    return {
        "score": score,
        "total_reports": total_reports,
        "country_code": country_code,
        "isp": isp,
        "domain": domain,
        "usage_type": usage_type,
        "from_cache": False,
        "checked_at": datetime.utcnow(),
    }
