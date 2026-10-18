"""Client wldeh/bible-api (jsdelivr CDN, gratuit, sans clé).

Endpoint racine :
  https://cdn.jsdelivr.net/gh/wldeh/bible-api/bibles

Format URL :
  /bibles/{version}/books/{book}/chapters/{chapter}/verses/{verse}.json
  /bibles/{version}/books/{book}/chapters/{chapter}.json
  /bibles/bibles.json                                        # liste des versions

Le user peut taper des références en français (Jean 3:16) ou en anglais
(John 3:16). Un mapping normalise vers les noms de livres attendus par le
CDN (lowercase, sans espace, format `1john`, `2corinthians`, etc.).

Cache LRU 30 min en mémoire — évite de bombarder jsdelivr pour un même
verset en boucle. TTL choisi court parce que le CDN est déjà edge-cached.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("chatbot.bible")

CDN_ROOT = "https://cdn.jsdelivr.net/gh/wldeh/bible-api/bibles"
_CACHE_TTL_SEC = 1800  # 30 min
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# =============================================================================
# Mapping livres FR/EN → codes wldeh (lowercase, sans espace)
# =============================================================================


_BOOK_ALIASES: dict[str, str] = {
    # Ancien Testament
    "genese": "genesis", "genèse": "genesis", "gen": "genesis", "genesis": "genesis",
    "exode": "exodus", "ex": "exodus", "exodus": "exodus",
    "levitique": "leviticus", "lévitique": "leviticus", "lev": "leviticus", "leviticus": "leviticus",
    "nombres": "numbers", "nomb": "numbers", "num": "numbers", "numbers": "numbers",
    "deuteronome": "deuteronomy", "deutéronome": "deuteronomy", "deut": "deuteronomy", "deuteronomy": "deuteronomy",
    "josue": "joshua", "josué": "joshua", "jos": "joshua", "joshua": "joshua",
    "juges": "judges", "jug": "judges", "judges": "judges",
    "ruth": "ruth",
    "1samuel": "1samuel", "1sam": "1samuel", "isamuel": "1samuel",
    "2samuel": "2samuel", "2sam": "2samuel", "iisamuel": "2samuel",
    "1rois": "1kings", "1roi": "1kings", "1kings": "1kings", "1ki": "1kings",
    "2rois": "2kings", "2roi": "2kings", "2kings": "2kings", "2ki": "2kings",
    "1chroniques": "1chronicles", "1chr": "1chronicles", "1ch": "1chronicles", "1chronicles": "1chronicles",
    "2chroniques": "2chronicles", "2chr": "2chronicles", "2ch": "2chronicles", "2chronicles": "2chronicles",
    "esdras": "ezra", "esd": "ezra", "ezra": "ezra",
    "nehemie": "nehemiah", "néhémie": "nehemiah", "neh": "nehemiah", "nehemiah": "nehemiah",
    "esther": "esther", "est": "esther",
    "job": "job",
    "psaumes": "psalms", "psaume": "psalms", "ps": "psalms", "psalms": "psalms", "psalm": "psalms",
    "proverbes": "proverbs", "prov": "proverbs", "pr": "proverbs", "proverbs": "proverbs",
    "ecclesiaste": "ecclesiastes", "ecclésiaste": "ecclesiastes", "ecc": "ecclesiastes", "eccl": "ecclesiastes",
    "ecclesiastes": "ecclesiastes",
    "cantique": "songofsolomon", "cantiques": "songofsolomon", "cant": "songofsolomon",
    "songofsolomon": "songofsolomon", "song": "songofsolomon", "sos": "songofsolomon",
    "esaie": "isaiah", "ésaïe": "isaiah", "isaie": "isaiah", "isa": "isaiah", "isaiah": "isaiah",
    "jeremie": "jeremiah", "jérémie": "jeremiah", "jer": "jeremiah", "jeremiah": "jeremiah",
    "lamentations": "lamentations", "lam": "lamentations",
    "ezechiel": "ezekiel", "ézéchiel": "ezekiel", "ez": "ezekiel", "ezekiel": "ezekiel",
    "daniel": "daniel", "dan": "daniel",
    "osee": "hosea", "osée": "hosea", "os": "hosea", "hosea": "hosea",
    "joel": "joel", "joël": "joel",
    "amos": "amos", "am": "amos",
    "abdias": "obadiah", "obadiah": "obadiah", "obd": "obadiah",
    "jonas": "jonah", "jonah": "jonah",
    "michee": "micah", "michée": "micah", "mi": "micah", "micah": "micah",
    "nahum": "nahum", "nah": "nahum",
    "habacuc": "habakkuk", "hab": "habakkuk", "habakkuk": "habakkuk",
    "sophonie": "zephaniah", "soph": "zephaniah", "zephaniah": "zephaniah",
    "aggee": "haggai", "aggée": "haggai", "hag": "haggai", "haggai": "haggai",
    "zacharie": "zechariah", "zac": "zechariah", "zech": "zechariah", "zechariah": "zechariah",
    "malachie": "malachi", "mal": "malachi", "malachi": "malachi",
    # Nouveau Testament
    "matthieu": "matthew", "matt": "matthew", "mt": "matthew", "matthew": "matthew",
    "marc": "mark", "mc": "mark", "mark": "mark", "mk": "mark",
    "luc": "luke", "lc": "luke", "luke": "luke", "lk": "luke",
    "jean": "john", "jn": "john", "john": "john",
    "actes": "acts", "act": "acts", "acts": "acts",
    "romains": "romans", "rom": "romans", "romans": "romans",
    "1corinthiens": "1corinthians", "1cor": "1corinthians", "1co": "1corinthians", "1corinthians": "1corinthians",
    "2corinthiens": "2corinthians", "2cor": "2corinthians", "2co": "2corinthians", "2corinthians": "2corinthians",
    "galates": "galatians", "gal": "galatians", "galatians": "galatians",
    "ephesiens": "ephesians", "éphésiens": "ephesians", "eph": "ephesians", "ephesians": "ephesians",
    "philippiens": "philippians", "phil": "philippians", "philippians": "philippians",
    "colossiens": "colossians", "col": "colossians", "colossians": "colossians",
    "1thessaloniciens": "1thessalonians", "1thess": "1thessalonians", "1th": "1thessalonians",
    "1thessalonians": "1thessalonians",
    "2thessaloniciens": "2thessalonians", "2thess": "2thessalonians", "2th": "2thessalonians",
    "2thessalonians": "2thessalonians",
    "1timothee": "1timothy", "1timothée": "1timothy", "1tim": "1timothy", "1ti": "1timothy",
    "1timothy": "1timothy",
    "2timothee": "2timothy", "2timothée": "2timothy", "2tim": "2timothy", "2ti": "2timothy",
    "2timothy": "2timothy",
    "tite": "titus", "titus": "titus",
    "philemon": "philemon", "philémon": "philemon", "phm": "philemon",
    "hebreux": "hebrews", "hébreux": "hebrews", "heb": "hebrews", "hebrews": "hebrews",
    "jacques": "james", "jac": "james", "jc": "james", "james": "james",
    "1pierre": "1peter", "1pier": "1peter", "1pi": "1peter", "1peter": "1peter", "1pt": "1peter",
    "2pierre": "2peter", "2pier": "2peter", "2pi": "2peter", "2peter": "2peter", "2pt": "2peter",
    "1jean": "1john", "1jn": "1john", "1john": "1john",
    "2jean": "2john", "2jn": "2john", "2john": "2john",
    "3jean": "3john", "3jn": "3john", "3john": "3john",
    "jude": "jude",
    "apocalypse": "revelation", "apo": "revelation", "ap": "revelation",
    "revelation": "revelation", "rev": "revelation",
}


# Nombre de chapitres par livre (pour tirer un random valide sans faire un
# appel réseau à chaque fois). Source : consensus canonique protestant.
BOOK_CHAPTERS: dict[str, int] = {
    "genesis": 50, "exodus": 40, "leviticus": 27, "numbers": 36, "deuteronomy": 34,
    "joshua": 24, "judges": 21, "ruth": 4,
    "1samuel": 31, "2samuel": 24, "1kings": 22, "2kings": 25,
    "1chronicles": 29, "2chronicles": 36, "ezra": 10, "nehemiah": 13, "esther": 10,
    "job": 42, "psalms": 150, "proverbs": 31, "ecclesiastes": 12, "songofsolomon": 8,
    "isaiah": 66, "jeremiah": 52, "lamentations": 5, "ezekiel": 48, "daniel": 12,
    "hosea": 14, "joel": 3, "amos": 9, "obadiah": 1, "jonah": 4, "micah": 7,
    "nahum": 3, "habakkuk": 3, "zephaniah": 3, "haggai": 2, "zechariah": 14, "malachi": 4,
    "matthew": 28, "mark": 16, "luke": 24, "john": 21, "acts": 28,
    "romans": 16, "1corinthians": 16, "2corinthians": 13, "galatians": 6,
    "ephesians": 6, "philippians": 4, "colossians": 4,
    "1thessalonians": 5, "2thessalonians": 3, "1timothy": 6, "2timothy": 4,
    "titus": 3, "philemon": 1, "hebrews": 13, "james": 5,
    "1peter": 5, "2peter": 3, "1john": 5, "2john": 1, "3john": 1, "jude": 1,
    "revelation": 22,
}


NT_BOOKS: list[str] = [
    "matthew", "mark", "luke", "john", "acts",
    "romans", "1corinthians", "2corinthians", "galatians",
    "ephesians", "philippians", "colossians",
    "1thessalonians", "2thessalonians", "1timothy", "2timothy",
    "titus", "philemon", "hebrews", "james",
    "1peter", "2peter", "1john", "2john", "3john", "jude", "revelation",
]


def normalize_book(name: str) -> Optional[str]:
    """Retourne le code wldeh d'un livre, ou None si inconnu."""
    if not name:
        return None
    # Retire les accents pour matcher les clés du dict (qui ont version avec ET sans accents)
    key = re.sub(r"\s+", "", name.strip().lower())
    return _BOOK_ALIASES.get(key)


