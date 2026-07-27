"""Guard against DATABASE_URL_VIZ silently resolving to production (issue #4).

backend/app.py:20 calls db.load_dotenv(".env") at import time, and .env
points DATABASE_URL_VIZ at the live production `claudit` database.
load_dotenv only ever os.environ.setdefault()s, so whoever sets the
variable first wins — and conftest.py claims it before any test module is
collected. This is a backstop, not a replacement: DB-touching tests still
monkeypatch their own scratch database.

.env is gitignored and does not exist in CI, so any check here that reads
it must degrade gracefully rather than crash — see _dotenv_value below.
These tests hold conftest's claim in place, and reproduce the actual
trigger (a module-scope `import backend.app`) rather than relying on it
never happening.
"""
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The database backend/../.env names. Hardcoded so the check still means
# something in a checkout that has no .env at all (CI).
PRODUCTION_DB = "claudit"


def _dbname(dsn: str) -> str:
    """Database name out of a postgresql:// DSN, sans query string."""
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def _dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def test_conftest_default_wins_the_race_against_production_dotenv():
    """Reproduce the exact trigger a future unguarded test would hit —
    importing backend.app — and confirm DATABASE_URL_VIZ still isn't
    production afterward.

    Checks the *current* env value rather than asserting anything about
    what set it, so this holds whether conftest's setdefault won the race
    (nothing else claims the var, e.g. running this suite locally) or the
    environment already had a non-production DSN before conftest ran (e.g.
    CI setting it externally) — either is a pass; only production is a
    failure.
    """
    import backend.app  # noqa: F401  (import triggers db.load_dotenv(".env"))

    dsn = os.environ["DATABASE_URL_VIZ"]
    assert _dbname(dsn) != PRODUCTION_DB, (
        f"the suite is pointed at the production database: {dsn}"
    )

    configured = _dotenv_value(_REPO_ROOT / ".env", "DATABASE_URL_VIZ")
    if configured is not None:
        assert dsn != configured, (
            f"the suite is pointed at the DSN .env deploys against: {dsn}"
        )


def test_a_later_dotenv_load_cannot_repoint_the_suite_at_production(tmp_path):
    """load_dotenv is setdefault-only, which is what makes conftest's claim
    stick. If that ever changed, importing backend.app would drag every
    test onto production.
    """
    from backend import db

    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DATABASE_URL_VIZ=postgresql:///{PRODUCTION_DB}\n")

    before = os.environ["DATABASE_URL_VIZ"]
    db.load_dotenv(str(dotenv))

    assert os.environ["DATABASE_URL_VIZ"] == before
    assert _dbname(os.environ["DATABASE_URL_VIZ"]) != PRODUCTION_DB
