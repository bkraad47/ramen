"""Deleting a group removes its synced files from the groups bucket (1.0.0c live re-check: 115 files were left behind).
Only `<group>/` goes — never a group whose name merely starts with it."""

from ramen_console.cloud.local import LocalCloud
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures


async def test_gcp_deletes_only_the_groups_prefix():
    from ramen_console.cloud.gcp import GcpCloud
    from tests.fakes_gcp import FakeClients

    fk = FakeClients()
    c = GcpCloud(project="p1", region="us-central1", bucket="b", image="i", clients=fk, wait_secs=1, poll=0)
    data = fk.storage.bucket("b").data
    data.update({"demo/mcp/a.py": b"1", "demo/.ramen/env": b"2", "demo2/mcp/a.py": b"3", "other/x": b"4"})
    assert await c.delete_files("demo") == 2
    assert sorted(data) == ["demo2/mcp/a.py", "other/x"]
    assert await c.delete_files("demo") == 0


async def test_aws_deletes_only_the_groups_prefix():
    from ramen_console.cloud.aws import AwsCloud
    from tests.fakes_aws import FakeAwsClients
    from tests.test_aws_cloud import aws_env

    with aws_env():
        fk = FakeAwsClients().seed(bucket="ramen-b")
        for k in ("demo/mcp/a.py", "demo/.ramen/env", "demo2/mcp/a.py"):
            fk.s3.put_object(Bucket="ramen-b", Key=k, Body=b"x")
        c = AwsCloud(region="us-east-1", bucket="ramen-b", image="i", admin_key="adm", clients=fk)
        assert await c.delete_files("demo") == 2
        left = [o["Key"] for o in fk.s3.list_objects_v2(Bucket="ramen-b").get("Contents", [])]
        assert left == ["demo2/mcp/a.py"]


async def test_azure_deletes_only_the_groups_prefix():
    from ramen_console.cloud.azure import AzureCloud
    from tests.fakes_azure import FakeAzureClients

    fz = FakeAzureClients()
    fz.container.blobs.update({"demo/mcp/a.py": b"1", "demo/x": b"2", "demo2/y": b"3"})
    c = AzureCloud(
        subscription="s",
        resource_group="rg",
        location="l",
        storage_account="acct",
        key_vault="kv",
        oidc_issuer="https://i",
        image="i",
        admin_key="adm",
        clients=fz,
    )
    assert await c.delete_files("demo") == 2
    assert sorted(fz.container.blobs) == ["demo2/y"]


async def test_local_removes_the_groups_checkout(tmp_path):
    local = LocalCloud(tmp_path)
    (tmp_path / "demo" / "mcp").mkdir(parents=True)
    (tmp_path / "demo" / "mcp" / "a.py").write_text("x")
    (tmp_path / "demo2").mkdir()
    assert await local.delete_files("demo") == 1
    assert not (tmp_path / "demo").exists() and (tmp_path / "demo2").exists()
    assert await local.delete_files("demo") == 0


def test_deleting_a_group_deletes_its_files(demo, cloud):
    files = cloud.root / "demo"
    files.mkdir(parents=True, exist_ok=True)
    (files / "keep-me-not.txt").write_text("x")
    assert demo.delete("/api/v1/groups/demo").status_code == 200
    assert not files.exists()
