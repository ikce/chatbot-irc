from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel, Session, create_engine, select

from .config import settings

log = logging.getLogger("chatbot.db")


class IrcConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    host: str = "znc.example.tld"
    port: int = 6697
    tls: bool = True
    user: str = ""
    password: str = ""
    nick: str = "bibi"
    ident: str = "bibi"
    realname: str = "Bibi le chatter bot"
    channels: str = "#lab"
    # Préfixe global des commandes bot (!, ., ~, +, etc.). Appliqué à toutes
    # les CustomCommand (mode simple ET regex) et au bible_handler. Modifiable
    # via la WebUI : impacte immédiatement toutes les commandes existantes.
    command_prefix: str = "!"


class ImageGenConfig(SQLModel, table=True):
    """Config pour la génération d'image via OpenWebUI + share Immich.

    - `enabled` : kill-switch global.
    - `model`, `size`, `n` : passés au payload OpenWebUI. `model` vide = default
      OpenWebUI (le workflow ComfyUI par défaut). `n` = nb d'images par requête.
    - **URL + api_key réutilisés depuis LlmConfig** — tout passe par le même
      endpoint OpenWebUI, cf. décision architecture "tout centralisé côté OWU".
    - `immich_url`, `immich_api_key` : dans le bot (Immich n'est PAS connu
      d'OpenWebUI). Bot orchestre gen → download → upload → share.
    - `share_expiry_hours` : 0 = permanent, sinon Immich expire le share après.
    - `max_concurrent` : semaphore global — évite de faire fondre le GPU si
      3 users lancent des gens simultanément.
    - `throttle_per_nick_sec` : cooldown anti-abuse par nick.
    - `fetch_timeout_sec` : image gen est LENT (30s-3min), timeout large.
    """
    id: Optional[int] = Field(default=1, primary_key=True)
    enabled: bool = False
    model: str = ""                    # vide = default OpenWebUI
    size: str = "1024x1024"
    n: int = 1
    fetch_timeout_sec: float = 300.0   # 5 min max pour gen + download
    immich_url: str = ""               # ex: https://immich.example.com
    immich_api_key: str = ""
    share_expiry_hours: int = 0        # 0 = permanent
    max_concurrent: int = 1
    throttle_per_nick_sec: float = 60.0


class UrlIntelConfig(SQLModel, table=True):
    """Config pour la détection auto d'URL + fetch du titre HTML.

    - `enabled` : kill-switch global (peut être désactivé sans toucher les ACL).
    - `fetch_timeout_sec` : abandon si le serveur cible met plus longtemps.
    - `max_bytes_html` : cap de lecture du body — inutile de rapatrier 10 MB
      pour trouver un <title> qui est dans les 4 premiers KB.
    - `cache_ttl_sec` : durée mémoire (in-process, pas de Redis) pour éviter
      de re-fetcher la même URL en boucle.
    - `max_urls_per_message` : anti-flood si quelqu'un colle 10 liens d'un coup.
    - Filtrage SSRF (RFC1918/loopback/link-local) est HARDCODÉ dans url_intel.py,
      pas configurable — jamais laisser un user IRC forcer une requête sur
      un service interne.
    """
    id: Optional[int] = Field(default=1, primary_key=True)
    enabled: bool = True
    fetch_timeout_sec: float = 8.0
    max_bytes_html: int = 524288      # 512 KB
    cache_ttl_sec: int = 1800         # 30 min
    max_urls_per_message: int = 3
    # Gabarits d'affichage (cf. url_intel.render). None/NULL = défaut du code.
    link_format: Optional[str] = None
    desc_format: Optional[str] = None
    yt_format: Optional[str] = None
    yt_desc_format: Optional[str] = None


class AdminAuth(SQLModel, table=True):
    """Compte admin unique de la WebUI + secret de signature des sessions.

    - `username` vide = aucun compte → /login passe en mode setup (cf. auth.py).
    - `password_hash` : format `scrypt$n$r$p$salt$hash` (stdlib, pas de dépendance).
    - `session_secret` : clé HMAC des cookies de session, générée au 1er boot.
      La régénérer (vider le champ) déconnecte toutes les sessions.
    """
    id: Optional[int] = Field(default=1, primary_key=True)
    username: str = ""
    password_hash: str = ""
    session_secret: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LlmConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    # Endpoint OpenAI-compatible (OpenWebUI, LiteLLM, etc.)
    llm_url: str = "http://openwebui.local:3000"
    llm_api_key: str = ""
    model: str = "hf.co/cognitivecomputations/dolphin-2.9.3-mistral-nemo-12b-gguf:Q6_K"
    max_tokens: int = 400
    temperature: float = 0.7
    system_prompt: str = ""
    throttle_global_sec: float = 3.0
    throttle_per_nick_sec: float = 10.0
    web_search_enabled: bool = False
    # Query intent : le bot tente d'abord de répondre à partir de la DB de
    # modération (tracelog, incidents, bans) via 2 appels LLM (extract + humanise).
    # Fallback sur chat classique si intent=chat ou si le premier appel foire.
    query_intent_enabled: bool = True
    intent_extraction_prompt: str = ""      # vide = default hardcodé
    humanization_prompt: str = ""            # vide = fallback sur system_prompt
    # Mémoire PV persistante : fenêtre rolling max de messages à conserver +
    # injecter comme history dans generate_reply quand PmMemory.enabled=True.
    pm_memory_window: int = 30
    # Mémoire long terme par personne (cf. app/memory.py) : identité résolue
    # par compte NickServ > ident@host > nick, faits extraits automatiquement.
    memory_enabled: bool = True
    memory_model: str = ""                 # modèle d'extraction ; vide = `model`
    memory_extract_every: int = 6          # extraction toutes les N questions au bot
    memory_context_lines: int = 12         # lignes récentes du chan/PV injectées
    # Mots/expressions interdits dans les faits retenus (un par ligne ;
    # sous-chaîne sans casse ni accents, ou /regex/). Cf. memory.blocked_term.
    memory_blacklist: str = ""
    # Réponses spontanées (cf. app/autonomy.py) : chans où le bot décide seul
    # d'intervenir, modèle du « juge » (vide = memory_model puis model), et
    # délai minimum entre deux interventions non sollicitées dans un même chan.
    autonomous_channels: str = ""
    autonomous_model: str = ""
    autonomous_cooldown_sec: float = 300.0
    # Ignore auto (cf. app/ignore.py) : durée en minutes, 0 = désactivé.
    auto_ignore_minutes: int = 30
    # Au revoir spontané (cf. app/farewell.py) : chans séparés par des
    # virgules, `*` = tous, vide = désactivé.
    farewell_channels: str = "*"
    # Tools Open WebUI (IDs séparés par des virgules, ex. `web_fetch`) envoyés
    # en `tool_ids` quand un message adressé au bot contient un lien et que
    # l'auteur a allow_url_fetch. Vide = désactivé.
    owui_tool_ids: str = ""


class Trigger(SQLModel, table=True):
    """Autorisation de déclencher le LLM : un user (par mask IRC) + un mot-clé.

    Même sémantique que Whitelist côté modération : `mask` est un pattern
    nick!ident@host avec wildcards `*` et `?`. Le bot ne répond QUE si :
      1. Le mask du user émetteur matche `mask` (fnmatch case-insensitive)
      2. `keyword` (case-insensitive substring) est présent dans le message
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    mask: str = "*!*@*"                                        # ex: Bob!*@*.isp.com
    keyword: str = ""                                          # ex: "bibi" (substring CI)
    reason: str = ""
    added_by: str = ""
    added_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True


class MessageLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow)
    channel: str = ""
    nick: str = ""
    message: str = ""
    direction: str = "in"     # "in" | "out" | "system"


# =============================================================================
# Modération — nouvelles tables (Phases 1 à 4)
# =============================================================================


class ChannelPolicy(SQLModel, table=True):
    """Config par canal : quelles features de modération sont actives."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(unique=True, index=True)
    # Feature toggles — tout OFF par défaut sauf tracelog (safe observation)
    tracelog_enabled: bool = True
    whitelist_enforced: bool = True
    reputation_check_enabled: bool = False        # activé en Phase 2
    llm_moderation_enabled: bool = False          # activé en Phase 3
    pattern_detection_enabled: bool = False       # activé en Phase 3
    auto_ban_enabled: bool = False                # activé en Phase 4 (dry-run avant)
    dry_run: bool = True                          # log les décisions ban sans exécuter
    join_check_enabled: bool = False              # check reputation dès le JOIN
    # Seuils
    abuseipdb_threshold: int = 50                 # 0-100, ban si >=
    local_incident_threshold: int = 2             # nb incidents locaux avant flag
    # Ban défauts
    default_ban_duration_sec: int = 0             # 0 = permanent
    ban_reason_template_id: Optional[int] = None  # FK logique → BanReasonTemplate.id
    # LLM overrides (NULL = fallback sur LlmConfig global)
    llm_model_for_moderation: Optional[str] = None
    llm_model_for_chat: Optional[str] = None
    # Notifications
    notify_channel: Optional[str] = None          # #ops-alerts par ex.
    notify_nick: Optional[str] = None             # DM à un op


class Whitelist(SQLModel, table=True):
    """Masks IRC protégés (ne subissent jamais la modération auto)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel: Optional[str] = Field(default=None, index=True)  # NULL = global
    mask: str                                                  # nick!ident@host avec * et ?
    reason: str = ""
    added_by: str = ""
    added_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True


class IpTraceLog(SQLModel, table=True):
    """Observation IRC : tout nick↔ident↔host↔IP↔timestamp qu'on voit passer."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    event_type: str = Field(index=True)  # msg | join | part | quit | nick_change | whois
    channel: str = Field(default="", index=True)
    nick: str = Field(index=True)
    ident: str = ""
    host: str = Field(index=True)
    ip: Optional[str] = Field(default=None, index=True)
    resolved_host: Optional[str] = None  # PTR si host était IP directe
    context: str = ""  # message si msg, quit reason si quit, ancien nick si nick_change


class BannedEntity(SQLModel, table=True):
    """Bans actifs / historiques (peuplé en Phase 4)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(index=True)
    mask: str = Field(index=True)  # *!*@ip ou *!*@host
    ip: Optional[str] = Field(default=None, index=True)
    host: Optional[str] = Field(default=None, index=True)
    reason: str = ""
    source: str = ""  # spamhaus | abuseipdb | llm | pattern | manual | local_history
    banned_by_nick: str = ""
    banned_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None  # NULL = permanent
    active: bool = True
    original_nick: str = ""       # nick au moment du ban
    trigger_context: str = ""     # message ayant déclenché (si applicable)


class IncidentHistory(SQLModel, table=True):
    """Historique des incidents par IP/host — intelligence locale pour résidentiels compromis."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    ip: Optional[str] = Field(default=None, index=True)
    host: Optional[str] = Field(default=None, index=True)
    nick: str = ""
    channel: str = ""
    incident_type: str = ""  # ban | flag_reputation | flag_llm | flag_pattern | manual
    severity: int = 1        # 1-10
    detail: str = ""


