"""Scoped live checks against the throwaway project; skipped unless RAMEN_GCP_PROJECT is set."""

import os
import uuid

import pytest

pytestmark = pytest.mark.integration
PROJECT = os.environ.get("RAMEN_GCP_PROJECT")
skip = pytest.mark.skipif(not PROJECT, reason="RAMEN_GCP_PROJECT not set")


@skip
async def test_secret_manager_roundtrip():
    from ramen_console.cloud.gcp_clients import GcpClients
    from ramen_console.secrets.gcp import GcpSecrets

    b = GcpSecrets(PROJECT, GcpClients(PROJECT).secretmanager)
    name = "ITEST_" + uuid.uuid4().hex[:8].upper()
    doc = await b.put("itest", None, None, name, "v1")
    try:
        assert doc["ref"].endswith(f"/secrets/ramen-itest-all-all-{name}")
        assert await b.resolve(doc["ref"]) == "v1"
        assert (await b.put("itest", None, None, name, "v2")) == doc and await b.resolve(doc["ref"]) == "v2"
    finally:
        await b.delete(doc)


@skip
async def test_gcs_sync(tmp_path):
    import subprocess

    from ramen_console.cloud import gcp_api
    from ramen_console.cloud.gcp_clients import GcpClients

    bucket = os.environ.get("RAMEN_GROUPS_BUCKET", f"ramen-{PROJECT}-groups")
    src = tmp_path / "src"
    (src / "mcp").mkdir(parents=True)
    (src / "mcp" / "requirements.txt").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True
    )
    storage = GcpClients(PROJECT).storage
    group = "itest/" + uuid.uuid4().hex[:8]
    try:
        assert gcp_api.sync_repo_to_gcs(storage, bucket, group, str(src), "main", None) == f"gs://{bucket}/{group}"
        names = [b.name for b in storage.bucket(bucket).list_blobs(prefix=group + "/")]
        assert names == [f"{group}/mcp/requirements.txt"]
    finally:
        for b in storage.bucket(bucket).list_blobs(prefix=group + "/"):
            b.delete()
