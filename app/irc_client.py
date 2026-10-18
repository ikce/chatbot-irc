from __future__ import annotations

import asyncio
import logging
import re

import pydle

from . import autonomy, bible_handler, farewell, ignore, custom_commands, image_gen, memory, pm_memory, url_intel, user_admin
from .db import (
    get_channel_policy,
    get_irc_config,
    log_message,
)
from .event_bus import Event, bus
from .llm import generate_reply, tool_ids_for
from .lookup import try_answer as try_lookup_answer
from .moderation import ban_engine, message_pipeline, reputation, tracelog
from .throttle import throttle
from .triggers import evaluate

log = logging.getLogger("chatbot.irc")


# Parseur pour les messages du pseudo-user ZNC *buffextras injectés au playback.
# Format standard :
#   [HH:MM:SS] *** nick (ident@host) has joined
#   [HH:MM:SS] *** nick (ident@host) has parted (reason)
#   [HH:MM:SS] *** nick (ident@host) has quit (reason)
#   [HH:MM:SS] *** Joins: nick (ident@host)          # variante ancienne
#   [HH:MM:SS] *** nick has changed his/her nickname to newnick
_BUFFEXTRAS_JOIN_PART_QUIT = re.compile(
    r"^\s*\[?[^\]]*\]?\s*\*{2,3}\s+"
    r"(?:(?P<v_action>Joins|Parts|Quits):\s+)?"
    r"(?P<nick>\S+)\s+"
    r"\((?P<ident>[^@()]+)@(?P<host>[^)]+)\)"
    r"(?:\s+has\s+(?P<action>joined|parted|left|quit))?"
    r"(?:\s*[:\-]?\s*\(?(?P<reason>.*?)\)?)?\s*$",
    re.IGNORECASE,
)
_BUFFEXTRAS_NICK = re.compile(
    r"^\s*\[?[^\]]*\]?\s*\*{2,3}\s+(?P<old>\S+)\s+is\s+now\s+known\s+as\s+(?P<new>\S+)\s*$",
    re.IGNORECASE,
)


def _parse_buffextras(text: str) -> dict | None:
    """Extrait un event structuré depuis une ligne *buffextras. None si non-parsable."""
    m = _BUFFEXTRAS_NICK.match(text)
    if m:
        return {
            "event_type": "nick_change",
            "nick": m.group("new"),
            "ident": "",
            "host": "",
            "context": f"was:{m.group('old')}",
        }
    m = _BUFFEXTRAS_JOIN_PART_QUIT.match(text)
    if not m:
        return None
    verb = (m.group("action") or m.group("v_action") or "").lower()
    if verb in ("joined", "joins"):
        et = "join"
    elif verb in ("parted", "left", "parts"):
        et = "part"
    elif verb in ("quit", "quits"):
        et = "quit"
    else:
        return None
    return {
        "event_type": et,
        "nick": m.group("nick"),
        "ident": (m.group("ident") or "").lstrip("~"),
        "host": m.group("host") or "",
        "context": (m.group("reason") or "").strip(),
    }


def _user_parts(client: "ChatterBot", nick: str) -> tuple[str, str]:
    """Extrait (ident, host) depuis pydle self.users, avec fallback vide.

    pydle peuple self.users[nick] au fil des JOIN/WHOIS/etc. Certains champs
    peuvent être absents (JOIN sans WHO extended, par ex.) — on retourne "" si
    manquant plutôt que de crasher.
    """
    info = getattr(client, "users", {}).get(nick, {}) or {}
    ident = (info.get("username") or "").lstrip(":")
    host = info.get("hostname") or ""
    return ident, host


