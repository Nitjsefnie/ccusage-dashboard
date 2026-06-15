"""R2 (S3 API) client with a file:// mode for the local mirror.

When R2_ENDPOINT starts with 'file://', the client walks the local
directory tree at the path. Otherwise it uses boto3 against R2's
S3-compatible endpoint.

API surface:
- list_keys(prefix='') -> iterator of R2Object
- get_object(key) -> bytes
- get_stream(key) -> file-like (for line-streaming large transcripts)
"""
from __future__ import annotations

import hashlib
import lzma
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple
from urllib.parse import urlparse


class R2Object(NamedTuple):
    key: str
    etag: str
    size: int
    last_modified: datetime


def _is_file_mode() -> tuple[bool, str]:
    """Return (in_file_mode, root_path). root_path is '' when not in file mode."""
    endpoint = os.environ.get("R2_ENDPOINT", "")
    if endpoint.startswith("file://"):
        parsed = urlparse(endpoint)
        return True, parsed.path
    return False, ""


def _safe_join(root: str, key: str) -> str:
    """Join root + key, refuse keys that escape the bucket root.

    Defense for design doc risk #4: a malicious sidecar request like
    '?path=../../../etc/passwd' must not escape /tmp/.../r2/.
    """
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, key))
    if not (full == base or full.startswith(base + os.sep)):
        raise PermissionError(f"key escapes bucket root: {key!r}")
    return full


def list_keys(prefix: str = "") -> Iterator[R2Object]:
    file_mode, root = _is_file_mode()
    bucket = os.environ.get("R2_BUCKET", "claude")
    if file_mode:
        bucket_root = os.path.join(root, bucket) if os.path.isdir(
            os.path.join(root, bucket)
        ) else root
        scan_root = bucket_root
        prefix_path = _safe_join(scan_root, prefix) if prefix else scan_root
        if not os.path.isdir(prefix_path):
            return
        for dp, _dirs, fns in os.walk(prefix_path, followlinks=True):
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, scan_root).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                etag = hashlib.sha1(
                    f"{int(st.st_mtime_ns)}:{st.st_size}".encode()
                ).hexdigest()
                yield R2Object(
                    key=rel,
                    etag=etag,
                    size=st.st_size,
                    last_modified=datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ),
                )
    else:
        s3 = _boto_client()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                yield R2Object(
                    key=o["Key"],
                    etag=str(o["ETag"]).strip('"'),
                    size=int(o["Size"]),
                    last_modified=o["LastModified"],
                )


def get_object(key: str) -> bytes:
    file_mode, root = _is_file_mode()
    bucket = os.environ.get("R2_BUCKET", "claude")
    if file_mode:
        scan_root = os.path.join(root, bucket) if os.path.isdir(
            os.path.join(root, bucket)
        ) else root
        full = _safe_join(scan_root, key)
        with open(full, "rb") as f:
            data = f.read()
    else:
        s3 = _boto_client()
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    # Bucket objects may be stored per-object xz-compressed (`*.jsonl.xz`).
    # Inflate transparently so callers (ingest, transcript serving) always
    # see the plain JSONL bytes. xz is stdlib (`lzma`) — no extra dependency.
    if key.endswith(".xz"):
        data = lzma.decompress(data)
    return data


def get_stream(key: str):
    """Open a streaming reader. Caller is responsible for closing it.

    For `*.jsonl.xz` keys the returned stream transparently inflates xz on
    read (stdlib `lzma`), so callers line-iterate plain JSONL either way.
    """
    file_mode, root = _is_file_mode()
    bucket = os.environ.get("R2_BUCKET", "claude")
    if file_mode:
        scan_root = os.path.join(root, bucket) if os.path.isdir(
            os.path.join(root, bucket)
        ) else root
        full = _safe_join(scan_root, key)
        raw = open(full, "rb")
    else:
        s3 = _boto_client()
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"]
    if key.endswith(".xz"):
        return lzma.LZMAFile(raw)
    return raw


def _boto_client():
    import boto3
    endpoint = os.environ["R2_ENDPOINT"]
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
