"""Authentification WebUI : compte admin unique, session par cookie signé.

Design :
    - **Mot de passe** : scrypt (hashlib stdlib), sel aléatoire, comparaison
      à temps constant. Calcul déporté en thread (~50 ms CPU, pas sur la loop).
    - **Session** : cookie `chatbot_session` = base64url(json{u, exp, v}) + "."
      + HMAC-SHA256(session_secret). `v` = empreinte du hash de mot de passe
      → changer le mot de passe invalide toutes les sessions existantes.
      HttpOnly + SameSite=Lax, durée `SESSION_TTL_SEC`.
    - **Premier boot** (aucun compte en DB) : un code de setup à usage unique
      est généré et loggé (`docker logs chatbot-irc`). /login affiche alors le
      formulaire de création qui l'exige — le premier venu du LAN ne peut pas
      réclamer le compte.
    - **Middleware ASGI pur** (pas BaseHTTPMiddleware) : couvre aussi le
      WebSocket /ws. Seuls /login et /healthz sont publics.
    - **CSRF / Cross-Site WebSocket Hijacking** : en plus de SameSite=Lax,
      toute méthode non-GET et tout handshake WebSocket doit avoir un header
      Origin égal au Host (ou pas d'Origin du tout = client non-navigateur,
      qui n'a de toute façon pas le cookie).
    - **Brute-force** : `_MAX_FAILS` échecs par IP dans `_FAIL_WINDOW_SEC`
      → login refusé jusqu'à expiration de la fenêtre (mémoire process).

CLI (dans le container) :
    python -m app.auth set-password   # crée/remplace le compte (interactif)
    python -m app.auth reset          # efface le compte → retour au mode setup
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from .db import AdminAuth, get_admin_auth, update_admin_auth


log = logging.getLogger("chatbot.auth")


COOKIE_NAME = "chatbot_session"
SESSION_TTL_SEC = 7 * 24 * 3600
PUBLIC_PATHS = {"/login", "/healthz"}
MIN_PASSWORD_LEN = 10

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_MAX_FAILS = 5
_FAIL_WINDOW_SEC = 300
_FAILS: dict[str, list[float]] = {}

# Code de setup à usage unique (mémoire process). None tant qu'un compte existe.
_setup_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Mots de passe
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        expected_raw = _b64d(expected)
        candidate = hashlib.scrypt(password.encode(), salt=_b64d(salt),
                                   n=int(n), r=int(r), p=int(p),
                                   dklen=len(expected_raw))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected_raw)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _ensure_secret(auth: AdminAuth) -> AdminAuth:
    if not auth.session_secret:
        auth = update_admin_auth(session_secret=secrets.token_hex(32))
    return auth


def _password_fingerprint(auth: AdminAuth) -> str:
    return hashlib.sha256(auth.password_hash.encode()).hexdigest()[:16]


def _sign(body: str, auth: AdminAuth) -> str:
    mac = hmac.new(bytes.fromhex(auth.session_secret), body.encode(), hashlib.sha256)
    return _b64e(mac.digest())


def make_session_token(auth: AdminAuth) -> str:
    auth = _ensure_secret(auth)
    payload = {
        "u": auth.username,
        "exp": int(time.time()) + SESSION_TTL_SEC,
        "v": _password_fingerprint(auth),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body, auth)}"


def read_session_token(token: Optional[str], auth: AdminAuth) -> Optional[str]:
    """Username si le token est valide, None sinon."""
    if not token or not auth.username or not auth.session_secret:
        return None
    body, _, sig = token.partition(".")
    # Comparaison en bytes : compare_digest lève TypeError sur str non-ASCII.
    if not body or not sig or not hmac.compare_digest(sig.encode(), _sign(body, auth).encode()):
        return None
    try:
        payload = json.loads(_b64d(body))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < time.time():
        return None
    if payload.get("u") != auth.username or payload.get("v") != _password_fingerprint(auth):
        return None
    return auth.username


# ---------------------------------------------------------------------------
# Setup (premier boot) + brute-force
# ---------------------------------------------------------------------------

def setup_required() -> bool:
    auth = get_admin_auth()
    return not (auth.username and auth.password_hash)


def announce_setup_if_needed() -> None:
    """Génère + logge le code de setup si aucun compte. Idempotent."""
    global _setup_code
    if not setup_required():
        _setup_code = None
        return
    if _setup_code is None:
        _setup_code = secrets.token_hex(4).upper()
    log.warning(
        "Aucun compte admin WebUI. Ouvre /login et utilise le code de setup : %s",
        _setup_code,
    )


def _check_setup_code(code: str) -> bool:
    return bool(_setup_code) and hmac.compare_digest(
        code.strip().upper().encode(), _setup_code.encode(),
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _is_blocked(ip: str) -> bool:
    now = time.monotonic()
    fails = [t for t in _FAILS.get(ip, []) if now - t < _FAIL_WINDOW_SEC]
    _FAILS[ip] = fails
    return len(fails) >= _MAX_FAILS


def _record_fail(ip: str) -> None:
    _FAILS.setdefault(ip, []).append(time.monotonic())
    log.warning("login échoué ip=%s (%d/%d)", ip, len(_FAILS[ip]), _MAX_FAILS)


def _safe_next(target: Optional[str]) -> str:
    """Redirection post-login : chemins locaux uniquement (anti open-redirect)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


# ---------------------------------------------------------------------------
# Middleware ASGI
# ---------------------------------------------------------------------------

