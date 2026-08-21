#!/usr/bin/env python3
"""Boot the real server and check it actually serves.

WHY THIS EXISTS. The pytest suite mounts `api.router` into a clean
FastAPI app, which is the right call for testing endpoints in isolation —
but it means `backend/app.py` is never executed as the application. Three
things live only there and are therefore only checked here:

  * `db.schema_check()`, the fail-fast that aborts startup on a bad schema
    (SV-SCHEMA-FAIL-FAST). A suite that never boots the app cannot tell
    you the fail-fast still fires.
  * the startup ingest and the APScheduler wiring around it.
  * the auth middleware and the index.html rewrite, both of which the
    router-only tests deliberately mount around.

Run against a throwaway Postgres and the committed fixtures/r2_mini
mirror. Usable locally, not just in CI:

    PGHOST=localhost PGUSER=postgres python3 scripts/ci/smoke.py

Exits nonzero with the server's own log on any failure.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VIZ_DB = "claudit_smoke"
AUTH_DB = "claudit_smoke_auth"

# The mini mirror holds 2 projects / 4 sessions / 1 sidecar. Ingesting it
# is a few hundred milliseconds, so a generous timeout here only ever
# costs time on a genuine hang.
BOOT_TIMEOUT_S = 120
POLL_INTERVAL_S = 0.5


class SmokeFailure(RuntimeError):
    """A check failed. Carries a message; the caller dumps the server log."""


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def psql(dbname: str, sql: str) -> None:
    """Run one statement, raising with stderr attached on failure."""
    proc = subprocess.run(
        ["psql", "--quiet", "--no-psqlrc", "-v", "ON_ERROR_STOP=1",
         "-d", dbname, "-c", sql],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SmokeFailure(
            f"psql on {dbname!r} failed: {sql!r}\n{proc.stderr.strip()}"
        )


def psql_file(dbname: str, path: Path) -> None:
    proc = subprocess.run(
        ["psql", "--quiet", "--no-psqlrc", "-v", "ON_ERROR_STOP=1",
         "-d", dbname, "-f", str(path)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SmokeFailure(
            f"applying {path.name} to {dbname!r} failed:\n{proc.stderr.strip()}"
        )


def recreate_database(dbname: str) -> None:
    """Drop and create, so a rerun on a dirty runner starts clean."""
    subprocess.run(["dropdb", "--if-exists", dbname],
                   capture_output=True, text=True, check=False)
    proc = subprocess.run(["createdb", dbname],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SmokeFailure(
            f"createdb {dbname!r} failed:\n{proc.stderr.strip()}"
        )


def provision() -> None:
    """Both databases, in the shape db.schema_check() insists on."""
    log(f"creating {VIZ_DB} and applying backend/schema.sql")
    recreate_database(VIZ_DB)
    psql_file(VIZ_DB, REPO_ROOT / "backend" / "schema.sql")

    # The auth DB is external in production and this repo owns no schema
    # for it. schema_check() requires exactly one thing — users.config as
    # JSONB — so that is exactly what gets built, and nothing more: a
    # richer fake would drift from the real table without anyone noticing.
    log(f"creating {AUTH_DB} with the minimal users.config shape")
    recreate_database(AUTH_DB)
    psql(AUTH_DB, "CREATE TABLE users (id BIGINT PRIMARY KEY, "
                  "config JSONB NOT NULL DEFAULT '{}'::jsonb)")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(port: int) -> dict:
    env = dict(os.environ)
    env.update({
        "DATABASE_URL_VIZ": f"postgresql:///{VIZ_DB}",
        "DATABASE_URL_AUTH": f"postgresql:///{AUTH_DB}",
        # The committed mini mirror, through the R2 client's file:// mode.
        # The trailing slash matters: r2.py treats the endpoint as a
        # directory root.
        "R2_ENDPOINT": f"file://{REPO_ROOT / 'fixtures' / 'r2_mini'}/",
        "R2_BUCKET": "claude",
        "R2_ACCOUNT_ID": "",
        "R2_ACCESS_KEY_ID": "",
        "R2_SECRET_ACCESS_KEY": "",
        "PARSER_VERSION": "smoke",
        "ADMIN_TOKEN": "smoke-admin",
        # TestClient-equivalent: this talks plain HTTP, so Secure cookies
        # would never come back and the guest flow could not be checked.
        "COOKIE_SECURE": "0",
        "CLAUDIT_WARM_CACHE": "0",
        "PORT": str(port),
    })
    return env


def get(url: str, cookie: str = "") -> tuple:
    """(status, body, headers). Never raises on an HTTP error status.

    Headers come back as the raw email.message.Message, NOT a dict:
    `dict(resp.headers)` looks equivalent and is not. It loses the
    case-insensitive lookup, and Starlette emits `set-cookie` in lower
    case, so a `.get("Set-Cookie")` against the dict silently returns None
    and this script reports "the server set no cookie".
    """
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a 3xx into an HTTPError instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_no_redirect(url: str) -> tuple:
    """POST that does NOT follow redirects — the Set-Cookie is the point."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers


def wait_for_health(base: str, proc: subprocess.Popen) -> dict:
    """Poll /health until ingest reports finished.

    Polls the PROCESS as well as the port. A server that died during the
    startup ingest would otherwise leave this spinning until the timeout
    and report "never became ready", hiding a traceback that is sitting in
    the log right now.
    """
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    last = "no response yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SmokeFailure(
                f"server exited with code {proc.returncode} during startup"
            )
        try:
            status, body, _ = get(f"{base}/health")
        except OSError as exc:            # not listening yet
            last = f"connect: {exc}"
            time.sleep(POLL_INTERVAL_S)
            continue

        if status != 200:
            last = f"HTTP {status}: {body[:200]!r}"
            time.sleep(POLL_INTERVAL_S)
            continue

        payload = json.loads(body)
        if not payload.get("ok"):
            last = f"not ok: {payload.get('error')!r}"
            time.sleep(POLL_INTERVAL_S)
            continue
        if payload.get("ingest_running"):
            prog = payload.get("ingest_progress") or {}
            last = f"ingest {prog.get('phase')} {prog.get('pct')}%"
            log(f"waiting: {last}")
            time.sleep(POLL_INTERVAL_S)
            continue
        if not (payload.get("last_ingest") or {}).get("finished_at"):
            last = "no finished ingest recorded yet"
            time.sleep(POLL_INTERVAL_S)
            continue
        return payload

    raise SmokeFailure(f"/health never reported a finished ingest ({last})")


def check_health(payload: dict) -> None:
    expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if payload.get("version") != expected:
        raise SmokeFailure(
            f"/health version is {payload.get('version')!r}, "
            f"VERSION says {expected!r}"
        )
    run = payload.get("last_ingest") or {}
    if run.get("error"):
        raise SmokeFailure(f"startup ingest recorded an error: {run['error']}")
    if not run.get("r2_listed"):
        raise SmokeFailure(
            "startup ingest listed 0 objects — it did not read the mirror, "
            "so a green run here would prove nothing"
        )
    log(f"health ok: version={payload['version']} "
        f"listed={run['r2_listed']} reparsed={run.get('reparsed')}")


def check_auth_is_enforced(base: str) -> None:
    """The middleware is mounted. Router-only tests cannot see this."""
    status, _, _ = get(f"{base}/api/dashboard?range=all")
    if status != 401:
        raise SmokeFailure(
            f"GET /api/dashboard unauthenticated returned {status}, want 401 — "
            "the auth middleware is not doing its job"
        )
    log("auth enforced: unauthenticated /api/dashboard is 401")


def check_guest_flow(base: str) -> None:
    status, headers = post_no_redirect(f"{base}/login/guest")
    if status != 303:
        raise SmokeFailure(f"POST /login/guest returned {status}, want 303")
    raw = headers.get("Set-Cookie")
    if not raw:
        raise SmokeFailure("POST /login/guest set no cookie")
    cookie = raw.split(";", 1)[0]

    status, body, _ = get(f"{base}/api/dashboard?range=all", cookie=cookie)
    if status != 200:
        raise SmokeFailure(
            f"guest GET /api/dashboard returned {status}: {body[:300]!r}"
        )
    payload = json.loads(body)
    # Summed from the hourly buckets rather than read off a totals field,
    # because that is the series the dashboard actually renders: a
    # regression that empties the buckets while leaving a total intact is
    # exactly the one worth catching.
    requests = sum(int(bucket.get("requests") or 0)
                   for bucket in payload.get("hourly") or [])
    sessions = int(payload.get("total_sessions") or 0)
    if not requests or not sessions:
        raise SmokeFailure(
            "guest dashboard is empty — the fixtures were ingested but did "
            f"not reach the read path: {body[:300]!r}"
        )
    if not payload.get("cost_by_model"):
        raise SmokeFailure("guest dashboard has no cost_by_model breakdown")
    log(f"guest flow ok: {requests} requests across {sessions} sessions")


def check_index_is_rewritten(base: str) -> None:
    """app.py rewrites index.html per request; nothing else covers that."""
    status, body, _ = get(f"{base}/login")
    if status != 200:
        raise SmokeFailure(f"GET /login returned {status}, want 200")
    text = body.decode("utf-8", "replace")
    if "<form" not in text.lower():
        raise SmokeFailure("GET /login served no form")
    log("login page ok")


def run_checks(base: str, proc: subprocess.Popen) -> None:
    check_health(wait_for_health(base, proc))
    check_index_is_rewritten(base)
    check_auth_is_enforced(base)
    check_guest_flow(base)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-databases", action="store_true",
        help="do not drop the throwaway databases on exit (for debugging)",
    )
    args = parser.parse_args()

    try:
        provision()
    except SmokeFailure as exc:
        print(f"[smoke] FAILED during provisioning: {exc}", file=sys.stderr)
        return 1

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log(f"booting backend.app:app on {base}")

    # Output goes to a pipe we drain at the end rather than to the console,
    # so a failure report shows the server's log next to the failed check
    # instead of interleaved with it hundreds of lines earlier.
    with subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--timeout-graceful-shutdown", "5"],
        cwd=str(REPO_ROOT), env=server_env(port),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ) as proc:
        failure = None
        try:
            run_checks(base, proc)
        except SmokeFailure as exc:
            failure = str(exc)
        except Exception as exc:          # pylint: disable=broad-except
            failure = f"unexpected {type(exc).__name__}: {exc}"
        finally:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()

        if failure is not None:
            print(f"\n[smoke] FAILED: {failure}\n", file=sys.stderr)
            print("---- server log ----", file=sys.stderr)
            print(out, file=sys.stderr)
            print("---- end server log ----", file=sys.stderr)
            return 1

    if not args.keep_databases:
        for dbname in (VIZ_DB, AUTH_DB):
            subprocess.run(["dropdb", "--if-exists", dbname],
                           capture_output=True, text=True, check=False)

    log("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
