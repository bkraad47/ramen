import base64
import subprocess

import pytest

from ramen_console.cloud import make_cloud
from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.base import Cloud
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.local import LocalCloud, git_env
from ramen_console.grpcclient import Client
from tests.fake_grpc import FakeWorker


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
def worker():
    w = FakeWorker(admin_key="adm").start()
    w.metrics = {"inflight": 1, "total": 5, "errors": 0, "load": "even"}
    w.load_result = {"tools": [{"name": "t"}], "errors": []}
    yield w
    w.stop()


@pytest.fixture
def cloud(tmp_path, worker):
    return LocalCloud(
        bucket_root=tmp_path / "buckets",
        log_root=tmp_path / "logs",
        workers={"demo/local-a": [f"http://{worker.target}", "down:8080"]},
        default_worker="default:8080",
        admin_key="adm",
        rpc=Client(deadline=2, resolve=lambda t: worker.target if t.startswith("default") else worker.resolve(t)),
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


def test_git_env_never_puts_the_token_in_argv_or_url(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "9")  # stale caller config must not leak into the child
    env = git_env("tok")
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    basic = base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode()
    assert basic == "x-access-token:tok" and "tok" not in env["GIT_CONFIG_VALUE_0"]
    assert "GIT_CONFIG_COUNT" not in git_env(None) and git_env(None)["GIT_TERMINAL_PROMPT"] == "0"


async def test_sync_repo_keeps_token_out_of_git_config(cloud, repo, tmp_path):  # SEC-12
    await cloud.sync_repo("demo", str(repo), "main", "ghp_secret_token")
    await cloud.sync_repo("demo", str(repo), "main", "ghp_secret_token")  # fetch path too
    cfg = (tmp_path / "buckets" / "demo" / ".git" / "config").read_text()
    assert "ghp_secret_token" not in cfg and "x-access-token" not in cfg and "extraheader" not in cfg.lower()
    assert f"url = {repo}" in cfg
    with pytest.raises(RuntimeError) as e:
        await cloud.sync_repo("demo", str(tmp_path / "nope"), "main", "ghp_secret_token")
    assert "ghp_secret_token" not in str(e.value)


async def test_deploy_writes_env_and_reloads(cloud, tmp_path, worker):
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
    assert res["workers"][0]["ok"] is True and res["workers"][0]["result"]["tools"] == [{"name": "t"}]
    assert res["workers"][1]["ok"] is False and res["workers"][1]["status"] == "UNAVAILABLE"
    assert res["workers"][1]["id"] == "down:8080" and "UNAVAILABLE" in res["workers"][1]["error"]
    reloads = [c for c in worker.calls if c[0] == "Admin/Reload"]
    assert reloads and reloads[0][1]["x-ramen-admin-key"] == "adm"


async def test_deploy_default_worker(cloud, worker):
    res = await cloud.deploy("other", "dev", "zone-x")
    assert res["ok"] is True and res["workers"][0]["id"] == "default:8080"
    worker.admin_key = "other"
    res = await cloud.deploy("other", "dev", "zone-x")
    assert res["ok"] is False and res["workers"][0]["status"] == "UNAUTHENTICATED"


async def test_workers(cloud):
    ws = await cloud.workers("demo", "local-a")
    assert ws[0]["id"] == cloud.workers_map["demo/local-a"][0] and ws[0]["load"] == "even"
    assert ws[0]["metrics"]["total"] == 5
    assert ws[1]["load"] == "down" and ws[1]["id"] == "down:8080" and "UNAVAILABLE" in ws[1]["error"]
    assert (await cloud.workers("nogroup", "z"))[0]["id"] == "default:8080"


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
    monkeypatch.setenv("RAMEN_LOCAL_WORKERS", "demo/z=http://a:1|b:2,g2/z=http://c:3")
    monkeypatch.setenv("RAMEN_WORKER_TLS", "1")
    monkeypatch.setenv("RAMEN_WORKER_CA", "/etc/ramen/ca.pem")
    c = make_cloud()
    assert isinstance(c, LocalCloud) and c.workers_map["demo/z"] == ["a:1", "b:2"]
    assert c.default_worker == "worker:8080" and c.rpc.tls == {"ca": "/etc/ramen/ca.pem"}
    monkeypatch.delenv("RAMEN_WORKER_CA")
    assert make_cloud().rpc.tls is True
    monkeypatch.setenv("RAMEN_WORKER_TLS", "0")
    assert make_cloud().rpc.tls is None
    monkeypatch.setenv("RAMEN_CLOUD", "gcp")
    monkeypatch.setenv("RAMEN_GCP_PROJECT", "p1")
    assert isinstance(make_cloud(), GcpCloud)
    monkeypatch.setenv("RAMEN_CLOUD", "aws")
    monkeypatch.setenv("RAMEN_AWS_REGION", "us-east-1")
    assert isinstance(make_cloud(), AwsCloud)
    monkeypatch.setenv("RAMEN_CLOUD", "x")
    with pytest.raises(ValueError):
        make_cloud()
