"""Query intent : le bot répond aux questions du user en tapant la DB modération.

Flux (2 tours LLM + dispatcher Python) :

    1. `extract_intent(message)` — mini-appel LLM au format JSON strict :
       { "intent": "lookup_ips_for_nick" | ..., "entity": "<str|null>" }
       Petits modèles (Gemma 2B) sont OK sur ce genre de JSON contraint si
       le prompt est carré et l'ancrage explicite.
    2. `dispatch_query(intent, entity)` — route vers un helper db.py, renvoie
       des `facts` = string compacte lisible.
    3. `humanize(facts, message, nick)` — 2e appel LLM avec la persona
       (system_prompt du bot) + les facts en contexte. Le modèle formule pour
       IRC. Aucun outillage requis côté LLM.

Si l'intent est "chat" ou si l'extraction foire (JSON invalide, exception),
`try_answer()` retourne None → l'appelant fallback sur `llm.generate_reply`.

Regex d'entités faciles (IP, host FQDN) : extraction Python directe pour ne
pas embêter le LLM avec des cas triviaux.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

from . import ip_intel
from .db import (
    get_llm_config,
    lookup_ips_for_nick,
    lookup_nicks_for_host,
    lookup_nicks_for_ip,
    seen_nick_summary,
    stats_bans,
    stats_flags,
    top_hosts,
)

log = logging.getLogger("chatbot.lookup")

# Regex helpers : IPv4 basique (bornes 0-255 non-strictes, on trust la DB)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# FQDN : au moins un point, TLD 2+ lettres. Évite le faux positif "3.5"
_HOST_RE = re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b")

_ALLOWED_INTENTS = {
    "lookup_ips_for_nick",
    "lookup_nicks_for_ip",
    "lookup_nicks_for_host",
    "seen_nick",
    "stats_bans",
    "stats_flags",
    "top_hosts",
    "describe_ip",     # dossier complet IP (annotations + externes + local)
    "chat",
}


_DEFAULT_EXTRACTION_PROMPT = """Tu es un extracteur d'intent pour un bot IRC de modération.
Réponds UNIQUEMENT par un objet JSON valide, aucun texte autour, aucun markdown.

Schéma exact:
{"intent": "<voir liste>", "entity": "<string ou null>"}

Intents autorisés:
- "lookup_ips_for_nick": user veut voir les IP/hosts qu'un nick a utilisé. entity = le nick.
- "lookup_nicks_for_ip": user veut voir les nicks qui ont utilisé une IP. entity = l'IP.
- "lookup_nicks_for_host": user veut voir les nicks depuis un host/domaine. entity = le host (peut contenir des wildcards SQL %).
- "seen_nick": user demande depuis quand / combien on a vu un nick. entity = le nick.
- "stats_bans": stats globales sur les bans actifs. entity = un channel #xxx ou null.
- "stats_flags": stats globales sur les incidents/flags récents. entity = un channel #xxx ou null.
- "top_hosts": top des hosts les plus actifs. entity = null.
- "describe_ip": user veut TOUT savoir sur une IP (annotation op, AbuseIPDB, Spamhaus, incidents, ban, nicks). entity = l'IP (ou host).
- "chat": tout le reste (conversation normale). entity = null.

Exemples:
Q: "quel IP a eu ikce ?" → {"intent": "lookup_ips_for_nick", "entity": "ikce"}
Q: "qui a utilisé 192.168.1.5 ?" → {"intent": "lookup_nicks_for_ip", "entity": "192.168.1.5"}
Q: "les nicks de videotron.ca" → {"intent": "lookup_nicks_for_host", "entity": "%.videotron.ca"}
Q: "depuis quand tu vois bob ?" → {"intent": "seen_nick", "entity": "bob"}
Q: "combien de bans sur #lab ?" → {"intent": "stats_bans", "entity": "#lab"}
Q: "combien de flags cette semaine ?" → {"intent": "stats_flags", "entity": null}
Q: "top hosts ?" → {"intent": "top_hosts", "entity": null}
Q: "c'est quoi 1.2.3.4 ?" → {"intent": "describe_ip", "entity": "1.2.3.4"}
Q: "que sais-tu sur 8.8.8.8 ?" → {"intent": "describe_ip", "entity": "8.8.8.8"}
Q: "salut, ça va ?" → {"intent": "chat", "entity": null}

