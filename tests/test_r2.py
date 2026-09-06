import lzma
import shutil
import tempfile
from pathlib import Path

import pytest

from backend import r2


@pytest.fixture(name="mini_r2")
def _mini_r2_fixture(monkeypatch):
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


def test_get_object_inflates_xz(mini_r2):
    # An xz-compressed object inflates transparently to its plain bytes.
    plain = b'{"type":"user"}\n{"type":"assistant"}\n'
    key = "proj-a/sess-1/sess-1.jsonl.xz"
    (mini_r2 / "proj-a" / "sess-1" / "sess-1.jsonl.xz").write_bytes(
        lzma.compress(plain)
    )
    assert r2.get_object(key) == plain


def test_get_stream_inflates_xz(mini_r2):
    # Streaming a `.xz` key yields the decompressed lines.
    plain = b"alpha\nbeta\ngamma\n"
    (mini_r2 / "proj-a" / "sess-1" / "s.jsonl.xz").write_bytes(
        lzma.compress(plain)
    )
    with r2.get_stream("proj-a/sess-1/s.jsonl.xz") as fh:
        assert fh.read() == plain


def test_path_traversal_blocked(mini_r2):
    with pytest.raises(PermissionError):
        r2.get_object("../etc/passwd")
    with pytest.raises(PermissionError):
        r2.get_object("proj-a/../../../etc/passwd")


def test_list_keys_through_symlinked_root(mini_r2, tmp_path, monkeypatch):
    """A canonicalized prefix must still produce bucket-relative object keys."""
    alias = tmp_path / "mirror"
    try:
        alias.symlink_to(mini_r2.parent, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks is not permitted on this host")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{alias}/")
    all_keys = {obj.key for obj in r2.list_keys()}
    prefixed_keys = {obj.key for obj in r2.list_keys(prefix="proj-a")}
    assert prefixed_keys == {"proj-a/sess-1/sess-1.jsonl"}
    assert prefixed_keys <= all_keys
