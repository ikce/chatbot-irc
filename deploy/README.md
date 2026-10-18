# chatbot-irc — Déploiement

Chatter bot IRC branché sur **Ollama** + **OpenWebUI** (externes) via une WebUI FastAPI. Se connecte à un **ZNC externe**.

## TL;DR — First boot

```bash
# 1. Copier le template d'env et remplir tes vraies valeurs
cp .env.example .env
$EDITOR .env      # ZNC creds + Ollama URL/API key + OpenWebUI URL/API key

# 2. Build + up
podman compose up -d --build   # (ou docker compose)

# 3. Ouvre la WebUI
xdg-open http://localhost:54878/
```

Le bot boote en mode **provisioning DB** : au premier démarrage, il lit `.env`, écrit les valeurs comme défauts en SQLite, puis se connecte à ZNC. Toute modification ultérieure se fait via la WebUI (aucun redémarrage requis pour LLM/triggers ; **redémarrage requis** pour les paramètres IRC).

## Pré-requis externes (déjà en place chez Eric)

| Service | Rôle | Configuré via |
|---|---|---|
| **ZNC** | Bouncer IRC (bot s'y connecte comme à un serveur IRC) | Onglet `IRC / ZNC` de la WebUI |
| **OpenWebUI** | Endpoint LLM unifié OpenAI-compat (`/api/chat/completions`) + web search intégré (`features.web_search`) | Onglet `LLM / Triggers` |

**Ollama** peut tourner derrière OpenWebUI — le bot ne le voit pas directement, il passe par le proxy OpenAI-compat d'OpenWebUI avec un simple Bearer token.

## Interface web (http://localhost:54878)

- `/` — **IRC / ZNC** : host, port, TLS, user/pass, nick, ident, realname, canaux
- `/llm` — **LLM / Triggers** :
  - Modèle Ollama, max_tokens (limite mots), temperature, system prompt
  - Cooldown global + par nick (throttle)
  - OpenWebUI URL/key + toggle web search
  - Gestion des triggers (`nick` regex, `message` regex, `mention` substring)
- `/moderation` — **Modération** (Phases 1-4 actives) :
  - **Policies** : toggle par canal des features (tracelog, whitelist, réputation, LLM, patterns, auto-ban), seuils, modèles LLM override, template raison de ban, notifications
  - **Whitelist** : masks `nick!ident@host` protégés (wildcards `*` et `?`), globaux ou par chan
  - **Tracelog** : recherche paginée sur toutes les observations IRC (msg/join/part/quit/nick_change) avec IP résolue via 8.8.8.8
  - **Réputation** : Spamhaus DROP/EDROP (auto-refresh 24h) + AbuseIPDB (clé API requise, cache 24h) + historique local (IncidentHistory). Verdict par user au JOIN si `reputation_check_enabled` + `join_check_enabled`
  - **Patterns** : regex CRUD pour détection téléphones / adresses / cartes / NAS / mots-clés sensibles. Test-drive intégré (colle un sample, vois les matches)
  - **LLM** : classifier JSON strict via OpenWebUI, modèle par chan configurable, throttle global + per-nick, sample rate pour surveillance passive, table des décisions récentes
  - **Bans** : ban engine adaptif (MODE +b si op, sinon /msg X BAN Undernet), templates de raisons configurables, historique bans avec bouton Unban, safeguards (rate limit horaire + dedup fenêtre)
- `/console` — **Console live** : flux IRC temps réel via WebSocket + input pour parler comme le bot

## Endpoints REST

| Verbe | Path | Rôle |
|---|---|---|
| GET/POST | `/api/irc` | Config IRC/ZNC |
| GET/POST | `/api/llm` | Config Ollama + OpenWebUI + throttle |
| GET/POST/DELETE | `/api/triggers[/{id}]` | CRUD triggers |
| GET | `/api/messages?limit=100` | Historique messages |
| GET | `/api/status` | État connexion IRC |
| GET | `/healthz` | Healthcheck (utilisé par le healthcheck compose) |
| WS | `/ws` | Console live (bidirectionnel) |

## Modération — roadmap en 4 phases

Le bloc modération est déployé en phases progressives pour éviter les faux positifs catastrophiques :

| Phase | Livre | Effet observable |
|---|---|---|
| **P1** ✅ | Tables DB + tracelog + whitelist + DNS resolver + WebUI | Le bot loggue tout (nick↔ident↔host↔IP), aucune action de modération |
| **P2** ✅ | Spamhaus DROP/EDROP + AbuseIPDB + IncidentHistory | Le bot flag les IPs suspectes au JOIN, log incident + notif optionnelle, **aucun ban réel** |
| **P3** ✅ | LLM moderation via OpenWebUI + patterns (téléphone, adresse, contenu envers mineurs) | Le bot classifie chaque message, alimente les décisions dry-run |
| **P4** ✅ | Ban engine adaptif (MODE +b direct si op, sinon /msg X BAN Undernet) + templates raisons + rate limit + dedup + unban WebUI | Bans réels avec `dry_run` toggle par chan dans la WebUI. `dry_run=ON` par défaut = simule sans exécuter |

### Phase 4 — ban engine adaptif

Quand un flag sévère tombe (verdict réputation `should_ban`, ou verdict message pipeline `is_severe`) ET la policy chan a `auto_ban_enabled=ON`, le ban_engine décide de la méthode :

1. **Whitelist first** — un mask whitelisté n'atteint jamais ce point (court-circuit en amont)
2. **Compose les masks bidirectionnels** :
   - `*!*@<ip>` si IP publique connue
   - `*!*@<host>` si host connu (cloak Undernet, résidentiel, etc.) et distinct de l'IP
   - couvre reconnexion avec cloak vs IP directe
3. **Dedup fenêtre** — skip si un ban actif existe déjà pour ce couple (chan, mask) dans `duplicate_dedupe_window_sec` (défaut 300s)
4. **Rate limit** — skip si `max_bans_per_hour` déjà atteint globalement (safeguard runaway)
5. **Compose la raison** — template interpolé avec `{nick}` `{ip}` `{host}` `{source}` `{score}` `{chan}` `{verdict}` `{pattern_kinds}` `{cidr}` `{ref}` `{bot}`. Le template = celui de la policy chan si défini, sinon default global, sinon 1er actif
6. **Émission** :
   - Si `dry_run=ON` → `BannedEntity(active=False)` + `IncidentHistory(incident_type='dry_run_ban')` + broadcast console. **Aucun ordre IRC.**
   - Sinon → détecte si bot est op (`+o` sur le chan) :
     - Op → `MODE +b <mask>` direct (rawmsg)
     - Pas op → `/msg <chanserv_nick> BAN <chan> <mask> <duration> :<reason>` (défaut nick = `X`, config Undernet)
     - Optionnellement `KICK` avant le ban si `kick_before_ban=ON` et bot op
7. **Log central** — `BannedEntity(active=True)` + `IncidentHistory(incident_type='ban')` + broadcast console + notif `notify_channel`/`notify_nick` si configurés

**Unban** disponible dans `/moderation/bans` : bouton Unban envoie `MODE -b <mask>` et marque `BannedEntity.active=False`. Fonctionne uniquement si bot est op sur le chan (sinon utiliser X directement).

**Passage en prod** : commence avec `dry_run=ON` sur chaque chan, laisse tourner quelques heures/jours, valide dans `/moderation/bans` que les bans simulés font sens, puis flip `dry_run=OFF` chan par chan.

### Phase 3 — pipeline par message

Pour chaque message d'un chan avec `llm_moderation_enabled` OU `pattern_detection_enabled`, un task background enchaîne :

1. **Whitelist** — court-circuit si le mask matche
2. **Longueur min** — skip si le message est trop court (`min_message_len`, défaut 8 chars)
3. **Pattern scan** (si `pattern_detection_enabled`) — regex compilés en mémoire, extraits + severity
4. **LLM classify** (si `llm_moderation_enabled`) — appelé si :
   - au moins un pattern a matché (confirmation contexte), OU
   - échantillonnage aléatoire selon `sample_rate` (surveillance passive)
   - respecte `throttle_global_sec` + `throttle_per_nick_sec`
5. **Décision** :
   - Verdict LLM sévère avec confidence ≥ 0.5 → `record_llm_moderation` + `record_incident(flag_llm)`
   - Pattern seul avec severity ≥ 7 → `record_incident(flag_pattern)` (signal autonome)
   - Aucun `MODE +b` en P3 — `action_taken = "flag"` ou `"dry_run_ban"` selon les toggles P4

**Coût compute** : le classifier utilise `default_model` ou l'override par chan (recommandé : petit modèle rapide type `qwen2.5:3b`, `llama3.2:3b`). Le throttle protège l'endpoint : par défaut max 1 check/nick/5s + 1 check/500ms global.

### Phase 2 — sources et cheminement

Au JOIN sur un chan avec `reputation_check_enabled=ON` et `join_check_enabled=ON`, un task background enchaîne :

1. **Whitelist** — si le mask `nick!ident@host` matche, court-circuite tout (jamais suspect)
2. **DNS** — résout host↔IP bidirectionnellement via 8.8.8.8
3. **Spamhaus DROP/EDROP** — lookup CIDR en mémoire (cache rafraîchi toutes les 24h). Match = score 100, `should_ban=True`
4. **IncidentHistory locale** — compte les incidents précédents sur l'IP/host dans la fenêtre `incident_lookback_days`. Si ≥ `local_incident_threshold` → flag
5. **AbuseIPDB** — cache DB (TTL 24h) puis HTTP si stale/miss. Score ≥ `abuseipdb_threshold` → flag

Chaque flag écrit un `IncidentHistory` (qui alimente le compteur local du prochain check) et broadcast dans la console live. Si `notify_channel` ou `notify_nick` sont configurés dans la policy, une notif IRC est envoyée.

**Rate limit AbuseIPDB** : tier gratuit = 1000 checks/jour. Cache TTL 24h par défaut = même IP testée max 1×/jour. Un chan actif avec 100 users uniques/jour reste largement dans le quota.

**Whitelist** = protection des "gentils orduriers" : un user matché par un mask de la whitelist n'est jamais banni auto peu importe la sévérité des signaux détectés.

**DNS bidirectionnel** : à chaque observation, le bot résout host→IP (forward A record) ET IP→host (reverse PTR) via **8.8.8.8** (contourne les DNS locaux trop stricts). Aux phases 2-4 les bans utiliseront les 2 identifiants pour couvrir les reconnexions avec cloak vs IP directe.

## Cérémonie de reconfiguration IRC/ZNC

La lib pydle ouvre une connexion socket persistante ; changer host/port/creds **à chaud** n'est pas propre. Workflow recommandé :

1. Modifier via `/` (IRC/ZNC), sauvegarder
2. `podman compose restart chatbot`
3. Vérifier dans `/console` que le bot se reconnecte

Les changements LLM/throttle/triggers sont pris en compte **immédiatement** (lus en DB à chaque message entrant).

## Persistance

Volume nommé `chatbot_data` → `/app/data/chatbot.db` (SQLite). Backup trivial :

```bash
podman compose cp chatbot:/app/data/chatbot.db ./backup-$(date +%F).db
```

## Troubleshooting

| Symptôme | Piste |
|---|---|
| WebUI répond mais status "déconnecté" | Vérifier `podman compose logs chatbot` — ZNC creds / TLS |
| Bot ne répond jamais à un message | Onglet Triggers vide, ou pattern regex ne matche pas |
| `IRC crash: ...` répété dans la console | Cred ZNC invalide (format `user/network:password` — cf. `.env.example`) |
| LLM error dans la console | OpenWebUI URL joignable depuis le container ? `podman compose exec chatbot curl -s -H "Authorization: Bearer $LLM_API_KEY" $LLM_URL/api/models` |
| Message trop long tronqué à `...` | Augmenter `max_tokens` ou ajuster `_tidy_for_irc` dans `app/llm.py` |

## Cible déploiement

Développement/test local : `chatte-arch` (Podman rootless + crun — cf. `feedback_grafana_install_plugins.md` pour les pièges compose).

Prod perso : TrueNAS Scale (comme `truenas-lexus-manuals` et `mediabot`).