# Regex de parse de référence : "Jean 3:16", "1 Cor 13:4-7", "Ps 23", "Prov 3:5-6"
# On accepte espaces ou non entre le numéro préfixe (1/2/3) et le nom.
_REF_RE = re.compile(
    r"^\s*"
    r"(?P<book>[123]?\s?[A-Za-zÀ-ÿ.]+)"
    r"\s+"
    r"(?P<chapter>\d{1,3})"
    r"(?:\s*[:\.]\s*(?P<vstart>\d{1,3})(?:\s*[-–]\s*(?P<vend>\d{1,3}))?)?"
    r"\s*$"
)


@dataclass(frozen=True)
class BibleRef:
    book: str          # code wldeh (john, 1corinthians, ...)
    chapter: int
    verse_start: Optional[int] = None  # None = chapitre complet
    verse_end: Optional[int] = None    # None = verset unique (si verse_start), ou fin chapitre

    def human(self) -> str:
        """Retourne une notation lisible : 'john 3:16' ou 'psalms 23' ou 'proverbs 3:5-6'."""
        if self.verse_start is None:
            return f"{self.book} {self.chapter}"
        if self.verse_end is None or self.verse_end == self.verse_start:
            return f"{self.book} {self.chapter}:{self.verse_start}"
        return f"{self.book} {self.chapter}:{self.verse_start}-{self.verse_end}"


