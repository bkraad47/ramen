import json
import subprocess
from datetime import UTC
from types import SimpleNamespace as NS

import pytest

from ramen_console.cloud import make_cloud
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.gcp_k8s import SIZES, manifests, normalize_size
from ramen_console.errors import ApiError
from ramen_console.grpcclient import Client
from tests.fake_grpc import FakeWorker
from tests.fakes_gcp import FakeApiError, FakeClients

SPEC = {"region": "us-central1-a", "size": "s", "count": 2}
ADMIN = "adm-key"


@pytest.fixture
def http_state():
    """The fake worker: `.calls` (rpc, metadata, body), `.smoke_ok`, `.reload_ok`, `.paths()`."""
    w = FakeWorker(admin_key=ADMIN, mcp_keys=("rmk_1",)).start()
    w.load_fn = lambda n: "high" if n % 2 else "even"  # every second metrics call reports a hot worker
    yield w
    w.stop()


@pytest.fixture
def fk():
    return FakeClients()


@pytest.fixture
def cloud(fk, http_state):
    return GcpCloud(
        project="p1",
        region="us-central1",
        bucket="p1-groups",
        image="img/worker:0.2.0",
        admin_key=ADMIN,
        clients=fk,
        rpc=Client(deadline=2, resolve=http_state.resolve),
        wait_secs=1,
        poll=0,
    )


def obj(fk, kind, ns, name):
    return fk.k8s.objs[(kind, ns, name)]


def test_sizes():
    assert normalize_size("small") == "s" and normalize_size("l") == "l" and normalize_size("big") is None
    assert SIZES["m"] == {"cpu": "500m", "memory": "1Gi"}


def test_manifests_shape():
    docs = manifests("demo", "a", SPEC, "img", "gs://b/demo", gsa="ramen-demo-a@p1.iam.gserviceaccount.com")
    kinds = [d["kind"] for d in docs]
    # the canary restarts without a surge pod: zone-pinned pods on a small node cannot hold two canaries at once
    # (AWS 0.6.0 run: "0/3 nodes are available: 1 Insufficient memory, 2 didn't match node affinity")
    deps = [d for d in docs if d["kind"] == "Deployment"]
    strategies = {d["metadata"]["name"]: d["spec"]["strategy"]["rollingUpdate"] for d in deps}
    assert strategies["worker-canary"] == {"maxUnavailable": 1, "maxSurge": 0}
    assert strategies["worker"] == {"maxUnavailable": 0, "maxSurge": 1}
    assert kinds == [
        "Namespace",
        "NetworkPolicy",
        "ServiceAccount",
        "Service",
        "Deployment",
        "Deployment",
        "HorizontalPodAutoscaler",
        "HTTPRoute",
        "HealthCheckPolicy",
    ]
    assert docs[0]["metadata"]["labels"]["ramen.io/routes"] == "true"
    route = docs[7]
    assert route["apiVersion"] == "gateway.networking.k8s.io/v1" and route["metadata"] == {
        "name": "worker",
        "namespace": "ramen-demo-a",
        "labels": {"ramen.io/group": "demo", "ramen.io/zone": "a"},
    }
    assert route["spec"]["parentRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "ramen", "namespace": "ramen-system"}
    ]
    rule = route["spec"]["rules"][0]  # CONTRACTS §11: header routing, no rewrite; Mcp + Health + reflection, no Admin
    headers = [{"name": "ramen-group", "value": "demo"}, {"name": "ramen-zone", "value": "a"}]
    assert rule["matches"] == [
        {"path": {"type": "PathPrefix", "value": p}, "headers": headers}
        for p in (
            "/ramen.v1.Mcp",
            "/grpc.health.v1.Health",
            "/grpc.reflection.v1.ServerReflection",
            "/grpc.reflection.v1alpha.ServerReflection",
            "/mcp",
            "/.well-known/oauth-protected-resource",
        )
    ]
    assert not any("/ramen.v1.Admin" in json.dumps(m) for m in rule["matches"])
    assert "filters" not in rule and rule["backendRefs"] == [{"name": "worker", "port": 8080}]
    hcp = docs[8]
    assert hcp["apiVersion"] == "networking.gke.io/v1" and hcp["spec"]["targetRef"]["name"] == "worker"
    assert hcp["spec"]["default"]["config"] == {"type": "GRPC", "grpcHealthCheck": {"port": 8080}}
    svc = docs[3]
    assert svc["spec"]["ports"] == [
        {"name": "grpc", "port": 8080, "targetPort": "grpc", "appProtocol": "kubernetes.io/h2c"}
    ]
    c = docs[4]["spec"]["template"]["spec"]["containers"][0]
    assert c["ports"] == [{"name": "grpc", "containerPort": 8080}]
    assert c["readinessProbe"]["grpc"] == {"port": 8080}  # service "" = ready once runtime.load succeeded
    assert c["livenessProbe"]["grpc"] == {"port": 8080, "service": "ramen.v1.Admin"}  # alive even while loading
    assert "httpGet" not in json.dumps(docs)
    dep = docs[4]
    assert dep["metadata"]["name"] == "worker" and dep["spec"]["replicas"] == 2
    assert dep["spec"]["template"]["spec"]["nodeSelector"] == {"topology.kubernetes.io/zone": "us-central1-a"}
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["RAMEN_BUCKET_URI"] == "gs://b/demo" and env["RAMEN_BUCKET"] == "/data/bucket"
    assert docs[5]["metadata"]["name"] == "worker-canary" and docs[5]["spec"]["replicas"] == 0
    assert docs[2]["metadata"]["annotations"]["iam.gke.io/gcp-service-account"].startswith("ramen-demo-a@")
    assert "ramen-demo-a" in docs[3]["metadata"]["annotations"]["cloud.google.com/neg"]
    assert docs[6]["spec"]["minReplicas"] == 2 and docs[6]["spec"]["maxReplicas"] == 4
    no_zone = manifests("demo", "a", {"region": "", "size": "xl", "count": 1}, "img", "gs://b/demo")
    assert "nodeSelector" not in no_zone[4]["spec"]["template"]["spec"]
    assert no_zone[4]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] == SIZES["s"]


async def test_attach_zone_waits_for_rbac_propagation(cloud, fk):
    """A RoleBinding is not honoured the instant it is created (API-server RBAC cache lags): zone-scoped reads answer
    403 for a moment. Seen live on GKE (0.3.2); attach_zone must wait, not fail."""
    fk.k8s.rbac_lag = 3
    r = await cloud.attach_zone("demo", "a", SPEC)
    assert r["ok"] and fk.k8s.rbac_lag == 0
    assert ("ServiceAccount", "ramen-demo-a", "worker") in fk.k8s.objs


async def test_attach_zone_gives_up_when_rbac_never_propagates(cloud, fk):
    fk.k8s.rbac_lag, cloud.kube.rbac_wait = 10**6, 0.01
    with pytest.raises(ApiError) as e:
        await cloud.attach_zone("demo", "a", SPEC)
    assert e.value.status_code == 502 and "Forbidden" in e.value.detail


