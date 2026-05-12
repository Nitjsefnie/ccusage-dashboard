"""FastAPI entrypoint for ccudash."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, Response

from backend import api, db, login, session


_REPO_ROOT = Path(__file__).resolve().parent.parent
db.load_dotenv(str(_REPO_ROOT / ".env"))

_PUBLIC = _REPO_ROOT / "public"
_SRC = _REPO_ROOT / "src"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio as _asyncio
    db.schema_check()
    from backend import ingest, events as _events
    _events.set_loop(_asyncio.get_running_loop())

    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    # Hourly maintenance.
    sched.add_job(
        lambda: ingest.run_ingest(trigger="cron"),
        "cron", minute=15,
    )
    # Startup ingest: fire ASAP via a one-shot in the scheduler thread so
    # lifespan returns immediately and uvicorn starts serving. /health
    # reflects ingest state via the ingest_runs table.
    sched.add_job(
        lambda: ingest.run_ingest(trigger="startup"),
        next_run_time=datetime.now(timezone.utc),
    )
    sched.start()
    app.state.scheduler = sched

    yield

    # Wake SSE generators so uvicorn's graceful-shutdown drains immediately
    # instead of waiting for the (never-ending) heartbeat response.
    _events.signal_shutdown()
    sched.shutdown(wait=False)


app = FastAPI(
    title="ccudash",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.middleware("http")(session.auth_middleware)
app.include_router(login.router)
app.include_router(api.router)


@app.get("/health")
def health() -> dict:
    parser_version = os.environ.get("PARSER_VERSION", "?")
    last_ingest = None
    try:
        with db.viz_conn() as c:
            row = c.execute(
                "SELECT id, started_at, finished_at, trigger, "
                "r2_listed, reparsed, error "
                "FROM ingest_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                last_ingest = {
                    "id": row[0],
                    "started_at": row[1].isoformat() if row[1] else None,
                    "finished_at": row[2].isoformat() if row[2] else None,
                    "trigger": row[3],
                    "r2_listed": row[4],
                    "reparsed": row[5],
                    "error": row[6],
                }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "db": False, "error": str(e),
            "parser_version": parser_version,
            "now": datetime.now(timezone.utc).isoformat(),
        }
    return {
        "ok": True, "db": True,
        "last_ingest": last_ingest,
        "parser_version": parser_version,
        "now": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/admin/ingest")
async def admin_ingest() -> dict:
    from backend import ingest
    return ingest.run_ingest(trigger="manual")


@app.get("/")
async def root_index(request: Request) -> Response:
    html = (_PUBLIC / "index.html").read_text(encoding="utf-8")
    # The default in-page snippet is `window.BACKEND_URL || ''`; when we
    # serve from the backend, set it to '/' so the frontend knows to use
    # this origin for /api/* fetches. Inject IS_GUEST in the same shot
    # so the React initial render already knows whether to hide
    # guest-restricted UI — prevents the brief flash of Sessions/
    # Inspector tabs before /api/me resolves.
    is_guest = bool(getattr(request.state, "is_guest", False))
    html = html.replace(
        "<script>window.BACKEND_URL = window.BACKEND_URL || '';</script>",
        f"<script>window.BACKEND_URL = '/'; window.IS_GUEST = {str(is_guest).lower()};</script>",
    )
    # Bust intermediary caches (Cloudflare, browser) on every static-asset
    # change by appending the file's mtime to its URL. Cache lookup keys
    # by URL, so a different ?v= forces a full fetch.
    html = html.replace(
        'href="/app.css"',
        f'href="/app.css?v={int((_PUBLIC / "app.css").stat().st_mtime)}"',
    )
    # Also bust /src/* JSX/JS modules so Babel always picks up the latest.
    import re as _re
    src_root = _PUBLIC.parent / "src"

    def _bust_src(m: _re.Match) -> str:
        path = m.group(1)
        try:
            v = int((src_root / path.lstrip("/").removeprefix("src/")).stat().st_mtime)
        except OSError:
            return m.group(0)
        return m.group(0).replace(path, f"{path}?v={v}")

    html = _re.sub(r'src="(/src/[^"?]+)"', _bust_src, html)
    return HTMLResponse(html)


@app.get("/app.css")
async def root_css() -> Response:
    return FileResponse(
        str(_PUBLIC / "app.css"),
        media_type="text/css",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# /src/* is mounted via StaticFiles. The middleware gates it because the
# path doesn't start with /api or /admin and isn't in _AUTH_PUBLIC_PATHS.
app.mount("/src", StaticFiles(directory=str(_SRC)), name="src")