def parse_reference(text: str) -> Optional[BibleRef]:
    """Parse 'Jean 3:16', '1 Cor 13:4-7', 'Ps 23', etc. Retourne None si invalide."""
    if not text:
        return None
    m = _REF_RE.match(text)
    if not m:
        return None
    book_code = normalize_book(m.group("book"))
    if not book_code:
        return None
    chapter = int(m.group("chapter"))
    max_chap = BOOK_CHAPTERS.get(book_code)
    if not max_chap or chapter < 1 or chapter > max_chap:
        return None
    vstart = m.group("vstart")
    vend = m.group("vend")
    vs = int(vstart) if vstart else None
    ve = int(vend) if vend else None
    if vs is not None and ve is not None and ve < vs:
        vs, ve = ve, vs
    return BibleRef(book=book_code, chapter=chapter, verse_start=vs, verse_end=ve)


# =============================================================================
# Cache en mémoire (TTL 30 min)
# =============================================================================


@dataclass
class _CacheEntry:
    ts: float
    value: Optional[str]  # None = miss confirmé (verset 404)


_cache: dict[str, _CacheEntry] = {}
_cache_lock = asyncio.Lock()


def _cache_get(key: str) -> tuple[bool, Optional[str]]:
    """Retourne (hit, value). value=None si miss confirmé négatif."""
    ent = _cache.get(key)
    if ent is None:
        return False, None
    if time.monotonic() - ent.ts > _CACHE_TTL_SEC:
        return False, None
    return True, ent.value


def _cache_put(key: str, value: Optional[str]) -> None:
    _cache[key] = _CacheEntry(ts=time.monotonic(), value=value)
    # Cap la taille pour éviter croissance illimitée
    if len(_cache) > 5000:
        # Vire le plus vieux (approx O(N), tolérable à cette échelle)
        oldest = min(_cache.items(), key=lambda kv: kv[1].ts)[0]
        _cache.pop(oldest, None)


# =============================================================================
# Fetch d'un verset / plage / chapitre
# =============================================================================


@dataclass
class FetchedPassage:
    version: str
    ref: BibleRef
    text: str = ""
    ok: bool = False
    error: str = ""

    def render(self) -> str:
        """Ligne IRC prête : '[fr-lsg] Jean 3:16 — texte...'"""
        tag = f"[{self.version}] {self.ref.human()}"
        if not self.ok:
            return f"{tag} — (indisponible: {self.error or 'no data'})"
        return f"{tag} — {self.text}"


