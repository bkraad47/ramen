"""P4-B1 (1.0.0c Phase 4 verifier, CONTRACTS §26.3 "Push on update"): a policy push reaches the canary track too.
A `worker-canary` left by a canary deploy serves ~half the zone and reads the deploy Secret as env; before the fix
only `worker` was rolled, so the two tracks answered different policy versions."""

import asyncio

import pytest

from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.aws_k8s import ACTION, parse_weights
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.local import LocalCloud
from ramen_console.grpcclient import Client
from tests.fake_grpc import FakeWorker
from tests.fakes_aws import BUCKET, FakeAwsClients, aws_env
from tests.fakes_gcp import FakeClients
from tests.test_api import PW, app, client, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import wait_job
from tests.test_v100_otel import CFG, DEPLOY, ON, cloud, collector, demo  # noqa: F401 - fixtures (the stub cloud)

ADMIN, NS = "adm-key", "ramen-demo-a"
SPEC = {"region": "us-central1-a", "size": "s", "count": 2}
OLD = {"RAMEN_MCP_KEYS": "rmk_1", "RAMEN_EVALS_REQUIRED": "0", "RAMEN_EVALS_VERSION": "88eddaf61d83"}
NEW = {**OLD, "RAMEN_EVALS_REQUIRED": "1", "RAMEN_EVALS_VERSION": "ef9d5db55d0d"}


@pytest.fixture
def stable():
    w = FakeWorker(admin_key=ADMIN, mcp_keys=("rmk_1",)).start()
    yield w
    w.stop()


@pytest.fixture
def canary():
    """The fake pods of the canary track (`10.1.0.*`) land here; the stable ones on `stable`."""
    w = FakeWorker(admin_key=ADMIN, mcp_keys=("rmk_1",)).start()
    yield w
    w.stop()


def by_track(stable, canary):
    return lambda t: canary.target if t.startswith("10.1.") else stable.target


@pytest.fixture
def fk():
    return FakeClients()


@pytest.fixture
def gcp(fk, stable, canary):
    return GcpCloud(
        project="p1",
        region="us-central1",
        bucket="p1-groups",
        image="img/worker:1",
        admin_key=ADMIN,
        clients=fk,
        rpc=Client(deadline=0.5, resolve=by_track(stable, canary)),
        wait_secs=1,
        node_wait_secs=2,
        poll=0,
    )


def record_restarts(cloud, monkeypatch):
    restarted, real = [], cloud.kube.restart

    def restart(ns, name):
        restarted.append(name)
        real(ns, name)

    monkeypatch.setattr(cloud.kube, "restart", restart)
    return restarted


def secret(fk):
    return fk.k8s.objs[("Secret", NS, "ramen-deploy")]["stringData"]


def tracks_read_the_secret(fk):
    for name in ("worker", "worker-canary"):
        d = fk.k8s.objs[("Deployment", NS, name)]
        assert {"secretRef": {"name": "ramen-deploy", "optional": True}} in d["spec"]["template"]["spec"]["containers"][
            0
        ]["envFrom"], name


# the GCP adapter (Azure inherits push_config unchanged) -------------------------------------------------------------


async def test_a_push_with_a_live_canary_rolls_both_tracks(gcp, fk, stable, canary, monkeypatch):
    assert (await gcp.deploy("demo", "prod", "a", canary=True, config=OLD, spec=SPEC))["ok"]
    assert fk.k8s.objs[("Deployment", NS, "worker-canary")]["spec"]["replicas"] == 1  # it lingers after the deploy
    restarted = record_restarts(gcp, monkeypatch)
    seen_s, seen_c = len(stable.calls), len(canary.calls)
    r = await gcp.push_config("demo", "prod", "a", NEW)
    assert r == {"ok": True, "reloaded": 3, "restarted": ["worker", "worker-canary"], "error": None, "warning": None}
    assert restarted == ["worker", "worker-canary"]
    assert "Admin/Reload" in stable.paths()[seen_s:] and "Admin/Reload" in canary.paths()[seen_c:]
    assert secret(fk)["RAMEN_EVALS_REQUIRED"] == "1" and secret(fk)["RAMEN_EVALS_VERSION"] == "ef9d5db55d0d"
    tracks_read_the_secret(fk)  # one Secret, read as env by both Deployments: both rolled = both on the new flags


