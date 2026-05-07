import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend import login as login_mod
from backend import session as session_mod
from backend import auth


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The login module's rate-limit dict is process-global; clear between tests
    so test_session_cookie_round_trip doesn't inherit failures from
    test_rate_limit_after_5_failures (both POST from the same TestClient host).
    """
    login_mod._LOGIN_FAILURES.clear()
    yield
    login_mod._LOGIN_FAILURES.clear()


@pytest.fixture
def app(monkeypatch):
    """Build a fresh FastAPI app per test, with the auth DB mocked."""
    from fastapi import FastAPI
    from starlette.requests import Request
    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    a.include_router(login_mod.router)

    @a.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return a


@pytest.fixture
def fake_user(monkeypatch):
    """Stub the auth DB with one user that has a known password."""
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}

    def _load(user_id):
        return store.get(user_id)

    def _write(user_id, cfg):
        store[user_id] = cfg

    def _exists(user_id):
        return user_id in store

    monkeypatch.setattr(session_mod, "load_user_config", _load)
    monkeypatch.setattr(session_mod, "write_user_config", _write)
    monkeypatch.setattr(login_mod, "user_exists", _exists)
    return store


def test_login_page_is_html(app):
    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text and "user_id" in r.text


def test_successful_login_sets_cookie(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session_mod.SESSION_COOKIE_NAME in r.cookies


def test_wrong_password_is_401(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "wrong"},
    )
    assert r.status_code == 401


def test_unknown_user_is_404(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "999", "password": "anything"},
    )
    assert r.status_code == 404


def test_rate_limit_after_5_failures(app, fake_user):
    client = TestClient(app)
    for _ in range(5):
        client.post("/login", data={"user_id": "12345", "password": "x"})
    r = client.post("/login", data={"user_id": "12345", "password": "x"})
    assert r.status_code == 429


def test_logout_clears_cookie(app, fake_user):
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert any(
        session_mod.SESSION_COOKIE_NAME in v
        for v in r.headers.get_list("set-cookie")
    )


def test_session_cookie_round_trip(app, fake_user):
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": 12345}
