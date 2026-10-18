"""Résolveur DNS bidirectionnel via 8.8.8.8 avec cache TTL.

- forward : host → IP (record A/AAAA, on prend l'IPv4 en priorité)
- reverse : IP → host (PTR)

Cache in-memory partagé processus, TTL configurable. Les négatifs (NXDOMAIN,
timeout) sont aussi cachés pour éviter de re-hammer sur les mêmes hôtes cassés.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Optional

import dns.asyncresolver
import dns.exception
import dns.resolver
import dns.reversename

log = logging.getLogger("chatbot.dns")

# Résolveur explicite : bypass DNS locaux (Pi-hole/AdGuard trop strict côté user)
_RESOLVER = dns.asyncresolver.Resolver(configure=False)
_RESOLVER.nameservers = ["8.8.8.8", "1.1.1.1"]  # 1.1.1.1 en fallback
_RESOLVER.timeout = 3.0
_RESOLVER.lifetime = 5.0

# Cache : key → (value, expires_at)
_CACHE_TTL = 300.0            # succès : 5 min
_CACHE_TTL_NEGATIVE = 60.0    # échec : 1 min (on retente vite si le host a été réparé)
_cache: dict[str, tuple[Optional[str], float]] = {}


@dataclass(frozen=True)
class DnsResult:
    input: str
    resolved: Optional[str]
    was_cached: bool
    is_ip_input: bool


def _is_ip(candidate: str) -> bool:
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _cache_get(key: str) -> tuple[bool, Optional[str]]:
    """Retourne (was_cached, value). value peut être None si negative cache."""
    hit = _cache.get(key)
    if hit is None:
        return False, None
    value, expires_at = hit
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return False, None
    return True, value


def _cache_set(key: str, value: Optional[str]) -> None:
    ttl = _CACHE_TTL if value is not None else _CACHE_TTL_NEGATIVE
    _cache[key] = (value, time.monotonic() + ttl)


async def resolve_host_to_ip(host: str) -> DnsResult:
    """Résout un hostname → IPv4. Si `host` est déjà une IP, retourne tel quel."""
    host = host.strip().lower()
    if _is_ip(host):
        return DnsResult(input=host, resolved=host, was_cached=False, is_ip_input=True)

    key = f"fwd:{host}"
    was_cached, cached = _cache_get(key)
    if was_cached:
        return DnsResult(input=host, resolved=cached, was_cached=True, is_ip_input=False)

    try:
        # A record en priorité — la plupart des IRC hosts ont de l'IPv4
        answer = await _RESOLVER.resolve(host, "A")
        ip = str(answer[0].address) if len(answer) > 0 else None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        ip = None
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as e:
        log.warning("DNS forward timeout/failure on %s: %s", host, e)
        ip = None
    except Exception as e:  # noqa: BLE001
        log.warning("DNS forward unexpected error on %s: %s", host, e)
        ip = None

    _cache_set(key, ip)
    return DnsResult(input=host, resolved=ip, was_cached=False, is_ip_input=False)


async def resolve_ip_to_host(ip: str) -> DnsResult:
    """Résout une IP → hostname (PTR). Si `ip` n'est pas une IP valide, retourne None."""
    ip = ip.strip()
    if not _is_ip(ip):
        return DnsResult(input=ip, resolved=None, was_cached=False, is_ip_input=False)

    key = f"rev:{ip}"
    was_cached, cached = _cache_get(key)
    if was_cached:
        return DnsResult(input=ip, resolved=cached, was_cached=True, is_ip_input=True)

    try:
        rev_name = dns.reversename.from_address(ip)
        answer = await _RESOLVER.resolve(rev_name, "PTR")
        host = str(answer[0].target).rstrip(".") if len(answer) > 0 else None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        host = None
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as e:
        log.warning("DNS reverse timeout/failure on %s: %s", ip, e)
        host = None
    except Exception as e:  # noqa: BLE001
        log.warning("DNS reverse unexpected error on %s: %s", ip, e)
        host = None

    _cache_set(key, host)
    return DnsResult(input=ip, resolved=host, was_cached=False, is_ip_input=True)


async def resolve_both(host_or_ip: str) -> tuple[Optional[str], Optional[str]]:
    """Retourne (ip, host) peu importe ce qu'on lui donne en entrée.

    - Si input = IP  → PTR pour trouver host, IP telle quelle
    - Si input = host → A pour trouver IP, host tel quel
    - Si résolution échoue, la partie manquante = None

    Utilisé par tracelog et ban_engine pour avoir toujours les 2 identifiants.
    """
    host_or_ip = host_or_ip.strip()
    if _is_ip(host_or_ip):
        rev = await resolve_ip_to_host(host_or_ip)
        return host_or_ip, rev.resolved
    fwd = await resolve_host_to_ip(host_or_ip)
    return fwd.resolved, host_or_ip


def cache_stats() -> dict:
    """Debug endpoint : combien de trucs dans le cache, combien positifs/négatifs."""
    now = time.monotonic()
    live = [(k, v) for k, (v, exp) in _cache.items() if exp > now]
    return {
        "total": len(_cache),
        "live": len(live),
        "positive": sum(1 for _, v in live if v is not None),
        "negative": sum(1 for _, v in live if v is None),
    }


def cache_clear() -> None:
    """Utile pour tests / debug."""
    _cache.clear()