async def test_a_push_without_a_canary_rolls_the_stable_track_only(gcp, fk, monkeypatch):
    assert (await gcp.deploy("demo", "prod", "a", canary=False, config=OLD, spec=SPEC))["ok"]
    assert fk.k8s.objs[("Deployment", NS, "worker-canary")]["spec"]["replicas"] == 0
    restarted = record_restarts(gcp, monkeypatch)
    r = await gcp.push_config("demo", "prod", "a", NEW)
    assert r["ok"] and r["restarted"] == ["worker"] and r["warning"] is None and restarted == ["worker"]
    assert secret(fk)["RAMEN_EVALS_REQUIRED"] == "1"


async def test_an_unreachable_canary_is_a_warning_an_unreachable_stable_pod_an_error(gcp, stable, canary, monkeypatch):
    """The verifier saw `pushed {"demo/a2": "worker-canary-…: DEADLINE_EXCEEDED"}` — the whole zone marked failed
    although the stable track took the push. The canary's restart still applies the Secret, so it is a warning."""
    assert (await gcp.deploy("demo", "prod", "a", canary=True, config=OLD, spec=SPEC))["ok"]
    restarted = record_restarts(gcp, monkeypatch)
    canary.reload_delay = 2  # past the 0.5 s client deadline
    r = await gcp.push_config("demo", "prod", "a", NEW)
    assert r["ok"] and r["error"] is None and r["reloaded"] == 2
    assert r["warning"] == "worker-canary-0: DEADLINE_EXCEEDED" and r["restarted"] == ["worker", "worker-canary"]
    assert restarted == ["worker", "worker-canary"]  # named, never silent; still rolled onto the new Secret
    canary.reload_delay, stable.reload_ok = 0, False
    r = await gcp.push_config("demo", "prod", "a", NEW)
    assert r["ok"] is False and r["error"] == "worker-0: INTERNAL; worker-1: INTERNAL" and r["warning"] is None


async def test_a_canary_whose_restart_fails_is_a_warning_too(gcp, fk, monkeypatch):
    assert (await gcp.deploy("demo", "prod", "a", canary=True, config=OLD, spec=SPEC))["ok"]
    real = gcp.kube.restart

    def restart(ns, name):
        if name == "worker-canary":
            raise RuntimeError("patch refused")
        real(ns, name)

    monkeypatch.setattr(gcp.kube, "restart", restart)
    r = await gcp.push_config("demo", "prod", "a", NEW)
    assert r["ok"] and r["restarted"] == ["worker"]
    assert r["warning"] == "worker-canary: restart failed: RuntimeError: patch refused"


# the AWS adapter: the canary leaves the ALB split while it rolls (maxSurge 0) and gets its share back once Ready ----


@pytest.fixture
def aws(stable, canary, monkeypatch):
    monkeypatch.setenv("RAMEN_ALB_SPLIT_DRAIN_SECS", "0")
    with aws_env():
        yield AwsCloud(
            region="us-east-1",
            bucket=BUCKET,
            image="img/worker:1",
            admin_key=ADMIN,
            clients=FakeAwsClients().seed(),
            rpc=Client(deadline=0.5, resolve=by_track(stable, canary)),
            wait_secs=1,
            node_wait_secs=2,
            poll=0,
        )


async def test_on_aws_the_canary_is_out_of_the_split_while_it_rolls_and_back_after(aws, monkeypatch):
    spec = {**SPEC, "region": "us-east-1a"}
    assert (await aws.deploy("demo", "prod", "a", canary=True, config=OLD, spec=spec))["ok"]
    ing = aws.kube.read("Ingress", NS, "worker")
    assert parse_weights(ing) == {"worker": 67, "worker-canary": 33}
    during, real = {}, aws.kube.restart

    def restart(ns, name):
        during[name] = parse_weights(aws.kube.read("Ingress", ns, "worker"))
        real(ns, name)

    monkeypatch.setattr(aws.kube, "restart", restart)
    r = await aws.push_config("demo", "prod", "a", NEW)
    assert r["ok"] and r["restarted"] == ["worker", "worker-canary"]
    assert during["worker-canary"] == {"worker": 100, "worker-canary": 0}  # an empty target group would answer 503
    await asyncio.gather(*getattr(aws, "_bg", ()))  # the share comes back in the background, once the canary is Ready
    assert parse_weights(aws.kube.read("Ingress", NS, "worker")) == {"worker": 67, "worker-canary": 33}
    assert ACTION in aws.kube.read("Ingress", NS, "worker")["metadata"]["annotations"]


