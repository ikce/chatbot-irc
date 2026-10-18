"""Génération auto de patterns depuis un flood pasté par l'admin.

Cas d'usage : ton ami se fait spam-flood/diffamer. Tu colles le message complet
dans /moderation/patterns → l'analyseur extrait les éléments distinctifs
(URLs, emails, numéros, handles, n-grams répétés), propose un regex + kind +
sévérité pour chacun, marque ceux déjà couverts par les patterns actifs, et te
laisse cocher ceux à ajouter en batch.

Ne remplace jamais un humain : suggère, marque le contexte, laisse trancher.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from ..db import PatternRule


# Kinds valides côté DB (voir _default_pattern_rules)
KNOWN_KINDS = ("phone", "address", "minor_content", "credit_card", "social_id", "other")


@dataclass
class Suggestion:
    source_snippet: str                # texte source détecté
    proposed_regex: str                # regex prêt à insérer
    proposed_name: str                 # slug kebab_case unique-ish
    proposed_kind: str                 # phone|address|...|other
    proposed_severity: int             # 1-10
    reason: str                        # phrase courte expliquant la détection
    covered_by: Optional[str] = None   # nom du pattern actif qui matche déjà (si applicable)
    match_count: int = 1               # occurrences dans le texte source


def _slugify(s: str, maxlen: int = 40) -> str:
    """Normalise en ascii kebab_case sans caractères spéciaux."""
    if not s:
        return "x"
    # Normalise accents → ascii
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s.lower())
    s = s.strip("_")
    return (s or "x")[:maxlen]


def _existing_names(patterns: list[PatternRule]) -> set[str]:
    return {p.name for p in patterns}


def _unique_name(base: str, taken: set[str]) -> str:
    """Retourne base ou base_2/3/... jusqu'à trouver un nom libre."""
    if base not in taken:
        taken.add(base)
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    n = f"{base}_{i}"
    taken.add(n)
    return n


def _compiled_actives(patterns: list[PatternRule]) -> list[tuple[re.Pattern, str]]:
    """Compile les patterns actifs pour le check de couverture. Skip les invalides."""
    out: list[tuple[re.Pattern, str]] = []
    for p in patterns:
        if not p.active:
            continue
        try:
            out.append((re.compile(p.regex, re.IGNORECASE), p.name))
        except re.error:
            continue
    return out


def _covered_by(snippet: str, compiled: list[tuple[re.Pattern, str]]) -> Optional[str]:
    """Retourne le nom du 1er pattern actif qui matche le snippet, ou None."""
    for rx, name in compiled:
        try:
            if rx.search(snippet):
                return name
        except Exception:
            continue
    return None


# ---------- extractors -------------------------------------------------------


_URL_RE = re.compile(
    r"""(?xi)
    (?:https?://[^\s<>"']+                          # explicite http(s)
     | (?<![\w@.])                                   # pas précédé par email-ish
       [a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?         # domaine
       (?:\.[a-z]{2,20})+                            # TLD(s)
       (?:/[^\s<>"']*)?                              # path optionnel
    )
    """,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)

_HANDLE_RE = re.compile(r"(?<![\w])@[A-Za-z0-9_.-]{2,32}")

# Séquence de 9-15 chiffres avec séparateurs (téléphone possible)
_PHONE_LIKE_RE = re.compile(
    r"(?<!\d)(\+?\d[\d\s.\-()]{7,20}\d)(?!\d)"
)

# Adresse civique : num + type de voie
_ADDRESS_LIKE_RE = re.compile(
    r"\b\d{1,5}[,\s]+(?:rue|avenue|av\.?|boulevard|boul\.?|blvd\.?|chemin|ch\.?|"
    r"place|impasse|allée|route|rte\.?|street|st\.?|road|rd\.?)\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' \-]{2,50}",
    re.IGNORECASE,
)

# Séquence 13-19 chiffres compat carte (déjà covered mais on essaie)
_CC_LIKE_RE = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")

# Codes postaux
_POSTAL_CA_RE = re.compile(r"\b[A-CEGHJ-NPRSTVXY]\d[A-CEGHJ-NPRSTV-Z][ \-]?\d[A-CEGHJ-NPRSTV-Z]\d\b")

# Stopwords FR/EN pour filtrer les n-grams (mots trop courants)
_STOP = frozenset("""
le la les un une des de du au aux à en dans sur pour par avec sans sous
et ou mais donc car ni or que qui quoi dont où
je tu il elle on nous vous ils elles me te se lui leur
ce cet cette ces mon ton son ma ta sa mes tes ses notre votre nos vos
est sont était étaient sera seront été suis es sommes êtes ont a ai avons avez
pas plus moins très trop bien mal fait faire dit dire
the a an of to in on at for by with from up out so if
is are was were be been being have has had do does did will would could
i you he she it we they me him her them my your his our their
this that these those what which who whom whose where when why how
""".split())