class ReputationCache(SQLModel, table=True):
    """Cache résultats sources externes (AbuseIPDB principalement, TTL 24h)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ip: str = Field(index=True)
    source: str = Field(default="abuseipdb", index=True)
    score: int = 0
    is_public: bool = True
    country_code: str = ""
    isp: str = ""
    domain: str = ""
    usage_type: str = ""
    total_reports: int = 0
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    raw_response: str = ""  # JSON dump pour audit


class LlmModerationLog(SQLModel, table=True):
    """Log des décisions LLM sur chaque message analysé (peuplé en Phase 3)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    channel: str = ""
    nick: str = ""
    host: str = ""
    ip: Optional[str] = None
    message: str = ""
    verdict: str = ""         # normal | toxic | phone | address | minor_content | spam | proxy
    confidence: float = 0.0
    model_used: str = ""
    raw_llm_response: str = ""
    action_taken: str = ""    # none | flag | ban | dry_run_ban | notify_op


class BanReasonTemplate(SQLModel, table=True):
    """Templates de raisons de ban configurables via WebUI (peuplé en Phase 4)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)  # "Proxy AbuseIPDB", "Pattern téléphone", etc.
    template: str = ""              # supporte {nick} {ip} {host} {source} {score} {chan}
    active: bool = True


class ReputationList(SQLModel, table=True):
    """Snapshot des listes réputation externes (Spamhaus DROP/EDROP principalement).

    Une entrée par CIDR par source. `snapshot_at` permet d'identifier le batch le
    plus récent (on garde l'historique pour audit)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True)          # spamhaus_drop | spamhaus_edrop
    cidr: str = Field(index=True)            # ex 1.2.3.0/24
    ref: str = ""                             # ex SBL12345
    snapshot_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PatternRule(SQLModel, table=True):
    """Règle regex pour détection de contenu sensible (P3).

    kind : phone | address | minor_content | credit_card | social_id | other
    severity : 1-10 (10 = quasi-toujours à ban immédiatement en P4)
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    kind: str = Field(index=True)
    regex: str
    severity: int = 5
    description: str = ""
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LlmModerationConfig(SQLModel, table=True):
    """Singleton (id=1) : config globale du classifier LLM."""
    id: Optional[int] = Field(default=1, primary_key=True)
    system_prompt: str = ""
    default_model: str = ""              # fallback si policy.llm_model_for_moderation est vide
    max_message_len: int = 800           # tronque le message avant envoi au LLM
    min_message_len: int = 8             # skip si trop court
    sample_rate: float = 0.0             # 0.0-1.0, échantillonnage aléatoire pour messages sans pattern match
    throttle_per_nick_sec: float = 5.0   # délai min entre 2 checks LLM sur un même nick
    throttle_global_sec: float = 0.5     # délai min global (protection endpoint)
    timeout_sec: float = 15.0


class ReputationConfig(SQLModel, table=True):
    """Singleton (id=1) : config globale des sources de réputation."""
    id: Optional[int] = Field(default=1, primary_key=True)
    abuseipdb_api_key: str = ""
    abuseipdb_max_age_days: int = 90
    abuseipdb_cache_ttl_sec: int = 86400            # 24h
    spamhaus_refresh_interval_sec: int = 86400      # 24h
    spamhaus_enabled: bool = True
    abuseipdb_enabled: bool = False                 # requires API key
    incident_lookback_days: int = 30                # fenêtre pour compter incidents locaux
    # Snapshot metadata écrit par le worker Spamhaus
    spamhaus_last_update: Optional[datetime] = None
    spamhaus_drop_count: int = 0
    spamhaus_edrop_count: int = 0
    spamhaus_last_error: str = ""


class BibleAcl(SQLModel, table=True):
    """[LEGACY] Autorisation Bible/custom commands — remplacée par UserAcl.

    Conservée pour compatibilité read-only : la migration `_migrate_bibleacl_to_useracl`
    seed UserAcl à partir de ses entrées au premier boot après upgrade.
    Ne plus écrire ici — utiliser UserAcl et ses flags granulaires.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    mask: str = "*!*@*"
    reason: str = ""
    added_by: str = ""
    added_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True


class UserAcl(SQLModel, table=True):
    """ACL granulaire par user avec 8 flags-catégorie (opt-in explicite).

    Un `mask` matche via `mask_utils.mask_matches` (fnmatch case-insensitive)
    sur `nick!ident@host`. Les wildcards `*` et `?` permettent :
      - user précis : `Bob!~bobi@host.example.com`
      - user + tous ses hosts : `Bob!*@*`
      - tout un domaine : `*!*@*.videotron.ca`  (accès par host, no user needed)

    Chaque flag = catégorie de feature. `!add user` crée avec tous les flags
    à False → l'op coche via la WebUI /users ce qui est autorisé.
    `is_admin` débloque les commandes admin (!add/!del/!list users).
    `active=False` = kill-switch global sans supprimer l'entrée.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    mask: str = "*!*@*"
    note: str = ""
    added_by: str = ""
    added_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True
    # Keyword de déclenchement LLM (vide = réagit sur mention du nick du bot OU en PV)
    keyword: str = ""
    # Flags par catégorie (default False = tout OFF, opt-in explicite)
    allow_llm_chat: bool = False           # peut trigger le LLM chat classique (mask+keyword)
    allow_query_intent: bool = False       # peut trigger le lookup DB via question naturelle
    allow_bible: bool = False              # peut !bible / !bible-apo / !bible-meme
    allow_intel: bool = False              # peut !flag / !ip / !seen / !host / !topflag
    allow_custom_commands: bool = False    # peut TOUTE autre commande custom (hors intel seed)
    allow_url_fetch: bool = False          # ses URLs postées déclenchent fetch title
    allow_image_gen: bool = False          # peut !img <prompt> et gen via natural language
    allow_web_search: bool = False         # injecte features.web_search dans le payload (kill-switch global doit être ON)
    is_admin: bool = False                 # peut !add / !del / !list users


class PmMemory(SQLModel, table=True):
    """Toggle de mémoire persistante pour les PV avec le bot, par nick.

    Clé = `nick_key` (nick.lower()) — on identifie un interlocuteur par son
    nick, pas par mask complet, parce que ident/host peuvent changer (VPN,
    NAT, dual-boot) alors que le nick est stable dans la pratique IRC.

    Quand `enabled=True` ET le message arrive en PM :
        - le pipeline bypass le trigger keyword classique
        - le message est stocké en PmMessage (role='user')
        - les N derniers messages sont injectés comme history dans generate_reply
        - la réponse est stockée aussi (role='assistant')

    `!pv on/off/clear/reset/status` gère le toggle et l'historique.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    nick_key: str = Field(index=True, unique=True)         # nick.lower()
    enabled: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active: datetime = Field(default_factory=datetime.utcnow)


class PmMessage(SQLModel, table=True):
    """Un tour de la conversation PV — role user OU assistant.

    Rolling window : capped par `LlmConfig.pm_memory_window` (default 30).
    Le vieux est purgé au write pour garder la table bornée.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    nick_key: str = Field(index=True)                      # nick.lower()
    role: str                                              # 'user' | 'assistant'
    content: str
    ts: datetime = Field(default_factory=datetime.utcnow)



class Person(SQLModel, table=True):
    """Un interlocuteur IRC reconnu dans la durée (cf. app/memory.py).

    Une personne regroupe plusieurs identités (PersonLink) : compte NickServ,
    masques ident@host, nicks. `memory_opt_out` = la personne a demandé
    `!mem off` → plus d'extraction auto ni d'injection de faits.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    display_nick: str = ""
    memory_opt_out: bool = False
    memory_locked: bool = False      # `!mem lock` : refuse les faits ajoutés par d'autres
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)


class PersonLink(SQLModel, table=True):
    """Une identité rattachée à une Person.

    kind = 'account' (compte services, le plus fiable), 'mask' (ident@host
    sans ~, en minuscules) ou 'nick' (en minuscules, indice faible).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    person_id: int = Field(index=True)
    kind: str = Field(index=True)
    value: str = Field(index=True)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)


class MemoryFact(SQLModel, table=True):
    """Un fait retenu sur une Person.

    `private=True` : appris en PV (ou ajouté via !mem add) → injecté en PV
    seulement. `source` = 'auto' (extraction LLM), 'manual' (!mem add) ou
    'owui' (import des anciennes Memories OpenWebUI).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    person_id: int = Field(index=True)
    content: str
    private: bool = True
    source: str = "auto"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BibleConfig(SQLModel, table=True):
    """Singleton (id=1) : config globale de la feature Bible.

    - `versions_enabled` : JSON list de codes wldeh (ex ["fr-lsg","en-kjv","en-web"]).
    - `default_mode` : "apologist" ou "meme" — mode LLM utilisé si l'user tape
      juste `!bible <ref>` sans variante. Les commandes `!bible-apo` et
      `!bible-meme` forcent le mode.
    - Prompts LLM : éditables via WebUI. Le passage biblique est fourni au LLM
      dans le message user, les prompts en system.
    - `throttle_per_mask_sec` : cooldown anti-flood par mask utilisateur.
    - `command_prefix` : préfixe des commandes (défaut `!`). Permet d'utiliser
      `.bible` ou `+bible` si un autre bot squatte déjà `!`.
    """
    id: Optional[int] = Field(default=1, primary_key=True)
    versions_enabled: str = '["fr-lsg","en-kjv"]'
    default_mode: str = "apologist"
    prompt_apologist: str = (
        "Tu es un apologète TREMENDOUS, provocateur et sarcastique. "
        "On te donne un passage biblique (potentiellement en plusieurs versions). "
        "Tu défends ce passage avec logique + punchline, en 1 à 3 phrases. "
        "Style Trump : adjectifs GIGANTIC, ton executive. "
        "Ne cite PAS le verset, on l'a déjà posté juste avant. "
        "Réponds en français, plat, sans markdown, prêt pour IRC."
    )
    prompt_meme: str = (
        "Tu es un shitposteur biblique. On te donne un passage. "
        "Transpose-le en situation contemporaine absurde en 1 à 3 phrases. "
        "Blague moderne, meme-friendly, sans hostilité. "
        "Ne cite PAS le verset, on l'a déjà posté juste avant. "
        "Réponds en français, plat, sans markdown, prêt pour IRC."
    )
    throttle_per_mask_sec: float = 15.0
    throttle_global_sec: float = 3.0
    command_prefix: str = "!"
    llm_commentary_enabled: bool = True
    llm_commentary_for_broadcast: bool = False  # commentaire aussi sur broadcast auto
    allow_pm: bool = True                                # False = channel-only pour !bible*


