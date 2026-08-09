"""Server-Sent Events broadcast for ingest-completion notifications.

Each browser opens an SSE stream to /api/events and receives an
`ingest_done` event whenever a fresh ingest run lands in the DB. The
frontend re-fetches /api/dashboard on receipt — no full page reload.

Thread-safety:
- run_ingest() runs in APScheduler's worker thread (or in the request
  thread for /admin/ingest), so the broadcaster must be callable from
  outside the event loop.
- The captured loop reference (set at FastAPI startup) is the main
  uvicorn event loop; call_soon_threadsafe pushes into per-subscriber
  asyncio.Queue safely from any thread.
"""
from __future__ import annotations

import asyncio
import json
import threading

_subscribers: set[asyncio.Queue] = set()
_subscribers_lock = threading.Lock()
_main_loop: asyncio.AbstractEventLoop | None = None
_shutdown_event: asyncio.Event | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at startup. Required before broadcast_threadsafe works."""
    global _main_loop, _shutdown_event
    _main_loop = loop
    _shutdown_event = asyncio.Event()


def clear_loop() -> None:
    """Release lifespan-owned asyncio state before the loop closes."""
    global _main_loop, _shutdown_event
    _main_loop = None
    _shutdown_event = None


def shutdown_event() -> asyncio.Event | None:
    """Subscribed-to by SSE generators so they can exit promptly when the
    server is shutting down (otherwise uvicorn's graceful-shutdown waits
    forever for the never-ending response)."""
    return _shutdown_event


def signal_shutdown() -> None:
    """Called from lifespan exit. Wakes up every SSE generator."""
    loop = _main_loop
    event = _shutdown_event
    if event is None or loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # The event loop can close between is_closed() and this call.
        if not loop.is_closed():
            raise


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    with _subscribers_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _subscribers_lock:
        _subscribers.discard(q)


def broadcast_threadsafe(event: str, data: dict) -> None:
    """Push an SSE event to every connected subscriber. Safe from any
    thread (worker, scheduler, request handler). No-op if startup
    hasn't captured the loop yet."""
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _subscribers_lock:
        targets = list(_subscribers)

    def _put_all() -> None:
        for q in targets:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    try:
        loop.call_soon_threadsafe(_put_all)
    except RuntimeError:
        # Lifespan teardown can close the loop after the check above.
        if not loop.is_closed():
            raise