Question à classifier:"""


async def try_answer(nick: str, message: str) -> Optional[str]:
    """Point d'entrée. Retourne le texte de réponse si intent DB détecté,
    None si conversation normale ou si l'extraction a foiré.

    Jamais raise : toute exception → None (fallback chat).
    """
    cfg = get_llm_config()
    if not getattr(cfg, "query_intent_enabled", True):
        return None

    try:
        intent, entity = await _extract_intent(message, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("intent extraction failed: %s", e)
        return None

    if intent == "chat" or intent not in _ALLOWED_INTENTS:
        return None

    # Entité pré-extraite en Python quand elle est triviale (le LLM peut se
    # tromper sur ce point avec les petits modèles)
    if intent in ("lookup_nicks_for_ip", "describe_ip"):
        m = _IP_RE.search(message)
        if m:
            entity = m.group(0)

    if not entity and intent not in ("top_hosts", "stats_bans", "stats_flags"):
        log.info("intent=%s sans entity → fallback chat", intent)
        return None

    facts = _dispatch_query(intent, entity)
    if not facts:
        return f"Rien trouvé pour ta requête ({intent})."

    try:
        return await _humanize(facts, message, nick, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("humanization failed, returning raw facts: %s", e)
        return facts  # dégradé mais utile


async def _extract_intent(message: str, cfg) -> tuple[str, Optional[str]]:
    """Appel LLM #1 : classification JSON stricte."""
    system_prompt = cfg.intent_extraction_prompt or _DEFAULT_EXTRACTION_PROMPT

    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key}"

    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        "max_tokens": 80,           # JSON minimal, 80 tokens largement
        "temperature": 0.1,          # déterministe pour classification
        "stream": False,
    }

    url = f"{cfg.llm_url.rstrip('/')}/api/chat/completions"
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    raw = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    parsed = _parse_intent_json(raw)
    if parsed is None:
        raise ValueError(f"intent JSON invalide: {raw!r}")
    intent = str(parsed.get("intent", "chat")).strip()
    entity = parsed.get("entity")
    if entity is not None:
        entity = str(entity).strip() or None
    return intent, entity


def _parse_intent_json(raw: str) -> Optional[dict]:
    """Tolère un peu de bruit autour du JSON (Gemma peut ajouter ```json ou du texte)."""
    raw = raw.strip()
    # Enlève les fences markdown
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    # Cherche le premier { ... } complet
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _dispatch_query(intent: str, entity: Optional[str]) -> str:
    """Route vers le helper DB et retourne des `facts` compactes en texte."""
    if intent == "lookup_ips_for_nick" and entity:
        rows = lookup_ips_for_nick(entity, limit=8)
        if not rows:
            return f"Aucune observation pour le nick {entity}."
        parts = [f"IP/host vus pour {entity} :"]
        for r in rows:
            parts.append(
                f"- {r['ip'] or '?'} ({r['host'] or '?'}), {r['count']}x, "
                f"dernier: {_fmt_ts(r['last_seen'])}"
            )
        return "\n".join(parts)

    if intent == "lookup_nicks_for_ip" and entity:
        rows = lookup_nicks_for_ip(entity, limit=8)
        if not rows:
            return f"Aucun nick vu depuis {entity}."
        parts = [f"Nicks vus depuis {entity} :"]
        for r in rows:
            parts.append(
                f"- {r['nick']} ({r['host'] or '?'}), {r['count']}x, "
                f"dernier: {_fmt_ts(r['last_seen'])}"
            )
        return "\n".join(parts)

    if intent == "lookup_nicks_for_host" and entity:
        # Si l'entity n'a pas de % on wrap en '%host%' pour être permissif
        pat = entity if "%" in entity else f"%{entity}%"
        rows = lookup_nicks_for_host(pat, limit=8)
        if not rows:
            return f"Aucun nick vu depuis un host matchant {entity}."
        parts = [f"Nicks depuis {entity} :"]
        for r in rows:
            parts.append(
                f"- {r['nick']} @ {r['host']} (ip={r['ip'] or '?'}), {r['count']}x, "
                f"dernier: {_fmt_ts(r['last_seen'])}"
            )
        return "\n".join(parts)

    if intent == "seen_nick" and entity:
        s = seen_nick_summary(entity)
        if not s:
            return f"Jamais vu {entity} dans le tracelog."
        chans = ", ".join(s["channels"]) or "aucun channel"
        return (
            f"{s['nick']}: première fois {_fmt_ts(s['first_seen'])}, "
            f"dernière {_fmt_ts(s['last_seen'])}, {s['events']} events, "
            f"chans: {chans}"
        )

    if intent == "stats_bans":
        s = stats_bans(channel=entity, active_only=True)
        scope = f"sur {entity}" if entity else "globalement"
        return f"Bans actifs {scope}: {s['total']}"

    if intent == "stats_flags":
        s = stats_flags(since_days=30, channel=entity)
        scope = f"sur {entity}" if entity else "globalement"
        return f"Flags/incidents 30 derniers jours {scope}: {s['total']}"

    if intent == "top_hosts":
        rows = top_hosts(limit=5, since_days=30)
        if not rows:
            return "Aucun host actif dans les 30 derniers jours."
        parts = ["Top hosts (30j) :"]
        for r in rows:
            parts.append(
                f"- {r['host']}: {r['events']} events, {r['distinct_nicks']} nicks distincts"
            )
        return "\n".join(parts)

    if intent == "describe_ip" and entity:
        summary = ip_intel.summarize_ip(entity)
        return ip_intel.format_for_llm(summary)

    return ""


async def _humanize(facts: str, original_message: str, nick: str, cfg) -> str:
    """Appel LLM #2 : reformule les facts pour IRC en gardant la persona."""
    persona = cfg.humanization_prompt or cfg.system_prompt or (
        "Tu es un chatter bot IRC concis. Réponds en 1 à 3 phrases."
    )
    user_content = (
        f"Question de {nick}: {original_message}\n\n"
        f"Données de la DB de modération:\n{facts}\n\n"
        "Reformule pour IRC en 1 à 3 phrases, factuel, sans inventer, "
        "sans coller les timestamps bruts si tu peux les résumer."
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
    text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    return " ".join(text.splitlines()).strip()


def _fmt_ts(ts) -> str:
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    return str(ts)