async def _fetch_verse(client: httpx.AsyncClient, version: str, ref: BibleRef,
                       verse_num: int) -> Optional[str]:
    """Fetch UN verset. Retourne le texte ou None. Utilise le cache."""
    key = f"{version}|{ref.book}|{ref.chapter}|{verse_num}"
    hit, cached = _cache_get(key)
    if hit:
        return cached
    url = f"{CDN_ROOT}/{version}/books/{ref.book}/chapters/{ref.chapter}/verses/{verse_num}.json"
    try:
        r = await client.get(url)
        if r.status_code == 404:
            _cache_put(key, None)
            return None
        r.raise_for_status()
        data = r.json()
        text = (data.get("text") or "").strip()
        _cache_put(key, text or None)
        return text or None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("fetch_verse %s %s:%d failed: %s", version, ref.human(), verse_num, e)
        return None


async def fetch_passage(version: str, ref: BibleRef) -> FetchedPassage:
    """Fetch le passage complet (peut être 1 verset, une plage, ou un chapitre entier)."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        if ref.verse_start is None:
            # Chapitre entier — trop long pour IRC, on limite aux 5 premiers versets
            verses = []
            for v in range(1, 6):
                text = await _fetch_verse(client, version, ref, v)
                if text is None:
                    break
                verses.append(f"{v}. {text}")
            if not verses:
                return FetchedPassage(version=version, ref=ref, error="chapitre vide/404")
            joined = " ".join(verses)
            if len(verses) == 5:
                joined += " [...]"
            return FetchedPassage(version=version, ref=ref, text=joined, ok=True)

        # Plage ou verset unique
        end = ref.verse_end or ref.verse_start
        # Cap la plage à 10 versets max (protection flood + latence)
        if end - ref.verse_start > 9:
            end = ref.verse_start + 9
        parts: list[str] = []
        for v in range(ref.verse_start, end + 1):
            text = await _fetch_verse(client, version, ref, v)
            if text is None:
                if v == ref.verse_start:
                    return FetchedPassage(
                        version=version, ref=ref,
                        error=f"verset {v} introuvable"
                    )
                break
            parts.append(text if v == ref.verse_start else f"({v}) {text}")
        return FetchedPassage(
            version=version, ref=ref, text=" ".join(parts), ok=True,
        )


async def fetch_all_versions(versions: list[str], ref: BibleRef) -> list[FetchedPassage]:
    """Fetch le passage dans N versions en parallèle. Ordre préservé."""
    if not versions:
        return []
    tasks = [fetch_passage(v, ref) for v in versions]
    return list(await asyncio.gather(*tasks))


# =============================================================================
# Random verse / VOTD
# =============================================================================


async def random_reference(books_filter: Optional[list[str]] = None) -> BibleRef:
    """Tire une référence random : livre → chapitre → verset (1er).

    Le verset choisi = 1 (on ne connaît pas le nb de versets par chapitre sans
    fetch, alors on prend le premier — quasi-toujours valide). Si le fetch
    échoue plus tard, le broadcaster loggue et re-tirera au tour suivant.
    """
    books = books_filter if books_filter else list(BOOK_CHAPTERS.keys())
    # Filtre par ceux qu'on connaît
    books = [b for b in books if b in BOOK_CHAPTERS]
    if not books:
        books = list(BOOK_CHAPTERS.keys())
    book = random.choice(books)
    chapter = random.randint(1, BOOK_CHAPTERS[book])
    return BibleRef(book=book, chapter=chapter, verse_start=1, verse_end=1)


async def fetch_votd(versions: list[str]) -> tuple[BibleRef, list[FetchedPassage]]:
    """Verset du jour — on tire pseudo-random déterministe basé sur le jour UTC.

    Aucun endpoint VOTD officiel côté wldeh, on synthétise : hash(date) → seed
    → même verset partout dans le monde le même jour.
    """
    import hashlib
    from datetime import datetime as _dt
    today = _dt.utcnow().strftime("%Y-%m-%d")
    seed = int(hashlib.md5(today.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    # Bias vers NT pour le VOTD (plus grand-public que Lévitique)
    book = rng.choice(NT_BOOKS)
    chapter = rng.randint(1, BOOK_CHAPTERS[book])
    verse = rng.randint(1, 3)  # 1er/2e/3e verset — quasi-toujours existant
    ref = BibleRef(book=book, chapter=chapter, verse_start=verse, verse_end=verse)
    passages = await fetch_all_versions(versions, ref)
    return ref, passages


# =============================================================================
# Utils
# =============================================================================


def parse_versions_json(raw: str) -> list[str]:
    """Parse une string JSON list; retourne liste vide si invalide."""
    import json
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()]
    except (ValueError, TypeError):
        pass
    return []
