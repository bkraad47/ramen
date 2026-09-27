"""GCS prefix → local dir sync (CONTRACTS §7). md5-based: downloads changed/new objects, deletes stale files.
Local state (`.ramen*`, `__pycache__`) is left alone. Client auth is ADC / Workload Identity."""
import base64
import hashlib
from pathlib import Path

from .log import log

KEEP = (".ramen", "__pycache__")


class SyncError(RuntimeError):
    pass


def parse_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"bucket uri must start with gs://: {uri}")
    name, _, prefix = uri[5:].partition("/")
    return name, prefix.strip("/")


def _client():
    try:
        from google.cloud import storage
    except ImportError as e:
        raise SyncError("google-cloud-storage is not installed (pip install 'ramen-runtime[gcp]')") from e
    return storage.Client()


def _md5(path: Path) -> str:
    return base64.b64encode(hashlib.md5(path.read_bytes()).digest()).decode()


def _local_state(rel: Path) -> bool:
    return any(p.startswith(KEEP) for p in rel.parts)


def sync(uri: str, dest: Path, client=None) -> dict:
    name, prefix = parse_uri(uri)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    client = client or _client()
    strip = len(prefix) + 1 if prefix else 0
    wanted: set[Path] = set()
    downloaded = unchanged = 0
    for blob in client.list_blobs(name, prefix=f"{prefix}/" if prefix else None):
        rel = blob.name[strip:]
        if not rel or rel.endswith("/"):
            continue
        local = dest / rel
        wanted.add(local)
        if local.is_file() and blob.md5_hash and _md5(local) == blob.md5_hash:
            unchanged += 1
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local))
        downloaded += 1
    deleted = 0
    for f in sorted(dest.rglob("*"), key=lambda p: -len(p.parts)):
        if _local_state(f.relative_to(dest)):
            continue
        if f.is_file() and f not in wanted:
            f.unlink()
            deleted += 1
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()
    out = {"uri": uri, "downloaded": downloaded, "unchanged": unchanged, "deleted": deleted, "total": len(wanted)}
    log("info", "bucket synced", **out)
    return out
