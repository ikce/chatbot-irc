# chatbot-irc — Architecture

## Vue d'ensemble

```
┌──────────────────────────────────────────────┐
│  Container chatbot-irc (Debian slim + Py 3.12) │
│                                              │
│   ┌─────────────────────────────────────┐    │
│   │  FastAPI (uvicorn :8080 → :54878)   │    │
│   │  ├── /            (config IRC)      │    │
│   │  ├── /llm         (config Ollama)   │    │
│   │  ├── /bible       (Bible feature)   │    │
│   │  ├── /intel       (IP notes + cmds) │    │
│   │  ├── /moderation  (P1-P4)           │    │
│   │  ├── /console     (WebSocket UI)    │    │
│   │  ├── /api/*       (REST)            │    │
│   │  └── /ws          (WebSocket)       │    │
│   └────────────┬────────────────────────┘    │
│                │  event_bus (asyncio.Queue)  │
│   ┌────────────▼────────────────────────┐    │
│   │  IrcSupervisor (asyncio task)       │    │
│   │  └── ChatterBot(pydle.Client) ──────┼───→ ZNC externe (TLS)
│   └──────────────┬──────────────────────┘    │
│                  │ on_message()             │
│   ┌──────────────▼──────────────────────┐    │
│   │  triggers → throttle → llm.generate │    │
│   │                       │              │    │
│   └───────────────────────┼──────────────┘    │
│                           ▼                  │
│                    httpx AsyncClient ────────┼──→ OpenWebUI externe
│                    (Bearer + JSON)           │    POST /api/chat/completions
│                                              │    (features.web_search si activé)
│                                              │
│   ┌─────────────────────────────────────┐    │
│   │  SQLite  (/app/data/chatbot.db)     │    │
│   │  Tables: IrcConfig, LlmConfig,      │    │
│   │          Trigger, MessageLog        │    │
│   └─────────────────────────────────────┘    │
└──────────────────────────────────────────────┘
```

## Choix de stack

| Composant | Choix | Pourquoi |
|---|---|---|
| Web framework | **FastAPI** | Async natif, WebSocket first-class, DI propre, templates Jinja2 intégrés |
| Client IRC | **pydle** | Async native pure-Python, TLS + SASL, dispatch événementiel clair |
| Endpoint LLM | **OpenWebUI OpenAI-compat** | 1 seul endpoint pour chat + web search (via `features.web_search`), format standard portable (LiteLLM, vLLM, Ollama proxy, etc.) |
| DB | **SQLite + SQLModel** | Zéro service externe, 1 fichier backupable, hydration au boot depuis `.env` |
| HTTP client | **httpx** | Async natif, cohérent avec FastAPI/uvicorn |
| Templates | **Jinja2** | Aucun build front-end, CSS inline, JS vanilla, pas de node_modules |
| Persistence config | **DB > env** | `.env` = bootstrap only, WebUI = source de vérité runtime |

## Flux d'un message IRC

```
1. Message arrive sur ZNC → pydle → ChatterBot.on_message()
2. log_message(direction="in") + bus.publish(irc_msg)  ─┐
3. triggers.evaluate() lit la DB, applique regex/mention │
4. Si match ET throttle.can_respond() OK :               │
     5. throttle.mark()                                  │→ Consommé en parallèle
     6. llm.generate_reply() → httpx → Ollama            │  par les WebSocket
     7. (optionnel) OpenWebUI web search injecté         │  clients (/console)
     8. Chunking IRC (max 400 chars/ligne)               │
     9. client.message() → ZNC → réseau                  │
    10. log_message(direction="out") + bus.publish(bot_reply) ─┘
```

## Concurrence

- **1 seul asyncio event loop** partagé entre FastAPI (uvicorn), pydle (IRC), et httpx (LLM). Zéro thread partagé.
- `throttle` = mutations mono-loop, pas de lock nécessaire.
- SQLite avec `check_same_thread=False` + 1 Session par requête = OK pour un bot mono-instance.
- `EventBus` = simple pub-sub asyncio, chaque subscriber a sa Queue (`maxsize=500`, drop-on-full).
- **BibleBroadcaster** = task asyncio dédié (tick 30s), boucle indépendante du supervisor IRC, dort si le client n'est pas connecté ou si le bot n'est pas sur le chan cible.

