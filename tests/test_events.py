"""SSE broadcaster lifecycle regressions."""
import asyncio

from fastapi import FastAPI
import pytest

import backend.app as app_mod
from backend import events


def test_broadcast_after_loop_closed_is_a_noop(monkeypatch):
    """A scheduler job finishing after shutdown must not raise from its
    daemon thread when the captured event loop has already closed."""
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    loop = asyncio.new_event_loop()
    events.set_loop(loop)
    loop.close()

    events.broadcast_threadsafe("late", {"ok": True})


@pytest.mark.asyncio
async def test_lifespan_signals_before_clearing_loop(monkeypatch):
    """SSE drain must be signalled before broadcaster state is cleared."""
    calls = []

    class Scheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def shutdown(self, *, wait):
            calls.append(("scheduler", wait))

    monkeypatch.setattr(app_mod.db, "schema_check", lambda: None)
    monkeypatch.setattr(app_mod, "BackgroundScheduler", Scheduler)
    monkeypatch.setattr(
        app_mod.events, "set_loop", lambda _loop: calls.append("set")
    )
    monkeypatch.setattr(
        app_mod.events, "signal_shutdown", lambda: calls.append("signal")
    )
    monkeypatch.setattr(
        app_mod.events,
        "clear_loop",
        lambda: calls.append("clear"),
        raising=False,
    )

    async with app_mod.lifespan(FastAPI()):
        pass

    assert calls[-3:] == ["signal", ("scheduler", False), "clear"]
