"""The root VERSION file and its exposure through /health.

VERSION is the single source of truth for the release machinery:
`.github/workflows/release.yml` tags when it changes, and `speed.yml`
benchmarks HEAD against the release it names. Both read the file with
`cat`, so its shape matters as much as its content — a stray second line
or a `v` prefix would produce a malformed tag.
"""
from __future__ import annotations

import importlib
import re
import subprocess
from pathlib import Path

from backend import constants


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = REPO_ROOT / "VERSION"

# Bare semver, no leading `v`: release.yml prefixes the tag itself, so a
# `v` here would produce `vv0.1.0`.
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_file_exists():
    assert VERSION_PATH.is_file(), "VERSION is the release machinery's input"


def test_version_file_is_one_bare_semver_line():
    raw = VERSION_PATH.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 1, f"VERSION must hold exactly one line, got {lines!r}"
    assert SEMVER.match(lines[0]), (
        f"VERSION must be bare semver with no leading 'v', got {lines[0]!r}"
    )


def test_version_file_ends_with_a_newline():
    # `cat VERSION` in a workflow feeds a shell var; a missing trailing
    # newline is harmless to $(cat) but makes the file annoying to edit and
    # shows up as "\ No newline at end of file" in every release diff.
    assert VERSION_PATH.read_bytes().endswith(b"\n")


def test_constants_version_matches_the_file():
    assert constants.VERSION == VERSION_PATH.read_text(encoding="utf-8").strip()


def test_version_is_not_tracked_as_ignored():
    # The deny-by-default .gitignore names files back one at a time. A
    # VERSION that git cannot see would let release.yml tag a version the
    # repo never records.
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "VERSION"],
        cwd=REPO_ROOT, check=False,
    )
    assert proc.returncode != 0, ".gitignore hides VERSION from git"


def test_missing_version_file_degrades_to_unknown(monkeypatch, tmp_path):
    """A deploy without the file reports "unknown" rather than crashing.

    /health answering with an unknown version beats /health not answering.
    """
    missing = tmp_path / "backend" / "constants.py"
    missing.parent.mkdir(parents=True)
    missing.write_text("", encoding="utf-8")
    monkeypatch.setattr(constants, "__file__", str(missing))

    assert constants._read_version() == "unknown"  # pylint: disable=protected-access


def test_blank_version_file_degrades_to_unknown(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("   \n", encoding="utf-8")
    fake = tmp_path / "backend" / "constants.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(constants, "__file__", str(fake))

    assert constants._read_version() == "unknown"  # pylint: disable=protected-access


class _FakeCursor:
    """Enough of a psycopg cursor for /health's single ingest_runs query."""

    @staticmethod
    def fetchone():
        return None


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    @staticmethod
    def execute(*_args, **_kwargs):
        return _FakeCursor()


def test_health_error_branch_reports_version(monkeypatch):
    """`version` must survive the DB-error branch.

    "Which build is broken?" is precisely the question asked while /health
    is failing, so that is the worst possible moment for the field to be
    the one that goes missing.
    """
    app_mod = importlib.import_module("backend.app")

    def _boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(app_mod.db, "viz_conn", _boom)

    payload = app_mod.health()

    assert payload["ok"] is False
    assert payload["version"] == constants.VERSION
    assert "parser_version" in payload


def test_health_ok_branch_reports_version(monkeypatch):
    app_mod = importlib.import_module("backend.app")

    monkeypatch.setattr(app_mod.db, "viz_conn", _FakeConn)
    monkeypatch.setattr(
        app_mod.ingest, "progress_snapshot", lambda: {"phase": "idle"}
    )

    payload = app_mod.health()

    assert payload["ok"] is True
    assert payload["version"] == constants.VERSION
