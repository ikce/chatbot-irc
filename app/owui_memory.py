"""[LEGACY] Mémoire long terme par nick, stockée dans les Memories d'OpenWebUI.

Remplacé par `app/memory.py` (SQLite du bot, identité par compte/host). Ce
module ne sert plus qu'à `!mem import` (reprise des anciens faits) via
`list_for()`. Le texte ci-dessous décrit l'ancien fonctionnement.

Commandes (en PV avec le bot uniquement — la liste des faits est privée) :

    !mem add <fait>       retient un fait sur toi
    !mem list             liste tes faits (numérotés)
    !mem forget <n>       oublie le fait n° n (numéro de !mem list)
    !mem forget all       oublie tout

Injection : en PV, les faits du nick sont ajoutés au system prompt de
`generate_reply` (voir `recall()`). Jamais en channel : un fait privé ne doit
pas fuiter dans une réponse publique.

Design :
    - API OpenWebUI : /api/v1/memories/{,add,query,<id>} avec le même Bearer
      que le chat. Les Memories OWU appartiennent au COMPTE de la clé API →
      tous les nicks partagent le même stock. On isole par préfixe
      `[irc:<nick>] ` dans le contenu, et on filtre côté bot.
    - recall() : si le nick a peu de faits (≤ RECALL_ALL_MAX), on les injecte
      tous (1 GET, déterministe). Au-delà, recherche sémantique via /query
      avec un k large, puis filtre par préfixe.
    - Best-effort : toute erreur réseau/API dans recall() → [] + log, le chat
      continue sans mémoire.
    - ACL : allow_llm_chat ou is_admin (la mémoire ne sert qu'au chat LLM).
"""
from __future__ import annotations

import logging

import httpx

from .db import get_irc_config, get_llm_config, user_acl_check
from .event_bus import Event, bus

log = logging.getLogger("chatbot.owui_memory")

MAX_FACT_LEN = 300
MAX_FACTS_PER_NICK = 50
RECALL_ALL_MAX = 15
RECALL_K = 8

_FALLBACK_PREFIXES = ("!", ".", "+", "~")


# --------------------------------------------------------------------------
# API OpenWebUI
# --------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    cfg = get_llm_config()
    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key}"
    return httpx.AsyncClient(
        base_url=cfg.llm_url.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )


def _tag(nick: str) -> str:
    return f"[irc:{nick.lower()}] "


