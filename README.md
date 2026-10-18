# chatbot-irc

Bot IRC en Python (FastAPI + pydle) qui se connecte via un ZNC et répond avec un LLM servi par
Open WebUI (modèles locaux Ollama, et Grok en option). Il a une WebUI pour tout régler : IRC,
LLM, utilisateurs et droits, mémoire long terme, réponses spontanées, lecture des liens,
modération, console en direct.

- Architecture détaillée : [ARCHITECTURE.md](ARCHITECTURE.md)
- Pages de la WebUI et API REST : [deploy/README.md](deploy/README.md)

Aucun secret n'est dans le code : ils vivent dans `deploy/.env` (premier démarrage) et dans la
base SQLite, tous deux exclus de git.

---

## Déploiement, étape par étape

Ce guide part de zéro : Ollama, puis Open WebUI, puis (optionnel) Grok, puis le bot lui-même.
À la fin, le bot est sur IRC, il répond aux gens que tu autorises, et tu le pilotes depuis sa WebUI.

```
 IRC  <──>  ZNC  <──>  chatbot-irc (WebUI :54878)  ──>  Open WebUI  ──>  Ollama (modèles locaux)
                                                              └────────>  Grok / xAI (optionnel)
```

Le bot ne parle jamais directement à Ollama ni à Grok : il passe toujours par l'API d'Open WebUI
(`/api/chat/completions`) avec une clé API. C'est Open WebUI qui choisit où envoyer la requête
selon le nom du modèle.

### Sommaire

