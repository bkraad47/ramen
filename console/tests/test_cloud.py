import subprocess

import httpx
import pytest

from ramen_console.cloud import make_cloud
from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.base import Cloud
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.local import LocalCloud


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "mcp").mkdir()
    (src / "mcp" / "requirements.txt").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True
    )
    return src


@pytest.fixture
def calls():
    return []


@pytest.fixture
def cloud(tmp_path, calls):
    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.host == "down":
            raise httpx.ConnectError("down")
        if req.url.path == "/metrics":
            return httpx.Response(200, json={"inflight": 1, "total": 5, "errors": 0, "load": "even"})
        if req.url.path == "/admin/reload":
            if req.headers.get("X-Ramen-Admin-Key") != "adm":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"tools": [{"name": "t"}], "errors": []})
        return httpx.Response(404)

    return LocalCloud(
        bucket_root=tmp_path / "buckets",
        log_root=tmp_path / "logs",
        workers={"demo/local-a": ["http://w1:8080", "http://down:8080"]},
        default_worker="http://default:8080",
        admin_key="adm",
        transport=httpx.MockTransport(handler),
    )


async def test_sync_repo_clone_then_pull(cloud, repo, tmp_path):
    uri = await cloud.sync_repo("demo", str(repo), "main", None)
    assert uri == str(tmp_path / "buckets" / "demo")
    assert (tmp_path / "buckets" / "demo" / "mcp" / "requirements.txt").exists()
    (repo / "mcp" / "new.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "2"], check=True
    )
    await cloud.sync_repo("demo", str(repo), "main", None)
    assert (tmp_path / "buckets" / "demo" / "mcp" / "new.txt").exists()


async def test_sync_repo_bad_url(cloud, tmp_path):
    with pytest.raises(RuntimeError, match="git"):
        await cloud.sync_repo("demo", str(tmp_path / "nope"), "main", None)


def test_auth_url():
    assert LocalCloud.auth_url("https://github.com/a/b.git", "tok") == "https://x-access-token:tok@github.com/a/b.git"
    assert LocalCloud.auth_url("https://github.com/a/b.git", None) == "https://github.com/a/b.git"
    assert LocalCloud.auth_url("/local/path", "tok") == "/local/path"


async def test_deploy_writes_env_and_reloads(cloud, tmp_path, calls):
    await cloud.set_ip_rules("demo", "local-a", ["10.0.0.0/8"])
    res = await cloud.deploy(
        "demo", "prod", "local-a", canary=True, config={"RAMEN_VERBOSE": "1", "RAMEN_SECRET_DEMO__TOKEN": "s3cret"}
    )
    env = (tmp_path / "buckets" / "demo" / ".ramen" / "env").read_text()
    assert env == (tmp_path / "buckets" / "demo" / ".ramen" / "env-local-a").read_text()
    assert "RAMEN_SECRET_DEMO__TOKEN=s3cret" in env
    assert "RAMEN_GROUP=demo" in env and "RAMEN_ENV=prod" in env and "RAMEN_ZONE=local-a" in env
    assert "RAMEN_ALLOWED_CIDRS=10.0.0.0/8" in env and "RAMEN_CANARY=1" in env
    assert res["ok"] is False and len(res["workers"]) == 2
    assert res["workers"][0]["ok"] is True and res["workers"][1]["ok"] is False
    assert "down" in res["workers"][1]["error"]
    reload_calls = [c for c in calls if c.url.path == "/admin/reload"]
    assert reload_calls and reload_calls[0].headers["X-Ramen-Admin-Key"] == "adm"


async def test_deploy_default_worker(cloud, calls):
    res = await cloud.deploy("other", "dev", "zone-x")
    assert res["ok"] is True and res["workers"][0]["id"] == "http://default:8080"


async def test_workers(cloud):
    ws = await cloud.workers("demo", "local-a")
    assert ws[0]["id"] == "http://w1:8080" and ws[0]["load"] == "even" and ws[0]["metrics"]["total"] == 5
    assert ws[1]["load"] == "down" and "down" in ws[1]["error"]
    assert (await cloud.workers("nogroup", "z"))[0]["id"] == "http://default:8080"


async def test_logs(cloud, tmp_path):
    assert await cloud.logs("demo", "local-a") == ""
    p = tmp_path / "logs" / "demo" / "local-a" / "worker.log"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    assert await cloud.logs("demo", "local-a", tail=3) == "line7\nline8\nline9\n"
    assert await cloud.logs("demo", "local-a", worker="http://w1:8080") == ""


async def test_misc(cloud, tmp_path):
    assert (await cloud.rebalance("demo", "local-a"))["ok"] is True
    sa = await cloud.create_service_account("demo", "local-a")
    assert sa["name"] == "local-sa-demo-local-a"
    (tmp_path / "buckets" / "demo").mkdir(parents=True)
    r = await cloud.refresh()
    assert r["groups"] == ["demo"] and "demo/local-a" in r["workers"]


def test_aws_adapter_is_a_full_cloud():  # v0.3.0: the AWS stub is gone (tests/test_aws_*.py cover it)
    for m in (
        "sync_repo",
        "deploy",
        "rebalance",
        "workers",
        "logs",
        "set_ip_rules",
        "create_service_account",
        "refresh",
        "detach_group",
    ):
        assert getattr(AwsCloud, m) is not getattr(Cloud, m, None) and getattr(AwsCloud, m) is not None


def test_factory(monkeypatch, tmp_path):
    monkeypatch.setenv("RAMEN_CLOUD", "local")
    monkeypatch.setenv("RAMEN_BUCKET_ROOT", str(tmp_path))
    monkeypatch.setenv("RAMEN_LOCAL_WORKERS", "demo/z=http://a:1|http://b:2,g2/z=http://c:3")
    c = make_cloud()
    assert isinstance(c, LocalCloud) and c.workers_map["demo/z"] == ["http://a:1", "http://b:2"]
    monkeypatch.setenv("RAMEN_CLOUD", "gcp")
    monkeypatch.setenv("RAMEN_GCP_PROJECT", "p1")
    assert isinstance(make_cloud(), GcpCloud)
    monkeypatch.setenv("RAMEN_CLOUD", "aws")
    monkeypatch.setenv("RAMEN_AWS_REGION", "us-east-1")
    assert isinstance(make_cloud(), AwsCloud)
    monkeypatch.setenv("RAMEN_CLOUD", "x")
    with pytest.raises(ValueError):
        make_cloud()