def _raise_for(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400:
        snippet = (r.text or "")[:300].replace("\n", " ")
        raise RuntimeError(f"OWU memories {what} → HTTP {r.status_code}: {snippet}")


async def _list_all(c: httpx.AsyncClient) -> list[dict]:
    r = await c.get("/api/v1/memories/")
    _raise_for(r, "list")
    return r.json() or []


async def list_for(nick: str) -> list[dict]:
    """Faits du nick, plus ancien d'abord. Chaque dict : id, content (sans tag)."""
    tag = _tag(nick)
    async with _client() as c:
        mems = await _list_all(c)
    out = [
        {"id": m["id"], "content": m["content"][len(tag):],
         "created_at": m.get("created_at") or 0}
        for m in mems if (m.get("content") or "").startswith(tag)
    ]
    out.sort(key=lambda m: m["created_at"])
    return out


async def add(nick: str, fact: str) -> None:
    async with _client() as c:
        r = await c.post("/api/v1/memories/add", json={"content": _tag(nick) + fact})
        _raise_for(r, "add")


async def delete(memory_id: str) -> None:
    async with _client() as c:
        r = await c.delete(f"/api/v1/memories/{memory_id}")
        _raise_for(r, "delete")


async def recall(nick: str, query: str) -> list[str]:
    """Faits pertinents pour ce message. Best-effort : [] en cas d'erreur."""
    tag = _tag(nick)
    try:
        mine = await list_for(nick)
        if len(mine) <= RECALL_ALL_MAX:
            return [m["content"] for m in mine]

        async with _client() as c:
            r = await c.post(
                "/api/v1/memories/query",
                json={"content": f"{tag}{query}", "k": RECALL_K * 4},
            )
            _raise_for(r, "query")
            data = r.json() or {}
        docs = (data.get("documents") or [[]])[0] or []
        hits = [d[len(tag):] for d in docs if isinstance(d, str) and d.startswith(tag)]
        return hits[:RECALL_K]
    except Exception as e:  # noqa: BLE001
        log.warning("recall failed for %s: %s", nick, e)
        return []


# --------------------------------------------------------------------------
# Commandes IRC
# --------------------------------------------------------------------------

def _prefix() -> str:
    try:
        return get_irc_config().command_prefix or "!"
    except Exception:  # noqa: BLE001
        return "!"


def _match_mem_command(stripped: str, prefix: str) -> str | None:
    """'' si `!mem` seul, le reste si `!mem ...`, None si pas une commande mem."""
    for p in (prefix, *_FALLBACK_PREFIXES):
        head = p + "mem"
        if stripped == head:
            return ""
        if stripped.startswith(head + " ") or stripped.startswith(head + "\t"):
            return stripped[len(head):].strip()
    return None


async def try_handle(client, source: str, ident: str, host: str,
                     channel: str, message: str) -> bool:
    """Traite `!mem ...`. True = consommé."""
    stripped = message.strip()
    prefix = _prefix()
    rest = _match_mem_command(stripped, prefix)
    if rest is None:
        return False

    is_pm = channel == source
    if not is_pm:
        await client.message(
            channel, f"{source}: {prefix}mem s'utilise en PV avec moi (tes faits sont privés).",
        )
        return True

    if not user_acl_check(source, ident, host, "is_admin") \
            and not user_acl_check(source, ident, host, "allow_llm_chat"):
        await client.message(
            source,
            "❌ Il faut allow_llm_chat pour utiliser la mémoire. Demande à un admin (/users).",
        )
        return True

    sub, _, arg = rest.partition(" ")
    sub = sub.lower() or "help"
    arg = arg.strip()

    try:
        if sub == "add":
            await _cmd_add(client, source, arg, prefix)
        elif sub in ("list", "ls"):
            await _cmd_list(client, source, prefix)
        elif sub in ("forget", "del", "rm"):
            await _cmd_forget(client, source, arg, prefix)
        else:
            await client.message(
                source,
                f"Usage: {prefix}mem add <fait> | {prefix}mem list | "
                f"{prefix}mem forget <n|all>",
            )
    except Exception as e:  # noqa: BLE001
        log.exception("!mem %s failed for %s", sub, source)
        await bus.publish(Event(type="error", payload={"message": f"owui_memory: {e}"}))
        await client.message(source, "⚠️ Erreur côté OpenWebUI, réessaie plus tard.")
    return True


async def _cmd_add(client, source: str, fact: str, prefix: str) -> None:
    if not fact:
        await client.message(source, f"Usage: {prefix}mem add <fait à retenir>")
        return
    if len(fact) > MAX_FACT_LEN:
        await client.message(source, f"Trop long ({len(fact)} car., max {MAX_FACT_LEN}).")
        return
    mine = await list_for(source)
    if len(mine) >= MAX_FACTS_PER_NICK:
        await client.message(
            source,
            f"Limite atteinte ({MAX_FACTS_PER_NICK} faits). "
            f"Fais du ménage avec {prefix}mem forget <n>.",
        )
        return
    if any(m["content"].strip().lower() == fact.lower() for m in mine):
        await client.message(source, "Je le savais déjà 🙂")
        return
    await add(source, fact)
    await bus.publish(Event(type="system", payload={
        "message": f"[owui_memory] {source} add ({len(mine) + 1} faits)"
    }))
    await client.message(source, f"✅ Retenu (n°{len(mine) + 1}).")


async def _cmd_list(client, source: str, prefix: str) -> None:
    mine = await list_for(source)
    if not mine:
        await client.message(source, f"Aucun fait retenu. {prefix}mem add <fait> pour commencer.")
        return
    for i, m in enumerate(mine, 1):
        await client.message(source, f"{i}. {m['content']}"[:400])


async def _cmd_forget(client, source: str, arg: str, prefix: str) -> None:
    mine = await list_for(source)
    if arg.lower() == "all":
        for m in mine:
            await delete(m["id"])
        await bus.publish(Event(type="system", payload={
            "message": f"[owui_memory] {source} forget all ({len(mine)})"
        }))
        await client.message(source, f"🗑 {len(mine)} fait(s) oublié(s).")
        return
    if not arg.isdigit() or not (1 <= int(arg) <= len(mine)):
        await client.message(
            source, f"Usage: {prefix}mem forget <n|all> (n = numéro dans {prefix}mem list)",
        )
        return
    m = mine[int(arg) - 1]
    await delete(m["id"])
    await client.message(source, f"🗑 Oublié : {m['content'][:200]}")