1. [Prérequis](#1-prérequis)
2. [Ollama](#2-ollama)
3. [Open WebUI](#3-open-webui)
4. [Grok dans Open WebUI (optionnel)](#4-grok-dans-open-webui-optionnel)
5. [Clé API Open WebUI pour le bot](#5-clé-api-open-webui-pour-le-bot)
6. [Tool « Web Fetch + Vision » (optionnel)](#6-tool--web-fetch--vision--optionnel)
7. [ZNC](#7-znc)
8. [Configurer le bot (.env)](#8-configurer-le-bot-env)
9. [Construire et lancer le bot](#9-construire-et-lancer-le-bot)
10. [Premier démarrage : compte admin](#10-premier-démarrage--compte-admin)
11. [Réglages dans la WebUI du bot](#11-réglages-dans-la-webui-du-bot)
12. [Mettre à jour et revenir en arrière](#12-mettre-à-jour-et-revenir-en-arrière)
13. [Déploiement sur TrueNAS (Custom App)](#13-déploiement-sur-truenas-custom-app)
14. [Dépannage](#14-dépannage)

---

### 1. Prérequis

- Une machine Linux (ou un NAS type TrueNAS SCALE) avec **Docker** et **Docker Compose**.
- Pour des modèles locaux corrects : un GPU (NVIDIA ou AMD) ou beaucoup de RAM. Sans GPU, prends
  de petits modèles (3B à 8B) ou utilise Grok.
- Un **bouncer ZNC** qui a déjà accès au réseau IRC (voir [étape 7](#7-znc)).
- `git` pour récupérer le code.

Ports utilisés par défaut :

| Service    | Port | Rôle |
|------------|------|------|
| Ollama     | 11434 | API des modèles locaux |
| Open WebUI | 3000  | Interface + API OpenAI-compatible |
| chatbot-irc | 54878 | WebUI du bot |
| ZNC        | 6697 (TLS) | Connexion IRC du bot |

### 2. Ollama

Ollama fait tourner les modèles en local.

**Installation directe (Linux) :**

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**Ou en Docker :**

```bash
docker run -d --name ollama --restart unless-stopped \
  -p 11434:11434 -v ollama:/root/.ollama \
  --gpus=all \            # retirer cette ligne sans GPU NVIDIA
  ollama/ollama
```

**Télécharger des modèles.** Il en faut au moins un pour discuter. Un petit modèle rapide est
pratique pour les tâches de fond du bot (juge des réponses spontanées, extraction de mémoire) :

```bash
ollama pull llama3.2:3b                 # petit et rapide (juge / extraction)
ollama pull mistral-nemo:12b            # discussion, si la machine suit
```

Vérifier : `curl http://localhost:11434/api/tags` doit lister les modèles.

> Si Ollama tourne sur une autre machine qu'Open WebUI, lance-le avec `OLLAMA_HOST=0.0.0.0`
> pour qu'il écoute sur le réseau.

### 3. Open WebUI

Open WebUI sert d'interface et, surtout, d'API unique pour le bot.

```bash
docker run -d --name open-webui --restart unless-stopped \
  -p 3000:8080 \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  --add-host=host.docker.internal:host-gateway \
  -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

- Remplace `OLLAMA_BASE_URL` par l'adresse de ton Ollama s'il est ailleurs
  (ex. `http://192.168.1.10:11434`).
- Le bot a été testé avec **Open WebUI 0.12.3**.

Ouvre `http://<machine>:3000`. Le **premier compte créé devient administrateur**.

Vérifier que les modèles Ollama apparaissent : **Admin Panel > Settings > Connections**
(connexion Ollama au vert), puis dans la liste des modèles d'une nouvelle discussion.

### 4. Grok dans Open WebUI (optionnel)

Pour utiliser Grok (xAI) comme modèle de discussion :

1. Crée une clé API sur la console xAI.
2. Dans Open WebUI : **Admin Panel > Settings > Connections > OpenAI API > +**
   - URL : `https://api.x.ai/v1`
   - Clé : ta clé xAI
3. Enregistre. Les modèles Grok (ex. `grok-4.7`) apparaissent dans la liste des modèles.

Le nom du modèle à mettre dans le bot est exactement l'ID affiché par Open WebUI.

### 5. Clé API Open WebUI pour le bot

Le bot s'authentifie auprès d'Open WebUI avec une clé API (Bearer).

1. **Admin Panel > Settings > General** : active **Enable API Keys** (si l'option est désactivée).
2. Avec le compte qui servira au bot : **Settings > Account > API Keys > Create new secret key**.
3. Copie la clé (`sk-...`). Elle va dans `LLM_API_KEY` à l'[étape 8](#8-configurer-le-bot-env).

> Le compte doit avoir accès aux modèles que le bot utilisera. Le plus simple : utiliser le compte admin,
> ou donner l'accès aux modèles au compte dédié dans **Admin Panel > Settings > Models**.

**Recherche web (optionnel).** Si tu veux que le bot puisse chercher sur le web, configure un
moteur dans **Admin Panel > Settings > Web Search**. Le bot l'active ensuite par requête
(voir [étape 11](#11-réglages-dans-la-webui-du-bot)).

### 6. Tool « Web Fetch + Vision » (optionnel)

Ce tool permet au bot de **lire les liens** qu'on lui donne (pages web et images). Les images sont
décrites par Grok, donc il faut une clé xAI pour cette partie.

1. Dans Open WebUI : **Workspace > Tools > +**.
2. Colle le contenu de [`openwebui/web_fetch_vision.py`](openwebui/web_fetch_vision.py).
3. Donne-lui un ID simple, par exemple `web_fetch`, puis enregistre. **Note l'ID** : c'est lui
   qu'on met dans le bot.
4. Clique sur l'engrenage du tool (**Valves**) et renseigne :
   - `xai_api_key` : ta clé xAI (sans elle, seul le texte des pages est lu, pas les images) ;
   - `vision_model` : le modèle Grok avec vision (par défaut `grok-4.7`).

Inutile de toucher au mode « function calling » d'Open WebUI : le bot force lui-même le mode
compatible dans ses requêtes.

### 7. ZNC

Le bot se connecte à IRC **à travers un ZNC**, comme un client IRC normal.

1. Dans ZNC, crée un utilisateur pour le bot et un réseau (ex. `libera`) pointant vers le serveur IRC.
2. Si le réseau l'exige, configure l'identification NickServ dans ZNC (module `nickserv` ou `sasl`).
3. Note : hôte, port, TLS oui/non, utilisateur au format `utilisateur/réseau`, mot de passe.

### 8. Configurer le bot (.env)

```bash
git clone <url-du-dépôt> chatbot-irc
cd chatbot-irc
cp deploy/.env.example deploy/.env
nano deploy/.env
```

Variables principales :

| Variable | Exemple | Rôle |
|----------|---------|------|
| `APP_SECRET` | une longue chaîne aléatoire | Secret de l'application. Génère-le avec `openssl rand -hex 32`. |
| `ZNC_HOST` / `ZNC_PORT` / `ZNC_TLS` | `znc.maison.lan` / `6697` / `true` | Où joindre ZNC |
| `ZNC_USER` | `bot/libera` | Utilisateur ZNC au format `utilisateur/réseau` |
| `ZNC_PASS` | | Mot de passe ZNC |
| `ZNC_NICK` / `ZNC_IDENT` / `ZNC_REALNAME` | | Identité IRC du bot |
| `ZNC_CHANNELS` | `#lab,#autre` | Canaux à rejoindre, séparés par des virgules |
| `LLM_URL` | `http://192.168.1.10:3000` | **Racine** d'Open WebUI (le bot ajoute `/api/chat/completions`) |
| `LLM_API_KEY` | `sk-...` | Clé de l'[étape 5](#5-clé-api-open-webui-pour-le-bot) |
| `LLM_MODEL` | `grok-4.7` ou `mistral-nemo:12b` | ID exact du modèle dans Open WebUI |
| `LLM_SYSTEM_PROMPT` | | Personnalité du bot |
| `WEB_SEARCH_ENABLED` | `false` | Interrupteur général de la recherche web |

> Ces valeurs ne servent **qu'au premier démarrage** : le bot les copie dans sa base, puis tout se
> règle depuis sa WebUI. Changer le `.env` après coup n'a plus d'effet.
>
> `deploy/.env` contient des secrets : il est exclu de git par le `.gitignore`. Ne le commite jamais.

### 9. Construire et lancer le bot

Depuis la racine du dépôt :

```bash
cd deploy
docker compose up -d --build
docker compose logs -f
```

- L'image installe les versions exactes de `requirements.lock`. **Ne mets pas à jour sqlmodel
  au-delà de 0.0.44** : les versions 0.0.45 et plus font planter le bot au démarrage.
- La base SQLite est dans le volume `chatbot_data` (monté sur `/app/data`). Au premier démarrage,
  elle est **vide** : aucun log, aucun utilisateur. Le bot y ajoute seulement quelques valeurs par
  défaut (modèles de raison de ban, patterns de détection).
- La WebUI écoute sur `http://<machine>:54878/`.

### 10. Premier démarrage : compte admin

Au premier lancement, il n'existe aucun compte pour la WebUI du bot. Le bot écrit un code dans
ses logs :

```
Aucun compte admin WebUI. Ouvre /login et utilise le code de setup : 1A2B3C4D
```

1. Ouvre `http://<machine>:54878/login`.
2. Entre ce code, choisis un nom d'utilisateur et un mot de passe.

En cas d'oubli du mot de passe : `docker exec -it chatbot-irc python -m app.auth set-password`.

### 11. Réglages dans la WebUI du bot

Dans l'ordre :

1. **IRC / ZNC (`/`)** : vérifie la connexion, les canaux et le préfixe des commandes.
   Les changements IRC demandent un redémarrage du conteneur.
2. **Users (`/users`)** : **étape indispensable**. Le bot ne répond qu'aux personnes listées ici.
   Ajoute-toi avec ton masque IRC (ex. `*!*@ton.host`) et coche **Admin**. Pour les autres,
   coche les droits voulus :
   - **LLM chat** : peut parler au bot ;
   - **Web search** : peut déclencher une recherche web ;
   - **URL fetch** : le bot lit les liens qu'il envoie (aussi requis pour les admins) ;
   - Bible, Intel, commandes perso, génération d'images, requêtes sur la modération.
3. **LLM (`/llm`)** :
   - endpoint, clé, modèle et personnalité (repris du `.env`) ;
   - **Tools Open WebUI** : mets l'ID du tool de l'[étape 6](#6-tool--web-fetch--vision--optionnel)
     (ex. `web_fetch`). Vide = lecture des liens désactivée ;
   - **Web search** : interrupteur général (il faut aussi le droit par utilisateur) ;
   - **Mémoire long terme** : le bot retient des faits sur les gens (modèle d'extraction conseillé :
     un petit modèle local) ;
   - **Réponses spontanées** : canaux où le bot peut intervenir de lui-même (vide = jamais), et
     modèle « juge » qui décide s'il parle ;
   - **Au revoir spontané** et **ignore automatique** ;
   - **Aperçu des liens** : format des titres de pages et de vidéos YouTube.
4. **Modération (`/moderation`)** : tout est désactivé par défaut. Active canal par canal
   (traçage, whitelist, réputation, modération LLM, bans automatiques). Une clé AbuseIPDB est
   nécessaire pour la réputation AbuseIPDB.
5. **Console (`/console`)** : voir le flux IRC en direct et parler à la place du bot.
6. **Commandes (`/help`)** : la liste des commandes IRC, dont `!mem` pour la mémoire.

### 12. Mettre à jour et revenir en arrière

Avant chaque mise à jour, garde l'image actuelle sous un autre nom :

```bash
docker tag chatbot-irc:latest chatbot-irc:avant-maj
git pull
cd deploy && docker compose up -d --build
```

Retour arrière :

```bash
docker tag chatbot-irc:avant-maj chatbot-irc:latest
cd deploy && docker compose up -d --force-recreate
```

La base n'est pas touchée par une mise à jour : elle vit dans le volume, pas dans l'image.
Pour la sauvegarder : `docker cp chatbot-irc:/app/data ./sauvegarde-data`.

### 13. Déploiement sur TrueNAS (Custom App)

Sur TrueNAS SCALE, on peut faire tourner le bot comme **Custom App** plutôt qu'avec
`docker compose` à la main.

1. Clone le dépôt dans un dataset, par exemple `/mnt/<pool>/chatte-bot/src`.
2. Construis l'image (Docker demande `sudo` sur TrueNAS) :
   ```bash
   cd /mnt/<pool>/chatte-bot/src
   sudo docker build -t chatbot-irc:latest -f deploy/Dockerfile .
   ```
3. Crée un dossier pour la base : `/mnt/<pool>/chatte-bot/chatbot-data`
   (propriétaire UID 1000, l'utilisateur du conteneur).
4. **Apps > Discover Apps > Custom App** (ou installation via YAML), avec :
   - image `chatbot-irc`, tag `latest`, politique de pull : ne jamais tirer
     (l'image est locale) ;
   - port `54878` vers le port conteneur `8080` ;
   - montage : `/mnt/<pool>/chatte-bot/chatbot-data` vers `/app/data` ;
   - variables d'environnement : celles du `.env` (premier démarrage seulement).

**Mise à jour sur TrueNAS :**

```bash
cd /mnt/<pool>/chatte-bot/src
sudo docker tag chatbot-irc:latest chatbot-irc:avant-<changement>
git pull
sudo docker build -t chatbot-irc:latest -f deploy/Dockerfile .
```

Puis **Apps > (ton app) > Edit > Save** pour recréer le conteneur. Un simple redémarrage garde
l'ancienne image.

> ⚠️ Sur un NAS où le bot tourne déjà en Custom App, ne lance **pas** `docker compose up` depuis
> `deploy/` : ça créerait un second bot, avec une base vide, sur le même pseudo IRC.

### 14. Dépannage

| Symptôme | Cause probable |
|----------|----------------|
| Le bot plante au démarrage avec une erreur sur les dates (`datetime`) | sqlmodel ≥ 0.0.45 installé. Reconstruis avec `requirements.lock`. |
| Le bot est sur IRC mais ne répond à personne | Personne dans `/users`, ou droit **LLM chat** non coché. |
| Erreur 401 / 403 vers Open WebUI | Clé API invalide, ou clés API désactivées dans Open WebUI. |
| Erreur « model not found » | `LLM_MODEL` ne correspond pas exactement à l'ID du modèle dans Open WebUI. |
| Le bot ignore les liens | Tool non renseigné dans `/llm`, ou l'utilisateur n'a pas le droit **URL fetch**. |
| Les images des liens ne sont pas décrites | Valve `xai_api_key` du tool vide. |
| La réponse du bot mentionne « Task » ou « citations » | Version d'Open WebUI différente : vérifie que le tool répond bien (testé en 0.12.3). |
| Changement dans `.env` sans effet | Normal : le `.env` ne sert qu'au premier démarrage. Règle depuis la WebUI. |
| Pas de compte, code de setup perdu | Redémarre le conteneur, un nouveau code apparaît dans les logs. |
