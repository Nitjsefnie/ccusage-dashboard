import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


@pytest.fixture(autouse=True)
def _reset_response_cache():
    # response_cache is a process-global. Two tests with different fixtures
    # but identical query params would otherwise read each other's payloads.
    from backend import cache
    cache.response_cache.clear()
    yield
    cache.response_cache.clear()
