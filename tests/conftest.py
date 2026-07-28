# Ordering is load-bearing: the DATABASE_URL_VIZ setdefault below MUST run
# before anything imports backend.app (directly, or transitively via
# backend.api/db/etc.), because backend/app.py:20 calls
# db.load_dotenv(".env") at import time, and .env pins DATABASE_URL_VIZ at
# the live production `claudit` database. Since db.load_dotenv only ever
# os.environ.setdefault()s (never overwrites), the first setdefault to run
# for this key wins the race for the whole test process. backend.cache is
# safe to import above it: it is stdlib-only and never touches the env.
import os
import sys
from pathlib import Path

import pytest

from backend import cache

os.environ.setdefault("DATABASE_URL_VIZ", "postgresql:///claudit_test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ...and this directory, so one test module can import another's fixture
# instead of duplicating an expensive fresh-DB + mini-R2 setup.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Force file-mode R2 for unit tests; pytest never hits real R2.
os.environ.setdefault("R2_ENDPOINT", "file:///tmp/sv-test-r2/")
os.environ.setdefault("R2_BUCKET", "claude")
os.environ.setdefault("R2_ACCOUNT_ID", "")
os.environ.setdefault("R2_ACCESS_KEY_ID", "")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "")
os.environ.setdefault("PARSER_VERSION", "test")
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
# TestClient runs over plain HTTP — Secure-flag cookies would never come back.
os.environ.setdefault("COOKIE_SECURE", "0")
# No background cache warming under test: a warm queued by run_ingest
# outlives the fixture that created its DB, and its queries then race the
# teardown that drops it — producing failures in unrelated tests.
os.environ["CLAUDIT_WARM_CACHE"] = "0"


@pytest.fixture(autouse=True)
def _reset_response_cache():
    # response_cache is a process-global. Two tests with different fixtures
    # but identical query params would otherwise read each other's payloads.
    cache.response_cache.clear()
    yield
    cache.response_cache.clear()
