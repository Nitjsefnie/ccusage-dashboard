"""/login GET + POST + /logout.

Login UI is inlined HTML (no shared layout chrome — ccudash uses
its own visualizer dark theme). Rate limiting: 5 failures per IP per
5-minute window.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from backend import auth
from backend import session as session_mod
from backend import db


router = APIRouter()

_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300


def _check_login_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [
        t for t in _LOGIN_FAILURES.get(ip, [])
        if now - t < _LOGIN_WINDOW_SECONDS
    ]
    _LOGIN_FAILURES[ip] = attempts
    return len(attempts) >= _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    now = time.time()
    attempts = [
        t for t in _LOGIN_FAILURES.get(ip, [])
        if now - t < _LOGIN_WINDOW_SECONDS
    ]
    attempts.append(now)
    _LOGIN_FAILURES[ip] = attempts


def user_exists(user_id: int) -> bool:
    """Cheap existence probe in the auth DB's users table."""
    with db.auth_conn() as c:
        row = c.execute(
            "SELECT 1 FROM users WHERE user_id = %s LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<title>Sign in · CCUSAGE</title>
<style>
  body {{ background:#0b0d10; color:#dde; font-family: 'Inter',sans-serif;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; }}
  form {{ background:#14181d; padding:24px 28px; border:1px solid #25303c;
         border-radius:8px; min-width:320px; }}
  h1 {{ margin:0 0 16px 0; font-size:18px; letter-spacing:.04em; color:#9bd; }}
  label {{ display:block; margin:10px 0 4px 0; font-size:12px; color:#8aa; }}
  input {{ width:100%; box-sizing:border-box; padding:8px 10px;
          background:#0e1216; color:#dde; border:1px solid #25303c;
          border-radius:4px; font: 14px 'JetBrains Mono', monospace; }}
  button {{ margin-top:18px; width:100%; padding:10px 14px;
           background:#1f6f9c; color:#fff; border:0; border-radius:4px;
           font-weight:600; cursor:pointer; }}
  .guest-btn {{ background:#1a1c2e; border:1px solid #25303c; }}
  .guest-btn:hover {{ background:#222640; }}
  .or {{ text-align:center; color:#556; font-size:11px; margin:16px 0 4px; letter-spacing:.2em; }}
  .err {{ color:#e76; font-size:12px; min-height:16px; margin-top:8px; }}
</style>
</head><body>
<form method="post" action="/login">
  <h1>CCUSAGE · sign in</h1>
  <label>Username</label>
  <input name="user_id" required inputmode="numeric" pattern="[0-9]+"
         autocomplete="username">
  <label>Password</label>
  <input name="password" type="password" required autocomplete="current-password">
  <button type="submit">Sign in</button>
  <div class="err">{err}</div>
  <div class="or">or</div>
  <button type="submit" class="guest-btn"
          formaction="/login/guest" formmethod="post" formnovalidate>
    Continue as guest
  </button>
</form>
</body></html>
"""


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML.format(err=""))


@router.post("/login")
async def login_post(
    request: Request,
    user_id: str = Form(""),
    password: str = Form(""),
) -> Response:
    ip = request.client.host if request.client else "unknown"
    if _check_login_rate_limit(ip):
        return Response(
            "Too many login attempts. Try again later.",
            status_code=429, media_type="text/plain",
        )
    try:
        uid = int(user_id.strip())
    except ValueError:
        uid = 0
    if uid <= 0:
        return Response(
            "Invalid user ID", status_code=400, media_type="text/plain"
        )
    if not user_exists(uid):
        return Response(
            "User not found.", status_code=404, media_type="text/plain"
        )
    config = session_mod.load_user_config(uid)
    if not config or not auth.has_web_password(config):
        return Response(
            "Password not configured.", status_code=503,
            media_type="text/plain",
        )
    if not auth.verify_web_password(config, password):
        _record_login_failure(ip)
        return Response(
            "Invalid password", status_code=401, media_type="text/plain"
        )
    secret = session_mod.get_or_create_session_secret(config)
    session_mod.write_user_config(uid, config)
    token = session_mod.make_session_token(uid, secret)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        session_mod.SESSION_COOKIE_NAME, token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict",
        max_age=session_mod.SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    return response


@router.get("/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(session_mod.SESSION_COOKIE_NAME, path="/")
    return response


@router.post("/login/guest")
async def login_guest(request: Request) -> Response:
    """Mint an unauthenticated 'guest' session — read-only, gated to
    aggregate-only views (no per-project filter, no per-session detail).
    Cookie invalidates on every server restart since the guest secret
    is regenerated."""
    token = session_mod.make_guest_session_token()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        session_mod.SESSION_COOKIE_NAME, token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict",
        max_age=session_mod.SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    return response
