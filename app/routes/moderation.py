"""Routes WebUI pour la modération : policies par chan, whitelist, tracelog."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from ..db import (
    ChannelPolicy,
    add_ban_template,
    add_pattern_rule,
    add_whitelist,
    clone_channel_policy,
    delete_ban_template,
    delete_channel_policy,
    delete_incident,
    delete_pattern_rule,
    delete_whitelist,
    get_ban,
    get_ban_engine_config,
    get_engine,
    get_llm_moderation_config,
    get_reputation_config,
    incident_type_counts,
    list_ban_templates,
    list_bans,
    list_channel_policies,
    list_incidents,
    list_llm_moderation_logs,
    list_pattern_rules,
    list_whitelists,
    mark_ban_inactive,
    purge_buffextras_noise,
    query_incidents,
    query_tracelog,
    rename_channel_policy,
    update_ban_engine_config,
    update_ban_template,
    update_llm_moderation_config,
    update_pattern_rule,
    update_reputation_config,
    upsert_channel_policy,
)
from ..irc_client import supervisor
from ..moderation import ban_engine as ban_engine_mod
from ..moderation import pattern_suggester
from ..moderation import patterns as patterns_mod
from ..moderation import spamhaus as spamhaus_mod
from ..moderation import tracelog as tracelog_mod
from ..moderation import whitelist as whitelist_mod
from ..moderation.dns_resolver import cache_stats as dns_cache_stats


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix="/moderation")

    # -------- Policies -------------------------------------------------------

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def page_policies(request: Request):
        policies = list_channel_policies()
        return templates.TemplateResponse(
            request=request,
            name="moderation_policies.html",
            context={
                "policies": policies,
                "active": "moderation",
                "sub_active": "policies",
                "queue_stats": tracelog_mod.queue_stats(),
                "dns_stats": dns_cache_stats(),
            },
        )

    @router.post("/policies/save", response_class=HTMLResponse)
    async def save_policy(request: Request):
        form = await request.form()
        channel = (form.get("channel") or "").strip()
        if not channel:
            return RedirectResponse(url="/moderation?err=missing_channel", status_code=303)

        def _bool(name: str) -> bool:
            return form.get(name) == "on"

        def _int(name: str, default: int) -> int:
            try:
                return int(form.get(name, default))
            except (TypeError, ValueError):
                return default

        raw_tpl = (form.get("ban_reason_template_id") or "").strip()
        try:
            tpl_id = int(raw_tpl) if raw_tpl else None
        except (TypeError, ValueError):
            tpl_id = None

        upsert_channel_policy(
            channel=channel,
            tracelog_enabled=_bool("tracelog_enabled"),
            whitelist_enforced=_bool("whitelist_enforced"),
            reputation_check_enabled=_bool("reputation_check_enabled"),
            llm_moderation_enabled=_bool("llm_moderation_enabled"),
            pattern_detection_enabled=_bool("pattern_detection_enabled"),
            auto_ban_enabled=_bool("auto_ban_enabled"),
            dry_run=_bool("dry_run"),
            join_check_enabled=_bool("join_check_enabled"),
            abuseipdb_threshold=_int("abuseipdb_threshold", 50),
            local_incident_threshold=_int("local_incident_threshold", 2),
            default_ban_duration_sec=_int("default_ban_duration_sec", 0),
            ban_reason_template_id=tpl_id,
            llm_model_for_moderation=(form.get("llm_model_for_moderation") or None),
            llm_model_for_chat=(form.get("llm_model_for_chat") or None),
            notify_channel=(form.get("notify_channel") or None),
            notify_nick=(form.get("notify_nick") or None),
        )
        return RedirectResponse(url="/moderation?saved=1", status_code=303)

    @router.post("/policies/new", response_class=HTMLResponse)
    async def new_policy(request: Request):
        """Ajoute une policy pour un chan qui n'était pas dans ZNC_CHANNELS au bootstrap.

        Si `clone_from` est fourni et non-vide, copie les settings de cette policy
        source ; sinon crée avec les defaults du modèle ChannelPolicy.
        """
        form = await request.form()
        channel = (form.get("channel") or "").strip()
        clone_from = (form.get("clone_from") or "").strip()
        if not channel:
            return RedirectResponse(url="/moderation?err=missing_channel", status_code=303)
        if clone_from:
            _, err = clone_channel_policy(source_channel=clone_from, target_channel=channel)
            if err:
                return RedirectResponse(url=f"/moderation?err={err}", status_code=303)
        else:
            upsert_channel_policy(channel=channel)
        return RedirectResponse(url="/moderation?saved=1", status_code=303)

    @router.post("/policies/delete", response_class=HTMLResponse)
    async def delete_policy(request: Request):
        """Supprime la ChannelPolicy d'un chan (historique tracelog/bans préservé)."""
        form = await request.form()
        channel = (form.get("channel") or "").strip()
        if channel:
            delete_channel_policy(channel)
        return RedirectResponse(url="/moderation?saved=1", status_code=303)

    @router.post("/policies/rename", response_class=HTMLResponse)
    async def rename_policy(request: Request):
        """Renomme la ChannelPolicy (utile pour corriger casse/typo). Ne touche
        pas aux rows historiques."""
        form = await request.form()
        old_channel = (form.get("channel") or "").strip()
        new_channel = (form.get("new_channel") or "").strip()
        ok, err = rename_channel_policy(old_channel=old_channel, new_channel=new_channel)
        if not ok:
            return RedirectResponse(url=f"/moderation?err={err}", status_code=303)
        return RedirectResponse(url="/moderation?saved=1", status_code=303)

    # -------- Whitelist -----------------------------------------------------

    @router.get("/whitelist", response_class=HTMLResponse)
    async def page_whitelist(request: Request):
        rows = list_whitelists(channel=None, active_only=False)
        policies = list_channel_policies()
        return templates.TemplateResponse(
            request=request,
            name="moderation_whitelist.html",
            context={
                "rows": rows,
                "policies": policies,
                "active": "moderation",
                "sub_active": "whitelist",
            },
        )

    @router.post("/whitelist/add", response_class=HTMLResponse)
    async def add_wl(request: Request):
        form = await request.form()
        mask = (form.get("mask") or "").strip()
        raw_chan = (form.get("channel") or "").strip()
        channel = raw_chan if raw_chan and raw_chan != "*" else None
        reason = (form.get("reason") or "").strip()
        added_by = (form.get("added_by") or "webui").strip()
        if mask:
            add_whitelist(mask=mask, channel=channel, reason=reason, added_by=added_by)
            whitelist_mod.refresh()
        return RedirectResponse(url="/moderation/whitelist", status_code=303)

    @router.post("/whitelist/{row_id}/delete", response_class=HTMLResponse)
    async def del_wl(row_id: int):
        delete_whitelist(row_id)
        whitelist_mod.refresh()
        return RedirectResponse(url="/moderation/whitelist", status_code=303)

    # -------- Tracelog -------------------------------------------------------

    @router.get("/tracelog", response_class=HTMLResponse)
    async def page_tracelog(
        request: Request,
        channel: Optional[str] = None,
        nick: Optional[str] = None,
        ip: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ):
        limit = max(10, min(limit, 1000))
        rows = query_tracelog(
            limit=limit,
            offset=offset,
            channel=channel or None,
            nick=nick or None,
            ip=ip or None,
            event_type=event_type or None,
        )
        policies = list_channel_policies()
        return templates.TemplateResponse(
            request=request,
            name="moderation_tracelog.html",
            context={
                "rows": rows,
                "policies": policies,
                "filters": {
                    "channel": channel or "",
                    "nick": nick or "",
                    "ip": ip or "",
                    "event_type": event_type or "",
                    "limit": limit,
                    "offset": offset,
                },
                "next_offset": offset + limit if len(rows) == limit else None,
                "prev_offset": max(0, offset - limit) if offset > 0 else None,
                "active": "moderation",
                "sub_active": "tracelog",
            },
        )

    # -------- Réputation ----------------------------------------------------

    @router.get("/reputation", response_class=HTMLResponse)
    async def page_reputation(request: Request):
        cfg = get_reputation_config()
        sh_stats = spamhaus_mod.stats()
        incidents = list_incidents(limit=100)
        # Masquer la clé (afficher les 4 derniers chars seulement)
        api_key_masked = ""
        if cfg.abuseipdb_api_key:
            k = cfg.abuseipdb_api_key
            api_key_masked = ("*" * max(0, len(k) - 4)) + k[-4:] if len(k) > 4 else ("*" * len(k))
        return templates.TemplateResponse(
            request=request,
            name="moderation_reputation.html",
            context={
                "cfg": cfg,
                "api_key_masked": api_key_masked,
                "spamhaus_stats": sh_stats,
                "incidents": incidents,
                "active": "moderation",
                "sub_active": "reputation",
            },
        )

    @router.post("/reputation/save", response_class=HTMLResponse)
    async def save_reputation(request: Request):
        form = await request.form()

        def _bool(name: str) -> bool:
            return form.get(name) == "on"

        def _int(name: str, default: int) -> int:
            try:
                return int(form.get(name, default))
            except (TypeError, ValueError):
                return default

        raw_key = (form.get("abuseipdb_api_key") or "").strip()
        # Si le champ contient uniquement des astérisques, garder l'ancienne clé
        fields = {
            "abuseipdb_enabled": _bool("abuseipdb_enabled"),
            "abuseipdb_max_age_days": _int("abuseipdb_max_age_days", 90),
            "abuseipdb_cache_ttl_sec": _int("abuseipdb_cache_ttl_sec", 86400),
            "spamhaus_enabled": _bool("spamhaus_enabled"),
            "spamhaus_refresh_interval_sec": _int("spamhaus_refresh_interval_sec", 86400),
            "incident_lookback_days": _int("incident_lookback_days", 30),
        }
        if raw_key and not all(c == "*" for c in raw_key):
            fields["abuseipdb_api_key"] = raw_key

        update_reputation_config(**fields)
        return RedirectResponse(url="/moderation/reputation?saved=1", status_code=303)

    @router.post("/reputation/refresh", response_class=HTMLResponse)
    async def refresh_spamhaus(request: Request):
        result = await spamhaus_mod.refresh_now()
        msg = f"drop={result['drop']}&edrop={result['edrop']}"
        if result["error"]:
            msg += f"&err=1"
        return RedirectResponse(url=f"/moderation/reputation?refreshed=1&{msg}", status_code=303)

    # -------- Patterns (Phase 3) --------------------------------------------

    @router.get("/patterns", response_class=HTMLResponse)
    async def page_patterns(request: Request, test_sample: Optional[str] = None):
        rows = list_pattern_rules(active_only=False)
        stats = patterns_mod.stats()
        # test-drive : si un sample est fourni, on liste ce qui matche
        test_result = None
        if test_sample:
            matches = patterns_mod.scan_message(test_sample)
            test_result = [{
                "name": m.name, "kind": m.kind, "severity": m.severity,
                "snippet": m.snippet, "span": list(m.span),
            } for m in matches]
        return templates.TemplateResponse(
            request=request,
            name="moderation_patterns.html",
            context={
                "rules": rows,
                "stats": stats,
                "test_sample": test_sample or "",
                "test_result": test_result,
                "flood_sample": "",
                "suggestions": None,
                "suggest_summary": None,
                "active": "moderation",
                "sub_active": "patterns",
            },
        )

    @router.post("/patterns/add", response_class=HTMLResponse)
    async def add_pattern(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip()
        kind = (form.get("kind") or "other").strip()
        regex = (form.get("regex") or "").strip()
        severity = int(form.get("severity") or 5)
        description = (form.get("description") or "").strip()
        if not name or not regex:
            return RedirectResponse(url="/moderation/patterns?err=missing", status_code=303)
        try:
            add_pattern_rule(name=name, kind=kind, regex=regex,
                             severity=severity, description=description)
        except Exception as e:
            return RedirectResponse(url=f"/moderation/patterns?err={e.__class__.__name__}",
                                    status_code=303)
        patterns_mod.refresh()
        return RedirectResponse(url="/moderation/patterns?added=1", status_code=303)

    @router.post("/patterns/suggest", response_class=HTMLResponse)
    async def suggest_patterns_route(request: Request):
        """Analyse un flood et affiche des suggestions cochables (POST → même page)."""
        form = await request.form()
        flood = (form.get("flood_sample") or "").strip()
        existing = list_pattern_rules(active_only=False)
        suggestions = pattern_suggester.suggest_patterns(flood, existing)
        summary = pattern_suggester.summarize(suggestions)
        stats = patterns_mod.stats()
        return templates.TemplateResponse(
            request=request,
            name="moderation_patterns.html",
            context={
                "rules": existing,
                "stats": stats,
                "test_sample": "",
                "test_result": None,
                "flood_sample": flood,
                "suggestions": suggestions,
                "suggest_summary": summary,
                "active": "moderation",
                "sub_active": "patterns",
            },
        )

    @router.post("/patterns/bulk_add", response_class=HTMLResponse)
    async def bulk_add_patterns(request: Request):
        """Insère en batch les patterns cochés dans le formulaire de suggestions.

        Attend des séries de champs indexés : sel_<i>=on, name_<i>, kind_<i>,
        regex_<i>, severity_<i>. On boucle tant qu'on trouve name_<i>.
        """
        form = await request.form()
        added = 0
        errors: list[str] = []
        i = 0
        while True:
            name = form.get(f"name_{i}")
            if name is None:
                break
            if form.get(f"sel_{i}") != "on":
                i += 1
                continue
            regex = (form.get(f"regex_{i}") or "").strip()
            kind = (form.get(f"kind_{i}") or "other").strip()
            try:
                severity = int(form.get(f"severity_{i}") or 5)
            except (TypeError, ValueError):
                severity = 5
            desc = f"auto-généré depuis flood ({form.get(f'reason_{i}') or ''})"[:200]
            if not name.strip() or not regex:
                errors.append(f"#{i} manquant name/regex")
                i += 1
                continue
            try:
                add_pattern_rule(
                    name=name.strip(), kind=kind, regex=regex,
                    severity=max(1, min(10, severity)),
                    description=desc,
                )
                added += 1
            except Exception as e:
                errors.append(f"{name}: {e.__class__.__name__}")
            i += 1

        patterns_mod.refresh()
        params = f"bulk_added={added}"
        if errors:
            params += f"&bulk_err={len(errors)}"
        return RedirectResponse(url=f"/moderation/patterns?{params}", status_code=303)

    @router.post("/patterns/{rule_id}/toggle", response_class=HTMLResponse)
    async def toggle_pattern(rule_id: int):
        rules = list_pattern_rules()
        current = next((r for r in rules if r.id == rule_id), None)
        if current is not None:
            update_pattern_rule(rule_id, active=not current.active)
            patterns_mod.refresh()
        return RedirectResponse(url="/moderation/patterns", status_code=303)

    @router.post("/patterns/{rule_id}/update", response_class=HTMLResponse)
    async def update_pattern(rule_id: int, request: Request):
        """Édition inline d'une règle existante : name/kind/severity/regex/description.

        Chaque champ est envoyé même s'il n'a pas changé, on écrit tout d'un coup.
        Le regex est recompilé au refresh() — si invalide, il apparaît dans les
        erreurs de compilation (visible sur la page).
        """
        form = await request.form()
        name = (form.get("name") or "").strip()
        kind = (form.get("kind") or "other").strip()
        regex = (form.get("regex") or "").strip()
        description = (form.get("description") or "").strip()
        try:
            severity = int(form.get("severity") or 5)
        except (TypeError, ValueError):
            severity = 5
        severity = max(1, min(10, severity))
        if not name or not regex:
            return RedirectResponse(url="/moderation/patterns?err=missing", status_code=303)
        updated = update_pattern_rule(
            rule_id,
            name=name, kind=kind, regex=regex,
            severity=severity, description=description,
        )
        if updated is None:
            return RedirectResponse(url="/moderation/patterns?err=not_found", status_code=303)
        patterns_mod.refresh()
        return RedirectResponse(url=f"/moderation/patterns?updated={rule_id}", status_code=303)

    @router.post("/patterns/{rule_id}/delete", response_class=HTMLResponse)
    async def del_pattern(rule_id: int):
        delete_pattern_rule(rule_id)
        patterns_mod.refresh()
        return RedirectResponse(url="/moderation/patterns", status_code=303)

    # -------- LLM Moderation (Phase 3) --------------------------------------

    @router.get("/llm", response_class=HTMLResponse)
    async def page_llm_mod(request: Request, verdict: Optional[str] = None):
        cfg = get_llm_moderation_config()
        logs = list_llm_moderation_logs(limit=100, verdict=verdict or None)
        return templates.TemplateResponse(
            request=request,
            name="moderation_llm.html",
            context={
                "cfg": cfg,
                "logs": logs,
                "filter_verdict": verdict or "",
                "active": "moderation",
                "sub_active": "llm",
            },
        )

    @router.post("/llm/save", response_class=HTMLResponse)
    async def save_llm_mod(request: Request):
        form = await request.form()

        def _int(name: str, default: int) -> int:
            try:
                return int(form.get(name, default))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(form.get(name, default))
            except (TypeError, ValueError):
                return default

        update_llm_moderation_config(
            system_prompt=(form.get("system_prompt") or "").strip(),
            default_model=(form.get("default_model") or "").strip(),
            max_message_len=_int("max_message_len", 800),
            min_message_len=_int("min_message_len", 8),
            sample_rate=_float("sample_rate", 0.0),
            throttle_per_nick_sec=_float("throttle_per_nick_sec", 5.0),
            throttle_global_sec=_float("throttle_global_sec", 0.5),
            timeout_sec=_float("timeout_sec", 15.0),
        )
        return RedirectResponse(url="/moderation/llm?saved=1", status_code=303)

    # -------- Alerts (timeline exhaustive de IncidentHistory) --------------

    @router.get("/alerts", response_class=HTMLResponse)
    async def page_alerts(
        request: Request,
        channel: Optional[str] = None,
        incident_type: Optional[str] = None,
        nick: Optional[str] = None,
        min_severity: int = 0,
        since_hours: Optional[int] = 24,
        limit: int = 200,
        offset: int = 0,
    ):
        limit = max(10, min(limit, 1000))
        incidents = query_incidents(
            limit=limit, offset=offset,
            channel=channel or None,
            incident_type=incident_type or None,
            nick=nick or None,
            min_severity=min_severity,
            since_hours=since_hours,
        )
        # Compteurs pour dashboard (24h par défaut)
        counts = incident_type_counts(since_hours=since_hours or 24)
        policies = list_channel_policies()
        return templates.TemplateResponse(
            request=request,
            name="moderation_alerts.html",
            context={
                "incidents": incidents,
                "counts": counts,
                "policies": policies,
                "filters": {
                    "channel": channel or "",
                    "incident_type": incident_type or "",
                    "nick": nick or "",
                    "min_severity": min_severity,
                    "since_hours": since_hours or 24,
                    "limit": limit,
                    "offset": offset,
                },
                "known_types": [
                    "pattern_hit", "flag_pattern",
                    "llm_hint", "flag_llm",
                    "flag_reputation",
                    "dry_run_ban", "ban",
                    "manual", "signal",
                ],
                "next_offset": offset + limit if len(incidents) == limit else None,
                "prev_offset": max(0, offset - limit) if offset > 0 else None,
                "active": "moderation",
                "sub_active": "alerts",
            },
        )

    @router.post("/alerts/{row_id}/delete", response_class=HTMLResponse)
    async def delete_alert(row_id: int):
        """Supprime un incident précis (faux positif / bruit)."""
        delete_incident(row_id)
        return RedirectResponse(url="/moderation/alerts?saved=1", status_code=303)

    @router.post("/alerts/purge_buffextras", response_class=HTMLResponse)
    async def purge_alerts_buffextras():
        """Bulk : purge tous les incidents/llm_logs/tracelog générés par
        *buffextras@znc.in avant que le skip soit en place."""
        stats = purge_buffextras_noise()
        msg = f"purgé: {stats['incidents']} incidents, {stats['llm_logs']} llm_logs, {stats['tracelog_msg']} tracelog msg"
        return RedirectResponse(url=f"/moderation/alerts?saved={msg}", status_code=303)

    # -------- Ban Engine (Phase 4) ------------------------------------------

    @router.get("/bans", response_class=HTMLResponse)
    async def page_bans(request: Request, channel: Optional[str] = None,
                        active_only: bool = False):
        cfg = get_ban_engine_config()
        templates_list = list_ban_templates(active_only=False)
        bans = list_bans(limit=200, channel=channel or None, active_only=active_only)
        policies = list_channel_policies()
        return templates.TemplateResponse(
            request=request,
            name="moderation_bans.html",
            context={
                "cfg": cfg,
                "templates_list": templates_list,
                "bans": bans,
                "policies": policies,
                "filter_channel": channel or "",
                "filter_active_only": active_only,
                "active": "moderation",
                "sub_active": "bans",
            },
        )

    @router.post("/bans/config/save", response_class=HTMLResponse)
    async def save_ban_config(request: Request):
        form = await request.form()

        def _bool(name: str) -> bool:
            return form.get(name) == "on"

        def _int(name: str, default: int) -> int:
            try:
                return int(form.get(name, default))
            except (TypeError, ValueError):
                return default

        default_tpl = form.get("default_template_id")
        try:
            default_tpl_id = int(default_tpl) if default_tpl else None
        except (TypeError, ValueError):
            default_tpl_id = None

        update_ban_engine_config(
            chanserv_nick=(form.get("chanserv_nick") or "X").strip(),
            chanserv_ban_cmd_template=(
                form.get("chanserv_ban_cmd_template")
                or "BAN {channel} {mask} {duration} :{reason}"
            ).strip(),
            prefer_chanserv=_bool("prefer_chanserv"),
            max_bans_per_hour=_int("max_bans_per_hour", 20),
            default_template_id=default_tpl_id,
            duplicate_dedupe_window_sec=_int("duplicate_dedupe_window_sec", 300),
            kick_before_ban=_bool("kick_before_ban"),
            kick_message_template=(form.get("kick_message_template") or "{reason}").strip(),
        )
        return RedirectResponse(url="/moderation/bans?saved=1", status_code=303)

    @router.post("/bans/templates/add", response_class=HTMLResponse)
    async def add_ban_tpl(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip()
        template_str = (form.get("template") or "").strip()
        if not name or not template_str:
            return RedirectResponse(url="/moderation/bans?err=missing", status_code=303)
        try:
            add_ban_template(name=name, template=template_str)
        except Exception as e:
            return RedirectResponse(url=f"/moderation/bans?err={e.__class__.__name__}",
                                    status_code=303)
        return RedirectResponse(url="/moderation/bans?tpl_added=1", status_code=303)

    @router.post("/bans/templates/{tpl_id}/toggle", response_class=HTMLResponse)
    async def toggle_ban_tpl(tpl_id: int):
        tpls = list_ban_templates()
        current = next((t for t in tpls if t.id == tpl_id), None)
        if current is not None:
            update_ban_template(tpl_id, active=not current.active)
        return RedirectResponse(url="/moderation/bans", status_code=303)

    @router.post("/bans/templates/{tpl_id}/delete", response_class=HTMLResponse)
    async def del_ban_tpl(tpl_id: int):
        delete_ban_template(tpl_id)
        return RedirectResponse(url="/moderation/bans", status_code=303)

    @router.post("/bans/{ban_id}/unban", response_class=HTMLResponse)
    async def unban(ban_id: int):
        """MODE -b + set BannedEntity.active=False. Ne peut pas ressusciter un
        ban dry-run (déjà inactif) — bouton dispo uniquement pour actifs."""
        row = get_ban(ban_id)
        if row is None or not row.active:
            return RedirectResponse(url="/moderation/bans?err=not_found", status_code=303)
        client = supervisor.get_client()
        if client is None:
            return RedirectResponse(url="/moderation/bans?err=not_connected",
                                    status_code=303)
        ok = await ban_engine_mod.unban_mask(client, row.channel, row.mask)
        if ok:
            mark_ban_inactive(ban_id)
            return RedirectResponse(url="/moderation/bans?unbanned=1", status_code=303)
        return RedirectResponse(url="/moderation/bans?err=send_failed", status_code=303)

    return router
