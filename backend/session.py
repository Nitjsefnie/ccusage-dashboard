"""Session token mint/verify + FastAPI auth middleware.

Looks up users in an external auth DB by `user_id`, fetching only the
`config` JSONB column (which carries the PBKDF2 web-password hash and
a per-user web-session secret).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from backend import db


SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600
WEB_SESSION_SECRET_KEY = "web_session_secret"

# Sentinel user_id reserved for unauthenticated guest sessions.
# Tokens with this user_id are signed with a process-local secret
# regenerated at startup, so guest cookies invalidate on restart.
GUEST_USER_ID = 0
_GUEST_SECRET = secrets.token_urlsafe(32)


def parse_session_token(token: str):
    parts = token.split(".")
    if len(parts) != 4:
        return None
    raw_uid, raw_ts, nonce, sig = parts
    try:
        user_id = int(raw_uid)
        issued_at = int(raw_ts)
    except ValueError:
        return None
    if not nonce or not sig:
        return None
    return user_id, issued_at, nonce, sig


def make_session_token(user_id: int, secret: str) -> str:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(10)
    payload = f"{user_id}.{issued_at}.{nonce}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str, secret: str):
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, issued_at, _nonce, sig = parsed
    now = int(time.time())
    if issued_at > now + 60:
        return None
    if now - issued_at > SESSION_COOKIE_MAX_AGE:
        return None
    payload = f"{user_id}.{issued_at}.{parsed[2]}"
    expected = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


def get_or_create_session_secret(config: dict) -> str:
    secret = str(config.get(WEB_SESSION_SECRET_KEY, "")).strip()
    if secret:
        return secret
    secret = secrets.token_urlsafe(32)
    config[WEB_SESSION_SECRET_KEY] = secret
    return secret


def load_user_config(user_id: int) -> dict | None:
    """Fetch the auth DB's users table.config for one user. Returns None if no row."""
    with db.auth_conn() as c:
        row = c.execute(
            "SELECT config FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def write_user_config(user_id: int, config: dict) -> None:
    """Persist a modified config back to the auth DB's users table.

    Used to store the freshly-minted web_session_secret on first login.
    """
    import json
    with db.auth_conn() as c:
        c.execute(
            "UPDATE users SET config = %s::jsonb WHERE user_id = %s",
            (json.dumps(config), user_id),
        )


def make_guest_session_token() -> str:
    return make_session_token(GUEST_USER_ID, _GUEST_SECRET)


def resolve_session_user_id(token: str) -> int | None:
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id = parsed[0]
    if user_id == GUEST_USER_ID:
        return verify_session_token(token, _GUEST_SECRET)
    config = load_user_config(user_id)
    if config is None:
        return None
    secret = str(config.get(WEB_SESSION_SECRET_KEY, "")).strip()
    if not secret:
        return None
    return verify_session_token(token, secret)


def check_origin(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if not host:
        return True
    if origin:
        return urlparse(origin).netloc == host
    if referer:
        return urlparse(referer).netloc == host
    return False


_AUTH_PUBLIC_PATHS = {"/health", "/login", "/logout", "/login/guest"}


async def auth_middleware(request: Request, call_next):
    import os
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS:
        return await call_next(request)
    if path.startswith("/admin/"):
        token = request.headers.get("x-admin-token", "")
        expected = os.environ.get("ADMIN_TOKEN", "")
        if not expected or not hmac.compare_digest(token, expected):
            return JSONResponse(
                {"ok": False, "error": "Unauthorized"}, status_code=401
            )
        if not check_origin(request):
            return Response("Forbidden (cross-origin)", status_code=403)
        return await call_next(request)
    if not check_origin(request):
        return Response("Forbidden (cross-origin)", status_code=403)
    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    user_id = resolve_session_user_id(cookie) if cookie else None
    if user_id is None:
        if path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": "Unauthorized"}, status_code=401
            )
        return RedirectResponse("/login", status_code=302)
    request.state.user_id = user_id
    request.state.is_guest = (user_id == GUEST_USER_ID)
    # Gate per-session and per-project endpoints from guests, plus
    # disallow `project=` filters on aggregate endpoints so guests can
    # only see project-mixed data.
    if request.state.is_guest:
        # /api/projects leaks project names (filesystem paths).
        # /api/sessions* covers list, single detail, raw transcript, sidecar.
        # Context-growth-by-session is allowed — just numbers, needed for graphs.
        if (
            path == "/api/projects"
            or path.startswith("/api/sessions")
            or path.startswith("/api/export")
        ):
            return JSONResponse(
                {"ok": False, "error": "Forbidden (guest)"}, status_code=403
            )
        # Block project= filtering on aggregate endpoints — guest sees
        # project-mixed data only.
        if request.query_params.get("project"):
            return JSONResponse(
                {"ok": False, "error": "Forbidden (guest cannot filter by project)"},
                status_code=403,
            )
    return await call_next(request)