class BibleBroadcast(SQLModel, table=True):
    """Configuration d'un broadcast récurrent par channel.

    3 modes de sélection cochés indépendamment (mode_random, mode_playlist,
    mode_votd). Si plusieurs cochés = round-robin. Si aucun coché = skip.

    - `playlist` : JSON list de refs (["Prov 3:5-6","Ecc 1:2"]).
    - `books_filter` : JSON list de codes wldeh de livres autorisés pour random
      (ex ["proverbs","ecclesiastes"]). Vide = toute la Bible.
    - `interval_minutes` : période entre 2 broadcasts.
    - `versions_override` : JSON list optionnelle qui override
      BibleConfig.versions_enabled pour ce channel (vide = fallback global).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(index=True)
    active: bool = True
    interval_minutes: int = 180
    mode_random: bool = False
    mode_playlist: bool = False
    mode_votd: bool = False
    playlist: str = "[]"
    books_filter: str = "[]"
    versions_override: str = "[]"
    rr_cursor: int = 0                              # round-robin pointer entre modes actifs
    playlist_cursor: int = 0                        # pointer courant dans la playlist
    last_run_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BanEngineConfig(SQLModel, table=True):
    """Singleton (id=1) : config globale du ban engine (P4).

    - `chanserv_nick` + `chanserv_ban_cmd_template` : si le bot n'est pas op sur
      le chan, il envoie `/msg <chanserv_nick> <cmd interpolé>`. Défaut Undernet X.
    - `max_bans_per_hour` : safeguard global anti-runaway (ex: bug qui banne tout
      le monde). Compte via BannedEntity.banned_at.
    - `default_template_id` : template utilisé si la policy du chan n'en spécifie pas.
    - `duplicate_dedupe_window_sec` : ne pas re-bannir un mask déjà banni dans cette
      fenêtre (évite bans dupliqués si plusieurs signaux tombent en même temps).
    """
    id: Optional[int] = Field(default=1, primary_key=True)
    chanserv_nick: str = "X"                        # X pour Undernet, ChanServ pour d'autres
    chanserv_ban_cmd_template: str = "BAN {channel} {mask} {duration} :{reason}"
    prefer_chanserv: bool = False                   # si True, toujours passer par X même si op
    max_bans_per_hour: int = 20                     # rate limit safeguard
    default_template_id: Optional[int] = None
    duplicate_dedupe_window_sec: int = 300          # 5 min
    kick_before_ban: bool = False                   # /kick avant /mode +b (bonus)
    kick_message_template: str = "{reason}"


# =============================================================================
# Intel IP — annotations manuelles pour aider le LLM à référencer les good/bad IPs
# =============================================================================


class IpAnnotation(SQLModel, table=True):
    """Note manuelle sur une IP (ou host) pour que le LLM sache quoi en penser.

    - `target` : soit une IP littérale (192.168.1.5), soit un host FQDN, soit
      un pattern wildcard (*.videotron.ca). Le lookup fait match exact IP +
      fallback fnmatch sur host.
    - `label` : good | bad | suspect | note. Contrôle le ton du LLM.
    - `note` : texte libre injecté au LLM.
    - `expires_at` : NULL = permanent. Si passé, considérée inactive au lookup.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    target: str = Field(index=True)
    label: str = Field(default="note", index=True)   # good | bad | suspect | note
    note: str = ""
    source: str = "manual"                            # manual | import | script
    added_by: str = ""
    added_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    active: bool = True


class CustomCommand(SQLModel, table=True):
    """Commande IRC custom qui exécute une recette read-only sur la DB.

    - `name` : nom logique unique (ex 'flag').
    - `trigger` : regex nommée qui match le message ENTIER (ex `^!flag\\s+(?P<ip>\\S+)\\s*$`).
      Les groupes nommés deviennent les args passés à la recette.
    - `recipe` : nom d'une fonction dans custom_commands.RECIPES (registre
      curé côté Python, PAS de SQL arbitraire côté DB).
    - `output_template` : template str.format qui reçoit les clés du résultat
      recette. Vide = fallback JSON compact.
    - `humanize_with_llm` : si True, passe le texte formaté au LLM pour
      reformulation persona (comme lookup._humanize).
    - `acl_required` : si True, requiert allow_custom_commands (ou allow_intel
      pour les seeds !flag/!ip/!seen/!host/!topflag) sur UserAcl.
    - `pattern_mode` : 'simple' → `trigger` contient un pattern lisible
      (ex `flag {ip}`) auto-converti en regex. 'regex' → trigger brut compilé
      tel quel. Le préfixe est TOUJOURS prepend depuis IrcConfig.command_prefix.
    - `allow_pm` : si False, ignoré en PM (channel == source), utile pour
      commandes lourdes réservées au channel.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    trigger: str = ""                                  # simple pattern OU regex complet SANS préfixe
    pattern_mode: str = "regex"                        # 'simple' | 'regex'
    recipe: str = ""                                   # clé dans RECIPES
    description: str = ""                              # aide human-readable pour /intel
    output_template: str = ""                          # {clé} → str.format
    humanize_with_llm: bool = False
    acl_required: bool = True
    allow_pm: bool = True                              # False = channel-only
    active: bool = True
    added_by: str = ""
    added_at: datetime = Field(default_factory=datetime.utcnow)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        db_path = os.path.join(settings.DATA_DIR, "chatbot.db")
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    _migrate_trigger_schema()
    SQLModel.metadata.create_all(get_engine())
    _migrate_llm_config_query_intent()
    _migrate_url_intel_formats()
    _migrate_irc_config_add_prefix()
    _migrate_custom_command_add_columns()
    _migrate_bibleconfig_add_allow_pm()
    _migrate_useracl_add_image_gen()
    _migrate_useracl_add_keyword_and_websearch()
    _bootstrap_defaults()
    _migrate_bibleacl_to_useracl()
    _migrate_trigger_to_useracl()
    _migrate_strip_prefix_from_custom_commands()


def _migrate_llm_config_query_intent() -> None:
    """Ajout non-destructif des colonnes query_intent_* sur llmconfig si absentes.

    SQLModel.create_all() ne fait pas d'ALTER sur tables existantes — on ajoute
    à la main. Pas de rollback nécessaire : tout ce qui manque devient NULL et
    est traité comme 'valeur par défaut' au read.
    """
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "llmconfig" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("llmconfig")}
    additions = []
    if "query_intent_enabled" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN query_intent_enabled BOOLEAN DEFAULT 1")
    if "intent_extraction_prompt" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN intent_extraction_prompt TEXT DEFAULT ''")
    if "humanization_prompt" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN humanization_prompt TEXT DEFAULT ''")
    if "pm_memory_window" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN pm_memory_window INTEGER DEFAULT 30")
    if "memory_enabled" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN memory_enabled BOOLEAN DEFAULT 1")
    if "memory_model" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN memory_model TEXT DEFAULT ''")
    if "memory_extract_every" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN memory_extract_every INTEGER DEFAULT 6")
    if "memory_context_lines" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN memory_context_lines INTEGER DEFAULT 12")
    if "memory_blacklist" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN memory_blacklist TEXT DEFAULT ''")
    if "autonomous_channels" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN autonomous_channels TEXT DEFAULT ''")
    if "autonomous_model" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN autonomous_model TEXT DEFAULT ''")
    if "autonomous_cooldown_sec" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN autonomous_cooldown_sec FLOAT DEFAULT 300")
    if "auto_ignore_minutes" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN auto_ignore_minutes INTEGER DEFAULT 30")
    if "farewell_channels" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN farewell_channels TEXT DEFAULT '*'")
    if "owui_tool_ids" not in cols:
        additions.append("ALTER TABLE llmconfig ADD COLUMN owui_tool_ids TEXT DEFAULT ''")
    if "person" in insp.get_table_names():
        pcols = {c["name"] for c in insp.get_columns("person")}
        if "memory_locked" not in pcols:
            additions.append("ALTER TABLE person ADD COLUMN memory_locked BOOLEAN DEFAULT 0")
    if not additions:
        return
    with eng.begin() as conn:
        for sql in additions:
            conn.execute(text(sql))


def _migrate_url_intel_formats() -> None:
    """Ajout des colonnes de gabarits d'affichage des liens (NULL = défaut)."""
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "urlintelconfig" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("urlintelconfig")}
    with eng.begin() as conn:
        for col in ("link_format", "desc_format", "yt_format", "yt_desc_format"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE urlintelconfig ADD COLUMN {col} TEXT"))


def _migrate_trigger_schema() -> None:
    """Migration one-shot destructive : ancienne table Trigger (kind, pattern)
    devient (mask, keyword, reason, added_by, added_at). Drop si l'ancien
    schema est détecté — lab perso, aucune donnée critique.
    """
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "trigger" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("trigger")}
    if "mask" in cols and "keyword" in cols:
        return  # déjà migré
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS trigger"))


def _migrate_irc_config_add_prefix() -> None:
    """Ajoute ircconfig.command_prefix si absent (default '!')."""
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "ircconfig" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("ircconfig")}
    if "command_prefix" in cols:
        return
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE ircconfig ADD COLUMN command_prefix TEXT DEFAULT '!'"))


def _migrate_bibleconfig_add_allow_pm() -> None:
    """Ajoute bibleconfig.allow_pm (True) si absent."""
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "bibleconfig" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("bibleconfig")}
    if "allow_pm" in cols:
        return
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE bibleconfig ADD COLUMN allow_pm BOOLEAN DEFAULT 1"))


def _migrate_useracl_add_image_gen() -> None:
    """Ajoute useracl.allow_image_gen (False) si absent."""
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "useracl" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("useracl")}
    if "allow_image_gen" in cols:
        return
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE useracl ADD COLUMN allow_image_gen BOOLEAN DEFAULT 0"))


def _migrate_useracl_add_keyword_and_websearch() -> None:
    """Ajoute useracl.keyword ('') et useracl.allow_web_search (False) si absents.

    Fait partie du merge Trigger → UserAcl : chaque user porte désormais
    son propre keyword (vide = mention/PV) et son opt-in web search.
    """
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "useracl" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("useracl")}
    additions = []
    if "keyword" not in cols:
        additions.append("ALTER TABLE useracl ADD COLUMN keyword TEXT DEFAULT ''")
    if "allow_web_search" not in cols:
        additions.append("ALTER TABLE useracl ADD COLUMN allow_web_search BOOLEAN DEFAULT 0")
    if not additions:
        return
    with eng.begin() as conn:
        for sql in additions:
            conn.execute(text(sql))


def _migrate_trigger_to_useracl() -> None:
    """Merge one-shot Trigger → UserAcl. Idempotent.

    Pour chaque Trigger actif :
      - Si un UserAcl matche déjà le mask ET qu'il n'a pas encore de keyword,
        patch : keyword=trigger.keyword + allow_llm_chat=True.
      - Sinon crée un UserAcl (allow_llm_chat=True, autres flags OFF) avec
        added_by='migration:trigger' pour traçabilité.

    Détection "déjà migré" : présence d'au moins un UserAcl avec
    added_by='migration:trigger'. Rejouer = no-op. Tolère l'ajout de
    nouveaux triggers manuels avant migration : ils passent tous.
    """
    with Session(get_engine()) as s:
        already = s.exec(
            select(UserAcl).where(UserAcl.added_by == "migration:trigger")
        ).first()
        if already is not None:
            return
        triggers = list(s.exec(select(Trigger).where(Trigger.active == True)))  # noqa: E712
        if not triggers:
            return
        migrated = 0
        patched = 0
        for t in triggers:
            existing = s.exec(select(UserAcl).where(UserAcl.mask == t.mask)).first()
            if existing is not None:
                if not existing.keyword:
                    existing.keyword = t.keyword or ""
                existing.allow_llm_chat = True
                s.add(existing)
                patched += 1
            else:
                s.add(UserAcl(
                    mask=t.mask,
                    note=f"[migré depuis Trigger #{t.id}] {t.reason}".strip(),
                    added_by="migration:trigger",
                    added_at=t.added_at,
                    active=True,
                    keyword=t.keyword or "",
                    allow_llm_chat=True,
                ))
                migrated += 1
        s.commit()
        log.warning(
            "_migrate_trigger_to_useracl: %d nouveaux UserAcl créés, %d patchés. "
            "Table Trigger laissée dormante (à dropper plus tard).",
            migrated, patched,
        )