async def test_attach_zone_ensures_worker_identity(cloud, fk):
    """CONTRACTS §7: the console creates the per-zone GSA. A zone attached without an explicit service-account call
    still gets its worker identity (live 0.3.1: the KSA had no GSA → runtime.load 403 on GCS → deploy timed out)."""
    await cloud.attach_zone("demo", "a", SPEC)
    ksa = obj(fk, "ServiceAccount", "ramen-demo-a", "worker")
    gsa = "ramen-demo-a@p1.iam.gserviceaccount.com"
    assert ksa["metadata"]["annotations"] == {"iam.gke.io/gcp-service-account": gsa}
    bucket = fk.storage.buckets["p1-groups"].policy
    assert [b["role"] for b in bucket.bindings] == ["roles/storage.objectViewer"]
    assert "p1-groups/objects/demo/" in bucket.bindings[0]["condition"]["expression"]
    r = await cloud.create_service_account("demo", "a")  # the explicit super-admin call is then a no-op create
    assert r["created"] is False
    await cloud.attach_zone("demo", "a", {**SPEC, "count": 3})  # re-attach keeps the identity, creates nothing
    assert obj(fk, "ServiceAccount", "ramen-demo-a", "worker")["metadata"]["annotations"] == {
        "iam.gke.io/gcp-service-account": gsa
    }


async def test_attach_zone_retries_bucket_grant_until_new_gsa_is_visible(cloud, fk, monkeypatch):
    """Live GKE: the bucket setIamPolicy issued right after the GSA was created fails with 400 "Service account … does
    not exist" until IAM has propagated the new account (a few seconds). Retry instead of failing the attach."""
    import ramen_console.cloud.gcp_api as api

    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    fk.storage.bucket("p1-groups").iam_lag = 3
    r = await cloud.attach_zone("demo", "a", SPEC)
    assert r["ok"] and fk.storage.bucket("p1-groups").iam_lag == 0
    assert [b["role"] for b in fk.storage.buckets["p1-groups"].policy.bindings] == ["roles/storage.objectViewer"]


async def test_attach_zone_gives_up_on_persistent_bad_request(cloud, fk, monkeypatch):
    import ramen_console.cloud.gcp_api as api

    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    fk.storage.bucket("p1-groups").iam_lag = 10**6
    with pytest.raises(ApiError) as e:
        await cloud.attach_zone("demo", "a", SPEC)
    assert e.value.status_code == 502 and "does not exist" in e.value.detail


async def test_attach_zone_idempotent(cloud, fk):
    r = await cloud.attach_zone("demo", "a", SPEC)
    assert r["namespace"] == "ramen-demo-a" and r["ok"]
    obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] = 1
    r2 = await cloud.attach_zone("demo", "a", {**SPEC, "count": 3})
    assert obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["replicas"] == 3
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 1  # canary replicas preserved
    assert obj(fk, "Namespace", None, "ramen-demo-a")["metadata"]["labels"] == {
        "ramen.io/group": "demo",
        "ramen.io/zone": "a",
        "ramen.io/routes": "true",
    }
    assert obj(fk, "HTTPRoute", "ramen-demo-a", "worker")["spec"]["rules"][0]["matches"][0]["headers"][0] == {
        "name": "ramen-group",
        "value": "demo",
    }
    rb = obj(fk, "RoleBinding", "ramen-demo-a", "ramen-console")  # SEC-09: zone-scoped console permissions
    assert rb["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "ramen-console-zone",
    }
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "console", "namespace": "ramen-system"}]
    order = [c for c in fk.k8s.calls if c[0] == "create"][:2]
    assert order == [
        ("create", "Namespace", None, "ramen-demo-a"),
        ("create", "RoleBinding", "ramen-demo-a", "ramen-console"),
    ]
    assert ("HealthCheckPolicy", "ramen-demo-a", "worker") in fk.k8s.objs
    assert ("create", "HTTPRoute", "ramen-demo-a", "worker") in fk.k8s.calls and (
        "patch",
        "HTTPRoute",
        "ramen-demo-a",
        "worker",
    ) in fk.k8s.calls
    assert r2["ok"]


async def test_deploy_canary_success(cloud, fk, http_state):
    lines = []
    cfg = {"RAMEN_VERBOSE": "0", "RAMEN_SECRET_DEMO__TOKEN": "s3cret", "RAMEN_MCP_KEYS": "rmk_1"}
    res = await cloud.deploy("demo", "prod", "a", canary=True, config=cfg, spec=SPEC, log=lines.append)
    assert res["ok"] is True, res
    sec = obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]
    assert (
        sec["RAMEN_SECRET_DEMO__TOKEN"] == "s3cret" and sec["RAMEN_ENV"] == "prod" and sec["RAMEN_ADMIN_KEY"] == ADMIN
    )
    canary = obj(fk, "Deployment", "ramen-demo-a", "worker-canary")
    assert canary["spec"]["replicas"] == 1
    assert canary["spec"]["template"]["metadata"]["annotations"]["ramen.io/restartedAt"]
    assert obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["template"]["metadata"]["annotations"][
        "ramen.io/restartedAt"
    ]
    paths = http_state.paths()
    assert paths.index("Admin/Reload") < paths.index("Mcp/Call")
    call = next(c for c in http_state.calls if c[0] == "Mcp/Call")
    assert call[1]["ramen-group"] == "demo" and call[1]["ramen-zone"] == "a" and call[2]["method"] == "tools/list"
    assert any("canary" in x for x in lines) and any("smoke" in x for x in lines)
    assert res["workers"][0]["result"]["tools"] == [{"name": "calc"}]
    assert "s3cret" not in json.dumps(res) and "s3cret" not in "\n".join(lines)


async def test_deploy_canary_bad_smoke_scales_canary_to_zero(cloud, fk, http_state):
    await cloud.attach_zone("demo", "a", SPEC)
    main_before = json.dumps(obj(fk, "Deployment", "ramen-demo-a", "worker"), sort_keys=True)
    http_state.smoke_ok = False
    lines = []
    res = await cloud.deploy("demo", "prod", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec=SPEC, log=lines.append)
    assert res["ok"] is False and "smoke" in res["error"]
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0
    assert json.dumps(obj(fk, "Deployment", "ramen-demo-a", "worker"), sort_keys=True) == main_before
    assert res["workers"] and res["workers"][0]["ok"] is False
    assert any("scaled canary to 0" in x for x in lines)


def _image(fk, name):
    return obj(fk, "Deployment", "ramen-demo-a", name)["spec"]["template"]["spec"]["containers"][0]["image"]


