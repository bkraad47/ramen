"""Bucket prefix → local dir sync (CONTRACTS §7 gs://, §8 s3://, §21.6 az://). md5-based: downloads changed/new
objects, deletes stale files. Local state (`.ramen*`, `__pycache__`) is left alone. Auth is ADC / Workload Identity on
GCP, the default boto3 chain (IRSA in EKS) on AWS, and DefaultAzureCredential (AKS workload identity) on Azure, where
the URI is `az://<storage account>/<container>/<prefix>`; `RAMEN_AZURE_BLOB_ENDPOINT` overrides the account URL
(`https://<account>.blob.core.windows.net`), e.g. for Azurite."""

import base64
import hashlib
import os
from pathlib import Path

from .log import log

KEEP = (".ramen", "__pycache__")
SCHEMES = ("gs://", "s3://", "az://")


class SyncError(RuntimeError):
    pass


def scheme(uri: str) -> str:
    for s in SCHEMES:
        if uri.startswith(s):
            return s[:2]
    raise ValueError(f"bucket uri must start with gs://, s3:// or az://: {uri}")


def parse_uri(uri: str) -> tuple[str, str]:
    """(bucket, prefix); on az:// the bucket is `<account>/<container>` and the prefix what follows the container."""
    kind = scheme(uri)
    name, _, prefix = uri[5:].partition("/")
    if kind == "az":
        container, _, prefix = prefix.partition("/")
        if not name or not container:
            raise ValueError(f"az:// uri needs a storage account and a container: {uri}")
        name = f"{name}/{container}"
    return name, prefix.strip("/")


def _client(kind: str, name: str = ""):
    if kind == "az":
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as e:
            raise SyncError("azure-storage-blob is not installed (pip install 'ramen-runtime[azure]')") from e
        account = name.split("/", 1)[0]
        url = os.environ.get("RAMEN_AZURE_BLOB_ENDPOINT") or f"https://{account}.blob.core.windows.net"
        return BlobServiceClient(url, credential=DefaultAzureCredential())
    if kind == "s3":
        try:
            import boto3
        except ImportError as e:
            raise SyncError("boto3 is not installed (pip install 'ramen-runtime[aws]')") from e
        return boto3.client("s3")
    try:
        from google.cloud import storage
    except ImportError as e:
        raise SyncError("google-cloud-storage is not installed (pip install 'ramen-runtime[gcp]')") from e
    return storage.Client()


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _local_state(rel: Path) -> bool:
    return any(p.startswith(KEEP) for p in rel.parts)


def _gcs_objects(client, name, prefix):
    """Yield (object name, md5 hex or None, download(path)) for every object under the prefix."""
    for blob in client.list_blobs(name, prefix=f"{prefix}/" if prefix else None):
        digest = base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None
        yield blob.name, digest, lambda p, b=blob: b.download_to_filename(p)


def _s3_objects(client, name, prefix):
    kw = {"Bucket": name}
    if prefix:
        kw["Prefix"] = f"{prefix}/"
    for page in client.get_paginator("list_objects_v2").paginate(**kw):
        for o in page.get("Contents", []):
            etag = o.get("ETag", "").strip('"')
            digest = etag if etag and "-" not in etag else None  # multipart ETags are not an md5: always re-download
            yield o["Key"], digest, lambda p, k=o["Key"]: client.download_file(name, k, p)


def _az_objects(client, name, prefix):
    container = client.get_container_client(name.split("/", 1)[1])
    for blob in container.list_blobs(name_starts_with=f"{prefix}/" if prefix else None):
        md5 = getattr(blob.content_settings, "content_md5", None) if blob.content_settings else None
        digest = bytes(md5).hex() if md5 else None  # blobs written in blocks may carry none: always re-download

        def download(path, n=blob.name):
            with open(path, "wb") as f:
                container.download_blob(n).readinto(f)

        yield blob.name, digest, download


def sync(uri: str, dest: Path, client=None) -> dict:
    kind = scheme(uri)
    name, prefix = parse_uri(uri)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    client = client or _client(kind, name)
    strip = len(prefix) + 1 if prefix else 0
    wanted: set[Path] = set()
    downloaded = unchanged = 0
    objects = {"s3": _s3_objects, "az": _az_objects}.get(kind, _gcs_objects)
    for key, digest, download in objects(client, name, prefix):
        rel = key[strip:]
        if not rel or rel.endswith("/"):
            continue
        local = dest / rel
        wanted.add(local)
        if local.is_file() and digest and _md5(local) == digest:
            unchanged += 1
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        download(str(local))
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