class ChatterBot(pydle.Client):
    """Client IRC pydle branché sur le bus d'événements."""

    def _ensure_chan_modes(self, channel: str) -> dict:
        """Retourne (en le créant si besoin) le dict modes du chan côté pydle.
        Permet à is_bot_op() et compagnie de lire client.channels[chan]['modes']['o']
        même quand pydle ne peuple pas la structure (parser cassé sur Undernet).

        CRITIQUE : si on doit créer le chan, utiliser _create_channel() de pydle
        pour matcher exactement sa struct interne (users:set, topic, banlist, ...).
        Sinon, le prochain on_raw_join de pydle crash avec KeyError:'users'.
        """
        chans = getattr(self, "channels", None)
        if chans is None:
            return {}
        if channel not in chans:
            creator = getattr(self, "_create_channel", None)
            if callable(creator):
                try:
                    creator(channel)
                except Exception as e:  # noqa: BLE001
                    log.warning("_create_channel(%s) failed, fallback bare dict: %s", channel, e)
                    chans[channel] = self._blank_channel_dict()
            else:
                chans[channel] = self._blank_channel_dict()
        chan_info = chans[channel]
        modes = chan_info.setdefault("modes", {})
        for k in ("o", "v", "h"):
            modes.setdefault(k, [])
        return modes

    @staticmethod
    def _blank_channel_dict() -> dict:
        """Fallback qui reproduit la shape attendue par pydle rfc1459."""
        return {
            "users": set(),
            "modes": {},
            "topic": None,
            "topic_by": None,
            "topic_set": None,
            "created": None,
            "password": None,
            "banlist": None,
            "public": True,
        }

    async def on_raw_352(self, message) -> None:
        """RPL_WHOREPLY — parser custom.

        Pydle v1.2 log "Unknown command: 352" sur Undernet → self.users pas peuplé,
        modes du chan pas peuplés non plus. On absorbe manuellement.

        Format : <me> <chan> <user> <host> <server> <nick> <flags> :<hop> <realname>
        flags typiques : H=Here G=Gone, suffixé @ (op), + (voice), % (halfop), * (ircop)
        """
        try:
            params = list(getattr(message, "params", []) or [])
            if len(params) >= 7:
                _me, chan, user, host, _server, nick, flags = params[:7]
                u = self.users.setdefault(nick, {"nickname": nick})
                u["username"] = (user or "").lstrip("~")
                u["hostname"] = host or ""

                # Enregistrer les modes chan pour ce nick → indispensable pour is_bot_op()
                if chan and chan.startswith(("#", "&", "+", "!")):
                    modes = self._ensure_chan_modes(chan)
                    flags = flags or ""
                    if "@" in flags and nick not in modes["o"]:
                        modes["o"].append(nick)
                    if "+" in flags and nick not in modes["v"]:
                        modes["v"].append(nick)
                    if "%" in flags and nick not in modes["h"]:
                        modes["h"].append(nick)
        except Exception as e:  # noqa: BLE001
            log.warning("on_raw_352 parse failed: %s (params=%r)", e, message)

    async def on_raw_353(self, message) -> None:
        """RPL_NAMREPLY — parser custom.

        Pydle v1.2 marque le 353 Undernet comme "strictly invalid". On extrait
        les nicks + leurs prefixes (@/+/%) et on peuple channels[chan]['modes'].

        Format : <me> <chan_type> <chan> :<[@+%]nick> <[@+%]nick> ...
        """
        try:
            params = list(getattr(message, "params", []) or [])
            if len(params) < 4:
                return
            chan = params[2]
            names_blob = params[-1] or ""
            if not chan or not chan.startswith(("#", "&", "+", "!")):
                return
            modes = self._ensure_chan_modes(chan)
            for raw in names_blob.split():
                # Prefix : @ op, + voice, % halfop, & admin, ~ owner (dépend du server)
                op = raw.startswith("@") or raw.startswith("&") or raw.startswith("~")
                voice = raw.startswith("+")
                halfop = raw.startswith("%")
                nick = raw.lstrip("@+%&~!")
                if not nick:
                    continue
                if nick not in self.users:
                    self.users[nick] = {"nickname": nick}
                if op and nick not in modes["o"]:
                    modes["o"].append(nick)
                if voice and nick not in modes["v"]:
                    modes["v"].append(nick)
                if halfop and nick not in modes["h"]:
                    modes["h"].append(nick)
        except Exception as e:  # noqa: BLE001
            log.warning("on_raw_353 parse failed: %s (msg=%r)", e, message)

    async def on_raw_375(self, message) -> None:
        """RPL_MOTDSTART — init self.motd = '' avant que le contenu arrive.
        Certains serveurs (Undernet vu via ZNC playback) skip le 375 et envoient
        direct un 376 → sans ce garde on_raw_376 crash en NoneType += str.
        """
        if getattr(self, "motd", None) is None:
            self.motd = ""
        try:
            await super().on_raw_375(message)
        except Exception as e:  # noqa: BLE001
            log.warning("on_raw_375 super() failed: %s", e)

    async def on_raw_372(self, message) -> None:
        """RPL_MOTD — ligne du MOTD. Même garde qu'au 375 par sécurité."""
        if getattr(self, "motd", None) is None:
            self.motd = ""
        try:
            await super().on_raw_372(message)
        except Exception as e:  # noqa: BLE001
            log.warning("on_raw_372 super() failed: %s", e)

    async def on_raw_376(self, message) -> None:
        """RPL_ENDOFMOTD — pydle fait `self.motd += params[1] + '\\n'`.
        Sur Undernet via ZNC, 375/372 n'arrivent pas toujours → self.motd=None
        → TypeError. On garantit motd initialisé, on tolère params[1] None.
        """
        if getattr(self, "motd", None) is None:
            self.motd = ""
        try:
            params = list(getattr(message, "params", []) or [])
            if len(params) >= 2 and params[1] is None:
                # Rendre le message safe pour le parent : remplacer None par ""
                params[1] = ""
                message.params = params
            await super().on_raw_376(message)
        except Exception as e:  # noqa: BLE001
            log.warning("on_raw_376 super() failed: %s", e)
            # On force _end_motd si dispo, sinon on avale silencieusement
            end = getattr(self, "_end_motd", None)
            if callable(end):
                try:
                    end()
                except Exception:  # noqa: BLE001
                    pass

    async def on_connect(self) -> None:
        await super().on_connect()
        cfg = get_irc_config()
        wanted = [c.strip() for c in cfg.channels.split(",") if c.strip()]

        # ZNC pousse son playback (JOIN des canaux déjà en session) juste
        # après le RPL_WELCOME. On lui laisse 5s pour peupler self.channels
        # avant de décider quels JOIN forcer nous-mêmes.
        await bus.publish(Event(type="system", payload={
            "message": "connected, waiting 5s for ZNC playback..."
        }))
        await asyncio.sleep(5)

        already_on = set(getattr(self, "channels", {}).keys())
        to_join = [ch for ch in wanted if ch not in already_on]

        for ch in to_join:
            try:
                await self.join(ch)
            except Exception as e:  # noqa: BLE001
                await bus.publish(Event(type="error", payload={
                    "message": f"JOIN {ch} rejected: {e}"
                }))

        # WHO sur les chans hérités du playback ZNC : on n'a pas vu de vrai JOIN,
        # donc self.users[nick] est vide pour les users déjà présents. WHO force
        # le serveur à envoyer les RPL_WHOREPLY (352) qu'on parse dans on_raw_352.
        # Pour les chans force-join ci-dessus, on_join(self) déclenchera son propre WHO.
        for ch in already_on:
            try:
                await self.rawmsg("WHO", ch)
            except Exception as e:  # noqa: BLE001
                log.warning("WHO %s (playback) failed: %s", ch, e)

        await bus.publish(Event(type="system", payload={
            "message": (
                f"channels ready — via ZNC playback: {sorted(already_on)}, "
                f"forced JOIN: {to_join}"
            )
        }))

    async def on_join(self, channel: str, user: str) -> None:
        # Utile pour voir dans la console live quand ZNC pousse un playback JOIN
        if user == self.nickname:
            await bus.publish(Event(type="system", payload={
                "message": f"now on {channel}"
            }))
            # WHO explicite : le serveur va envoyer un RPL_WHOREPLY (352) par
            # user présent, absorbé par on_raw_352. Sans ça, self.users reste
            # vide pour les users déjà là avant nous.
            try:
                await self.rawmsg("WHO", channel)
            except Exception as e:  # noqa: BLE001
                log.warning("WHO %s (self-join) failed: %s", channel, e)
            return

        # Tracelog observation (autre user rejoint le chan)
        ident, host = _user_parts(self, user)
        await tracelog.record(
            event_type="join",
            nick=user,
            ident=ident,
            host=host,
            channel=channel,
        )

        # Réputation (Phase 2) — check async si activé sur ce chan
        pol = get_channel_policy(channel)
        if pol and pol.reputation_check_enabled and pol.join_check_enabled:
            asyncio.create_task(
                _reputation_check_and_notify(self, user, ident, host, channel, pol),
                name=f"rep_check:{user}:{channel}",
            )

    async def on_part(self, channel: str, user: str, message: str = "") -> None:
        if user == self.nickname:
            return
        ident, host = _user_parts(self, user)
        await tracelog.record(
            event_type="part",
            nick=user,
            ident=ident,
            host=host,
            channel=channel,
            context=message or "",
        )
        # Le part message est un vecteur classique de diffamation ("adieu tous,
        # jean-truc habite au 132..."). On passe le contexte dans le pipeline si
        # la policy du chan a la modération activée.
        if message:
            pol = get_channel_policy(channel)
            if pol and (pol.llm_moderation_enabled or pol.pattern_detection_enabled):
                asyncio.create_task(
                    message_pipeline.process_message(
                        nick=user, ident=ident, host=host, channel=channel,
                        message=message, client=self, policy=pol,
                    ),
                    name=f"mod_pipeline_part:{user}:{channel}",
                )

    async def on_quit(self, user: str, message: str = "") -> None:
        if user == self.nickname:
            return
        # Le user va disparaître de self.users après on_quit — on grab avant
        ident, host = _user_parts(self, user)
        await tracelog.record(
            event_type="quit",
            nick=user,
            ident=ident,
            host=host,
            channel="",  # QUIT n'est pas per-chan
            context=message or "",
        )
        # Le quit message est visible dans TOUS les chans où le user était.
        # On lance le pipeline pour chaque chan où on est ET où la policy est ON.
        # Attention : self.channels peut ne plus contenir user à ce stade — on
        # itère les policies actives et on scanne dans chaque chan.
        if message:
            our_chans = list(getattr(self, "channels", {}).keys())
            for ch in our_chans:
                pol = get_channel_policy(ch)
                if pol and (pol.llm_moderation_enabled or pol.pattern_detection_enabled):
                    asyncio.create_task(
                        message_pipeline.process_message(
                            nick=user, ident=ident, host=host, channel=ch,
                            message=message, client=self, policy=pol,
                        ),
                        name=f"mod_pipeline_quit:{user}:{ch}",
                    )

    async def on_channel_notice(self, channel: str, user: str, message: str) -> None:
        """NOTICE reçue dans un channel — pydle n'appelle PAS on_message() pour
        ça (c'est un raw NOTICE, pas un PRIVMSG). Sans ce handler, les NOTICE
        étaient invisibles : pas de tracelog, pas de pipeline modération.
        Même traitement que on_part/on_quit : observation + modération si la
        policy du chan l'a activée. Pas de commandes bot ici — NOTICE n'est
        pas censé déclencher !commande côté convention IRC.
        """
        if user == self.nickname:
            return
        ident, host = _user_parts(self, user)
        await tracelog.record(
            event_type="notice",
            nick=user,
            ident=ident,
            host=host,
            channel=channel,
            context=message or "",
        )
        pol = get_channel_policy(channel)
        if pol and (pol.llm_moderation_enabled or pol.pattern_detection_enabled):
            asyncio.create_task(
                message_pipeline.process_message(
                    nick=user, ident=ident, host=host, channel=channel,
                    message=message, client=self, policy=pol,
                ),
                name=f"mod_pipeline_notice:{user}:{channel}",
            )

    async def on_nick_change(self, old: str, new: str) -> None:
        if new == self.nickname:
            return
        # nick_change : old est le nick avant, new après. self.users a probablement
        # déjà migré vers `new` selon le moment où pydle appelle le handler.
        ident, host = _user_parts(self, new)
        if not ident and not host:
            ident, host = _user_parts(self, old)
        await tracelog.record(
            event_type="nick_change",
            nick=new,
            ident=ident,
            host=host,
            channel="",
            context=f"was:{old}",
        )
        try:
            memory.note_nick_change(old, new, ident, host, memory.account_of(self, new))
        except Exception:  # noqa: BLE001
            log.exception("memory.note_nick_change failed")

    async def on_message(self, target: str, source: str, message: str) -> None:
        # Ignore ses propres messages
        if source == self.nickname:
            return
        # Ignore les server-notices non-channel
        channel = target if target.startswith("#") else source

        # ZNC playback : *buffextras injecte des faux PRIVMSG (join/part/quit
        # historiques) au moment où on se reconnecte. On les convertit en vrais
        # events tracelog et on SKIP tout le reste du pipeline (pas de log_message,
        # pas de modération, pas de LLM — c'est du contenu système).
        src_ident, src_host = _user_parts(self, source)
        if source == "*buffextras" or src_host == "znc.in":
            parsed = _parse_buffextras(message)
            if parsed and target.startswith(("#", "&", "+", "!")):
                await tracelog.record(
                    event_type=parsed["event_type"],
                    nick=parsed["nick"],
                    ident=parsed["ident"],
                    host=parsed["host"],
                    channel=target,
                    context=parsed["context"],
                )
            return

        log_message(channel=channel, nick=source, message=message, direction="in")

        # Tracelog observation
        ident, host = src_ident, src_host
        await tracelog.record(
            event_type="msg",
            nick=source,
            ident=ident,
            host=host,
            channel=channel,
            context=message,
        )

        # Pipeline modération Phase 3 (regex + LLM). Task background, jamais bloquant.
        pol = get_channel_policy(channel)
        if pol and (pol.llm_moderation_enabled or pol.pattern_detection_enabled):
            asyncio.create_task(
                message_pipeline.process_message(
                    nick=source, ident=ident, host=host, channel=channel,
                    message=message, client=self, policy=pol,
                ),
                name=f"mod_pipeline:{source}:{channel}",
            )

        # URL intel : fetch le titre HTML des URLs postées. Fire-and-forget,
        # jamais bloquant. Guardé par allow_url_fetch (backward-compat permissif).
        if "http" in message.lower():  # short-circuit cheap avant regex complet
            asyncio.create_task(
                url_intel.process_message(
                    self, source, ident, host, channel, message,
                ),
                name=f"url_intel:{source}:{channel}",
            )

        await bus.publish(Event(
            type="irc_msg",
            payload={"channel": channel, "nick": source, "message": message},
        ))

        # Commandes admin (!add user, !del user, !list users) — précédence
        # MAX pour ne pas être shadowées par un custom command au même nom.
        try:
            consumed = await user_admin.try_handle(
                self, source, ident, host, channel, message,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("user_admin crashed")
            await bus.publish(Event(type="error", payload={"message": f"user_admin: {e}"}))
            consumed = False
        if consumed:
            return

        # Image gen (`!img <prompt>`) — après admin, avant custom_commands
        # pour ne pas être shadowé par une custom command nommée "img".
        try:
            consumed = await image_gen.try_handle(
                self, source, ident, host, channel, message,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("image_gen crashed")
            await bus.publish(Event(type="error", payload={"message": f"image_gen: {e}"}))
            consumed = False
        if consumed:
            return

        # `!unignore <nick>` (admins) — lève un ignore automatique.
        try:
            consumed = await ignore.try_handle(self, source, ident, host, channel, message)
        except Exception as e:  # noqa: BLE001
            log.exception("ignore crashed")
            consumed = False
        if consumed:
            return

        # Mémoire long terme (`!mem list/add/forget/off/on/who/import`) — avant
        # custom_commands (pas de shadowing) et avant pm_memory (sinon `!mem`
        # partirait au LLM quand la mémoire PV est ON).
        try:
            consumed = await memory.try_handle(
                self, source, ident, host, channel, message,
                account=memory.account_of(self, source),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("memory crashed")
            await bus.publish(Event(type="error", payload={"message": f"memory: {e}"}))
            consumed = False
        if consumed:
            return

        # Commandes custom (!flag !ip !seen !host !topflag et user-défini)
        # AVANT bible_handler et AVANT trigger — première short-circuit surface.
        try:
            consumed = await custom_commands.try_handle(
                self, source, ident, host, channel, message,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("custom_commands crashed")
            await bus.publish(Event(type="error", payload={"message": f"cmd: {e}"}))
            consumed = False
        if consumed:
            return

        # Commande !bible / !bible-apo / !bible-meme — consomme le message
        # avant le pipeline trigger LLM classique. ACL séparée (BibleAcl).
        try:
            consumed = await bible_handler.try_handle(
                self, source, ident, host, channel, message,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("bible_handler crashed")
            await bus.publish(Event(type="error", payload={"message": f"bible: {e}"}))
            consumed = False
        if consumed:
            return

        # PM memory (`!pv` + chat PV avec history) — en PM uniquement, no-op en
        # channel. Après bible_handler pour laisser `!bible` marcher en PV.
        # Avant le trigger classique pour intercepter le chat PV en langage
        # naturel (bypass du mask+keyword quand mémoire ON).
        try:
            consumed = await pm_memory.try_handle(
                self, source, ident, host, channel, message,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("pm_memory crashed")
            await bus.publish(Event(type="error", payload={"message": f"pm_memory: {e}"}))
            consumed = False
        if consumed:
            return

        # Trigger check — evaluate() renvoie l'UserAcl actif qui matche
        # (mask + keyword ou mention/PV si keyword vide), ou None.
        from .db import get_llm_config
        llm_cfg = get_llm_config()
        is_pm = not target.startswith(("#", "&", "+", "!"))
        if ignore.is_ignored(source, ident, host):
            return  # ignore auto en cours : silence total côté LLM
        matched_user = evaluate(
            source, ident, host, message,
            bot_nick=self.nickname, is_pm=is_pm,
        )
        spontaneous = False
        saying_bye = False
        if matched_user is None and not is_pm:
            # Quelqu'un annonce son départ → au revoir (chans de farewell_channels).
            try:
                matched_user = await farewell.should_say_bye(
                    channel, source, ident, host, message,
                )
            except Exception:  # noqa: BLE001
                log.exception("farewell crashed")
                matched_user = None
            saying_bye = spontaneous = matched_user is not None
        if matched_user is None and not is_pm:
            # Pas sollicité : le bot peut décider d'intervenir (chans opt-in).
            try:
                matched_user = await autonomy.should_reply(
                    self.nickname, channel, source, ident, host, message,
                )
            except Exception:  # noqa: BLE001
                log.exception("autonomy crashed")
                matched_user = None
            spontaneous = matched_user is not None
        if matched_user is None:
            return

        # UserAcl guard : deny-by-default. Un user doit avoir is_admin OU
        # allow_llm_chat pour trigger le LLM (feature coûteuse GPU + tokens).
        # Refus silencieux + log admin only via event bus.
        is_admin = bool(matched_user.is_admin)
        if not is_admin and not matched_user.allow_llm_chat:
            await bus.publish(Event(type="system", payload={
                "message": f"[llm_chat] refusé (pas d'ACL allow_llm_chat) — {source}!{ident}@{host}"
            }))
            return

        if not throttle.can_respond(source, llm_cfg.throttle_global_sec, llm_cfg.throttle_per_nick_sec):
            await bus.publish(Event(type="system", payload={
                "message": f"throttled (nick={source}, mask={matched_user.mask})"
            }))
            return

        throttle.mark(source)

        # Même question en boucle → ignore direct, sans appel LLM
        if ignore.is_repetition(source, message):
            minutes = ignore.ignore(source, ident, host)
            if minutes:
                await self._announce_ignore(channel, source, minutes, "répétition")
                return

        # Étape 1 : tenter une réponse "query intent" (lecture DB modération)
        # via 2 appels LLM (extract JSON + humanise). Deny-by-default : requiert
        # is_admin ou allow_query_intent (feature coûteuse = double appel LLM).
        # Sur skip → fall-through vers generate_reply classique.
        reply: str = ""
        allow_qi = (is_admin or matched_user.allow_query_intent) and not spontaneous
        lookup_reply = None
        if allow_qi:
            try:
                lookup_reply = await try_lookup_answer(source, message)
            except Exception as e:  # noqa: BLE001
                log.exception("lookup crashed")
                await bus.publish(Event(type="error", payload={"message": f"lookup error: {e}"}))
                lookup_reply = None
        else:
            await bus.publish(Event(type="system", payload={
                "message": f"[query_intent] skip (allow_query_intent=OFF) — {source}!{ident}@{host}"
            }))

        identity = None
        if lookup_reply:
            reply = lookup_reply
        else:
            # web_search effectif = kill-switch global AND opt-in per-user
            effective_ws = bool(llm_cfg.web_search_enabled and matched_user.allow_web_search)
            # Mémoire : qui parle (compte > ident@host > nick), ses faits
            # (privés en PV seulement) et les derniers messages du chan/PV.
            try:
                identity = memory.resolve(source, ident, host, memory.account_of(self, source))
            except Exception:  # noqa: BLE001
                log.exception("memory.resolve failed")
                identity = None
            ctx = memory.build_context(identity, source, channel, is_pm)
            try:
                reply = await generate_reply(
                    message, source, web_search=effective_ws,
                    memory_facts=ctx.facts, aliases=ctx.aliases,
                    recent_lines=ctx.recent, is_pm=is_pm, spontaneous=spontaneous,
                    farewell=saying_bye,
                    tool_ids=None if spontaneous else tool_ids_for(
                        message, bool(getattr(matched_user, "allow_url_fetch", False)),
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.exception("LLM failed")
                await bus.publish(Event(type="error", payload={"message": f"LLM error: {e}"}))
                return

        reply, wants_ignore = ignore.split_tag(reply)
        if not reply:
            return

        # IRC : envoyer ligne par ligne, jamais > ~450 chars (marge protocole)
        for line in _chunk(reply, 400):
            await self.message(channel, line)
            log_message(channel=channel, nick=self.nickname, message=line, direction="out")
            await bus.publish(Event(type="bot_reply", payload={
                "channel": channel, "nick": self.nickname, "message": line,
            }))

        if wants_ignore:
            minutes = ignore.ignore(source, ident, host)
            if minutes:
                await self._announce_ignore(channel, source, minutes, "décision du LLM")
        if not is_pm:
            autonomy.note_bot_reply(channel, source)
        try:
            memory.record_exchange(identity, source, message, reply, is_pm)
        except Exception:  # noqa: BLE001
            log.exception("memory.record_exchange failed")

    async def _announce_ignore(self, channel: str, nick: str, minutes: int, why: str) -> None:
        await self.message(channel, f"{nick}: je t'ignore pendant {minutes} min. 🙄")
        log_message(channel=channel, nick=self.nickname,
                    message=f"(ignore {nick} {minutes} min)", direction="out")
        await bus.publish(Event(type="system", payload={
            "message": f"[ignore] {nick} ignoré {minutes} min ({why})"
        }))

    async def say_manual(self, channel: str, message: str) -> None:
        """Appelé depuis la WebUI (console) pour parler comme le bot."""
        for line in _chunk(message, 400):
            await self.message(channel, line)
            log_message(channel=channel, nick=self.nickname, message=line, direction="out")
            await bus.publish(Event(type="bot_reply", payload={
                "channel": channel, "nick": self.nickname, "message": line,
            }))


def _chunk(text: str, size: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _reputation_check_and_notify(
    client: "ChatterBot",
    nick: str,
    ident: str,
    host: str,
    channel: str,
    pol,
) -> None:
    """Task background : check réputation + éventuel ban (P4).

    Le ban_engine respecte lui-même `pol.auto_ban_enabled` + `pol.dry_run` :
        - auto_ban_enabled=False → observation seule
        - dry_run=True → BannedEntity enregistré inactif, aucun ordre IRC
    """
    try:
        verdict = await reputation.check_user(nick, ident, host, channel, policy=pol)
    except Exception as e:
        log.exception("reputation.check_user failed for %s@%s: %s", nick, host, e)
        return

    if not verdict.suspect:
        return

    # Broadcast console live + notif optionnelle
    dry_tag = ""
    if not pol.auto_ban_enabled:
        dry_tag = " [flag — auto_ban_enabled=OFF]"
    elif pol.dry_run:
        dry_tag = " [DRY-RUN — ban simulé]"
    line = (
        f"[REPUTATION] {channel} {nick}!{ident}@{host} "
        f"IP={verdict.ip or '?'} score={verdict.score} "
        f"sources=[{', '.join(verdict.sources)}]{dry_tag}"
    )
    await bus.publish(Event(type="system", payload={"message": line}))

    # Notif channel (si op-alerts défini)
    if pol.notify_channel:
        try:
            for sub in _chunk(f"REPUTATION {nick}@{host} → {verdict.summary()} :: {' | '.join(verdict.reasons)}", 400):
                await client.message(pol.notify_channel, sub)
        except Exception:
            log.exception("notify_channel send failed")

    # Notif nick (DM op)
    if pol.notify_nick:
        try:
            for sub in _chunk(f"[{channel}] {nick}@{host} → {verdict.summary()}", 400):
                await client.message(pol.notify_nick, sub)
        except Exception:
            log.exception("notify_nick send failed")

    # P4 : émettre le ban si verdict.should_ban ET policy.auto_ban_enabled
    # (le ban_engine gère dry_run/dedup/rate-limit lui-même)
    if verdict.should_ban and pol.auto_ban_enabled:
        try:
            await ban_engine.emit_from_verdict(
                client=client, verdict=verdict,
                channel=channel, nick=nick, ident=ident, host=host,
                policy=pol,
            )
        except Exception:
            log.exception("ban_engine.emit_from_verdict failed for %s@%s", nick, host)


class IrcSupervisor:
    """Encapsule le client + reconnect loop."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._client: ChatterBot | None = None
        self._stop = asyncio.Event()

    def get_client(self) -> ChatterBot | None:
        return self._client

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                await self._client.disconnect(expected=True)
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            cfg = get_irc_config()
            try:
                self._client = ChatterBot(
                    cfg.nick,
                    realname=cfg.realname,
                    username=cfg.ident,
                )
                await bus.publish(Event(type="system", payload={
                    "message": f"connecting to {cfg.host}:{cfg.port} tls={cfg.tls}"
                }))
                await self._client.connect(
                    hostname=cfg.host,
                    port=cfg.port,
                    tls=cfg.tls,
                    tls_verify=False,
                    password=f"{cfg.user}:{cfg.password}" if cfg.user else cfg.password,
                )
                # pydle 1.x : connect() a déjà scheduled le reader interne.
                # Appeler handle_forever() ici = 2ème reader sur le même
                # socket → RuntimeError("readuntil() called while another
                # coroutine is already waiting"). On attend juste le
                # disconnect en polling léger.
                while getattr(self._client, "connected", False) and not self._stop.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await bus.publish(Event(type="error", payload={
                    "message": f"IRC crash: {e}",
                }))
                log.exception("IRC crashed, retry in 15s")

            if self._stop.is_set():
                break
            await asyncio.sleep(15)


supervisor = IrcSupervisor()
