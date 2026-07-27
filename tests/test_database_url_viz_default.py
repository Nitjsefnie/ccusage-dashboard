"""Guard against DATABASE_URL_VIZ silently resolving to production (issue #4).

backend/app.py:20 calls db.load_dotenv(".env") at import time, which
os.environ.setdefault()s DATABASE_URL_VIZ onto the LIVE production
`claudit` database (see .env). conftest.py sets a scratch-DB default for
this key before any test module is collected, so it wins that setdefault
race — but only if nothing beats it to the punch. This test reproduces the
exact trigger (importing backend.app, which is what a future test that
forgets to monkeypatch its own scratch DB would do transitively) and
asserts the race stayed won.
"""
from pathlib import Path

import os

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _production_database_url_viz() -> str:
    """Read DATABASE_URL_VIZ straight out of .env — the DSN db.load_dotenv
    would setdefault onto the process if nothing claims the key first."""
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL_VIZ="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("DATABASE_URL_VIZ not found in .env — test setup drifted")


def test_conftest_default_wins_the_race_against_production_dotenv():
    import backend.app  # noqa: F401  (import triggers db.load_dotenv(".env"))

    production_dsn = _production_database_url_viz()
    assert os.environ.get("DATABASE_URL_VIZ") != production_dsn, (
        f"DATABASE_URL_VIZ == {production_dsn!r} — this is the LIVE "
        "production claudit database. conftest.py's scratch-DB setdefault "
        "must win the import race against backend.app's "
        "db.load_dotenv('.env') call (see conftest.py header comment, "
        "upstream issue #4)."
    )
