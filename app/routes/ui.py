from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from ..db import (
    USER_ACL_FLAGS,
    IrcConfig,
    LlmConfig,
    add_bible_acl,
    add_bible_broadcast,
    add_custom_command,
    add_ip_annotation,
    add_user_acl,
    delete_bible_acl,
    delete_bible_broadcast,
    delete_custom_command,
    delete_ip_annotation,
    delete_user_acl,
    get_bible_config,
    get_engine,
    get_image_gen_config,
    get_irc_config,
    list_bible_acls,
    list_bible_broadcasts,
    list_channel_policies,
    list_custom_commands,
    list_ip_annotations,
    list_user_acls,
    update_bible_broadcast,
    update_bible_config,
    update_custom_command,
    update_image_gen_config,
    update_user_acl,
)
from ..custom_commands import RECIPES as CUSTOM_RECIPES
from ..moderation.mask_utils import normalize_pattern


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def page_config(request: Request):
        with Session(get_engine()) as s:
            cfg = s.get(IrcConfig, 1)
        return templates.TemplateResponse(
            request=request,
            name="config.html",
            context={"cfg": cfg, "active": "config"},
        )

    @router.get("/help", response_class=HTMLResponse)
    async def page_help(request: Request):
        irc_cfg = get_irc_config()
        prefix = getattr(irc_cfg, "command_prefix", "!") or "!"
        custom_commands = list_custom_commands(active_only=False)
        return templates.TemplateResponse(
            request=request,
            name="help.html",
            context={
                "prefix": prefix,
                "custom_commands": custom_commands,
                "active": "help",
            },
        )

    @router.post("/", response_class=HTMLResponse)
    async def save_config(request: Request):
        form = await request.form()
        with Session(get_engine()) as s:
            cfg = s.get(IrcConfig, 1) or IrcConfig(id=1)
            cfg.host = form.get("host", cfg.host)
            cfg.port = int(form.get("port", cfg.port))
            cfg.tls = form.get("tls") == "on"
            cfg.user = form.get("user", cfg.user)
            cfg.password = form.get("password", cfg.password)
            cfg.nick = form.get("nick", cfg.nick)
            cfg.ident = form.get("ident", cfg.ident)
            cfg.realname = form.get("realname", cfg.realname)
            cfg.channels = form.get("channels", cfg.channels)
            # command_prefix : 1 char non-alphanum, sinon on garde l'ancien
            new_prefix = (form.get("command_prefix") or "").strip()
            if new_prefix and len(new_prefix) <= 3 and not new_prefix.isalnum():
                cfg.command_prefix = new_prefix
            s.add(cfg)
            s.commit()
        return RedirectResponse(url="/?saved=1", status_code=303)

    @router.get("/llm", response_class=HTMLResponse)
    async def page_llm(request: Request):
        with Session(get_engine()) as s:
            cfg = s.get(LlmConfig, 1)
        from ..db import get_url_intel_config
        from ..url_intel import (DEFAULT_DESC_FORMAT, DEFAULT_LINK_FORMAT,
                                 DEFAULT_YT_DESC_FORMAT, DEFAULT_YT_FORMAT)
        ui = get_url_intel_config()
        fmt = {
            "link_format": DEFAULT_LINK_FORMAT if ui.link_format is None else ui.link_format,
            "desc_format": DEFAULT_DESC_FORMAT if ui.desc_format is None else ui.desc_format,
            "yt_format": DEFAULT_YT_FORMAT if ui.yt_format is None else ui.yt_format,
            "yt_desc_format": DEFAULT_YT_DESC_FORMAT if ui.yt_desc_format is None else ui.yt_desc_format,
        }
        return templates.TemplateResponse(
            request=request,
            name="llm.html",
            context={"cfg": cfg, "active": "llm", "ui": ui, "fmt": fmt},
        )

    @router.post("/llm", response_class=HTMLResponse)
    async def save_llm(request: Request):
        form = await request.form()
        with Session(get_engine()) as s:
            cfg = s.get(LlmConfig, 1) or LlmConfig(id=1)
            cfg.llm_url = form.get("llm_url", cfg.llm_url)
            cfg.llm_api_key = form.get("llm_api_key", cfg.llm_api_key)
            cfg.model = form.get("model", cfg.model)
            cfg.max_tokens = int(form.get("max_tokens", cfg.max_tokens))
            cfg.temperature = float(form.get("temperature", cfg.temperature))
            cfg.system_prompt = form.get("system_prompt", cfg.system_prompt)
            cfg.throttle_global_sec = float(form.get("throttle_global_sec", cfg.throttle_global_sec))
            cfg.throttle_per_nick_sec = float(form.get("throttle_per_nick_sec", cfg.throttle_per_nick_sec))
            cfg.web_search_enabled = form.get("web_search_enabled") == "on"
            cfg.query_intent_enabled = form.get("query_intent_enabled") == "on"
            cfg.intent_extraction_prompt = form.get("intent_extraction_prompt", cfg.intent_extraction_prompt or "")
            cfg.humanization_prompt = form.get("humanization_prompt", cfg.humanization_prompt or "")
            cfg.memory_enabled = form.get("memory_enabled") == "on"
            cfg.memory_model = form.get("memory_model", cfg.memory_model or "").strip()
            cfg.memory_extract_every = max(1, int(form.get("memory_extract_every") or cfg.memory_extract_every or 6))
            cfg.memory_context_lines = max(0, int(form.get("memory_context_lines") or 0))
            cfg.memory_blacklist = form.get("memory_blacklist", cfg.memory_blacklist or "")
            cfg.autonomous_channels = (form.get("autonomous_channels") or "").strip()
            cfg.autonomous_model = (form.get("autonomous_model") or "").strip()
            if "farewell_channels" in form:
                cfg.farewell_channels = (form.get("farewell_channels") or "").strip()
            if "owui_tool_ids" in form:
                cfg.owui_tool_ids = (form.get("owui_tool_ids") or "").strip()
            try:
                cfg.auto_ignore_minutes = max(0, int(form.get("auto_ignore_minutes") or 0))
            except ValueError:
                pass
            try:
                cfg.autonomous_cooldown_sec = max(0.0, float(form.get("autonomous_cooldown_sec") or 300))
            except ValueError:
                pass
            s.add(cfg)
            s.commit()
        if "url_link_format" in form:
            from ..db import update_url_intel_config
            url_fields = {
                "enabled": form.get("url_enabled") == "on",
                "link_format": (form.get("url_link_format") or "").strip(),
                "desc_format": (form.get("url_desc_format") or "").strip(),
                "yt_format": (form.get("url_yt_format") or "").strip(),
                "yt_desc_format": (form.get("url_yt_desc_format") or "").strip(),
            }
            try:
                url_fields["max_urls_per_message"] = max(
                    0, min(10, int(form.get("url_max_urls") or 3)))
            except ValueError:
                pass
            update_url_intel_config(**url_fields)
        return RedirectResponse(url="/llm?saved=1", status_code=303)

    # ------------------------------------------------------------------
    # Bible feature
    # ------------------------------------------------------------------

    @router.get("/bible", response_class=HTMLResponse)
    async def page_bible(request: Request):
        cfg = get_bible_config()
        acls = list_bible_acls()
        broadcasts = list_bible_broadcasts()
        return templates.TemplateResponse(
            request=request,
            name="bible.html",
            context={
                "cfg": cfg, "acls": acls, "broadcasts": broadcasts,
                "active": "bible",
            },
        )

    @router.post("/bible/config", response_class=HTMLResponse)
    async def save_bible_config(request: Request):
        form = await request.form()
        update_bible_config(
            versions_enabled=(form.get("versions_enabled") or '["fr-lsg","en-kjv"]').strip(),
            command_prefix=(form.get("command_prefix") or "!").strip() or "!",
            default_mode=(form.get("default_mode") or "apologist").strip(),
            throttle_per_mask_sec=float(form.get("throttle_per_mask_sec") or 15.0),
            throttle_global_sec=float(form.get("throttle_global_sec") or 3.0),
            prompt_apologist=(form.get("prompt_apologist") or "").strip(),
            prompt_meme=(form.get("prompt_meme") or "").strip(),
            llm_commentary_enabled=form.get("llm_commentary_enabled") == "on",
            llm_commentary_for_broadcast=form.get("llm_commentary_for_broadcast") == "on",
        )
        return RedirectResponse(url="/bible?saved=1", status_code=303)

    @router.post("/bible/acls", response_class=HTMLResponse)
    async def add_bible_acl_route(request: Request):
        form = await request.form()
        mask = (form.get("mask") or "").strip()
        reason = (form.get("reason") or "").strip()
        added_by = (form.get("added_by") or "webui").strip()
        if mask:
            mask = normalize_pattern(mask)
            add_bible_acl(mask=mask, reason=reason, added_by=added_by)
        return RedirectResponse(url="/bible", status_code=303)

    @router.post("/bible/acls/{acl_id}/delete", response_class=HTMLResponse)
    async def delete_bible_acl_route(acl_id: int):
        delete_bible_acl(acl_id)
        return RedirectResponse(url="/bible", status_code=303)

    @router.post("/bible/broadcasts", response_class=HTMLResponse)
    async def add_bible_broadcast_route(request: Request):
        form = await request.form()
        channel = (form.get("channel") or "").strip()
        interval = int(form.get("interval_minutes") or 180)
        if channel:
            add_bible_broadcast(channel=channel, interval_minutes=max(1, interval))
        return RedirectResponse(url="/bible", status_code=303)

    @router.post("/bible/broadcasts/{bc_id}", response_class=HTMLResponse)
    async def save_bible_broadcast(bc_id: int, request: Request):
        form = await request.form()
        fields = {
            "active": form.get("active") == "on",
            "interval_minutes": max(1, int(form.get("interval_minutes") or 180)),
            "versions_override": (form.get("versions_override") or "[]").strip() or "[]",
            "books_filter": (form.get("books_filter") or "[]").strip() or "[]",
            "mode_random": form.get("mode_random") == "on",
            "mode_playlist": form.get("mode_playlist") == "on",
            "mode_votd": form.get("mode_votd") == "on",
            "playlist": (form.get("playlist") or "[]").strip() or "[]",
        }
        update_bible_broadcast(bc_id, **fields)
        return RedirectResponse(url="/bible", status_code=303)

    @router.post("/bible/broadcasts/{bc_id}/delete", response_class=HTMLResponse)
    async def delete_bible_broadcast_route(bc_id: int):
        delete_bible_broadcast(bc_id)
        return RedirectResponse(url="/bible", status_code=303)

    # ------------------------------------------------------------------
    # Intel IP + commandes custom
    # ------------------------------------------------------------------

    @router.get("/intel", response_class=HTMLResponse)
    async def page_intel(request: Request):
        annotations = list_ip_annotations()
        commands = list_custom_commands()
        irc_cfg = get_irc_config()
        return templates.TemplateResponse(
            request=request,
            name="intel.html",
            context={
                "annotations": annotations,
                "commands": commands,
                "recipes": sorted(CUSTOM_RECIPES.keys()),
                "command_prefix": (irc_cfg.command_prefix if irc_cfg else "!") or "!",
                "active": "intel",
            },
        )

    @router.post("/intel/annotations", response_class=HTMLResponse)
    async def add_intel_annotation(request: Request):
        form = await request.form()
        target = (form.get("target") or "").strip()
        label = (form.get("label") or "note").strip()
        note = (form.get("note") or "").strip()
        added_by = (form.get("added_by") or "webui").strip()
        if target:
            add_ip_annotation(target=target, label=label, note=note, added_by=added_by)
        return RedirectResponse(url="/intel", status_code=303)

    @router.post("/intel/annotations/{ann_id}/delete", response_class=HTMLResponse)
    async def delete_intel_annotation(ann_id: int):
        delete_ip_annotation(ann_id)
        return RedirectResponse(url="/intel", status_code=303)

    @router.post("/intel/commands", response_class=HTMLResponse)
    async def add_intel_command(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip()
        trigger_re = (form.get("trigger") or "").strip()
        recipe = (form.get("recipe") or "").strip()
        if not (name and trigger_re and recipe):
            return RedirectResponse(url="/intel", status_code=303)
        if recipe not in CUSTOM_RECIPES:
            return RedirectResponse(url="/intel?err=recipe_unknown", status_code=303)
        pattern_mode = (form.get("pattern_mode") or "simple").strip()
        if pattern_mode not in ("simple", "regex"):
            pattern_mode = "simple"
        add_custom_command(
            name=name,
            trigger=trigger_re,
            pattern_mode=pattern_mode,
            recipe=recipe,
            description=(form.get("description") or "").strip(),
            output_template=(form.get("output_template") or "").strip(),
            humanize_with_llm=form.get("humanize_with_llm") == "on",
            acl_required=form.get("acl_required") == "on",
            allow_pm=form.get("allow_pm") == "on",
            active=form.get("active") == "on",
            added_by=(form.get("added_by") or "webui").strip(),
        )
        return RedirectResponse(url="/intel", status_code=303)

    @router.post("/intel/commands/{cmd_id}", response_class=HTMLResponse)
    async def update_intel_command(cmd_id: int, request: Request):
        form = await request.form()
        recipe = (form.get("recipe") or "").strip()
        if recipe and recipe not in CUSTOM_RECIPES:
            return RedirectResponse(url="/intel?err=recipe_unknown", status_code=303)
        pattern_mode = (form.get("pattern_mode") or "regex").strip()
        if pattern_mode not in ("simple", "regex"):
            pattern_mode = "regex"
        update_custom_command(
            cmd_id,
            trigger=(form.get("trigger") or "").strip(),
            pattern_mode=pattern_mode,
            recipe=recipe,
            description=(form.get("description") or "").strip(),
            output_template=(form.get("output_template") or "").strip(),
            humanize_with_llm=form.get("humanize_with_llm") == "on",
            acl_required=form.get("acl_required") == "on",
            allow_pm=form.get("allow_pm") == "on",
            active=form.get("active") == "on",
        )
        return RedirectResponse(url="/intel", status_code=303)

    @router.post("/intel/commands/{cmd_id}/delete", response_class=HTMLResponse)
    async def delete_intel_command(cmd_id: int):
        delete_custom_command(cmd_id)
        return RedirectResponse(url="/intel", status_code=303)

    # ------------------------------------------------------------------
    # Page /users — UserAcl (ACL granulaire multi-flags)
    # ------------------------------------------------------------------

    @router.get("/users", response_class=HTMLResponse)
    async def page_users(request: Request):
        users = list_user_acls()
        irc_cfg = get_irc_config()
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context={
                "users": users,
                "command_prefix": (irc_cfg.command_prefix if irc_cfg else "!") or "!",
                "active": "users",
            },
        )

    @router.post("/users", response_class=HTMLResponse)
    async def add_user(request: Request):
        form = await request.form()
        mask = (form.get("mask") or "").strip()
        if not mask:
            return RedirectResponse(url="/users", status_code=303)
        add_user_acl(
            mask=mask,
            note=(form.get("note") or "").strip(),
            added_by=(form.get("added_by") or "webui").strip(),
        )
        return RedirectResponse(url="/users?added=1", status_code=303)

    @router.post("/users/{user_id}", response_class=HTMLResponse)
    async def save_user(user_id: int, request: Request):
        form = await request.form()
        updates: dict = {
            "mask": (form.get("mask") or "").strip(),
            "note": (form.get("note") or "").strip(),
            "keyword": (form.get("keyword") or "").strip(),
            "active": form.get("active") == "on",
        }
        # Chaque flag = checkbox; absente dans le POST = False
        for flag in USER_ACL_FLAGS:
            updates[flag] = form.get(flag) == "on"
        if not updates["mask"]:
            return RedirectResponse(url="/users", status_code=303)
        update_user_acl(user_id, **updates)
        return RedirectResponse(url="/users?saved=1", status_code=303)

    @router.post("/users/{user_id}/delete", response_class=HTMLResponse)
    async def delete_user(user_id: int):
        delete_user_acl(user_id)
        return RedirectResponse(url="/users", status_code=303)

    # ------------------------------------------------------------------
    # Page /images — ImageGenConfig (gen via OpenWebUI + share Immich)
    # ------------------------------------------------------------------

    @router.get("/images", response_class=HTMLResponse)
    async def page_images(request: Request):
        cfg = get_image_gen_config()
        irc_cfg = get_irc_config()
        return templates.TemplateResponse(
            request=request,
            name="images.html",
            context={
                "cfg": cfg,
                "command_prefix": (irc_cfg.command_prefix if irc_cfg else "!") or "!",
                "active": "images",
            },
        )

    @router.post("/images", response_class=HTMLResponse)
    async def save_images(request: Request):
        form = await request.form()
        # Parse defensively — bad int/float = keep old value
        def _int(field: str, default: int) -> int:
            try:
                return int(form.get(field) or default)
            except (TypeError, ValueError):
                return default
        def _float(field: str, default: float) -> float:
            try:
                return float(form.get(field) or default)
            except (TypeError, ValueError):
                return default

        update_image_gen_config(
            enabled=form.get("enabled") == "on",
            model=(form.get("model") or "").strip(),
            size=(form.get("size") or "1024x1024").strip() or "1024x1024",
            n=max(1, min(4, _int("n", 1))),
            fetch_timeout_sec=max(30.0, _float("fetch_timeout_sec", 300.0)),
            immich_url=(form.get("immich_url") or "").strip().rstrip("/"),
            immich_api_key=(form.get("immich_api_key") or "").strip(),
            share_expiry_hours=max(0, _int("share_expiry_hours", 0)),
            max_concurrent=max(1, min(8, _int("max_concurrent", 1))),
            throttle_per_nick_sec=max(0.0, _float("throttle_per_nick_sec", 60.0)),
        )
        return RedirectResponse(url="/images?saved=1", status_code=303)

    @router.get("/console", response_class=HTMLResponse)
    async def page_console(request: Request):
        with Session(get_engine()) as s:
            cfg = s.get(IrcConfig, 1)
        channels = [c.strip() for c in (cfg.channels if cfg else "").split(",") if c.strip()]

        # Récap modération par chan pour affichage dans la console
        policies = list_channel_policies()
        FEATURES = [
            ("tracelog_enabled", "tracelog"),
            ("whitelist_enforced", "whitelist"),
            ("reputation_check_enabled", "reputation"),
            ("llm_moderation_enabled", "llm"),
            ("pattern_detection_enabled", "patterns"),
            ("join_check_enabled", "join-check"),
            ("auto_ban_enabled", "auto-ban"),
        ]
        moderation_summary = []
        for p in policies:
            active_features = [label for attr, label in FEATURES if getattr(p, attr, False)]
            moderation_summary.append({
                "channel": p.channel,
                "features": active_features,
                "dry_run": p.dry_run,
                "any_enforcing": any(
                    getattr(p, attr, False)
                    for attr in ("reputation_check_enabled", "llm_moderation_enabled",
                                 "pattern_detection_enabled", "auto_ban_enabled")
                ),
            })

        return templates.TemplateResponse(
            request=request,
            name="console.html",
            context={
                "channels": channels,
                "moderation_summary": moderation_summary,
                "active": "console",
            },
        )

    return router
