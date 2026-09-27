import base64
import hashlib

import pytest

from ramen_runtime import bucket


class FakeBlob:
    def __init__(self, name: str, data: bytes):
        self.name, self.data = name, data
        self.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()

    def download_to_filename(self, path):
        with open(path, "wb") as f:
            f.write(self.data)


class FakeClient:
    def __init__(self, blobs):
        self.blobs, self.calls = blobs, []

    def list_blobs(self, name, prefix=None):
        self.calls.append((name, prefix))
        return [b for b in self.blobs if b.name.startswith(prefix or "")]


def test_parse_uri():
    assert bucket.parse_uri("gs://b/demo") == ("b", "demo")
    assert bucket.parse_uri("gs://b/demo/") == ("b", "demo")
    assert bucket.parse_uri("gs://b") == ("b", "")
    with pytest.raises(ValueError):
        bucket.parse_uri("s3://b/x")


def test_sync_downloads_skips_unchanged_and_deletes_stale(tmp_path):
    dest = tmp_path / "bucket"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    (dest / "mcp").mkdir()
    (dest / "mcp" / "same.py").write_text("same")
    (dest / "mcp" / ".ramen_requirements.sha256").write_text("keep")
    (dest / "mcp" / "__pycache__").mkdir()
    (dest / "mcp" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    client = FakeClient([FakeBlob("demo/mcp/same.py", b"same"), FakeBlob("demo/mcp/new.json", b"{}"), FakeBlob("demo/mcp/", b""), FakeBlob("other/x", b"x")])
    s = bucket.sync("gs://b/demo", dest, client=client)
    assert client.calls == [("b", "demo/")]
    assert s == {"uri": "gs://b/demo", "downloaded": 1, "unchanged": 1, "deleted": 1, "total": 2}
    assert (dest / "mcp" / "new.json").read_text() == "{}"
    assert not (dest / "stale.txt").exists()
    assert (dest / "mcp" / ".ramen_requirements.sha256").exists() and (dest / "mcp" / "__pycache__" / "x.pyc").exists()
    # second run: nothing changes; changed remote content is re-downloaded
    client.blobs[0] = FakeBlob("demo/mcp/same.py", b"changed")
    s = bucket.sync("gs://b/demo", dest, client=client)
    assert (s["downloaded"], s["deleted"], s["unchanged"]) == (1, 0, 1)
    assert (dest / "mcp" / "same.py").read_text() == "changed"


def test_sync_creates_dest_and_prunes_empty_dirs(tmp_path):
    dest = tmp_path / "new" / "deep"
    (tmp_path / "gone").mkdir()
    s = bucket.sync("gs://b", dest, client=FakeClient([FakeBlob("a/b/c.txt", b"c")]))
    assert s["downloaded"] == 1 and (dest / "a" / "b" / "c.txt").read_text() == "c"
    (dest / "a" / "b" / "c.txt").unlink()
    s = bucket.sync("gs://b", dest, client=FakeClient([]))
    assert s["total"] == 0 and dest.is_dir() and not (dest / "a").exists()


def test_default_client_needs_google_cloud_storage(monkeypatch, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    monkeypatch.setitem(sys.modules, "google.cloud", None)
    with pytest.raises(bucket.SyncError, match="google-cloud-storage"):
        bucket.sync("gs://b/x", tmp_path)


def test_load_syncs_when_uri_set(demo_bucket, monkeypatch):
    from ramen_runtime.rpc import Server

    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    seen = {}

    def fake_sync(uri, dest, client=None):
        seen.update(uri=uri, dest=str(dest))
        return {"uri": uri, "downloaded": 0, "unchanged": 3, "deleted": 0, "total": 3}

    monkeypatch.setattr("ramen_runtime.rpc.gcs.sync", fake_sync)
    monkeypatch.delenv("RAMEN_BUCKET_URI", raising=False)
    r = Server(demo_bucket).load({})
    assert "sync" not in r and seen == {}
    monkeypatch.setenv("RAMEN_BUCKET_URI", "gs://b/demo")
    r = Server(demo_bucket).load({})
    assert r["sync"]["total"] == 3 and seen == {"uri": "gs://b/demo", "dest": str(demo_bucket)}