## Feature Bible (verset à volonté)

Deux mécaniques dans une seule feature, backend commun :

```
!bible <ref>            → ACL check → wldeh CDN → post versets → LLM (mode default)
!bible-apo <ref>        → ...                                   → LLM mode apologist
!bible-meme <ref>       → ...                                   → LLM mode meme

BibleBroadcaster (tick 30s)
  ├─ pour chaque BibleBroadcast active :
  │    ├─ si interval_minutes écoulé
  │    ├─ mode picker (random / playlist / VOTD) — round-robin si plusieurs
  │    └─ fetch → post [+ commentaire LLM optionnel]
```

- **API externe** : [wldeh/bible-api](https://github.com/wldeh/bible-api) via jsdelivr CDN. Aucune clé, 200+ versions dont Louis Segond 1910 (`fr-lsg`). Cache mémoire TTL 30 min pour éviter de bombarder le CDN.
- **ACL séparée** : table `BibleAcl` (mask nick!ident@host, wildcards `*`/`?`) — indépendante des triggers LLM. Silence radio pour les non-autorisés (pas de leak de surface).
- **Prompts LLM** éditables par WebUI (`/bible`) — mode apologist ("défense sarcastique du passage") et meme ("shitpost moderne").
- **Broadcasts** configurables par channel : 3 flags checkbox indépendants (`mode_random`, `mode_playlist`, `mode_votd`) ; aucun coché = dodo ; plusieurs cochés = round-robin. Playlist = liste manuelle de refs cyclée.
- **Fallback silencieux** : une version qui 404 est rendue en `[version] ref — (indisponible)` sans planter les autres versions.

Tables ajoutées :

| Table | Rôle |
|---|---|
| `BibleAcl` | Masks autorisés (mask + reason + active) |
| `BibleConfig` | Singleton : versions activées, prompts LLM, throttle, préfixe commande |
| `BibleBroadcast` | 1 ligne par channel : interval, 3 mode flags, playlist, filtre livres, cursors round-robin |

## Feature Intel IP & Commandes custom

Deux surfaces couplées, la même page WebUI (`/intel`) :

```
!<cmd> <args>         → custom_commands.try_handle()
                        ├─ regex match (message entier, groupes nommés)
                        ├─ ACL check (BibleAcl si acl_required=True)
                        ├─ throttle (per-mask + global, séparé du LLM)
                        ├─ RECIPES[recipe_name](**groups)  ← read-only pur
                        ├─ format via output_template (str.format)
                        └─ humanize LLM (facultatif) → post IRC

Question LLM "c'est quoi 1.2.3.4 ?"
  → lookup.try_answer() → intent "describe_ip"
    └─ ip_intel.summarize_ip("1.2.3.4")
       ├─ IpAnnotation match (exact IP + fnmatch host)
       ├─ ReputationCache (AbuseIPDB)
       ├─ ReputationList (Spamhaus DROP/EDROP, CIDR match Python)
       ├─ IncidentHistory (count + types)
       ├─ BannedEntity (ban actif ?)
       └─ IpTraceLog (nicks vus)
    → format_for_llm() → texte compact
    → _humanize() → réponse IRC avec persona
```

- **Annotations manuelles** (`IpAnnotation`) : source de vérité op. Label ∈
  `good | bad | suspect | note`. Target = IP, host FQDN, ou pattern
  wildcard (`*.videotron.ca`). Verdict heuristique = annotation prime tout
  le reste (Spamhaus ensuite, puis score AbuseIPDB, puis incidents locaux).
- **Commandes custom** (`CustomCommand`) : mapping regex → recette Python
  read-only. **Aucun SQL arbitraire côté DB** — la liste des recettes
  invocables vit dans `custom_commands.RECIPES` et est éditée en Python.
  Ajouter une recette = ajouter une fonction + une entrée au dict.
  L'ACL réutilise `BibleAcl` (une seule liste de masks à maintenir pour
  toutes les commandes non-triviales).
- **Recettes seed** : `ip_flag_summary`, `ip_full_intel`, `nick_history`,
  `host_activity`, `ban_status`, `chan_top_flaggers`.
- **Commandes seed** : `!flag <ip>`, `!ip <ip>`, `!seen <nick>`,
  `!host <host>`, `!topflag [#chan]`.