def _migrate_custom_command_add_columns() -> None:
    """Ajoute customcommand.pattern_mode ('regex') et allow_pm (True) si absents.

    Les regex existantes restent en mode 'regex' — comportement inchangé.
    Nouvelles commandes créées via WebUI peuvent basculer en 'simple'.
    """
    from sqlalchemy import inspect, text
    eng = get_engine()
    insp = inspect(eng)
    if "customcommand" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("customcommand")}
    additions = []
    if "pattern_mode" not in cols:
        additions.append("ALTER TABLE customcommand ADD COLUMN pattern_mode TEXT DEFAULT 'regex'")
    if "allow_pm" not in cols:
        additions.append("ALTER TABLE customcommand ADD COLUMN allow_pm BOOLEAN DEFAULT 1")
    if not additions:
        return
    with eng.begin() as conn:
        for sql in additions:
            conn.execute(text(sql))


def _migrate_bibleacl_to_useracl() -> None:
    """Seed one-shot UserAcl depuis BibleAcl si UserAcl est vide.

    Sémantique préservée : chaque BibleAcl actif devient un UserAcl avec
    allow_bible=True + allow_custom_commands=True + allow_intel=True (car
    l'ancienne BibleAcl gardait aussi les intel seed commands avec
    acl_required=True).

    is_admin n'est JAMAIS auto-coché ici : promouvoir "la première ligne
    par id" était arbitraire (ordre d'insertion historique dans BibleAcl,
    aucun rapport avec une intention d'admin) et donnait silencieusement les
    droits admin globaux (donc accès à TOUTES les features, ex: image gen)
    à un user qui n'avait au départ qu'un accès Bible. L'op doit cocher
    is_admin explicitement via /users après migration.
    Ne s'exécute qu'une fois : dès qu'UserAcl a au moins 1 ligne, no-op.
    """
    with Session(get_engine()) as s:
        existing = s.exec(select(UserAcl)).first()
        if existing is not None:
            return
        bible_acls = list(s.exec(select(BibleAcl).order_by(BibleAcl.id)))
        if not bible_acls:
            return
        for b in bible_acls:
            s.add(UserAcl(
                mask=b.mask,
                note=f"[migré depuis BibleAcl] {b.reason}".strip(),
                added_by=b.added_by or "migration",
                added_at=b.added_at,
                active=b.active,
                allow_llm_chat=False,   # opt-in explicite pour le chat LLM
                allow_query_intent=False,
                allow_bible=True,
                allow_intel=True,
                allow_custom_commands=True,
                allow_url_fetch=False,
                is_admin=False,
            ))
        s.commit()
        log.warning(
            "_migrate_bibleacl_to_useracl: %d entrées migrées depuis BibleAcl, "
            "AUCUNE n'est admin — va sur /users et coche is_admin manuellement "
            "pour l'op voulu.",
            len(bible_acls),
        )


def _migrate_strip_prefix_from_custom_commands() -> None:
    """Migration destructive one-shot : strip le préfixe hardcodé (`^!` ou
    `^\\!`) des trigger existants pour que le compilateur puisse prepend
    dynamiquement IrcConfig.command_prefix.

    Détecte le pattern `^!` (avec ou sans échappement) au début et le retire.
    Idempotent : re-run = no-op si aucun trigger ne commence par le pattern.
    Ne touche que les entrées en pattern_mode='regex' (les 'simple' n'ont
    jamais le préfixe dans leur pattern).
    """
    import re as _re
    with Session(get_engine()) as s:
        rows = list(s.exec(select(CustomCommand)))
        modified = 0
        for row in rows:
            if row.pattern_mode != "regex":
                continue
            # Match ^! ou ^\! au tout début (avec ou sans backslash-escape)
            new_trigger = _re.sub(r"^\^\\?!", "^", row.trigger)
            if new_trigger != row.trigger:
                row.trigger = new_trigger
                modified += 1
        if modified:
            s.commit()


def _bootstrap_defaults() -> None:
    with Session(get_engine()) as s:
        irc = s.get(IrcConfig, 1)
        if irc is None:
            irc = IrcConfig(
                id=1,
                host=settings.ZNC_HOST if settings.BOOTSTRAP_FROM_ENV else "znc.example.tld",
                port=settings.ZNC_PORT if settings.BOOTSTRAP_FROM_ENV else 6697,
                tls=settings.ZNC_TLS if settings.BOOTSTRAP_FROM_ENV else True,
                user=settings.ZNC_USER if settings.BOOTSTRAP_FROM_ENV else "",
                password=settings.ZNC_PASS if settings.BOOTSTRAP_FROM_ENV else "",
                nick=settings.ZNC_NICK if settings.BOOTSTRAP_FROM_ENV else "bibi",
                ident=settings.ZNC_IDENT if settings.BOOTSTRAP_FROM_ENV else "bibi",
                realname=settings.ZNC_REALNAME if settings.BOOTSTRAP_FROM_ENV else "Bibi",
                channels=settings.ZNC_CHANNELS if settings.BOOTSTRAP_FROM_ENV else "#lab",
            )
            s.add(irc)

        llm = s.get(LlmConfig, 1)
        if llm is None:
            llm = LlmConfig(
                id=1,
                llm_url=settings.LLM_URL if settings.BOOTSTRAP_FROM_ENV else "http://openwebui.local:3000",
                llm_api_key=settings.LLM_API_KEY if settings.BOOTSTRAP_FROM_ENV else "",
                model=settings.LLM_MODEL if settings.BOOTSTRAP_FROM_ENV else "llama3.2:3b",
                max_tokens=settings.LLM_MAX_TOKENS if settings.BOOTSTRAP_FROM_ENV else 400,
                temperature=settings.LLM_TEMPERATURE if settings.BOOTSTRAP_FROM_ENV else 0.7,
                system_prompt=settings.LLM_SYSTEM_PROMPT if settings.BOOTSTRAP_FROM_ENV else "",
                throttle_global_sec=settings.THROTTLE_GLOBAL_SEC,
                throttle_per_nick_sec=settings.THROTTLE_PER_NICK_SEC,
                web_search_enabled=settings.WEB_SEARCH_ENABLED,
            )
            s.add(llm)

        # Seed UserAcl catch-all : n'importe quel user peut déclencher le bot
        # via la mention de son nick (keyword vide = mention/PV). À restreindre
        # via /users si tu veux limiter à des masks précis.
        # Seed uniquement si aucun UserAcl n'existe encore (post-migration OK).
        has_useracl = s.exec(select(UserAcl)).first() is not None
        if not has_useracl:
            s.add(UserAcl(
                mask="*!*@*",
                note="seed default — mention du nick du bot ou PV",
                added_by="bootstrap",
                active=True,
                keyword="",
                allow_llm_chat=True,
            ))

        # ChannelPolicy : une entrée par chan de ZNC_CHANNELS, toutes features
        # OFF par défaut sauf tracelog. On observe avant de modérer.
        existing_policies = {
            p.channel for p in s.exec(select(ChannelPolicy)).all()
        }
        for ch in (c.strip() for c in settings.ZNC_CHANNELS.split(",")):
            if ch and ch not in existing_policies:
                s.add(ChannelPolicy(channel=ch))

        # ReputationConfig singleton
        rep = s.get(ReputationConfig, 1)
        if rep is None:
            s.add(ReputationConfig(id=1))

        # LlmModerationConfig singleton
        llm_mod = s.get(LlmModerationConfig, 1)
        if llm_mod is None:
            s.add(LlmModerationConfig(
                id=1,
                system_prompt=(
                    "Tu es un classificateur de modération IRC. On te donne un message d'un canal. "
                    "Retourne UNIQUEMENT un objet JSON valide, sans texte autour, avec ces champs:\n"
                    '  {"verdict": "<label>", "confidence": <float 0-1>, "rationale": "<court>"}\n'
                    "Labels autorisés: normal, toxic, phone, address, minor_content, credit_card, "
                    "social_id, spam, proxy_hint.\n"
                    "- 'normal' pour toute conversation banale (blagues, insultes légères, vulgarité, débat).\n"
                    "- 'toxic' pour harcèlement direct, menaces, incitation à la violence.\n"
                    "- 'phone', 'address', 'credit_card', 'social_id' : détection de données personnelles réelles.\n"
                    "- 'minor_content' : contenu sexuel/prédateur envers un mineur.\n"
                    "- 'spam' : flood, publicité massive, phishing.\n"
                    "- 'proxy_hint' : le message ressemble à du texte auto/bot/proxy sortant.\n"
                    "Si le message est ambigu ou anodin, réponds 'normal' avec confidence basse.\n"
                    "Pas de markdown, pas de commentaire. UNIQUEMENT le JSON."
                ),
            ))

        # Patterns par défaut (uniquement si aucun n'existe encore)
        has_pattern = s.exec(select(PatternRule)).first() is not None
        if not has_pattern:
            for p in _default_pattern_rules():
                s.add(p)

        # BanEngineConfig singleton
        ban_cfg = s.get(BanEngineConfig, 1)
        if ban_cfg is None:
            s.add(BanEngineConfig(id=1))

        # BanReasonTemplate seeds
        has_template = s.exec(select(BanReasonTemplate)).first() is not None
        if not has_template:
            for t in _default_ban_templates():
                s.add(t)

        # BibleConfig singleton
        bible = s.get(BibleConfig, 1)
        if bible is None:
            s.add(BibleConfig(id=1))

        # CustomCommand seeds — commandes prêtes-à-l'emploi (éditables via WebUI)
        has_cmd = s.exec(select(CustomCommand)).first() is not None
        if not has_cmd:
            for c in _default_custom_commands():
                s.add(c)

        s.commit()


def _default_custom_commands() -> list["CustomCommand"]:
    """Commandes seed. Modifiables ou supprimables via /intel après boot."""
    return [
        CustomCommand(
            name="flag",
            trigger=r"^!flag\s+(?P<target>\S+)\s*$",
            recipe="ip_flag_summary",
            description="!flag <ip|host> — nombre d'incidents, types, dernier vu, ban actif ?",
            output_template=(
                "{target}: {incident_count} incidents ({incident_types}), "
                "dernier: {last_incident}, ban actif: {ban_status}, "
                "annotation: {annotation}"
            ),
            humanize_with_llm=False,
            acl_required=True,
            added_by="system",
        ),
        CustomCommand(
            name="ip",
            trigger=r"^!ip\s+(?P<ip>\S+)\s*$",
            recipe="ip_full_intel",
            description="!ip <ip> — dossier complet: annotation + AbuseIPDB + Spamhaus + local",
            output_template="",
            humanize_with_llm=True,
            acl_required=True,
            added_by="system",
        ),
        CustomCommand(
            name="seen",
            trigger=r"^!seen\s+(?P<nick>\S+)\s*$",
            recipe="nick_history",
            description="!seen <nick> — première/dernière obs, chans, nb events",
            output_template=(
                "{nick}: vu {events}x, du {first_seen} au {last_seen}, "
                "chans: {channels}, IPs: {ip_count}"
            ),
            humanize_with_llm=False,
            acl_required=False,
            added_by="system",
        ),
        CustomCommand(
            name="host",
            trigger=r"^!host\s+(?P<host>\S+)\s*$",
            recipe="host_activity",
            description="!host <host> — nb nicks distincts, IPs, dernière activité",
            output_template=(
                "{host}: {events} events, {nick_count} nicks distincts, "
                "{ip_count} IPs, dernier: {last_seen}"
            ),
            humanize_with_llm=False,
            acl_required=False,
            added_by="system",
        ),
        CustomCommand(
            name="topflag",
            trigger=r"^!topflag(?:\s+(?P<channel>#\S+))?\s*$",
            recipe="chan_top_flaggers",
            description="!topflag [#chan] — top 5 nicks les plus flaggés (30j)",
            output_template="{summary}",
            humanize_with_llm=False,
            acl_required=True,
            added_by="system",
        ),
    ]