def _same_origin(headers: Headers) -> bool:
    origin = headers.get("origin")
    if origin is None:
        return True  # client non-navigateur : pas de cookie volé possible
    host = headers.get("host", "")
    return bool(host) and urlparse(origin).netloc.lower() == host.lower()


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        is_ws = scope["type"] == "websocket"
        conn = HTTPConnection(scope)

        if (is_ws or scope.get("method", "GET") not in _SAFE_METHODS) \
                and not _same_origin(conn.headers):
            log.warning("Origin refusée path=%s origin=%s", scope.get("path"),
                        conn.headers.get("origin"))
            await self._deny(scope, receive, send, is_ws, 403, "Origin refusée")
            return

        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        user = read_session_token(conn.cookies.get(COOKIE_NAME), get_admin_auth())
        if user is None:
            if is_ws:
                await WebSocket(scope, receive, send).close(code=1008)
            elif path.startswith("/api/"):
                await JSONResponse({"detail": "authentification requise"},
                                   status_code=401)(scope, receive, send)
            else:
                target = path + (f"?{conn.url.query}" if conn.url.query else "")
                await RedirectResponse(f"/login?next={quote(target, safe='')}",
                                       status_code=303)(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send, is_ws: bool, status: int, msg: str) -> None:
        if is_ws:
            await WebSocket(scope, receive, send).close(code=1008)
        else:
            await PlainTextResponse(msg, status_code=status)(scope, receive, send)


# ---------------------------------------------------------------------------
# Routes /login, /logout
# ---------------------------------------------------------------------------

def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def render(request: Request, *, error: str = "", status_code: int = 200,
               username: str = "", next_url: str = "/"):
        setup = setup_required()
        if setup:
            announce_setup_if_needed()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "setup": setup,
                "error": error,
                "username": username,
                "next_url": next_url,
                "min_len": MIN_PASSWORD_LEN,
            },
            status_code=status_code,
        )

    def login_ok(request: Request, auth: AdminAuth, next_url: str) -> RedirectResponse:
        _FAILS.pop(_client_ip(request), None)
        resp = RedirectResponse(_safe_next(next_url), status_code=303)
        resp.set_cookie(
            COOKIE_NAME, make_session_token(auth),
            max_age=SESSION_TTL_SEC, httponly=True, samesite="lax",
            secure=request.url.scheme == "https", path="/",
        )
        return resp

    @router.get("/login", response_class=HTMLResponse)
    async def page_login(request: Request):
        next_url = _safe_next(request.query_params.get("next"))
        auth = get_admin_auth()
        if read_session_token(request.cookies.get(COOKIE_NAME), auth):
            return RedirectResponse(next_url, status_code=303)
        return render(request, next_url=next_url)

    @router.post("/login", response_class=HTMLResponse)
    async def do_login(request: Request):
        global _setup_code
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        next_url = _safe_next(str(form.get("next", "/")))
        ip = _client_ip(request)

        if _is_blocked(ip):
            return render(request, error="Trop de tentatives. Réessaie dans quelques minutes.",
                          status_code=429, username=username, next_url=next_url)

        if setup_required():
            if not _check_setup_code(str(form.get("setup_code", ""))):
                _record_fail(ip)
                return render(request, error="Code de setup invalide (voir les logs du container).",
                              status_code=403, username=username, next_url=next_url)
            if not username:
                return render(request, error="Nom d'utilisateur requis.",
                              status_code=400, next_url=next_url)
            if len(password) < MIN_PASSWORD_LEN:
                return render(request, error=f"Mot de passe : {MIN_PASSWORD_LEN} caractères minimum.",
                              status_code=400, username=username, next_url=next_url)
            if password != str(form.get("password_confirm", "")):
                return render(request, error="Les mots de passe ne correspondent pas.",
                              status_code=400, username=username, next_url=next_url)
            pw_hash = await asyncio.to_thread(hash_password, password)
            auth = _ensure_secret(update_admin_auth(username=username, password_hash=pw_hash))
            _setup_code = None
            log.warning("Compte admin WebUI créé : %s (ip=%s)", username, ip)
            return login_ok(request, auth, next_url)

        auth = get_admin_auth()
        user_ok = hmac.compare_digest(username.encode(), auth.username.encode())
        # verify_password tourne même si le user est faux : timing uniforme.
        pw_ok = await asyncio.to_thread(verify_password, password, auth.password_hash)
        if not (user_ok and pw_ok):
            _record_fail(ip)
            return render(request, error="Identifiants invalides.",
                          status_code=401, username=username, next_url=next_url)
        log.info("login ok user=%s ip=%s", username, ip)
        return login_ok(request, auth, next_url)

    @router.post("/logout")
    async def do_logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    return router


# ---------------------------------------------------------------------------
# CLI : python -m app.auth set-password | reset
# ---------------------------------------------------------------------------

def _cli() -> None:
    import getpass
    import sys

    from sqlmodel import SQLModel

    from .db import get_engine

    SQLModel.metadata.create_all(get_engine(), tables=[AdminAuth.__table__])
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "set-password":
        current = get_admin_auth().username
        username = input(f"Utilisateur [{current or 'admin'}] : ").strip() or current or "admin"
        password = getpass.getpass("Nouveau mot de passe : ")
        if len(password) < MIN_PASSWORD_LEN:
            sys.exit(f"Refusé : {MIN_PASSWORD_LEN} caractères minimum.")
        if password != getpass.getpass("Confirmation : "):
            sys.exit("Refusé : les mots de passe ne correspondent pas.")
        _ensure_secret(update_admin_auth(username=username, password_hash=hash_password(password)))
        print(f"OK — compte '{username}' enregistré. Les sessions existantes sont invalidées.")
    elif cmd == "reset":
        update_admin_auth(username="", password_hash="")
        print("OK — compte effacé. Le code de setup apparaîtra dans les logs à la prochaine visite de /login.")
    else:
        sys.exit("Usage : python -m app.auth set-password | reset")


if __name__ == "__main__":
    _cli()
