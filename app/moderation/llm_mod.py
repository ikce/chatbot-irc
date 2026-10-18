"""Classifier LLM pour la modération (Phase 3).

Séparé de app/llm.py : le classifier peut viser un modèle différent (plus petit,
plus rapide, dédié) et utilise un prompt JSON-only avec parsing tolérant.

Route : OpenWebUI OpenAI-compat /api/chat/completions (même endpoint que le chat).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..db import get_llm_config, get_llm_moderation_config

log = logging.getLogger("chatbot.llm_mod")

# Verdicts qu'on accepte du LLM. Tout autre valeur est ramenée à "normal".
VALID_VERDICTS = {
    "normal", "toxic", "phone", "address", "minor_content",
    "credit_card", "social_id", "spam", "proxy_hint",
}

# Verdicts qui devraient déclencher un IncidentHistory (P3) et un ban en P4
SEVERE_VERDICTS = {
    "toxic", "phone", "address", "minor_content",
    "credit_card", "social_id", "spam",
}


@dataclass
class ModerationVerdict:
    verdict: str = "normal"
    confidence: float = 0.0
    rationale: str = ""
    model_used: str = ""
    raw_response: str = ""
    error: str = ""
    hints: list[str] = field(default_factory=list)  # signaux additionnels (patterns pré-match, etc.)

    @property
    def is_severe(self) -> bool:
        return self.verdict in SEVERE_VERDICTS


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_json_block(text: str) -> Optional[dict]:
    """Le LLM ajoute parfois du texte autour du JSON. On cherche le premier {...}."""
    if not text:
        return None
    # Essai direct
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    # Extraction du premier bloc JSON dans la sortie
    for m in _JSON_BLOCK_RE.finditer(text):
        chunk = m.group(0)
        try:
            return json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


async def classify_message(
    message: str,
    nick: str,
    channel: str,
    model_override: Optional[str] = None,
    pattern_hints: Optional[list[str]] = None,
) -> ModerationVerdict:
    """Envoie le message au LLM et retourne un ModerationVerdict.

    Ne throw jamais — en cas d'erreur (timeout, LLM cassé, JSON invalide) retourne
    un verdict "normal" avec `error` rempli. C'est au pipeline de décider quoi
    en faire (par exemple : si patterns ont matché mais LLM en erreur, garder
    le match pattern comme signal indépendant).
    """
    v = ModerationVerdict(hints=list(pattern_hints or []))

    llm_cfg = get_llm_config()
    mod_cfg = get_llm_moderation_config()

    if not llm_cfg.llm_url:
        v.error = "LLM URL non configurée"
        return v

    model = model_override or mod_cfg.default_model or llm_cfg.model
    v.model_used = model

    # Tronque le message pour maîtriser les tokens
    max_len = max(50, mod_cfg.max_message_len)
    msg_trunc = message[:max_len]

    hints_line = ""
    if pattern_hints:
        hints_line = f"\n[Indices regex détectés : {', '.join(pattern_hints)}]"

    system_prompt = mod_cfg.system_prompt.strip() or (
        "Tu es un classificateur de modération IRC. Retourne UNIQUEMENT un JSON "
        '{"verdict":"normal|toxic|phone|address|minor_content|credit_card|social_id|spam|proxy_hint",'
        '"confidence":0.0-1.0,"rationale":"court"}'
    )

    user_prompt = (
        f"Canal: {channel}\n"
        f"Nick: {nick}\n"
        f"Message: {msg_trunc!r}"
        f"{hints_line}\n\n"
        "Classe ce message. JSON uniquement."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # petit et déterministe : moderation != créativité
        "max_tokens": 200,
        "temperature": 0.1,
        "stream": False,
    }

    headers = {"Content-Type": "application/json"}
    if llm_cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {llm_cfg.llm_api_key}"

    url = f"{llm_cfg.llm_url.rstrip('/')}/api/chat/completions"
    timeout = httpx.Timeout(mod_cfg.timeout_sec, connect=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
            r = await c.post(url, json=payload)
            if r.status_code >= 400:
                # Capture le body d'erreur OpenWebUI pour diagnostiquer les 400
                # intermittents (message content qui trippe la validation).
                # Tronque à 500 chars — les erreurs OWU sont typiquement courtes.
                body_snippet = (r.text or "")[:500].replace("\n", " ")
                v.error = f"http:{r.status_code}:{body_snippet}"
                return v
            data = r.json()
    except httpx.TimeoutException:
        v.error = "timeout"
        return v
    except httpx.HTTPError as e:
        v.error = f"http:{e.__class__.__name__}:{e}"
        return v
    except Exception as e:
        v.error = f"unknown:{e.__class__.__name__}:{e}"
        return v

    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        v.error = "shape:no_choices"
        v.raw_response = json.dumps(data)[:1000]
        return v

    v.raw_response = text[:2000]

    parsed = _extract_json_block(text)
    if not parsed:
        v.error = "json_parse_failed"
        return v

    verdict_raw = str(parsed.get("verdict", "normal")).strip().lower()
    if verdict_raw not in VALID_VERDICTS:
        # Coerce vers normal, mais on garde note dans rationale
        v.rationale = f"invalid_verdict:{verdict_raw!r} → normal"
        verdict_raw = "normal"
    v.verdict = verdict_raw

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    v.confidence = max(0.0, min(1.0, conf))

    if "rationale" in parsed and not v.rationale:
        v.rationale = str(parsed["rationale"])[:300]

    return v