def _tokenize(text: str) -> list[str]:
    """Extrait les tokens alphanumériques du texte, lowercased."""
    return re.findall(r"[a-zA-ZÀ-ÿ][\w'-]{2,}", text.lower())


def _ngrams_repeated(tokens: list[str], n: int, min_count: int = 2) -> list[tuple[str, int]]:
    """Retourne les n-grams apparaissant ≥ min_count fois, triés par count desc."""
    counts: dict[str, int] = {}
    for i in range(len(tokens) - n + 1):
        gram_toks = tokens[i:i + n]
        # Skip si le gram commence/finit par un stopword
        if gram_toks[0] in _STOP or gram_toks[-1] in _STOP:
            continue
        # Skip si + de moitié du gram est stopword
        if sum(1 for t in gram_toks if t in _STOP) > n // 2:
            continue
        gram = " ".join(gram_toks)
        if len(gram) < 6:
            continue
        counts[gram] = counts.get(gram, 0) + 1
    return sorted(
        [(g, c) for g, c in counts.items() if c >= min_count],
        key=lambda x: (-x[1], -len(x[0])),
    )


def _phrase_to_regex(phrase: str) -> str:
    """Convertit 'foo bar baz' en r'\\bfoo\\s+bar\\s+baz\\b' (case-insensitive
    laissé au moteur ; on ne wrappe pas dans (?i) pour rester compatible avec
    le compilateur global qui passe déjà re.IGNORECASE)."""
    parts = [re.escape(w) for w in phrase.split()]
    return r"\b" + r"\s+".join(parts) + r"\b"


def _url_to_regex(url: str) -> str:
    """Regex qui matche la variante avec ou sans http(s) et avec trailing path."""
    # Enlève trailing punct et scheme
    u = re.sub(r"^https?://", "", url).rstrip(".,;:!?)")
    escaped = re.escape(u)
    return r"\b" + escaped + r"\b"


def _phone_to_regex(phone_snippet: str) -> str:
    """Génère un regex qui matche le num avec séparateurs variables."""
    digits = re.sub(r"\D", "", phone_snippet)
    if len(digits) < 9:
        return re.escape(phone_snippet)
    # Chaque chiffre est obligatoire, séparateurs optionnels entre eux
    parts = [re.escape(d) for d in digits]
    return r"(?<!\d)" + r"[\s.\-()]*".join(parts) + r"(?!\d)"


# ---------- pipeline ---------------------------------------------------------


