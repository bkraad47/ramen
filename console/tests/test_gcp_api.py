"""Console API/UI on top of the gcp adapter + gcp secrets backend, all with fakes."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.secrets.gcp import GcpSecrets
from ramen_console.storage import make_store
from tests.fakes_gcp import FakeClients

VALUE = "sup3r-s3cret-gcp-value"


@pytest.fixture
def fk():
    return FakeClients()


@pytest.fixture
def state():
    return {"smoke_ok": True}


@pytest.fixture
def app(monkeypatch, fk, state):
    def handler(req: httpx.Request):
        if req.url.path == "/metrics":
            return httpx.Response(200, json={"inflight": 1, "total": 3, "errors": 0, "load": "even"})
        if req.url.path == "/admin/reload":
            assert req.headers["X-Ramen-Admin-Key"] == "adm"
            return httpx.Response(200, json={"tools": [{"name": "calc"}], "resources": [], "prompts": [], "errors": []})
        if req.url.path == "/mcp":
            assert VALUE not in req.headers["Authorization"]
            return httpx.Response(200 if state["smoke_ok"] else 500, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
        return httpx.Response(404)
    for k, v in {"RAMEN_STORE": "memory", "RAMEN_ADMIN_EMAIL": "root@ramen.local", "RAMEN_ADMIN_PASSWORD": "rootpw",
                 "RAMEN_CLOUD": "gcp", "RAMEN_SECRETS_BACKEND": "gcp", "RAMEN_GCP_PROJECT": "p1", "RAMEN_GROUPS_BUCKET": "p1-groups",
                 "RAMEN_IMAGE_WORKER": "img:0.2.0"}.items():
        monkeypatch.setenv(k, v)
    cloud = GcpCloud("p1", bucket="p1-groups", image="img:0.2.0", admin_key="adm", clients=fk,
                     transport=httpx.MockTransport(handler), wait_secs=1, poll=0)
    return create_app(store=make_store(), cloud=cloud, secrets=GcpSecrets("p1", fk.secretmanager))


@pytest.fixture
def root(app):
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture
def demo(root):
    assert root.post("/api/v1/groups", json={"name": "demo", "repo_url": "https://example.com/demo.git"}).status_code == 201
    assert root.post("/api/v1/zones", json={"name": "a", "provider": "gcp", "region": "us-central1-a"}).status_code == 201
    r = root.post("/api/v1/groups/demo/environments", json={"name": "prod", "zones": ["a"]})
    assert r.status_code == 201, r.text
    return root


def test_env_attach_creates_namespace(demo, fk):
    dep = fk.k8s.objs[("Deployment", "ramen-demo-a", "worker")]
    assert dep["spec"]["template"]["spec"]["nodeSelector"] == {"topology.kubernetes.io/zone": "us-central1-a"}
    assert demo.post("/api/v1/zones", json={"name": "b", "provider": "gcp", "region": "us-central1-b"}).status_code == 201
    assert demo.put("/api/v1/groups/demo/environments/prod", json={"zones": ["a", "b"]}).status_code == 200
    assert ("Namespace", None, "ramen-demo-b") in fk.k8s.objs


def test_secret_value_never_leaves_and_delete_removes_sm(demo, fk):
    r = demo.post("/api/v1/groups/demo/secrets", json={"name": "TOKEN", "value": VALUE, "env": "prod"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["ref"] == "sm://projects/p1/secrets/ramen-demo-prod-all-TOKEN" and "value" not in r.json()
    assert fk.secretmanager.secrets["projects/p1/secrets/ramen-demo-prod-all-TOKEN"]["versions"] == [VALUE.encode()]
    stored = demo.app.state.store._data["secrets"][sid] if hasattr(demo.app.state.store, "_data") else None
    if stored is None:
        stored = demo.app.state.store.inner._data["secrets"][sid]
    assert stored["value"] is None and stored["backend"] == "gcp"
    for path in ("/api/v1/groups/demo/secrets", "/secrets?group=demo", "/api/v1/groups/demo/secrets?format=csv", "/audit", "/api/v1/audit"):
        assert VALUE not in demo.get(path).text, path
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()
    assert key["key"].startswith("rmk_") and "projects/p1/secrets/ramen-demo-all-all-mcp-ci" in fk.secretmanager.secrets
    assert demo.delete(f"/api/v1/groups/demo/secrets/{sid}").status_code == 200
    assert "projects/p1/secrets/ramen-demo-prod-all-TOKEN" not in fk.secretmanager.secrets
    assert "delete" in fk.secretmanager.calls


def test_deploy_resolves_secrets_and_streams_log(demo, fk, monkeypatch, tmp_path):
    demo.post("/api/v1/groups/demo/secrets", json={"name": "TOKEN", "value": VALUE, "env": "prod"})
    demo.post("/api/v1/groups/demo/secrets", json={"name": "GITHUB_TOKEN", "value": "gh-token"})
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()["key"]
    seen = {}

    def fake_sync(storage, bucket, group, repo_url, ref, token):
        seen["token"] = token
        return f"gs://{bucket}/{group}"
    monkeypatch.setattr("ramen_console.cloud.gcp_api.sync_repo_to_gcs", fake_sync)
    job = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True}).json()
    j = demo.get(f"/api/v1/jobs/{job['id']}").json()
    assert j["status"] == "ok", j
    assert seen["token"] == "gh-token"
    sec = fk.k8s.objs[("Secret", "ramen-demo-a", "ramen-deploy")]["stringData"]
    assert sec["RAMEN_SECRET_DEMO__TOKEN"] == VALUE and sec["RAMEN_MCP_KEYS"] == key and sec["RAMEN_ENV"] == "prod"
    assert any("smoke ok" in l for l in j["log"]) and any("syncing repo" in l for l in j["log"])
    assert VALUE not in json.dumps(j) and "gh-token" not in json.dumps(j)
    page = demo.get(f"/ui/jobs/{job['id']}").text
    assert "smoke ok" in page and VALUE not in page
    g = demo.get("/groups/demo").text
    assert "calc" in g and "ramen-demo-a" in g and VALUE not in g


def test_deploy_canary_failure_in_job(demo, fk, monkeypatch, state):
    monkeypatch.setattr("ramen_console.cloud.gcp_api.sync_repo_to_gcs", lambda *a: "gs://p1-groups/demo")
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    state["smoke_ok"] = False
    job = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True}).json()
    j = demo.get(f"/api/v1/jobs/{job['id']}").json()
    assert j["status"] == "error" and "smoke" in j["error"]
    assert fk.k8s.objs[("Deployment", "ramen-demo-a", "worker-canary")]["spec"]["replicas"] == 0
    assert "restartedAt" not in json.dumps(fk.k8s.objs[("Deployment", "ramen-demo-a", "worker")])
    assert any("scaled canary to 0" in l for l in j["log"])
    assert "scaled canary to 0" in demo.get(f"/ui/jobs/{job['id']}").text


def test_workers_sizes_and_ui(demo, fk):
    r = demo.get("/api/v1/groups/demo/zones/a/workers").json()
    assert r["size"] == "s" and r["count"] == 1 and r["live"][0]["track"] == "stable" and r["live"][0]["load"] == "even"
    r = demo.put("/api/v1/groups/demo/zones/a/workers", json={"count": 2, "size": "m", "allowed_sizes": "s,m"})
    assert r.status_code == 200 and r.json()["allowed_sizes"] == ["s", "m"] and r.json()["cloud"]["ok"]
    dep = fk.k8s.objs[("Deployment", "ramen-demo-a", "worker")]
    assert dep["spec"]["replicas"] == 2 and dep["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] == "500m"
    assert demo.put("/api/v1/groups/demo/zones/a/workers", json={"size": "xl"}).status_code == 422
    assert demo.put("/api/v1/groups/demo/zones/a/workers", json={"allowed_sizes": ["huge"]}).status_code == 422
    demo.post("/api/v1/users", json={"email": "ga@x", "password": "pw", "role": "group_admin", "groups": ["demo"]})
    with TestClient(demo.app) as ga:
        ga.post("/login", data={"email": "ga@x", "password": "pw"}, follow_redirects=False)
        assert ga.put("/api/v1/groups/demo/zones/a/workers", json={"size": "s"}).status_code == 200
        assert ga.put("/api/v1/groups/demo/zones/a/workers", json={"size": "l"}).status_code == 403
        assert ga.put("/api/v1/groups/demo/zones/a/workers", json={"allowed_sizes": ["l"]}).status_code == 403
        html = ga.get("/groups/demo").text
        assert '<option value="s" selected>' in html and 'value="l"' not in html
    html = demo.get("/groups/demo").text
    assert '<option value="l" >' in html and 'name="allowed_sizes"' in html
    part = demo.get("/ui/groups/demo/zones/a/workers").text
    assert "worker-0" in part and "[stable]" in part and "2 × s" in part
    zones = demo.get("/zones").text
    assert "ramen-demo-a" in zones and "/ui/groups/demo/zones/a/workers" in zones


def test_workers_partial_with_canary_and_errors(demo, fk):
    fk.k8s.objs[("Deployment", "ramen-demo-a", "worker-canary")]["spec"]["replicas"] = 1
    part = demo.get("/ui/groups/demo/zones/a/workers").text
    assert "[canary]" in part and "canary:" in part and "1/1 ready" in part

    async def boom(group, zone):
        raise RuntimeError("k8s down")
    demo.app.state.services.cloud.workers = boom
    part = demo.get("/ui/groups/demo/zones/a/workers").text
    assert "k8s down" in part
    dash = demo.get("/ui/dashboard").text
    assert "k8s down" in dash


def test_refresh_reconciles_store_and_dashboard(demo, fk):
    fk.k8s.objs[("Deployment", "ramen-demo-a", "worker-canary")]["spec"]["replicas"] = 1
    demo.post("/api/v1/groups/demo/zones/a/service-account")
    r = demo.post("/api/v1/refresh").json()
    assert r["zones"][0]["namespace"] == "ramen-demo-a" and r["service_accounts"] == ["ramen-demo-a@p1.iam.gserviceaccount.com"]
    w = demo.get("/api/v1/groups/demo/zones/a/workers").json()
    assert w["live_state"]["canary_replicas"] == 1 and w["service_account"].startswith("ramen-demo-a@")
    part = demo.get("/ui/groups/demo/zones/a/workers").text
    assert "<code>ramen-demo-a</code>" in part
    dash = demo.get("/ui/dashboard").text
    assert "[canary]" in dash and "green" in dash


def test_rebalance_ip_rules_config(demo, fk):
    r = demo.post("/api/v1/groups/demo/zones/a/rebalance")
    assert r.status_code == 404 and "ramen-demo-a" in r.text
    fk.compute_state["backend"] = {"name": "gkegw1-ramen-demo-a", "backends": [{"group": "x/networkEndpointGroups/ramen-demo-a", "capacityScaler": 1.0}]}
    r = demo.post("/api/v1/groups/demo/zones/a/rebalance").json()
    assert r["ok"] and r["backend_service"] == "gkegw1-ramen-demo-a" and r["capacity_scaler"] == 1.0
    assert fk.k8s.objs[("HTTPRoute", "ramen-demo-a", "worker")]["spec"]["rules"][0]["matches"][0]["path"]["value"] == "/mcp/demo/a"
    r = demo.put("/api/v1/groups/demo/zones/a/ip-rules", json={"cidrs": ["10.0.0.0/8"]}).json()
    assert r["policy"] == "ramen-demo" and r["attached"] is True
    assert fk.k8s.objs[("Secret", "ramen-demo-a", "ramen-deploy")]["stringData"]["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"
    c = demo.get("/api/v1/config").json()
    assert c["secrets_backend"] == "gcp" and c["gcp"]["RAMEN_GCP_PROJECT"] == "p1" and c["env"]["RAMEN_SECRETS_BACKEND"] == "gcp"
    assert c["env"]["RAMEN_GROUPS_BUCKET"] == "p1-groups"
    assert "RAMEN_SECRETS_BACKEND" in demo.get("/config").text


def test_cloud_error_is_502(demo, fk):
    fk.k8s.objs.clear()

    def boom(*a, **k):
        raise RuntimeError("apiserver unreachable")
    fk.k8s._create = boom
    r = demo.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["a"]})
    assert r.status_code == 502 and "apiserver" in r.text
    r = demo.get("/api/v1/logs?group=demo&zone=a")
    assert r.status_code == 200 and r.text == ""
