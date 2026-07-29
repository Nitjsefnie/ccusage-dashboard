"""FastAPI entrypoint for claudit."""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, Response

from backend import api, db, events, ingest, login, session


_REPO_ROOT = Path(__file__).resolve().parent.parent
db.load_dotenv(str(_REPO_ROOT / ".env"))

_PUBLIC = _REPO_ROOT / "public"
_SRC = _REPO_ROOT / "src"


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    db.schema_check()
    events.set_loop(asyncio.get_running_loop())

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
    fastapi_app.state.scheduler = sched

    yield

    # Wake SSE generators so uvicorn's graceful-shutdown drains immediately
    # instead of waiting for the (never-ending) heartbeat response.
    events.signal_shutdown()
    sched.shutdown(wait=False)


app = FastAPI(
    title="claudit",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
    # /api/dashboard at range=all returns ~4.5 MB (8.8k ctx_traces spanning
    # 54k turns). Starlette's JSONResponse runs jsonable_encoder over that
    # whole structure and then stdlib json.dumps — measured at 475ms + 257ms.
    # orjson serialises the same payload in 11ms.
    default_response_class=ORJSONResponse,
)


class _SelectiveGZip(GZipMiddleware):
    """GZip everything except the SSE stream.

    GZipMiddleware cannot know a streaming response's size, so it
    compresses `/api/events` unconditionally — which buys nothing (events
    are a few bytes each), risks holding them in the compressor's buffer
    instead of delivering them live, and contradicts the `no-transform`
    the endpoint already sets.
    """

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/api/events":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# The origin was serving /api/dashboard uncompressed — ~3.2 MB per cold
# request, which the CDN then had to pull in full before it could
# compress and serve it on. The body is JSON and compresses ~5x.
# minimum_size skips the many small responses (/api/me, /api/models)
# where framing would cost more than it saves.
app.add_middleware(_SelectiveGZip, minimum_size=1024)
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
    # Live progress for the run in flight. ingest_runs only gains its
    # counters in the final UPDATE, and the derived-state rebuilds happen
    # AFTER finished_at is written — so a caller watching that row alone
    # sees nothing for minutes, then "done" while rebuilds are still going.
    prog = ingest.progress_snapshot()
    running = prog.get("phase") not in (None, "idle")
    ingest_progress = None
    if running:
        done, total = prog.get("done") or 0, prog.get("total") or 0
        ingest_progress = {
            "phase": prog.get("phase"),
            "done": done,
            "total": total,
            "pct": round(100.0 * done / total, 1) if total else None,
            "run_id": prog.get("run_id"),
            "started_at": prog.get("started_at"),
        }

    return {
        "ok": True, "db": True,
        "ingest_running": running,
        "ingest_progress": ingest_progress,
        "last_ingest": last_ingest,
        "parser_version": parser_version,
        "now": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/admin/ingest")
async def admin_ingest() -> dict:
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
    src_root = _PUBLIC.parent / "src"

    def _bust_src(m: re.Match) -> str:
        path = m.group(1)
        try:
            v = int((src_root / path.lstrip("/").removeprefix("src/")).stat().st_mtime)
        except OSError:
            return m.group(0)
        return m.group(0).replace(path, f"{path}?v={v}")

    html = re.sub(r'src="(/src/[^"?]+)"', _bust_src, html)
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