def suggest_patterns(text: str, existing: list[PatternRule]) -> list[Suggestion]:
    """Analyse `text` et retourne une liste de suggestions triées par utilité.

    Ordre logique :
        1. URLs
        2. Emails
        3. Handles / @mentions
        4. Numéros téléphone-like
        5. Adresses civiques
        6. Cartes / codes postaux
        7. N-grams répétés (5-2 mots)
    """
    if not text or not text.strip():
        return []

    compiled = _compiled_actives(existing)
    taken_names = _existing_names(existing)
    out: list[Suggestion] = []
    seen_regex: set[str] = set()

    def _emit(s: Suggestion) -> None:
        if s.proposed_regex in seen_regex:
            return
        seen_regex.add(s.proposed_regex)
        s.proposed_name = _unique_name(s.proposed_name, taken_names)
        s.covered_by = _covered_by(s.source_snippet, compiled)
        out.append(s)

    # 1. URLs
    url_seen: dict[str, int] = {}
    for m in _URL_RE.finditer(text):
        u = m.group(0).strip().rstrip(".,;:!?)")
        if len(u) < 5 or "@" in u:
            continue
        url_seen[u.lower()] = url_seen.get(u.lower(), 0) + 1
    for u, cnt in url_seen.items():
        _emit(Suggestion(
            source_snippet=u,
            proposed_regex=_url_to_regex(u),
            proposed_name=f"url_{_slugify(u, 30)}",
            proposed_kind="other",
            proposed_severity=7,
            reason=f"URL/domaine ({cnt}× dans le flood)",
            match_count=cnt,
        ))

    # 2. Emails
    email_seen: dict[str, int] = {}
    for m in _EMAIL_RE.finditer(text):
        email_seen[m.group(0).lower()] = email_seen.get(m.group(0).lower(), 0) + 1
    for e, cnt in email_seen.items():
        _emit(Suggestion(
            source_snippet=e,
            proposed_regex=r"\b" + re.escape(e) + r"\b",
            proposed_name=f"email_{_slugify(e, 30)}",
            proposed_kind="other",
            proposed_severity=6,
            reason=f"Email ({cnt}×)",
            match_count=cnt,
        ))

    # 3. Handles / @mentions
    handle_seen: dict[str, int] = {}
    for m in _HANDLE_RE.finditer(text):
        handle_seen[m.group(0)] = handle_seen.get(m.group(0), 0) + 1
    for h, cnt in handle_seen.items():
        _emit(Suggestion(
            source_snippet=h,
            proposed_regex=re.escape(h) + r"\b",
            proposed_name=f"handle_{_slugify(h, 30)}",
            proposed_kind="other",
            proposed_severity=5,
            reason=f"Handle/mention ({cnt}×)",
            match_count=cnt,
        ))

    # 4. Numéros téléphone-like
    phone_seen: dict[str, int] = {}
    for m in _PHONE_LIKE_RE.finditer(text):
        snippet = m.group(1).strip()
        digits = re.sub(r"\D", "", snippet)
        if len(digits) < 9 or len(digits) > 15:
            continue
        phone_seen[digits] = phone_seen.get(digits, 0) + 1
        # On stocke la 1re orthographe rencontrée
    for digits, cnt in phone_seen.items():
        _emit(Suggestion(
            source_snippet=digits,
            proposed_regex=_phone_to_regex(digits),
            proposed_name=f"phone_{digits[:12]}",
            proposed_kind="phone",
            proposed_severity=7,
            reason=f"Séquence {len(digits)} chiffres ({cnt}×)",
            match_count=cnt,
        ))

    # 5. Adresses civiques
    addr_seen: dict[str, int] = {}
    for m in _ADDRESS_LIKE_RE.finditer(text):
        s = m.group(0).strip()
        addr_seen[s] = addr_seen.get(s, 0) + 1
    for s, cnt in addr_seen.items():
        _emit(Suggestion(
            source_snippet=s,
            proposed_regex=r"\b" + re.escape(s) + r"\b",
            proposed_name=f"addr_{_slugify(s, 30)}",
            proposed_kind="address",
            proposed_severity=7,
            reason=f"Adresse civique ({cnt}×)",
            match_count=cnt,
        ))

    # 6. Codes postaux CA
    for m in _POSTAL_CA_RE.finditer(text):
        s = m.group(0).strip()
        _emit(Suggestion(
            source_snippet=s,
            proposed_regex=r"\b" + re.escape(s) + r"\b",
            proposed_name=f"postal_{_slugify(s, 20)}",
            proposed_kind="address",
            proposed_severity=6,
            reason="Code postal CA",
        ))

    # 7. Cartes de crédit (heuristique 13-19 chiffres)
    for m in _CC_LIKE_RE.finditer(text):
        s = m.group(0).strip()
        digits = re.sub(r"\D", "", s)
        if len(digits) < 13 or len(digits) > 19:
            continue
        _emit(Suggestion(
            source_snippet=digits,
            proposed_regex=r"(?<!\d)" + r"[ -]*".join(re.escape(d) for d in digits) + r"(?!\d)",
            proposed_name=f"cc_{digits[:8]}",
            proposed_kind="credit_card",
            proposed_severity=9,
            reason=f"Séquence CC {len(digits)} chiffres",
        ))

    # 8. N-grams répétés (spam signature)
    tokens = _tokenize(text)
    for n in (5, 4, 3, 2):
        for phrase, count in _ngrams_repeated(tokens, n, min_count=2):
            # Skip si tokens trop courts total
            if len(phrase.replace(" ", "")) < 8:
                continue
            _emit(Suggestion(
                source_snippet=f"{phrase}",
                proposed_regex=_phrase_to_regex(phrase),
                proposed_name=f"phrase_{_slugify(phrase, 30)}",
                proposed_kind="other",
                proposed_severity=6 if n >= 3 else 5,
                reason=f"N-gram {n} mots répété {count}×",
                match_count=count,
            ))

    # Tri final : non-covered d'abord, puis par severity desc, puis par count desc
    out.sort(key=lambda s: (
        s.covered_by is not None,          # 0=nouveau, 1=covered
        -s.proposed_severity,
        -s.match_count,
        s.proposed_name,
    ))
    return out


def summarize(suggestions: list[Suggestion]) -> dict:
    """Compteurs pour l'UI (KPI en tête)."""
    total = len(suggestions)
    covered = sum(1 for s in suggestions if s.covered_by)
    new_ = total - covered
    by_kind: dict[str, int] = {}
    for s in suggestions:
        by_kind[s.proposed_kind] = by_kind.get(s.proposed_kind, 0) + 1
    return {"total": total, "covered": covered, "new": new_, "by_kind": by_kind}
