import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import session


def test_token_roundtrip():
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(99, secret)
    assert session.verify_session_token(tok, secret) == 99


def test_verify_rejects_wrong_secret():
    tok = session.make_session_token(42, "secret-a" * 4)
    assert session.verify_session_token(tok, "secret-b" * 4) is None


def test_verify_rejects_expired_token():
    secret = "k" * 32
    tok = session.make_session_token(7, secret)
    far_future = int(time.time()) + session.SESSION_COOKIE_MAX_AGE + 60
    with patch.object(session.time, "time", return_value=far_future):
        assert session.verify_session_token(tok, secret) is None


def test_verify_rejects_future_token():
    secret = "k" * 32
    payload = "5.99999999999.nonce"
    import hashlib, hmac as _hmac
    sig = _hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    tok = f"{payload}.{sig}"
    assert session.verify_session_token(tok, secret) is None


def test_parse_session_token_rejects_garbage():
    assert session.parse_session_token("not.a.real.token.too.many") is None
    assert session.parse_session_token("missing-dots") is None
    assert session.parse_session_token("a.b.c.d") is None


def test_get_or_create_session_secret_persists():
    config: dict = {}
    s1 = session.get_or_create_session_secret(config)
    assert config[session.WEB_SESSION_SECRET_KEY] == s1
    s2 = session.get_or_create_session_secret(config)
    assert s2 == s1


def test_check_origin_allows_safe_methods():
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "GET", "headers": [],
        "path": "/api/projects",
    }
    req = Request(scope)
    assert session.check_origin(req)


def test_check_origin_rejects_cross_origin_post():
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"https://evil.example.com"),
        ],
        "path": "/admin/ingest",
    }
    req = Request(scope)
    assert not session.check_origin(req)


def test_check_origin_accepts_same_origin_post():
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"https://viz.example.com"),
        ],
        "path": "/admin/ingest",
    }
    req = Request(scope)
    assert session.check_origin(req)


def test_guest_blocked_from_export():
    app = FastAPI()
    app.middleware("http")(session.auth_middleware)

    @app.get("/api/export")
    async def _stub():
        return {"ok": True}

    client = TestClient(app)
    guest_cookie = session.make_guest_session_token()
    client.cookies.set(session.SESSION_COOKIE_NAME, guest_cookie)
    resp = client.get("/api/export?range=7d")
    assert resp.status_code == 403