def _default_ban_templates() -> list["BanReasonTemplate"]:
    """Templates de raisons de ban seed initiaux — éditables via WebUI ensuite."""
    return [
        BanReasonTemplate(
            name="default_reputation",
            template="Suspicious IP flagged by {source} (score {score}). Auto-ban via {bot}.",
        ),
        BanReasonTemplate(
            name="default_llm_content",
            template="Content violation: {verdict}. Auto-ban via {bot}. Contact ops.",
        ),
        BanReasonTemplate(
            name="default_pattern",
            template="Sensitive data detected ({pattern_kinds}). Auto-ban via {bot}.",
        ),
        BanReasonTemplate(
            name="spamhaus_hard",
            template="Spamhaus {source} listed ({cidr}). Banned by {bot}.",
        ),
        BanReasonTemplate(
            name="generic_safe",
            template="Automated moderation action. Contact ops if this is a mistake.",
        ),
    ]


def _default_pattern_rules() -> list["PatternRule"]:
    """Patterns seed initiaux — éditables depuis la WebUI ensuite."""
    return [
        # Téléphones FR (10 chiffres) et internationaux basiques
        PatternRule(
            name="phone_fr_10digits",
            kind="phone",
            regex=r"(?:^|[^\d])(?:0|\+33\s?)[1-9](?:[\s.\-]?\d{2}){4}(?:$|[^\d])",
            severity=6,
            description="Numéro français 10 chiffres (0X XX XX XX XX ou +33)",
        ),
        # Téléphones NANP (Canada/US) — 10 chiffres avec ou sans séparateurs
        PatternRule(
            name="phone_nanp",
            kind="phone",
            regex=r"(?:^|[^\d])(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?:$|[^\d])",
            severity=6,
            description="Numéro NANP (Canada/US) : (514) 555-1234, 1-800-555-1234, etc.",
        ),
        # Numéro international générique +XX XXXXXX
        PatternRule(
            name="phone_intl_generic",
            kind="phone",
            regex=r"\+\d{1,3}[\s.\-]?\d{6,12}",
            severity=5,
            description="Numéro international générique +XX...",
        ),
        # Adresse civique + rue (heuristique large)
        PatternRule(
            name="address_civic_fr_ca",
            kind="address",
            regex=r"\b\d{1,5}[,\s]+(?:rue|avenue|av\.?|boulevard|boul\.?|blvd\.?|chemin|ch\.?|place|impasse|allée|route|rte\.?|street|st\.?|road|rd\.?)\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' \-]{2,50}",
            severity=6,
            description="Adresse civique (numéro + type voie + nom rue) FR/CA/EN",
        ),
        # Code postal canadien A1A 1A1
        PatternRule(
            name="postal_code_ca",
            kind="address",
            regex=r"\b[A-CEGHJ-NPRSTVXY]\d[A-CEGHJ-NPRSTV-Z][ \-]?\d[A-CEGHJ-NPRSTV-Z]\d\b",
            severity=4,
            description="Code postal canadien (ex: H2X 1Y4)",
        ),
        # Code postal français (5 chiffres) — peu spécifique donc severity basse
        PatternRule(
            name="postal_code_fr",
            kind="address",
            regex=r"\b(?:0[1-9]|[1-8]\d|9[0-8])\d{3}\b",
            severity=2,
            description="Code postal français 5 chiffres (large — beaucoup de faux positifs)",
        ),
        # Carte de crédit (Luhn non vérifié, juste le format 13-19 chiffres)
        PatternRule(
            name="credit_card_generic",
            kind="credit_card",
            regex=r"\b(?:\d[ -]*?){13,19}\b",
            severity=8,
            description="Suite de 13-19 chiffres compatible carte de crédit",
        ),
        # Numéro d'assurance sociale canadien XXX XXX XXX
        PatternRule(
            name="nas_ca",
            kind="social_id",
            regex=r"\b\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3}\b",
            severity=8,
            description="NAS canadien (XXX XXX XXX)",
        ),
        # Mots-clés à confirmer par LLM — matériel sensible pédocrim
        PatternRule(
            name="minor_content_keywords",
            kind="minor_content",
            regex=r"\b(?:cp|pedo|p[ée]do(?:phile|philie)?|lolicon|shotacon|minor[s]?\s+(?:nsfw|nude|porn|sex))\b",
            severity=10,
            description="Mots-clés à confirmer par LLM (nécessite contexte)",
        ),
    ]


def get_irc_config() -> IrcConfig:
    with Session(get_engine()) as s:
        return s.get(IrcConfig, 1)


def get_llm_config() -> LlmConfig:
    with Session(get_engine()) as s:
        return s.get(LlmConfig, 1)


def list_triggers(active_only: bool = False) -> list[Trigger]:
    with Session(get_engine()) as s:
        stmt = select(Trigger)
        rows = list(s.exec(stmt))
        if active_only:
            rows = [r for r in rows if r.active]
        return rows