async def test_canary_gates_a_new_image_for_the_stable_track(cloud, fk, http_state):
    """0.6.1 GKE run: a canary deploy wrote the newly pinned image into the stable Deployment at manifest time, so
    stable rolled it in parallel with the canary and kept it after the canary failed ("main deployment untouched")."""
    await cloud.attach_zone("demo", "a", SPEC)
    old = _image(fk, "worker")
    http_state.smoke_ok = False
    res = await cloud.deploy("demo", "prod", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec={**SPEC, "image": "r/w:new"})
    assert res["ok"] is False and _image(fk, "worker-canary") == "r/w:new"
    assert _image(fk, "worker") == old
    http_state.smoke_ok = True
    res = await cloud.deploy("demo", "prod", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec={**SPEC, "image": "r/w:new"})
    assert res["ok"] is True and _image(fk, "worker") == "r/w:new"


async def test_deploy_canary_reload_failure(cloud, fk, http_state):
    http_state.reload_ok = False
    res = await cloud.deploy("demo", "prod", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec=SPEC)
    assert res["ok"] is False and "reload" in res["error"] and "INTERNAL" in res["error"]
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0


async def test_deploy_canary_not_ready_times_out(cloud, fk):
    fk.k8s.ready = False
    res = await cloud.deploy("demo", "prod", "a", config={}, spec=SPEC)
    assert res["ok"] is False and "not ready" in res["error"]
    # the job log must say *why* (the AWS 0.6.0 run only said "1/1 ready" while the new pod sat Unschedulable)
    assert "Pending" in res["error"] and "Insufficient cpu" in res["error"]
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0


async def test_not_ready_diagnostic_names_each_reason_once(cloud, fk):
    fk.k8s.ready, fk.k8s.pull_error = False, True
    res = await cloud.deploy("demo", "prod", "a", config={}, spec=SPEC)
    assert "ImagePullBackOff" in res["error"] and res["error"].count("ContainersNotReady") == 1, res["error"]


async def test_deploy_no_canary_and_health_smoke(cloud, fk, http_state):
    res = await cloud.deploy("demo", "prod", "a", canary=False, config={}, spec=SPEC)
    assert res["ok"] is True
    assert ("Deployment", "ramen-demo-a", "worker-canary") in fk.k8s.objs
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0
    assert "Mcp/Call" not in http_state.paths()
    # the group page's "Packages per zone" list (F5.6) reads this per worker — a stable-only deploy (no
    # canary) must still populate it, not just a canary deploy's pod.
    stable = next(w for w in res["workers"] if w["track"] == "stable")
    assert stable["result"]["tools"] == [{"name": "calc"}]
    # canary with no MCP keys smokes Health/Check (SERVING) instead of tools/list
    res = await cloud.deploy("demo", "prod", "a", canary=True, config={}, spec=SPEC)
    assert res["ok"] and any("health SERVING" in x for x in res["log"]) and "Mcp/Call" not in http_state.paths()
    http_state.set_serving(False)
    res = await cloud.deploy("demo", "prod", "a", canary=True, config={}, spec=SPEC)
    assert not res["ok"] and "health not SERVING" in res["error"]


async def test_deploy_smoke_reports_jsonrpc_error(cloud, fk, http_state, monkeypatch):
    async def bad(*a, **k):
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}

    monkeypatch.setattr(cloud.rpc, "call", bad)
    res = await cloud.deploy("demo", "prod", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec=SPEC)
    assert not res["ok"] and "smoke" in res["error"] and "-32601" in res["error"]


