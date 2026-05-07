import os
import shutil
import tempfile
from pathlib import Path

import pytest

from backend import r2


@pytest.fixture
def mini_r2(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="sv-test-r2-")
    root = Path(tmp) / "claude"
    (root / "proj-a" / "sess-1").mkdir(parents=True)
    (root / "proj-a" / "sess-1" / "sess-1.jsonl").write_text("hello\n")
    (root / "proj-b" / "sess-2").mkdir(parents=True)
    (root / "proj-b" / "sess-2" / "sess-2.jsonl").write_text("world\n")
    (root / "proj-b" / "sess-2" / "data" / "tool-results").mkdir(
        parents=True
    )
    (root / "proj-b" / "sess-2" / "data" / "tool-results" / "x.txt"
     ).write_text("payload")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/")
    yield root
    shutil.rmtree(tmp)


def test_list_keys_walks_recursively(mini_r2):
    keys = sorted(o.key for o in r2.list_keys())
    assert keys == [
        "proj-a/sess-1/sess-1.jsonl",
        "proj-b/sess-2/data/tool-results/x.txt",
        "proj-b/sess-2/sess-2.jsonl",
    ]


def test_list_keys_with_prefix(mini_r2):
    keys = [o.key for o in r2.list_keys(prefix="proj-a")]
    assert keys == ["proj-a/sess-1/sess-1.jsonl"]


def test_get_object(mini_r2):
    assert r2.get_object("proj-a/sess-1/sess-1.jsonl") == b"hello\n"


def test_path_traversal_blocked(mini_r2):
    with pytest.raises(PermissionError):
        r2.get_object("../etc/passwd")
    with pytest.raises(PermissionError):
        r2.get_object("proj-a/../../../etc/passwd")