def add_trigger(mask: str, keyword: str, reason: str = "", added_by: str = "") -> Trigger:
    with Session(get_engine()) as s:
        row = Trigger(mask=mask, keyword=keyword, reason=reason, added_by=added_by, active=True)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete_trigger(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(Trigger, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def log_message(channel: str, nick: str, message: str, direction: str = "in") -> None:
    with Session(get_engine()) as s:
        s.add(MessageLog(channel=channel, nick=nick, message=message, direction=direction))
        s.commit()


def list_recent_messages(channel: str, limit: int, since: datetime) -> list[MessageLog]:
    """Derniers messages d'un chan (ou d'un PV : channel = nick), oldest first."""
    with Session(get_engine()) as s:
        rows = list(s.exec(
            select(MessageLog)
            .where(MessageLog.channel == channel, MessageLog.ts >= since)
            .order_by(MessageLog.ts.desc())
            .limit(limit)
        ))
    return list(reversed(rows))


# =============================================================================
# Modération — helpers de query
# =============================================================================


def get_channel_policy(channel: str) -> Optional[ChannelPolicy]:
    """Retourne la policy du canal, ou None. Crée pas d'entrée par défaut."""
    with Session(get_engine()) as s:
        stmt = select(ChannelPolicy).where(ChannelPolicy.channel == channel)
        return s.exec(stmt).first()


def list_channel_policies() -> list[ChannelPolicy]:
    with Session(get_engine()) as s:
        return list(s.exec(select(ChannelPolicy).order_by(ChannelPolicy.channel)))


def upsert_channel_policy(channel: str, **fields) -> ChannelPolicy:
    """Crée ou update la policy du chan. Renvoie l'entité fraîche."""
    with Session(get_engine()) as s:
        stmt = select(ChannelPolicy).where(ChannelPolicy.channel == channel)
        row = s.exec(stmt).first()
        if row is None:
            row = ChannelPolicy(channel=channel, **fields)
            s.add(row)
        else:
            for k, v in fields.items():
                setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def delete_channel_policy(channel: str) -> bool:
    """Supprime la policy d'un chan. Les rows historiques (tracelog, incidents,
    bans, llm_mod_log) sont laissées telles quelles — c'est de l'historique.
    Retourne True si supprimée, False si aucune policy pour ce chan."""
    with Session(get_engine()) as s:
        row = s.exec(select(ChannelPolicy).where(ChannelPolicy.channel == channel)).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def rename_channel_policy(old_channel: str, new_channel: str) -> tuple[bool, str]:
    """Renomme une ChannelPolicy. Ne touche pas aux rows historiques.
    Retourne (success, error_msg). error_msg vide si success."""
    old_channel = old_channel.strip()
    new_channel = new_channel.strip()
    if not new_channel:
        return False, "nouveau nom vide"
    if old_channel == new_channel:
        return False, "aucun changement"
    with Session(get_engine()) as s:
        row = s.exec(select(ChannelPolicy).where(ChannelPolicy.channel == old_channel)).first()
        if row is None:
            return False, f"policy {old_channel} introuvable"
        clash = s.exec(select(ChannelPolicy).where(ChannelPolicy.channel == new_channel)).first()
        if clash is not None:
            return False, f"policy {new_channel} existe déjà"
        row.channel = new_channel
        s.commit()
        return True, ""


def clone_channel_policy(source_channel: str, target_channel: str) -> tuple[Optional[ChannelPolicy], str]:
    """Crée une nouvelle policy en copiant les settings d'une policy source.
    Retourne (nouvelle_policy, error_msg)."""
    target_channel = target_channel.strip()
    source_channel = source_channel.strip()
    if not target_channel:
        return None, "chan cible vide"
    with Session(get_engine()) as s:
        src = s.exec(select(ChannelPolicy).where(ChannelPolicy.channel == source_channel)).first()
        if src is None:
            return None, f"policy source {source_channel} introuvable"
        clash = s.exec(select(ChannelPolicy).where(ChannelPolicy.channel == target_channel)).first()
        if clash is not None:
            return None, f"policy {target_channel} existe déjà"
        data = src.model_dump(exclude={"id", "channel"})
        new_row = ChannelPolicy(channel=target_channel, **data)
        s.add(new_row)
        s.commit()
        s.refresh(new_row)
        return new_row, ""


def list_whitelists(channel: Optional[str] = None, active_only: bool = True) -> list[Whitelist]:
    """Liste les whitelists : celles du chan + celles globales (channel=NULL)."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(Whitelist)))
        out = []
        for r in rows:
            if active_only and not r.active:
                continue
            if channel is None:
                out.append(r)
                continue
            if r.channel is None or r.channel == channel:
                out.append(r)
        return out


def add_whitelist(mask: str, channel: Optional[str], reason: str, added_by: str) -> Whitelist:
    with Session(get_engine()) as s:
        row = Whitelist(mask=mask, channel=channel, reason=reason, added_by=added_by)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete_whitelist(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(Whitelist, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def record_trace(
    event_type: str,
    nick: str,
    ident: str,
    host: str,
    ip: Optional[str] = None,
    resolved_host: Optional[str] = None,
    channel: str = "",
    context: str = "",
) -> None:
    """Écrit une observation IRC dans le tracelog."""
    with Session(get_engine()) as s:
        s.add(IpTraceLog(
            event_type=event_type,
            channel=channel,
            nick=nick,
            ident=ident,
            host=host,
            ip=ip,
            resolved_host=resolved_host,
            context=context,
        ))
        s.commit()


def query_tracelog(
    limit: int = 200,
    offset: int = 0,
    channel: Optional[str] = None,
    nick: Optional[str] = None,
    ip: Optional[str] = None,
    event_type: Optional[str] = None,
) -> list[IpTraceLog]:
    """Recherche paginée dans le tracelog, filtres optionnels."""
    with Session(get_engine()) as s:
        stmt = select(IpTraceLog).order_by(IpTraceLog.ts.desc())
        if channel:
            stmt = stmt.where(IpTraceLog.channel == channel)
        if nick:
            stmt = stmt.where(IpTraceLog.nick == nick)
        if ip:
            stmt = stmt.where(IpTraceLog.ip == ip)
        if event_type:
            stmt = stmt.where(IpTraceLog.event_type == event_type)
        stmt = stmt.limit(limit).offset(offset)
        return list(s.exec(stmt))


# =============================================================================
# Réputation (Phase 2) — helpers
# =============================================================================


def get_reputation_config() -> ReputationConfig:
    with Session(get_engine()) as s:
        row = s.get(ReputationConfig, 1)
        if row is None:
            row = ReputationConfig(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_reputation_config(**fields) -> ReputationConfig:
    with Session(get_engine()) as s:
        row = s.get(ReputationConfig, 1)
        if row is None:
            row = ReputationConfig(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def replace_reputation_list(source: str, entries: list[tuple[str, str]]) -> int:
    """Remplace atomiquement la liste réputation d'une source (Spamhaus DROP par ex).

    entries = liste de (cidr, ref). Retourne le nombre d'entrées écrites."""
    from datetime import datetime as _dt
    ts = _dt.utcnow()
    with Session(get_engine()) as s:
        # Delete old rows for this source
        old = list(s.exec(select(ReputationList).where(ReputationList.source == source)))
        for row in old:
            s.delete(row)
        # Insert new snapshot
        for cidr, ref in entries:
            s.add(ReputationList(source=source, cidr=cidr, ref=ref, snapshot_at=ts))
        s.commit()
        return len(entries)


def list_reputation_entries(source: Optional[str] = None) -> list[ReputationList]:
    with Session(get_engine()) as s:
        stmt = select(ReputationList)
        if source:
            stmt = stmt.where(ReputationList.source == source)
        return list(s.exec(stmt))


def get_reputation_cache(ip: str, source: str = "abuseipdb") -> Optional[ReputationCache]:
    with Session(get_engine()) as s:
        stmt = (
            select(ReputationCache)
            .where(ReputationCache.ip == ip)
            .where(ReputationCache.source == source)
            .order_by(ReputationCache.checked_at.desc())
        )
        return s.exec(stmt).first()


def upsert_reputation_cache(**fields) -> ReputationCache:
    """Écrit ou update le cache pour un couple (ip, source)."""
    ip = fields["ip"]
    source = fields.get("source", "abuseipdb")
    with Session(get_engine()) as s:
        stmt = (
            select(ReputationCache)
            .where(ReputationCache.ip == ip)
            .where(ReputationCache.source == source)
        )
        row = s.exec(stmt).first()
        if row is None:
            row = ReputationCache(**fields)
            s.add(row)
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            row.checked_at = datetime.utcnow()
        s.commit()
        s.refresh(row)
        return row


def record_incident(
    ip: Optional[str],
    host: Optional[str],
    nick: str,
    channel: str,
    incident_type: str,
    severity: int = 1,
    detail: str = "",
) -> IncidentHistory:
    with Session(get_engine()) as s:
        row = IncidentHistory(
            ip=ip, host=host, nick=nick, channel=channel,
            incident_type=incident_type, severity=severity, detail=detail,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def count_incidents(
    ip: Optional[str] = None,
    host: Optional[str] = None,
    since_days: int = 30,
) -> int:
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    with Session(get_engine()) as s:
        stmt = select(IncidentHistory).where(IncidentHistory.ts >= cutoff)
        rows = list(s.exec(stmt))
        matches = 0
        for r in rows:
            if ip and r.ip == ip:
                matches += 1
                continue
            if host and r.host == host:
                matches += 1
        return matches


def list_incidents(limit: int = 100) -> list[IncidentHistory]:
    with Session(get_engine()) as s:
        stmt = select(IncidentHistory).order_by(IncidentHistory.ts.desc()).limit(limit)
        return list(s.exec(stmt))


def query_incidents(
    limit: int = 200,
    offset: int = 0,
    channel: Optional[str] = None,
    incident_type: Optional[str] = None,
    nick: Optional[str] = None,
    min_severity: int = 0,
    since_hours: Optional[int] = None,
) -> list[IncidentHistory]:
    """Query filtrée pour /moderation/alerts (timeline exhaustive)."""
    with Session(get_engine()) as s:
        stmt = select(IncidentHistory).order_by(IncidentHistory.ts.desc())
        if channel:
            stmt = stmt.where(IncidentHistory.channel == channel)
        if incident_type:
            stmt = stmt.where(IncidentHistory.incident_type == incident_type)
        if nick:
            stmt = stmt.where(IncidentHistory.nick == nick)
        if min_severity > 0:
            stmt = stmt.where(IncidentHistory.severity >= min_severity)
        if since_hours is not None and since_hours > 0:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(hours=since_hours)
            stmt = stmt.where(IncidentHistory.ts >= cutoff)
        stmt = stmt.limit(limit).offset(offset)
        return list(s.exec(stmt))


def delete_incident(row_id: int) -> bool:
    """Supprime un incident précis. Retourne True si supprimé."""
    with Session(get_engine()) as s:
        row = s.get(IncidentHistory, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def purge_buffextras_noise() -> dict[str, int]:
    """Purge tous les rows générés par le pseudo-user ZNC *buffextras.

    Nettoie : IncidentHistory (nick='*buffextras' ou host='znc.in'),
    LlmModerationLog (idem), IpTraceLog (idem).
    Retourne un dict {table: nb_supprimés}.
    """
    result: dict[str, int] = {}
    with Session(get_engine()) as s:
        # IncidentHistory
        stmt = select(IncidentHistory).where(
            (IncidentHistory.nick == "*buffextras") | (IncidentHistory.host == "znc.in")
        )
        rows = list(s.exec(stmt))
        for r in rows:
            s.delete(r)
        result["incidents"] = len(rows)

        # LlmModerationLog
        stmt = select(LlmModerationLog).where(
            (LlmModerationLog.nick == "*buffextras") | (LlmModerationLog.host == "znc.in")
        )
        rows = list(s.exec(stmt))
        for r in rows:
            s.delete(r)
        result["llm_logs"] = len(rows)

        # IpTraceLog : uniquement les event_type='msg' avec ce nick — les
        # rows join/part/quit avec host='znc.in' SONT des rows légitimes
        # (produites par notre parseur buffextras post-fix) et doivent rester.
        stmt = select(IpTraceLog).where(
            (IpTraceLog.nick == "*buffextras") & (IpTraceLog.event_type == "msg")
        )
        rows = list(s.exec(stmt))
        for r in rows:
            s.delete(r)
        result["tracelog_msg"] = len(rows)

        s.commit()
    return result


def incident_type_counts(since_hours: int = 24) -> dict[str, int]:
    """Compteur par type d'incident sur une fenêtre — pour le dashboard alerts."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    with Session(get_engine()) as s:
        rows = list(s.exec(
            select(IncidentHistory).where(IncidentHistory.ts >= cutoff)
        ))
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.incident_type] = counts.get(r.incident_type, 0) + 1
        return counts


# =============================================================================
# Patterns & LLM moderation (Phase 3) — helpers
# =============================================================================


def get_llm_moderation_config() -> LlmModerationConfig:
    with Session(get_engine()) as s:
        row = s.get(LlmModerationConfig, 1)
        if row is None:
            row = LlmModerationConfig(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_llm_moderation_config(**fields) -> LlmModerationConfig:
    with Session(get_engine()) as s:
        row = s.get(LlmModerationConfig, 1)
        if row is None:
            row = LlmModerationConfig(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def list_pattern_rules(active_only: bool = False) -> list[PatternRule]:
    with Session(get_engine()) as s:
        rows = list(s.exec(select(PatternRule).order_by(PatternRule.kind, PatternRule.name)))
        if active_only:
            rows = [r for r in rows if r.active]
        return rows


def add_pattern_rule(name: str, kind: str, regex: str, severity: int = 5,
                     description: str = "") -> PatternRule:
    with Session(get_engine()) as s:
        row = PatternRule(
            name=name, kind=kind, regex=regex, severity=severity, description=description,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def update_pattern_rule(rule_id: int, **fields) -> Optional[PatternRule]:
    with Session(get_engine()) as s:
        row = s.get(PatternRule, rule_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def delete_pattern_rule(rule_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(PatternRule, rule_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def record_llm_moderation(
    channel: str,
    nick: str,
    host: str,
    ip: Optional[str],
    message: str,
    verdict: str,
    confidence: float,
    model_used: str,
    raw_llm_response: str = "",
    action_taken: str = "none",
) -> LlmModerationLog:
    with Session(get_engine()) as s:
        row = LlmModerationLog(
            channel=channel, nick=nick, host=host, ip=ip,
            message=message[:2000],
            verdict=verdict, confidence=confidence,
            model_used=model_used,
            raw_llm_response=raw_llm_response[:4000],
            action_taken=action_taken,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def list_llm_moderation_logs(limit: int = 100,
                              channel: Optional[str] = None,
                              verdict: Optional[str] = None) -> list[LlmModerationLog]:
    with Session(get_engine()) as s:
        stmt = select(LlmModerationLog).order_by(LlmModerationLog.ts.desc())
        if channel:
            stmt = stmt.where(LlmModerationLog.channel == channel)
        if verdict:
            stmt = stmt.where(LlmModerationLog.verdict == verdict)
        stmt = stmt.limit(limit)
        return list(s.exec(stmt))


# =============================================================================
# Ban engine (Phase 4) — helpers
# =============================================================================


def get_ban_engine_config() -> BanEngineConfig:
    with Session(get_engine()) as s:
        row = s.get(BanEngineConfig, 1)
        if row is None:
            row = BanEngineConfig(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_ban_engine_config(**fields) -> BanEngineConfig:
    with Session(get_engine()) as s:
        row = s.get(BanEngineConfig, 1)
        if row is None:
            row = BanEngineConfig(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def list_ban_templates(active_only: bool = False) -> list[BanReasonTemplate]:
    with Session(get_engine()) as s:
        rows = list(s.exec(select(BanReasonTemplate).order_by(BanReasonTemplate.name)))
        if active_only:
            rows = [r for r in rows if r.active]
        return rows


def get_ban_template(template_id: int) -> Optional[BanReasonTemplate]:
    with Session(get_engine()) as s:
        return s.get(BanReasonTemplate, template_id)


def add_ban_template(name: str, template: str) -> BanReasonTemplate:
    with Session(get_engine()) as s:
        row = BanReasonTemplate(name=name, template=template)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def update_ban_template(template_id: int, **fields) -> Optional[BanReasonTemplate]:
    with Session(get_engine()) as s:
        row = s.get(BanReasonTemplate, template_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def delete_ban_template(template_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(BanReasonTemplate, template_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def record_ban(
    channel: str,
    mask: str,
    ip: Optional[str],
    host: Optional[str],
    reason: str,
    source: str,
    banned_by_nick: str,
    original_nick: str = "",
    trigger_context: str = "",
    expires_at: Optional[datetime] = None,
    active: bool = True,
) -> BannedEntity:
    """Enregistre un ban (actif ou dry-run) dans BannedEntity."""
    with Session(get_engine()) as s:
        row = BannedEntity(
            channel=channel, mask=mask, ip=ip, host=host,
            reason=reason[:500], source=source,
            banned_by_nick=banned_by_nick,
            original_nick=original_nick,
            trigger_context=trigger_context[:1000],
            expires_at=expires_at, active=active,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def list_bans(limit: int = 100, channel: Optional[str] = None,
              active_only: bool = False) -> list[BannedEntity]:
    with Session(get_engine()) as s:
        stmt = select(BannedEntity).order_by(BannedEntity.banned_at.desc())
        if channel:
            stmt = stmt.where(BannedEntity.channel == channel)
        if active_only:
            stmt = stmt.where(BannedEntity.active == True)  # noqa: E712
        stmt = stmt.limit(limit)
        return list(s.exec(stmt))


def get_ban(ban_id: int) -> Optional[BannedEntity]:
    with Session(get_engine()) as s:
        return s.get(BannedEntity, ban_id)


def mark_ban_inactive(ban_id: int) -> bool:
    """Marque un ban inactif (après unban manuel)."""
    with Session(get_engine()) as s:
        row = s.get(BannedEntity, ban_id)
        if row is None:
            return False
        row.active = False
        s.commit()
        return True


def find_recent_ban(channel: str, mask: str, window_sec: int) -> Optional[BannedEntity]:
    """Cherche un ban actif récent pour ce couple (chan, mask) dans la fenêtre."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(seconds=window_sec)
    with Session(get_engine()) as s:
        stmt = (
            select(BannedEntity)
            .where(BannedEntity.channel == channel)
            .where(BannedEntity.mask == mask)
            .where(BannedEntity.active == True)  # noqa: E712
            .where(BannedEntity.banned_at >= cutoff)
            .order_by(BannedEntity.banned_at.desc())
        )
        return s.exec(stmt).first()


def count_recent_bans(since_seconds: int) -> int:
    """Compte les bans posés dans la fenêtre (safeguard runaway).

    Attention : lit toute la fenêtre en mémoire (list()) — c'est le pattern
    utilisé partout ici, count(*) natif est plus propre mais on garde la
    cohérence tant qu'on n'a pas des dizaines de milliers de bans.
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(seconds=since_seconds)
    with Session(get_engine()) as s:
        stmt = select(BannedEntity).where(BannedEntity.banned_at >= cutoff)
        return len(list(s.exec(stmt)))


# =============================================================================
# Bible feature — helpers
# =============================================================================


def get_bible_config() -> BibleConfig:
    with Session(get_engine()) as s:
        row = s.get(BibleConfig, 1)
        if row is None:
            row = BibleConfig(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_bible_config(**fields) -> BibleConfig:
    with Session(get_engine()) as s:
        row = s.get(BibleConfig, 1)
        if row is None:
            row = BibleConfig(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def list_bible_acls(active_only: bool = False) -> list[BibleAcl]:
    with Session(get_engine()) as s:
        rows = list(s.exec(select(BibleAcl).order_by(BibleAcl.id)))
        if active_only:
            rows = [r for r in rows if r.active]
        return rows


def add_bible_acl(mask: str, reason: str = "", added_by: str = "") -> BibleAcl:
    with Session(get_engine()) as s:
        row = BibleAcl(mask=mask, reason=reason, added_by=added_by, active=True)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete_bible_acl(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(BibleAcl, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


# =============================================================================
# UserAcl helpers — ACL granulaire multi-flags (remplace BibleAcl)
# =============================================================================

USER_ACL_FLAGS = (
    "allow_llm_chat",
    "allow_query_intent",
    "allow_bible",
    "allow_intel",
    "allow_custom_commands",
    "allow_url_fetch",
    "allow_image_gen",
    "allow_web_search",
    "is_admin",
)


def list_user_acls(active_only: bool = False) -> list[UserAcl]:
    with Session(get_engine()) as s:
        rows = list(s.exec(select(UserAcl).order_by(UserAcl.id)))
        if active_only:
            rows = [r for r in rows if r.active]
        return rows


def get_user_acl(row_id: int) -> Optional[UserAcl]:
    with Session(get_engine()) as s:
        return s.get(UserAcl, row_id)


def find_user_acl_by_mask(mask: str) -> Optional[UserAcl]:
    """Match EXACT sur la valeur mask (pas fnmatch). Utile pour !del user."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(UserAcl).where(UserAcl.mask == mask)))
        return rows[0] if rows else None


def add_user_acl(mask: str, note: str = "", added_by: str = "", **flags) -> UserAcl:
    """Ajoute un UserAcl. Tous les flags à False par défaut (opt-in explicite).

    Passer allow_*=True dans **flags pour cocher directement. `active=True`
    par défaut. Retourne l'entrée créée avec son id populé.
    """
    with Session(get_engine()) as s:
        row = UserAcl(mask=mask, note=note, added_by=added_by, active=True, **flags)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def update_user_acl(row_id: int, **fields) -> Optional[UserAcl]:
    with Session(get_engine()) as s:
        row = s.get(UserAcl, row_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def delete_user_acl(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(UserAcl, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def user_acl_check(nick: str, ident: str, host: str, flag: str) -> bool:
    """True si au moins un UserAcl actif matche le mask ET a `flag`=True.

    `flag` est un nom d'attribut (ex 'allow_bible', 'is_admin'). Renvoie False
    si le flag n'existe pas sur le modèle (defensive contre les typos).
    Lazy import de mask_matches pour éviter circular deps app.moderation → app.db.
    """
    from .moderation.mask_utils import mask_matches
    if flag not in {*USER_ACL_FLAGS, "active"}:
        return False
    user_mask = f"{nick}!{ident}@{host}"
    for row in list_user_acls(active_only=True):
        if mask_matches(user_mask, row.mask) and getattr(row, flag, False):
            return True
    return False


def has_user_acl_match(nick: str, ident: str, host: str) -> bool:
    """True s'il existe au moins un UserAcl actif dont le mask matche ce user.

    Utilisé pour la stratégie *backward-compat permissive* : un user sans
    aucun UserAcl est *grandfathered* (comportement legacy, autorisé). Un
    user qui a un UserAcl doit avoir le flag spécifique coché sinon refusé.
    """
    from .moderation.mask_utils import mask_matches
    user_mask = f"{nick}!{ident}@{host}"
    for row in list_user_acls(active_only=True):
        if mask_matches(user_mask, row.mask):
            return True
    return False


def resolve_user_acl(nick: str, ident: str, host: str) -> Optional[UserAcl]:
    """Renvoie le premier UserAcl actif dont le mask matche ce user, ou None.

    Contrairement à user_acl_check (qui répond bool par flag), ce helper
    renvoie l'objet complet — utile pour lire plusieurs flags à la fois
    (ex: allow_llm_chat + allow_web_search dans le même code path).
    Ordre = ordre d'insertion (id ASC via list_user_acls).
    """
    from .moderation.mask_utils import mask_matches
    user_mask = f"{nick}!{ident}@{host}"
    for row in list_user_acls(active_only=True):
        if mask_matches(user_mask, row.mask):
            return row
    return None


def list_bible_broadcasts(active_only: bool = False) -> list[BibleBroadcast]:
    with Session(get_engine()) as s:
        rows = list(s.exec(select(BibleBroadcast).order_by(BibleBroadcast.channel)))
        if active_only:
            rows = [r for r in rows if r.active]
        return rows


def get_bible_broadcast(row_id: int) -> Optional[BibleBroadcast]:
    with Session(get_engine()) as s:
        return s.get(BibleBroadcast, row_id)


def add_bible_broadcast(channel: str, **fields) -> BibleBroadcast:
    with Session(get_engine()) as s:
        row = BibleBroadcast(channel=channel, **fields)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def update_bible_broadcast(row_id: int, **fields) -> Optional[BibleBroadcast]:
    with Session(get_engine()) as s:
        row = s.get(BibleBroadcast, row_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


def delete_bible_broadcast(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(BibleBroadcast, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


# =============================================================================
# Lookup queries (feature "query intent" — chat bot LLM lit la DB modération)
# =============================================================================


def lookup_ips_for_nick(nick: str, limit: int = 10) -> list[dict]:
    """Retourne les IP/host distincts vus pour un nick (case-insensitive).

    Agrège par (ip, host), avec last_seen et count. Trié par last_seen desc.
    Rows où ip est NULL (pas encore résolu) tombent dans le host seul.
    """
    from sqlalchemy import func, text
    with Session(get_engine()) as s:
        stmt = (
            select(
                IpTraceLog.ip,
                IpTraceLog.host,
                func.max(IpTraceLog.ts).label("last_seen"),
                func.count().label("cnt"),
            )
            .where(func.lower(IpTraceLog.nick) == nick.lower())
            .group_by(IpTraceLog.ip, IpTraceLog.host)
            .order_by(text("last_seen DESC"))
            .limit(limit)
        )
        return [
            {"ip": r[0], "host": r[1], "last_seen": r[2], "count": r[3]}
            for r in s.exec(stmt)
        ]


def lookup_nicks_for_ip(ip: str, limit: int = 10) -> list[dict]:
    """Reverse : quels nicks ont été vus depuis cet IP ?"""
    from sqlalchemy import func, text
    with Session(get_engine()) as s:
        stmt = (
            select(
                IpTraceLog.nick,
                IpTraceLog.host,
                func.max(IpTraceLog.ts).label("last_seen"),
                func.count().label("cnt"),
            )
            .where(IpTraceLog.ip == ip)
            .group_by(IpTraceLog.nick, IpTraceLog.host)
            .order_by(text("last_seen DESC"))
            .limit(limit)
        )
        return [
            {"nick": r[0], "host": r[1], "last_seen": r[2], "count": r[3]}
            for r in s.exec(stmt)
        ]


def lookup_nicks_for_host(host_pattern: str, limit: int = 10) -> list[dict]:
    """Quels nicks depuis ce host ? Le pattern accepte SQL LIKE avec %.

    Ex: '%.videotron.ca' matche tous les hosts videotron. Case-insensitive.
    """
    from sqlalchemy import func, text
    with Session(get_engine()) as s:
        stmt = (
            select(
                IpTraceLog.nick,
                IpTraceLog.host,
                IpTraceLog.ip,
                func.max(IpTraceLog.ts).label("last_seen"),
                func.count().label("cnt"),
            )
            .where(func.lower(IpTraceLog.host).like(host_pattern.lower()))
            .group_by(IpTraceLog.nick, IpTraceLog.host, IpTraceLog.ip)
            .order_by(text("last_seen DESC"))
            .limit(limit)
        )
        return [
            {"nick": r[0], "host": r[1], "ip": r[2], "last_seen": r[3], "count": r[4]}
            for r in s.exec(stmt)
        ]


def seen_nick_summary(nick: str) -> Optional[dict]:
    """Résumé rapide d'un nick : première/dernière observation, nb events, chans."""
    from sqlalchemy import func
    with Session(get_engine()) as s:
        stmt = (
            select(
                func.min(IpTraceLog.ts),
                func.max(IpTraceLog.ts),
                func.count(),
            )
            .where(func.lower(IpTraceLog.nick) == nick.lower())
        )
        row = s.exec(stmt).first()
        if not row or row[2] == 0:
            return None
        chans_stmt = (
            select(IpTraceLog.channel)
            .where(func.lower(IpTraceLog.nick) == nick.lower())
            .where(IpTraceLog.channel != "")
            .distinct()
        )
        chans = [c for c in s.exec(chans_stmt) if c]
        return {
            "nick": nick,
            "first_seen": row[0],
            "last_seen": row[1],
            "events": row[2],
            "channels": chans,
        }


def stats_bans(channel: Optional[str] = None, active_only: bool = True) -> dict:
    """Compteur de bans, éventuellement filtré par chan."""
    with Session(get_engine()) as s:
        stmt = select(BannedEntity)
        if channel:
            stmt = stmt.where(BannedEntity.channel == channel)
        if active_only:
            stmt = stmt.where(BannedEntity.active == True)  # noqa: E712
        rows = list(s.exec(stmt))
        return {
            "total": len(rows),
            "channel": channel,
            "active_only": active_only,
        }


def stats_flags(since_days: int = 30, channel: Optional[str] = None) -> dict:
    """Compteur d'incidents dans la fenêtre, éventuellement par chan."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    with Session(get_engine()) as s:
        stmt = select(IncidentHistory).where(IncidentHistory.ts >= cutoff)
        if channel:
            stmt = stmt.where(IncidentHistory.channel == channel)
        rows = list(s.exec(stmt))
        return {
            "total": len(rows),
            "since_days": since_days,
            "channel": channel,
        }


def top_hosts(limit: int = 5, since_days: int = 30) -> list[dict]:
    """Top-N hosts par volume d'événements dans la fenêtre. Utile pour repérer
    les résidentiels compromis / gros émetteurs.
    """
    from datetime import timedelta
    from sqlalchemy import func, text
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    with Session(get_engine()) as s:
        stmt = (
            select(
                IpTraceLog.host,
                func.count().label("cnt"),
                func.count(func.distinct(IpTraceLog.nick)).label("nick_cnt"),
            )
            .where(IpTraceLog.ts >= cutoff)
            .where(IpTraceLog.host != "")
            .group_by(IpTraceLog.host)
            .order_by(text("cnt DESC"))
            .limit(limit)
        )
        return [
            {"host": r[0], "events": r[1], "distinct_nicks": r[2]}
            for r in s.exec(stmt)
        ]


# =============================================================================
# IpAnnotation helpers
# =============================================================================


def list_ip_annotations(active_only: bool = False) -> list[IpAnnotation]:
    with Session(get_engine()) as s:
        stmt = select(IpAnnotation).order_by(IpAnnotation.added_at.desc())
        if active_only:
            stmt = stmt.where(IpAnnotation.active == True)  # noqa: E712
        return list(s.exec(stmt))


def add_ip_annotation(target: str, label: str = "note", note: str = "",
                      added_by: str = "webui", source: str = "manual") -> IpAnnotation:
    with Session(get_engine()) as s:
        row = IpAnnotation(target=target.strip(), label=label.strip(),
                           note=note.strip(), added_by=added_by, source=source)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete_ip_annotation(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(IpAnnotation, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def lookup_ip_annotations(ip: Optional[str], host: Optional[str]) -> list[IpAnnotation]:
    """Retourne annotations qui matchent :
       - target == ip (exact), ou
       - target == host (exact), ou
       - target avec wildcards `*`/`?` matché fnmatch contre host.
       Filtre les inactives et les expirées.
    """
    import fnmatch
    now = datetime.utcnow()
    with Session(get_engine()) as s:
        rows = list(s.exec(
            select(IpAnnotation).where(IpAnnotation.active == True)  # noqa: E712
        ))
    matches = []
    for r in rows:
        if r.expires_at and r.expires_at < now:
            continue
        t = r.target
        if ip and t == ip:
            matches.append(r)
            continue
        if host:
            if t == host:
                matches.append(r)
            elif ("*" in t or "?" in t) and fnmatch.fnmatch(host.lower(), t.lower()):
                matches.append(r)
    return matches


# =============================================================================
# CustomCommand helpers
# =============================================================================


def list_custom_commands(active_only: bool = False) -> list[CustomCommand]:
    with Session(get_engine()) as s:
        stmt = select(CustomCommand).order_by(CustomCommand.name)
        if active_only:
            stmt = stmt.where(CustomCommand.active == True)  # noqa: E712
        return list(s.exec(stmt))


def get_custom_command(row_id: int) -> Optional[CustomCommand]:
    with Session(get_engine()) as s:
        return s.get(CustomCommand, row_id)


def add_custom_command(**fields) -> CustomCommand:
    with Session(get_engine()) as s:
        row = CustomCommand(**fields)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def update_custom_command(row_id: int, **fields) -> Optional[CustomCommand]:
    with Session(get_engine()) as s:
        row = s.get(CustomCommand, row_id)
        if row is None:
            return None
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete_custom_command(row_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(CustomCommand, row_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


# =============================================================================
# Recipes-support queries (utilisées par app.custom_commands.RECIPES)
# =============================================================================


def count_incidents_for_target(ip: Optional[str], host: Optional[str],
                                since_days: int = 90) -> dict:
    """Agrège IncidentHistory pour une IP ou un host donné.

    Retourne {total, by_type: {type: n}, last_ts, severity_max}.
    """
    from datetime import timedelta
    from collections import Counter
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    with Session(get_engine()) as s:
        stmt = select(IncidentHistory).where(IncidentHistory.ts >= cutoff)
        if ip and host:
            stmt = stmt.where(
                (IncidentHistory.ip == ip) | (IncidentHistory.host == host)
            )
        elif ip:
            stmt = stmt.where(IncidentHistory.ip == ip)
        elif host:
            stmt = stmt.where(IncidentHistory.host == host)
        else:
            return {"total": 0, "by_type": {}, "last_ts": None, "severity_max": 0}
        rows = list(s.exec(stmt))
    counter = Counter(r.incident_type or "unknown" for r in rows)
    last_ts = max((r.ts for r in rows), default=None)
    sev_max = max((r.severity for r in rows), default=0)
    return {
        "total": len(rows),
        "by_type": dict(counter),
        "last_ts": last_ts,
        "severity_max": sev_max,
    }


def active_ban_for_target(ip: Optional[str], host: Optional[str]) -> Optional[BannedEntity]:
    """Retourne le premier ban actif matché sur ip ou host, sinon None."""
    with Session(get_engine()) as s:
        stmt = select(BannedEntity).where(BannedEntity.active == True)  # noqa: E712
        if ip and host:
            stmt = stmt.where(
                (BannedEntity.ip == ip) | (BannedEntity.host == host)
            )
        elif ip:
            stmt = stmt.where(BannedEntity.ip == ip)
        elif host:
            stmt = stmt.where(BannedEntity.host == host)
        else:
            return None
        return s.exec(stmt).first()


def top_flaggers_for_channel(channel: Optional[str], limit: int = 5,
                              since_days: int = 30) -> list[dict]:
    """Top nicks avec le plus d'incidents dans la fenêtre, optionnellement
    filtré par chan. Retourne [{nick, count, types}]."""
    from datetime import timedelta
    from collections import defaultdict, Counter
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    with Session(get_engine()) as s:
        stmt = select(IncidentHistory).where(IncidentHistory.ts >= cutoff)
        if channel:
            stmt = stmt.where(IncidentHistory.channel == channel)
        rows = list(s.exec(stmt))
    per_nick: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        nick = (r.nick or "").strip() or "(anon)"
        per_nick[nick][r.incident_type or "unknown"] += 1
    ranked = sorted(per_nick.items(),
                    key=lambda kv: sum(kv[1].values()), reverse=True)[:limit]
    return [
        {"nick": nick, "count": sum(cnts.values()), "types": dict(cnts)}
        for nick, cnts in ranked
    ]


# --------------------------------------------------------------------------
# UrlIntelConfig helpers (singleton id=1, auto-create)
# --------------------------------------------------------------------------


def get_url_intel_config() -> "UrlIntelConfig":
    with Session(get_engine()) as s:
        row = s.get(UrlIntelConfig, 1)
        if row is None:
            row = UrlIntelConfig(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_url_intel_config(**fields) -> "UrlIntelConfig":
    with Session(get_engine()) as s:
        row = s.get(UrlIntelConfig, 1)
        if row is None:
            row = UrlIntelConfig(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row


# --------------------------------------------------------------------------
# AdminAuth helpers (singleton id=1, auto-create)
# --------------------------------------------------------------------------


def get_admin_auth() -> "AdminAuth":
    with Session(get_engine()) as s:
        row = s.get(AdminAuth, 1)
        if row is None:
            row = AdminAuth(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_admin_auth(**fields) -> "AdminAuth":
    with Session(get_engine()) as s:
        row = s.get(AdminAuth, 1)
        if row is None:
            row = AdminAuth(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_at = datetime.utcnow()
        s.commit()
        s.refresh(row)
        return row


# --------------------------------------------------------------------------
# PmMemory + PmMessage helpers (mémoire PV persistante par nick.lower())
# --------------------------------------------------------------------------


def get_pm_memory(nick_key: str) -> Optional["PmMemory"]:
    """Récupère l'entrée PmMemory pour ce nick. None si jamais initialisée."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(PmMemory).where(PmMemory.nick_key == nick_key)))
        return rows[0] if rows else None


def set_pm_memory_enabled(nick_key: str, enabled: bool) -> "PmMemory":
    """Crée l'entrée si absente, sinon toggle le flag enabled. Idempotent."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(PmMemory).where(PmMemory.nick_key == nick_key)))
        if rows:
            row = rows[0]
        else:
            row = PmMemory(nick_key=nick_key)
            s.add(row)
        row.enabled = enabled
        row.last_active = datetime.utcnow()
        s.commit()
        s.refresh(row)
        return row


def touch_pm_memory(nick_key: str) -> None:
    """Update last_active sans changer enabled. Fire-and-forget côté handler."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(PmMemory).where(PmMemory.nick_key == nick_key)))
        if rows:
            rows[0].last_active = datetime.utcnow()
            s.commit()


def list_pm_messages(nick_key: str, limit: Optional[int] = None) -> list["PmMessage"]:
    """Retourne les messages du plus vieux au plus récent (order chronologique).

    `limit` cap le retour aux N derniers (par ts). Utile pour builder l'history
    à injecter dans generate_reply.
    """
    with Session(get_engine()) as s:
        stmt = select(PmMessage).where(PmMessage.nick_key == nick_key).order_by(PmMessage.ts.desc())
        if limit:
            stmt = stmt.limit(limit)
        rows = list(s.exec(stmt))
    return list(reversed(rows))


def add_pm_message(nick_key: str, role: str, content: str, window: int = 100) -> "PmMessage":
    """Append + rolling purge : garde uniquement les `window` derniers messages
    pour ce nick_key. Le `window` par défaut = 100 (bufffer supérieur à la
    fenêtre LLM de 30 pour absorber les modifs de config sans perte immédiate).
    """
    with Session(get_engine()) as s:
        msg = PmMessage(nick_key=nick_key, role=role, content=content)
        s.add(msg)
        s.commit()
        s.refresh(msg)
        # Purge : garde les N derniers (par ts DESC), delete le reste
        all_rows = list(s.exec(
            select(PmMessage).where(PmMessage.nick_key == nick_key).order_by(PmMessage.ts.desc())
        ))
        if len(all_rows) > window:
            for extra in all_rows[window:]:
                s.delete(extra)
            s.commit()
        return msg


def delete_pm_messages(nick_key: str) -> int:
    """Wipe l'historique pour ce nick. Retourne le nombre supprimé."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(PmMessage).where(PmMessage.nick_key == nick_key)))
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def count_pm_messages(nick_key: str) -> int:
    with Session(get_engine()) as s:
        rows = list(s.exec(select(PmMessage).where(PmMessage.nick_key == nick_key)))
        return len(rows)


# --------------------------------------------------------------------------
# ImageGenConfig helpers (singleton id=1, auto-create)
# --------------------------------------------------------------------------


def get_image_gen_config() -> "ImageGenConfig":
    with Session(get_engine()) as s:
        row = s.get(ImageGenConfig, 1)
        if row is None:
            row = ImageGenConfig(id=1)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def update_image_gen_config(**fields) -> "ImageGenConfig":
    with Session(get_engine()) as s:
        row = s.get(ImageGenConfig, 1)
        if row is None:
            row = ImageGenConfig(id=1)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
        s.refresh(row)
        return row