- **Priorité de dispatch dans `on_message`** : custom_commands →
  bible_handler → trigger LLM (avec query_intent).

Tables ajoutées :

| Table | Rôle |
|---|---|
| `IpAnnotation` | Label manuel op (good/bad/suspect/note) sur IP, host ou pattern wildcard |
| `CustomCommand` | Nom + regex + recette + template output + flags (acl_required, humanize_with_llm, active) |

## Mémoire long terme (`app/memory.py`)

- **Identité** : tables `Person` / `PersonLink` (kind `account` | `mask` | `nick`).
  Résolution : compte services (pydle `users[nick]["account"]`) > `ident@host`
  exact > nick connu + même ident (non `~`) ou même domaine d'host > nick seul.
  Nick seul = confiance faible : faits publics uniquement, aucune écriture, pas
  d'historique PV. Le nick d'une personne qui a un compte n'est jamais accepté
  sans ce compte.
- **Faits** : table `MemoryFact`, max 60 par personne. Extraits par le LLM
  (`LlmConfig.memory_model`, vide = modèle principal) toutes les
  `memory_extract_every` questions au bot, ou après 5 min de silence. Appris en
  PV → privés (jamais injectés en channel). Le LLM ne peut supprimer que des
  faits `auto`, pas ceux dictés par l'utilisateur.
- **Prompt** : `generate_reply` reçoit faits + autres nicks + les
  `memory_context_lines` derniers messages du chan (2 h max) ou du PV (24 h max,
  identité sûre uniquement), lus dans `MessageLog`.
- **Commandes** (PV) : `!mem list|add|forget|off|on|who|import`. `import`
  reprend les anciens faits `[irc:nick]` des Memories OpenWebUI
  (`owui_memory.py`, conservé pour ça seulement).
- Les Memories OpenWebUI ne sont plus utilisées : elles appartiennent au compte
  de la clé API, donc partagées entre tous les nicks.

## Sécurité (lab perso, durcie progressivement)

- **Auth WebUI** (`app/auth.py`) : compte admin unique en DB (table `AdminAuth`,
  hash scrypt), session par cookie HMAC signé (HttpOnly, SameSite=Lax, 7 jours).
  Middleware ASGI pur → protège HTTP **et** le WebSocket `/ws`. Publics :
  `/login`, `/healthz`. `/api/*` non authentifié → 401 JSON.
- **Premier boot** : aucun compte → code de setup à usage unique loggé
  (`docker logs chatbot-irc`), exigé par le formulaire de création.
- **CSRF / CSWSH** : SameSite=Lax + vérif `Origin == Host` sur méthodes non-GET
  et handshakes WebSocket. Brute-force : 5 échecs / IP / 5 min.
- **Récupération** : `docker exec -it chatbot-irc python -m app.auth set-password`
  (ou `reset` pour revenir au mode setup).
- Pas de TLS interne (uvicorn HTTP nu) → le cookie n'a pas `Secure` ; passer
  derrière un reverse proxy HTTPS si exposé hors LAN.
- Secrets en clair en DB et dans `.env` (cheap-out assumé)
- `tls_verify=False` sur la connexion ZNC (self-signed cert habituel)
- **URL preview** : SSRF guard vérifié à chaque hop de redirection, body lu en
  streaming avec cap.

## Points d'extension

- **Multi-serveurs IRC** : aujourd'hui 1 seul enregistrement `IrcConfig`. Rendre `id` non-unique + 1 supervisor par ligne = trivial.
- **Streaming Ollama** : passer `stream=True` + push chunks par WebSocket pour typing-effect.
- **Persona multi-canal** : ajouter colonne `channel` sur `LlmConfig` pour un system prompt par salon.
- **Métriques Prometheus** : ajouter `/metrics` avec `prometheus_client` (comme [[mediabot-stack]]).
- **Multi-comptes WebUI** : `AdminAuth` est un singleton ; passer à une table
  d'utilisateurs + rôles si plusieurs ops doivent se connecter.

## Compatibilité

- **Python** : 3.12+ (pattern matching, `from __future__`, type hints natifs)
- **pydle** : 1.0+ (API `Client.handle_forever()`)
- **Runtime** : Podman rootless + crun (cf. mémoire user), Docker également supporté