async def test_deploy_preserves_cidrs(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["backend"] = {"name": "gkegw1-x", "backends": [{"group": "x/networkEndpointGroups/ramen-demo-a"}]}
    await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    await cloud.deploy("demo", "prod", "a", canary=False, config={}, spec=SPEC)
    assert obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"


async def test_workers(cloud, fk, http_state):
    assert await cloud.workers("demo", "a") == []
    await cloud.attach_zone("demo", "a", SPEC)
    ws = await cloud.workers("demo", "a")
    assert [w["id"] for w in ws] == ["worker-0", "worker-1"]
    assert ws[0]["load"] == "even" and ws[1]["load"] == "high" and ws[0]["track"] == "stable"
    assert ws[0]["metrics"]["total"] == 9 and ws[0]["ip"] == "10.2.0.1"
    assert http_state.calls[-1][0] == "Admin/Metrics" and http_state.calls[-1][1]["x-ramen-admin-key"] == ADMIN
    fk.k8s.ready = False
    assert all(w["load"] == "down" for w in await cloud.workers("demo", "a"))


async def test_workers_metrics_error(fk):
    c = GcpCloud(
        project="p1",
        region="r",
        bucket="b",
        image="i",
        clients=fk,
        rpc=Client(deadline=2, resolve=lambda t: "127.0.0.1:1"),
        poll=0,
    )
    await c.attach_zone("demo", "a", SPEC)
    ws = await c.workers("demo", "a")
    assert ws[0]["load"] == "down" and "UNAVAILABLE" in ws[0]["error"]


async def test_logs(fk, cloud):
    from datetime import datetime

    fk.logging.entries = [
        NS(
            timestamp=datetime(2026, 9, 27, 1, 0, 1, tzinfo=UTC),
            severity="INFO",
            payload={"msg": "b"},
            resource=NS(labels={"pod_name": "worker-0"}),
        ),
        NS(
            timestamp=datetime(2026, 9, 27, 1, 0, 0, tzinfo=UTC),
            severity=None,
            payload="a",
            resource=NS(labels={"pod_name": "worker-1"}),
        ),
    ]
    text = await cloud.logs("demo", "a", tail=5)
    lines = text.splitlines()
    # v0.5.5 I9: a non-JSON payload stays verbatim; a JSON/dict payload gets ts/pod backfilled INTO the
    # object (never prefixed as plain text, which used to break logview.parse_log's json.loads on every
    # real worker line — every GKE log row rendered as "-"/"-" for consumer/timestamp).
    assert lines[0] == "a"
    doc = json.loads(lines[1])
    assert doc["msg"] == "b" and doc["pod"] == "worker-0" and doc["ts"] == "2026-09-27T01:00:01+00:00"
    assert (
        'resource.type="k8s_container"' in fk.logging.filters[0]
        and 'namespace_name="ramen-demo-a"' in fk.logging.filters[0]
    )
    await cloud.logs("demo", "a", worker="worker-0", tail=1)
    assert 'pod_name="worker-0"' in fk.logging.filters[1]


async def test_rebalance(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    r = await cloud.rebalance("demo", "a")  # backend not programmed yet: 200, nothing applied, reason given
    assert (
        r["ok"]
        and not r["applied"]
        and r["backend_service"] is None
        and "ramen-demo-a" in r["note"]
        and "ramen-demo" in r["note"]
    )
    # GKE Gateway auto-named backend service found through its NEG backend (second page)
    fk.compute_state["paged"] = True
    fk.compute_state["backend"] = {
        "name": "gkegw1-abcd-ramen-demo-a-worker-8080-xyz",
        "fingerprint": "f1",
        "backends": [
            {
                "group": "https://www.googleapis.com/compute/v1/projects/p1/zones/us-central1-a/networkEndpointGroups/ramen-demo-a",
                "capacityScaler": 1.0,
            },
            {"group": ".../networkEndpointGroups/ramen-demo-b", "capacityScaler": 1.0},
        ],
    }
    r = await cloud.rebalance("demo", "a")
    assert (
        r["ok"] and r["load"] == "high" and r["capacity_scaler"] == 0.5 and r["backend_service"].startswith("gkegw1-")
    )
    b = fk.compute_state["backend"]["backends"]
    assert b[0]["capacityScaler"] == 0.5 and b[1]["capacityScaler"] == 1.0
    fk.k8s.objs[("HorizontalPodAutoscaler", "ramen-demo-a", "worker")]["spec"]["minReplicas"] = 3
    r = await cloud.rebalance("demo", "a")
    assert r["scaled_to"] == 3 and obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["replicas"] == 3


async def test_rebalance_fallback_name(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["backend"] = {
        "name": "ramen-demo",
        "backends": [{"group": "x/networkEndpointGroups/unrelated", "capacityScaler": 1.0}],
    }
    r = await cloud.rebalance("demo", "a")
    assert (
        r["ok"]
        and r["backend_service"] == "ramen-demo"
        and fk.compute_state["backend"]["backends"][0]["capacityScaler"] == 1.0
    )


async def test_set_ip_rules(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8", "192.168.0.0/16"])
    assert r["ok"] and not r["attached"] and "not attached" in r["note"] and "enforced at the node" in r["note"]
    pol = fk.compute_state["policies"]["ramen-demo"]
    assert (
        obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"]
        == "10.0.0.0/8,192.168.0.0/16"
    )
    fk.compute_state["backend"] = {
        "name": "gkegw1-ramen-demo-a",
        "backends": [{"group": "x/networkEndpointGroups/ramen-demo-a"}],
    }
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8", "192.168.0.0/16"])
    assert (
        r["ok"]
        and r["policy"] == "ramen-demo"
        and r["cidrs"] == ["10.0.0.0/8", "192.168.0.0/16"]
        and r["backend_service"] == "gkegw1-ramen-demo-a"
    )
    pol = fk.compute_state["policies"]["ramen-demo"]
    allow = [x for x in pol["rules"] if x["action"] == "allow"]
    assert allow[0]["match"]["config"]["srcIpRanges"] == ["10.0.0.0/8", "192.168.0.0/16"]
    assert [x for x in pol["rules"] if x["priority"] == 2147483647][0]["action"] == "deny(403)"
    assert (
        obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"]
        == "10.0.0.0/8,192.168.0.0/16"
    )
    r = await cloud.set_ip_rules("demo", "a", [f"10.{i}.0.0/16" for i in range(12)])
    pol = fk.compute_state["policies"]["ramen-demo"]
    assert len([x for x in pol["rules"] if x["action"] == "allow"]) == 2 and r["attached"] is True
    assert fk.compute_state["backend"]["securityPolicy"].endswith("securityPolicies/ramen-demo")
    r = await cloud.set_ip_rules("demo", "a", [])
    pol = fk.compute_state["policies"]["ramen-demo"]
    assert [x for x in pol["rules"] if x["priority"] == 2147483647][0]["action"] == "allow"
    assert obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "0.0.0.0/0"


async def test_create_service_account_idempotent(cloud, fk):
    r = await cloud.create_service_account("demo", "a")
    assert r["name"] == "ramen-demo-a@p1.iam.gserviceaccount.com" and r["created"] is True
    assert r["ksa"] == "ramen-demo-a/worker"
    fk.secretmanager.create_secret({"parent": "projects/p1", "secret_id": "ramen-demo-prod-all-TOKEN", "secret": {}})
    fk.secretmanager.create_secret({"parent": "projects/p1", "secret_id": "ramen-other-prod-all-X", "secret": {}})
    r2 = await cloud.create_service_account("demo", "a")
    assert r2["created"] is False and r2["name"] == r["name"]
    assert r2["roles"] == ["roles/storage.objectViewer", "roles/secretmanager.secretAccessor"]
    # SEC-08: resource-level bindings only, nothing at project level (no projectIamAdmin needed)
    assert "project_policy" not in fk.iam_state
    bucket = fk.storage.buckets["p1-groups"].policy
    assert bucket.version == 3 and [b["role"] for b in bucket.bindings] == ["roles/storage.objectViewer"]
    assert "p1-groups/objects/demo/" in bucket.bindings[0]["condition"]["expression"]
    assert bucket.bindings[0]["members"] == [f"serviceAccount:{r['name']}"]
    sec = fk.secretmanager.secrets
    # Secret Manager policies are proto messages (google.iam.v1 Binding), never dicts (live TypeError otherwise)
    assert [(b.role, list(b.members)) for b in sec["projects/p1/secrets/ramen-demo-prod-all-TOKEN"]["policy"]] == [
        ("roles/secretmanager.secretAccessor", [f"serviceAccount:{r['name']}"])
    ]
    assert sec["projects/p1/secrets/ramen-other-prod-all-X"]["policy"] == []
    # a secret created later gets the group's worker accounts bound at creation
    cloud.bind_new_secret("projects/p1/secrets/ramen-demo-dev-all-NEW", "demo")  # not created yet: no-op, no error
    fk.secretmanager.create_secret({"parent": "projects/p1", "secret_id": "ramen-demo-dev-all-NEW", "secret": {}})
    cloud.bind_new_secret("projects/p1/secrets/ramen-demo-dev-all-NEW", "demo")
    new_pol = sec["projects/p1/secrets/ramen-demo-dev-all-NEW"]["policy"]
    assert list(new_pol[0].members) == [f"serviceAccount:{r['name']}"]
    cloud.bind_new_secret("projects/p1/secrets/ramen-demo-dev-all-NEW", "demo")  # idempotent
    assert fk.secretmanager.calls.count("set_iam_policy") == 2
    wi = fk.iam_state["sa_policy"][r["name"]]["bindings"]
    assert wi == [
        {"role": "roles/iam.workloadIdentityUser", "members": ["serviceAccount:p1.svc.id.goog[ramen-demo-a/worker]"]}
    ]
    ksa = obj(fk, "ServiceAccount", "ramen-demo-a", "worker")
    assert ksa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == r["name"]
    long = await cloud.create_service_account("a-very-long-group-name-here", "zone-name-x")
    acct = long["name"].split("@")[0]
    assert len(acct) <= 30 and acct.startswith("ramen-")


async def test_refresh_and_scale(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.create_service_account("demo", "a")
    r = await cloud.refresh()
    z = r["zones"][0]
    assert z["group"] == "demo" and z["zone"] == "a" and z["namespace"] == "ramen-demo-a"
    assert z["ready"] == 2 and z["canary_ready"] == 0 and z["service_account"].startswith("ramen-demo-a@")
    assert r["service_accounts"] == ["ramen-demo-a@p1.iam.gserviceaccount.com"]
    s = await cloud.scale("demo", "a", {**SPEC, "count": 4, "size": "l"})
    assert s["ok"]
    dep = obj(fk, "Deployment", "ramen-demo-a", "worker")
    assert dep["spec"]["replicas"] == 4
    assert dep["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] == SIZES["l"]
    assert obj(fk, "HorizontalPodAutoscaler", "ramen-demo-a", "worker")["spec"]["minReplicas"] == 4


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    (src / "mcp").mkdir(parents=True)
    (src / "mcp" / "requirements.txt").write_text("x")
    (src / "old.txt").write_text("old")
    for cmd in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", *cmd]
            if cmd[0] != "init"
            else ["git", "init", "-q", "-b", "main", str(src)],
            check=True,
        )
    return src


async def test_sync_repo(cloud, fk, repo):
    bucket = fk.storage.bucket("p1-groups")
    bucket.data["demo/stale.txt"] = b"stale"
    bucket.data["other/keep.txt"] = b"keep"
    uri = await cloud.sync_repo("demo", str(repo), "main", None)
    assert uri == "gs://p1-groups/demo"
    assert set(bucket.data) == {"demo/mcp/requirements.txt", "demo/old.txt", "other/keep.txt"}
    (repo / "old.txt").unlink()
    (repo / "new.txt").write_text("new")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "2"], check=True
    )
    await cloud.sync_repo("demo", str(repo), "main", "tok")
    assert set(bucket.data) == {"demo/mcp/requirements.txt", "demo/new.txt", "other/keep.txt"}
    with pytest.raises(ApiError) as e:
        await cloud.sync_repo("demo", str(repo / "nope"), "main", "tok")
    assert e.value.status_code == 502 and "tok" not in str(e.value.detail)


async def test_helm_template_path(cloud, fk, tmp_path, monkeypatch):
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: ramen-worker\n")
    seen = {}

    def fake_run(cmd, capture_output, text):
        seen["cmd"] = cmd
        return NS(
            returncode=0,
            stdout="apiVersion: v1\nkind: Namespace\nmetadata:\n  name: ramen-demo-a\n---\n"
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker\n  namespace: ramen-demo-a\n"
            "spec:\n  replicas: 2\n  template:\n    metadata:\n      labels: {app: worker, ramen.io/track: stable}\n",
            stderr="",
        )

    monkeypatch.setattr("ramen_console.cloud.gcp_k8s.subprocess.run", fake_run)
    monkeypatch.setattr("ramen_console.cloud.gcp_k8s.shutil.which", lambda _: "/usr/bin/helm")
    cloud.chart = str(chart)
    r = await cloud.attach_zone("demo", "a", SPEC)
    assert (
        r["renderer"] == "helm"
        and seen["cmd"][:2] == ["helm", "template"]
        and "secret.create=false" in seen["cmd"]
        and "project=p1" in seen["cmd"]
    )
    assert ("Deployment", "ramen-demo-a", "worker") in fk.k8s.objs
    assert ("HTTPRoute", "ramen-demo-a", "worker") in fk.k8s.objs  # appended when the chart renders none
    assert ("HealthCheckPolicy", "ramen-demo-a", "worker") in fk.k8s.objs
    assert ("RoleBinding", "ramen-demo-a", "ramen-console") in fk.k8s.objs
    assert obj(fk, "Namespace", None, "ramen-demo-a")["metadata"]["labels"]["ramen.io/routes"] == "true"
    monkeypatch.setattr(
        "ramen_console.cloud.gcp_k8s.subprocess.run", lambda *a, **k: NS(returncode=1, stdout="", stderr="bad chart")
    )
    with pytest.raises(ApiError, match="Helm"):
        await cloud.attach_zone("demo", "a", SPEC)


async def test_error_mapping(cloud, fk):
    def boom(*a, **k):
        raise RuntimeError("kaboom value=s3cret")

    fk.k8s.objs.clear()
    orig = fk.k8s._create
    fk.k8s._create = boom
    with pytest.raises(ApiError) as e:
        await cloud.attach_zone("demo", "a", SPEC)
    assert e.value.status_code == 502 and "RuntimeError" in e.value.detail
    fk.k8s._create = orig

    def unknown_kind():
        from ramen_console.cloud.gcp_k8s import Kube

        Kube(fk, poll=0).apply({"kind": "Weird", "metadata": {"name": "x"}})

    with pytest.raises(ApiError, match="Weird"):
        unknown_kind()


def test_factory_and_env(monkeypatch):
    monkeypatch.setenv("RAMEN_CLOUD", "gcp")
    monkeypatch.setenv("RAMEN_GCP_PROJECT", "p1")
    monkeypatch.setenv("RAMEN_GROUPS_BUCKET", "bkt")
    monkeypatch.setenv("RAMEN_IMAGE_WORKER", "img:1")
    monkeypatch.setenv("RAMEN_WORKER_DEADLINE", "3")
    c = make_cloud()
    assert isinstance(c, GcpCloud) and c.project == "p1" and c.bucket == "bkt" and c.image == "img:1"
    assert c.rpc.deadline == 3.0 and c.rpc.tls is None
    monkeypatch.delenv("RAMEN_GCP_PROJECT")
    with pytest.raises(ValueError, match="RAMEN_GCP_PROJECT"):
        make_cloud()


def test_real_clients_lazy(monkeypatch):
    from ramen_console.cloud.gcp_clients import GcpClients, _creds, http_status

    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    assert _creds() is None
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "tok")
    assert _creds().token == "tok"
    assert http_status(FakeApiError(409)) == 409
    assert http_status(NS(code=404)) == 404
    assert http_status(NS(resp=NS(status=403))) == 403
    assert http_status(RuntimeError()) is None
    g = GcpClients("p1")
    import types

    loaded = {}
    fake_cfg = types.SimpleNamespace(
        load_incluster_config=lambda: loaded.setdefault("mode", "incluster"),
        load_kube_config=lambda: loaded.setdefault("mode", "kubeconfig"),
        ConfigException=RuntimeError,
    )
    fake_client = types.SimpleNamespace(
        CoreV1Api=lambda: "core",
        AppsV1Api=lambda: "apps",
        AutoscalingV2Api=lambda: "hpa",
        CustomObjectsApi=lambda: "custom",
        RbacAuthorizationV1Api=lambda: "rbac",
        ApiClient=lambda: NS(sanitize_for_serialization=lambda o: {"x": o}),
    )
    monkeypatch.setattr(g, "_kube_modules", lambda: (fake_cfg, fake_client))
    assert (
        g.core == "core"
        and g.apps == "apps"
        and g.autoscaling == "hpa"
        and g.custom == "custom"
        and g.rbac == "rbac"
        and loaded["mode"] == "incluster"
    )
    assert g.to_dict({"a": 1}) == {"a": 1} and g.to_dict("o") == {"x": "o"}

    def fail_incluster():
        raise RuntimeError("no sa token")

    loaded.clear()
    g2 = GcpClients("p1")
    monkeypatch.setattr(
        g2,
        "_kube_modules",
        lambda: (
            types.SimpleNamespace(
                load_incluster_config=fail_incluster,
                load_kube_config=lambda: loaded.setdefault("mode", "kubeconfig"),
                ConfigException=RuntimeError,
            ),
            fake_client,
        ),
    )
    assert g2.core == "core" and loaded["mode"] == "kubeconfig"


async def test_failed_deploy_tears_down_leftover_canary(cloud, fk, http_state):
    await cloud.attach_zone("demo", "a", SPEC)
    fk.k8s.objs[("Deployment", "ramen-demo-a", "worker-canary")]["spec"]["replicas"] = 1  # left from an earlier deploy
    cloud._attach = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git ref not found"))
    r = await cloud.deploy("demo", "dev", "a", canary=True, config={"RAMEN_MCP_KEYS": "rmk_1"})
    assert not r["ok"] and "git ref not found" in r["error"]
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0


async def test_detach_group_destroys_namespaces_and_gsas(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.attach_zone("demo", "b", SPEC)
    await cloud.attach_zone("other", "a", SPEC)
    await cloud.create_service_account("demo", "a")
    await cloud.create_service_account("other", "a")
    r = await cloud.detach_group("demo")
    assert r["namespaces"] == ["ramen-demo-a", "ramen-demo-b"]
    # every attached zone got its identity on attach (§7), so detach removes both demo GSAs
    assert r["service_accounts"] == [
        "ramen-demo-a@p1.iam.gserviceaccount.com",
        "ramen-demo-b@p1.iam.gserviceaccount.com",
    ]
    assert ("Namespace", None, "ramen-other-a") in fk.k8s.objs and (
        "Namespace",
        None,
        "ramen-demo-a",
    ) not in fk.k8s.objs
    assert list(fk.iam_state["accounts"]) == ["ramen-other-a@p1.iam.gserviceaccount.com"]
    assert (await cloud.detach_group("demo")) == {"namespaces": [], "service_accounts": [], "security_policies": []}


async def test_detach_group_deletes_its_cloud_armor_policy(cloud, fk):
    """0.6.1 GKE run: `ramen-<group>` (made by IP rules, billed per policy and rule) outlived the group. A backend
    service still pointing at it (the Gateway collects them late) is cleared first: GCP refuses a used policy."""
    await cloud.attach_zone("demo", "a", SPEC)
    neg = "x/networkEndpointGroups/ramen-demo-a"
    fk.compute_state["backend"] = {"name": "gkegw1-demo-a", "backends": [{"group": neg}]}
    await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    fk.compute_state["policies"]["ramen-other"] = {"name": "ramen-other", "rules": []}
    assert fk.compute_state["backend"]["securityPolicy"].endswith("/securityPolicies/ramen-demo")
    r = await cloud.detach_group("demo")
    assert r["security_policies"] == ["ramen-demo"]
    assert list(fk.compute_state["policies"]) == ["ramen-other"]
    assert not fk.compute_state["backend"].get("securityPolicy")
    assert (await cloud.detach_group("demo"))["security_policies"] == []


async def test_detach_zone_destroys_one_namespace_and_its_gsa(cloud, fk):
    """0.6.0: a zone dropped by a group is torn down for real — its namespace and zone GSA — and nothing else."""
    for z in ("a", "b"):
        await cloud.attach_zone("demo", z, SPEC)
        await cloud.create_service_account("demo", z)
    r = await cloud.detach_zone("demo", "a")
    assert r == {"namespaces": ["ramen-demo-a"], "service_accounts": ["ramen-demo-a@p1.iam.gserviceaccount.com"]}
    assert ("Namespace", None, "ramen-demo-a") not in fk.k8s.objs and ("Namespace", None, "ramen-demo-b") in fk.k8s.objs
    assert (await cloud.detach_group("demo"))["service_accounts"] == ["ramen-demo-b@p1.iam.gserviceaccount.com"]
    assert (await cloud.detach_zone("demo", "a"))["service_accounts"] == []  # idempotent


async def test_set_ip_rules_retries_while_policy_not_ready(cloud, fk, monkeypatch):
    import ramen_console.cloud.gcp_api as api

    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["not_ready"] = 3
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    assert r["ok"] and fk.compute_state["not_ready"] == 0
    assert any(r["action"] == "deny(403)" for r in fk.compute_state["policies"]["ramen-demo"]["rules"])


async def test_refresh_lists_groups(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.attach_zone("other", "b", SPEC)
    r = await cloud.refresh()
    assert r["groups"] == ["demo", "other"]


async def test_deploy_waits_for_old_pods_to_drain(cloud, fk, http_state):
    await cloud.attach_zone("demo", "a", SPEC)
    calls = {"n": 0}
    real = fk.k8s.core.list_namespaced_pod

    def listing(ns, label_selector=""):
        out = real(ns, label_selector)
        calls["n"] += 1
        if calls["n"] <= 2:  # first two polls: an old pod is still terminating
            out["items"].append(
                {
                    "metadata": {
                        "name": "worker-old",
                        "labels": {"app": "worker", "ramen.io/track": "stable"},
                        "namespace": ns,
                        "deletionTimestamp": "2026-09-28T00:00:00Z",
                    },
                    "status": {"phase": "Running"},
                }
            )
        return out

    fk.k8s.core.list_namespaced_pod = listing
    r = await cloud.deploy("demo", "dev", "a", canary=True, config={"RAMEN_MCP_KEYS": "rmk_1"})
    assert r["ok"] and any("old pods drained" in x for x in r["log"])


async def test_rebalance_and_armor_retry_while_backend_not_ready(cloud, fk, monkeypatch):
    import ramen_console.cloud.gcp_api as api

    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["backend"] = {
        "name": "gkegw1-ramen-demo-a",
        "fingerprint": "f1",
        "backends": [{"group": "x/networkEndpointGroups/ramen-demo-a", "capacityScaler": 1.0}],
    }
    fk.compute_state["bs_not_ready"] = 2
    r = await cloud.rebalance("demo", "a")
    assert r["applied"] and fk.compute_state["bs_not_ready"] == 0
    fk.compute_state["bs_not_ready"] = 2
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    assert r["attached"] and fk.compute_state["bs_not_ready"] == 0


async def test_abort_deploy_scales_canary_to_zero(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    fk.k8s.objs[("Deployment", "ramen-demo-a", "worker-canary")]["spec"]["replicas"] = 1
    await cloud.abort_deploy("demo", "a")
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0
    await cloud.abort_deploy("demo", "nozone")  # no namespace: no-op


async def test_armor_add_rule_converges_when_retry_already_landed(cloud, fk, monkeypatch):
    import ramen_console.cloud.gcp_api as api

    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["dup_once"] = True
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    assert r["ok"]
    rules = [x for x in fk.compute_state["policies"]["ramen-demo"]["rules"] if x["priority"] == 1000]
    assert len(rules) == 1 and rules[0]["match"]["config"]["srcIpRanges"] == ["10.0.0.0/8"]


async def test_rebalance_hands_off_to_background_when_backend_stays_busy(cloud, fk, monkeypatch):
    import ramen_console.cloud.gcp_api as api

    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["backend"] = {
        "name": "gkegw1-ramen-demo-a",
        "fingerprint": "f1",
        "backends": [{"group": "x/networkEndpointGroups/ramen-demo-a", "capacityScaler": 1.0}],
    }
    fk.compute_state["bs_not_ready"] = 10  # more than the 6 synchronous attempts
    r = await cloud.rebalance("demo", "a")
    assert r["ok"] and not r["applied"] and "pending" in r["note"] and r["backend_service"] == "gkegw1-ramen-demo-a"
    import asyncio

    for t in list(getattr(cloud, "_bg", ())):
        await asyncio.wrap_future(t) if hasattr(t, "result") and not isinstance(t, asyncio.Future) else t
    assert fk.compute_state["bs_not_ready"] == 0


def test_real_client_class_exposes_every_lazy_client():
    from ramen_console.cloud.gcp_clients import GcpClients, fresh_http

    for name in (
        "core",
        "apps",
        "autoscaling",
        "custom",
        "storage",
        "secretmanager",
        "logging",
        "compute",
        "iam",
        "crm",
    ):
        assert isinstance(getattr(GcpClients, name), property), name
    assert callable(fresh_http)  # not invoked: without ADC it would probe the GCE metadata server (slow timeouts)


async def test_apply_sa_permissions_binds_mapped_roles(cloud, fk):
    r = await cloud.apply_sa_permissions("demo", "a", ["bucket.read", "logs.write", "secrets.read", "unknown.perm"])
    assert (
        r["ok"]
        and r["service_account"] == "ramen-demo-a@p1.iam.gserviceaccount.com"
        and r["ksa"] == "ramen-demo-a/worker"
    )
    assert r["applied"] == [  # resource-level grants first, project-wide ones last
        "roles/storage.objectViewer",
        "roles/secretmanager.secretAccessor",
        "roles/logging.logWriter",
    ]
    # bucket/secret roles are bound on the resources; only project-wide roles (logging) touch the project policy
    pol = {b["role"]: b for b in fk.iam_state["project_policy"]["bindings"]}
    assert list(pol) == ["roles/logging.logWriter"] and "condition" not in pol["roles/logging.logWriter"]
    assert pol["roles/logging.logWriter"]["members"] == ["serviceAccount:ramen-demo-a@p1.iam.gserviceaccount.com"]
    bucket = fk.storage.buckets["p1-groups"].policy.bindings
    assert [b["role"] for b in bucket] == ["roles/storage.objectViewer"]
    assert "p1-groups/objects/demo/" in bucket[0]["condition"]["expression"]
    # §13.2: applying a shorter list is a revoke — the project-wide role logging no longer needs is unbound, and the
    # emptied binding is shed. Baseline identity roles are reported as retained instead of being taken away.
    again = await cloud.apply_sa_permissions("demo", "a", ["bucket.read"])
    assert again["applied"] == ["roles/storage.objectViewer"]
    assert again["revoked"] == ["roles/logging.logWriter"] and fk.iam_state["project_policy"]["bindings"] == []
    assert again["retained"] == ["roles/secretmanager.secretAccessor"]
    assert [b["role"] for b in fk.storage.buckets["p1-groups"].policy.bindings] == ["roles/storage.objectViewer"]
    last = await cloud.apply_sa_permissions("demo", "a", [])
    assert last["applied"] == []
    assert last["retained"] == ["roles/storage.objectViewer", "roles/secretmanager.secretAccessor"]
    assert [b["role"] for b in fk.storage.buckets["p1-groups"].policy.bindings] == ["roles/storage.objectViewer"]


async def test_scoped_permissions_bind_on_the_named_buckets_and_secrets(cloud, fk):
    """0.5.93: a request's scope names the buckets / secrets a permission is for; `*` is the group's own area."""
    member = "serviceAccount:ramen-demo-a@p1.iam.gserviceaccount.com"
    for sid in ("ramen-demo-all-all-db-pass", "ramen-demo-all-all-other"):
        fk.secretmanager.create_secret(request={"parent": "projects/p1", "secret_id": sid, "secret": {}})
    scopes = {"bucket.write": ["bucket-a", "bucket-b"], "secrets.read": ["db-pass"], "logs.write": ["sink-a"]}
    r = await cloud.apply_sa_permissions("demo", "a", ["bucket.write", "secrets.read", "logs.write"], scopes)
    assert r["ok"] and r["scopes"] == scopes and r["unscoped"] == ["logs.write"]  # project-wide: no resource to name
    for name in ("bucket-a", "bucket-b"):
        b = fk.storage.buckets[name].policy.bindings
        assert [x["role"] for x in b] == ["roles/storage.objectUser"] and member in b[0]["members"]
        assert not b[0].get("condition")  # the whole named bucket, not a prefix of it
    groups_bucket = fk.storage.buckets.get("p1-groups")
    assert not groups_bucket or all(x["role"] != "roles/storage.objectUser" for x in groups_bucket.policy.bindings)
    secrets = fk.secretmanager.secrets
    assert any(member in b.members for b in secrets["projects/p1/secrets/ramen-demo-all-all-db-pass"]["policy"])
    assert not any(member in b.members for b in secrets["projects/p1/secrets/ramen-demo-all-all-other"]["policy"])
    # back to `*`: the named buckets lose the binding, the groups bucket gets it with the group-prefix condition
    again = await cloud.apply_sa_permissions("demo", "a", ["bucket.write"], {"bucket.write": ["*"]}, previous=scopes)
    assert fk.storage.buckets["bucket-a"].policy.bindings == [] and fk.storage.buckets["bucket-b"].policy.bindings == []
    groups = [x for x in fk.storage.buckets["p1-groups"].policy.bindings if x["role"] == "roles/storage.objectUser"]
    assert len(groups) == 1 and "p1-groups/objects/demo/" in groups[0]["condition"]["expression"]
    assert "unscoped" not in again
    # and dropping the permission unbinds it there too
    last = await cloud.apply_sa_permissions("demo", "a", [], {}, previous={"bucket.write": ["*"]})
    assert "roles/storage.objectUser" in last["revoked"]

    # a bucket the console identity cannot administer (or that does not exist) is a clear message, not a bare 403
    class Locked:
        def get_iam_policy(self, requested_policy_version=3):
            raise FakeApiError(403, "storage.buckets.getIamPolicy denied")

    fk.storage.buckets["locked"] = Locked()
    with pytest.raises(Exception, match="roles/storage.admin") as e:
        await cloud.apply_sa_permissions("demo", "a", ["bucket.read"], {"bucket.read": ["locked"]})
    assert "gs://locked" in str(e.value)
    # naming the groups bucket itself never widens past the group's prefix, and a revoke keeps the baseline binding
    await cloud.apply_sa_permissions("demo", "a", ["bucket.read"], {"bucket.read": ["p1-groups"]})
    viewer = [x for x in fk.storage.buckets["p1-groups"].policy.bindings if x["role"] == "roles/storage.objectViewer"]
    assert len(viewer) == 1 and "objects/demo/" in viewer[0]["condition"]["expression"]
    await cloud.apply_sa_permissions("demo", "a", [], {}, previous={"bucket.read": ["p1-groups"]})
    viewer = [x for x in fk.storage.buckets["p1-groups"].policy.bindings if x["role"] == "roles/storage.objectViewer"]
    assert len(viewer) == 1 and member in viewer[0]["members"]


async def test_apply_sa_permissions_without_project_iam_admin(cloud, fk, monkeypatch):
    """Project-wide roles need the optional projectIamAdmin grant (terraform var console_project_iam): a 403 is
    reported, not raised, and the resource-level roles are still applied."""
    import ramen_console.cloud.gcp_api as api

    def deny(self, email, bindings):
        raise FakeApiError(403, "setIamPolicy denied")

    monkeypatch.setattr(api.Iam, "grant_project_roles", deny)
    r = await cloud.apply_sa_permissions("demo", "a", ["bucket.read", "logs.write"])
    assert r["ok"] and r["applied"] == ["roles/storage.objectViewer"]
    assert r["skipped"] == ["roles/logging.logWriter"] and "console_project_iam" in r["note"]


async def test_rebalance_degrades_when_the_compute_api_is_unreachable(cloud, fk):
    """§7: the load-balancer leg never fails the call, whatever the compute API does — and the HPA
    re-scale, which needs no cloud API, must survive it (D-CONSOLE-1, found on kind)."""
    await cloud.attach_zone("demo", "a", SPEC)
    fk.k8s.objs[("HorizontalPodAutoscaler", "ramen-demo-a", "worker")]["spec"]["minReplicas"] = 3

    class NoCreds(Exception):
        pass

    def boom(*a, **k):
        raise NoCreds("Your default credentials were not found")

    fk.compute_state["find_raises"] = boom
    r = await cloud.rebalance("demo", "a")
    assert r["ok"] and r["applied"] is False and "NoCreds" in r["note"]
    assert r["scaled_to"] == 3, "the HPA re-scale must not be lost with the load-balancer leg"
    assert obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["replicas"] == 3
    out = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    assert out["ok"] and out["attached"] is False and "enforced at the node" in out["note"]
    # the control that matters reached the node even though no cloud call could be made
    assert obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"


async def test_refresh_survives_a_terminating_or_unreadable_namespace(cloud, fk):
    """D4 (found on GKE): a group delete leaves its namespace Terminating for minutes with the console's
    RoleBinding already collected, so reads inside it answer 403. Reconcile must survey what it can."""
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.attach_zone("demo", "b", SPEC)
    fk.k8s.objs[("Namespace", None, "ramen-demo-b")].setdefault("status", {})["phase"] = "Terminating"
    r = await cloud.refresh()
    assert [z["zone"] for z in r["zones"]] == ["a"]
    assert r["skipped"] == [{"namespace": "ramen-demo-b", "reason": "terminating"}]

    real_read = cloud.kube.read

    def forbidden(kind, ns, name):
        if ns == "ramen-demo-a":
            raise FakeApiError(403, "Forbidden")
        return real_read(kind, ns, name)

    cloud.kube.read = forbidden
    r = await cloud.refresh()
    assert r["zones"] == [] and r["skipped"][0]["namespace"] == "ramen-demo-a"
    assert "403" in r["skipped"][0]["reason"] or "Forbidden" in r["skipped"][0]["reason"]


async def test_a_pinned_group_image_is_what_the_zone_renders(cloud, fk):
    """§13.3: the spec carries the group's pin; without one the adapter's release image is used."""
    await cloud.attach_zone("demo", "a", SPEC)
    dep = fk.k8s.objs[("Deployment", "ramen-demo-a", "worker")]
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "img/worker:0.2.0"

    await cloud.attach_zone("demo", "a", {**SPEC, "image": "ar/worker:demo-3"})
    dep = fk.k8s.objs[("Deployment", "ramen-demo-a", "worker")]
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "ar/worker:demo-3"
    canary = fk.k8s.objs[("Deployment", "ramen-demo-a", "worker-canary")]
    assert canary["spec"]["template"]["spec"]["containers"][0]["image"] == "ar/worker:demo-3"


async def test_unbinding_a_secret_role_rewrites_only_that_binding(cloud, fk):
    """The secret leg of the revoke pass (§13.2). Every catalogue secret role is a baseline role today, so this is
    exercised directly: it is what keeps a future non-baseline secretmanager permission revocable."""
    iam = cloud._iam()
    email = "ramen-demo-a@p1.iam.gserviceaccount.com"
    member = f"serviceAccount:{email}"
    sm = fk.secretmanager
    sm.create_secret(request={"parent": "projects/p1", "secret_id": "ramen-demo-TOKEN", "secret": {}})
    name = iam.group_secrets(sm, "demo")[0]
    iam.bind_secret(sm, name, ["roles/secretmanager.admin", "roles/secretmanager.secretAccessor"], [member, "user:x@y"])

    assert iam.unbind_secret(sm, name, ["roles/secretmanager.admin"], [member]) == ["roles/secretmanager.admin"]
    pol = {b.role: list(b.members) for b in sm.get_iam_policy(request={"resource": name}).bindings}
    assert pol["roles/secretmanager.admin"] == ["user:x@y"]  # the other member is untouched
    assert member in pol["roles/secretmanager.secretAccessor"]  # the other role is untouched
    assert iam.unbind_secret(sm, name, ["roles/secretmanager.admin"], [member]) == []  # idempotent
    assert iam.revoke_secret_roles(sm, "demo", email, ["roles/secretmanager.secretAccessor"]) == [
        "roles/secretmanager.secretAccessor"
    ]
    assert iam.unbind_secret(sm, "projects/p1/secrets/ramen-demo-GONE", ["roles/x"], [member]) == []


async def test_revoking_project_roles_without_project_iam_admin_is_reported_not_raised(cloud, fk, monkeypatch):
    import ramen_console.cloud.gcp_api as api

    def deny(self, email, roles):
        raise FakeApiError(403, "setIamPolicy denied")

    await cloud.apply_sa_permissions("demo", "a", ["logs.write"])
    monkeypatch.setattr(api.Iam, "revoke_project_roles", deny)
    r = await cloud.apply_sa_permissions("demo", "a", ["bucket.write"])
    assert r["ok"] and "roles/logging.logWriter" not in r.get("revoked", [])

    def boom(self, email, roles):
        raise RuntimeError("iam unreachable")

    monkeypatch.setattr(api.Iam, "revoke_project_roles", boom)  # anything but a 403 is a real failure
    with pytest.raises(ApiError) as e:
        await cloud.apply_sa_permissions("demo", "a", [])
    assert e.value.status_code == 502