async def test_on_aws_a_canary_without_a_share_does_not_get_one_from_a_push(aws, monkeypatch):
    spec = {**SPEC, "region": "us-east-1a"}
    assert (await aws.deploy("demo", "prod", "a", canary=True, config=OLD, spec=spec))["ok"]
    aws._set_weights(NS, 100, 0)  # a rebalance found the canary busy and took it out
    r = await aws.push_config("demo", "prod", "a", NEW)
    assert r["ok"] and r["restarted"] == ["worker", "worker-canary"]
    await asyncio.gather(*getattr(aws, "_bg", ()))
    assert parse_weights(aws.kube.read("Ingress", NS, "worker")) == {"worker": 100, "worker-canary": 0}


# the local adapter: both env files, every worker of the zone ---------------------------------------------------------


async def test_locally_a_push_rewrites_both_env_files_and_reloads_every_worker(tmp_path, stable, canary):
    local = LocalCloud(
        bucket_root=tmp_path / "buckets",
        log_root=tmp_path / "logs",
        workers={"demo/a": [f"http://{stable.target}", f"http://{canary.target}"]},
        default_worker="default:8080",
        admin_key=ADMIN,
        rpc=Client(deadline=0.5, resolve=lambda t: t),
    )
    assert (await local.deploy("demo", "prod", "a", canary=True, config=OLD))["ok"]
    r = await local.push_config("demo", "prod", "a", NEW)
    assert r["ok"] and r["reloaded"] == 2 and r["error"] is None
    d = tmp_path / "buckets" / "demo" / ".ramen"
    assert (d / "env").read_text() == (d / "env-a").read_text() and "RAMEN_EVALS_REQUIRED=1\n" in (
        d / "env"
    ).read_text()
    assert stable.paths().count("Admin/Reload") == 2 and canary.paths().count("Admin/Reload") == 2  # deploy + push
    canary.reload_delay = 2
    r = await local.push_config("demo", "prod", "a", NEW)
    assert r["ok"] is False and "DEADLINE_EXCEEDED" in r["error"] and canary.target in r["error"]


# the console answer: `pushed` stays "ok" for the stable track, the canary is named under `warning` -------------------


def push_result(warning):
    async def push_config(group, env, zone, config):
        return {"ok": True, "reloaded": 2, "restarted": ["worker", "worker-canary"], "error": None, "warning": warning}

    return push_config


def test_the_settings_answers_carry_the_canary_warning_and_the_audit_names_it(demo, cloud, collector, monkeypatch):
    demo.put(CFG, json=ON)
    assert wait_job(demo, demo.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])["status"] == "ok"
    monkeypatch.setattr(cloud, "push_config", push_result("worker-canary-7b9dbf7bd9-bl9bs: DEADLINE_EXCEEDED"))
    r = demo.put(CFG, json={"client_endpoint": "https://otel2.example.com"})
    assert r.status_code == 200 and r.json()["pushed"] == {"demo/zone-a": "ok"}
    assert r.json()["warning"] == {"demo/zone-a": "worker-canary-7b9dbf7bd9-bl9bs: DEADLINE_EXCEEDED"}
    a = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "telemetry.push"]
    assert a and a[0]["ok"] and "demo/zone-a:ok" in a[0]["tags"]
    assert "warning:demo/zone-a worker-canary-7b9dbf7bd9-bl9bs: DEADLINE_EXCEEDED" in a[0]["tags"]
    r = demo.put("/api/v1/groups/demo/evals", json={"required": True})  # the group routes too
    assert r.json()["pushed"] == {"demo/zone-a": "ok"}
    assert r.json()["warning"] == {"demo/zone-a": "worker-canary-7b9dbf7bd9-bl9bs: DEADLINE_EXCEEDED"}
    monkeypatch.setattr(cloud, "push_config", push_result(None))
    r = demo.put("/api/v1/groups/demo/skillspector", json={"required": True})
    assert r.json()["pushed"] == {"demo/zone-a": "ok"} and "warning" not in r.json()  # no canary trouble, no key
