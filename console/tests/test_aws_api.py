"""Console API/UI on top of the aws adapter + aws secrets backend (moto + k8s fake). Untested on a real account."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.cloud.aws import AwsCloud
from ramen_console.secrets.aws import AwsSecrets
from ramen_console.storage import make_store
from tests.fakes_aws import BUCKET, FakeAwsClients, aws_env

VALUE = "sup3r-s3cret-aws-value"


@pytest.fixture
def fk():
    with aws_env():
        yield FakeAwsClients().seed()


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
                 "RAMEN_CLOUD": "aws", "RAMEN_SECRETS_BACKEND": "aws", "RAMEN_AWS_REGION": "us-east-1", "RAMEN_GROUPS_BUCKET": BUCKET,
                 "RAMEN_IMAGE_WORKER": "img:0.3.0", "RAMEN_EKS_CLUSTER": "ramen"}.items():
        monkeypatch.setenv(k, v)
    cloud = AwsCloud(bucket=BUCKET, image="img:0.3.0", admin_key="adm", clients=fk, transport=httpx.MockTransport(handler), wait_secs=1, poll=0)
    return create_app(store=make_store(), cloud=cloud, secrets=AwsSecrets("us-east-1", fk.secretsmanager))


@pytest.fixture
def root(app):
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture
def demo(root):
    assert root.post("/api/v1/groups", json={"name": "demo", "repo_url": "https://example.com/demo.git"}).status_code == 201
    assert root.post("/api/v1/zones", json={"name": "a", "provider": "aws", "region": "us-east-1a"}).status_code == 201
    r = root.post("/api/v1/groups/demo/environments", json={"name": "prod", "zones": ["a"]})
    assert r.status_code == 201, r.text
    return root


def test_env_attach_creates_namespace_with_ingress(demo, fk):
    dep = fk.k8s.objs[("Deployment", "ramen-demo-a", "worker")]
    assert dep["spec"]["template"]["spec"]["nodeSelector"] == {"topology.kubernetes.io/zone": "us-east-1a"}
    ing = fk.k8s.objs[("Ingress", "ramen-demo-a", "worker")]
    assert ing["spec"]["rules"][0]["http"]["paths"][0]["path"] == "/mcp/demo/a"
    assert ("HTTPRoute", "ramen-demo-a", "worker") not in fk.k8s.objs


def test_secret_value_never_leaves_and_delete_removes_asm(demo, fk):
    r = demo.post("/api/v1/groups/demo/secrets", json={"name": "TOKEN", "value": VALUE, "env": "prod"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["ref"] == "asm://ramen/demo/prod/all/TOKEN" and "value" not in r.json()
    assert fk.secretsmanager.get_secret_value(SecretId="ramen/demo/prod/all/TOKEN")["SecretString"] == VALUE
    for path in ("/api/v1/groups/demo/secrets", "/secrets?group=demo", "/api/v1/groups/demo/secrets?format=csv", "/audit", "/api/v1/audit"):
        assert VALUE not in demo.get(path).text, path
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()
    assert key["key"].startswith("rmk_") and fk.secretsmanager.get_secret_value(SecretId="ramen/demo/all/all/mcp-ci")["SecretString"] == key["key"]
    assert demo.delete(f"/api/v1/groups/demo/secrets/{sid}").status_code == 200
    assert "ramen/demo/prod/all/TOKEN" not in [s["Name"] for s in fk.secretsmanager.list_secrets()["SecretList"]]


def test_deploy_resolves_secrets_and_sets_split(demo, fk, monkeypatch):
    demo.post("/api/v1/groups/demo/secrets", json={"name": "TOKEN", "value": VALUE, "env": "prod"})
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()["key"]
    monkeypatch.setattr("ramen_console.cloud.aws_api.sync_repo_to_s3", lambda s3, bucket, group, *a: f"s3://{bucket}/{group}")
    job = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True}).json()
    j = demo.get(f"/api/v1/jobs/{job['id']}").json()
    assert j["status"] == "ok", j
    sec = fk.k8s.objs[("Secret", "ramen-demo-a", "ramen-deploy")]["stringData"]
    assert sec["RAMEN_SECRET_DEMO__TOKEN"] == VALUE and sec["RAMEN_MCP_KEYS"] == key and sec["RAMEN_ENV"] == "prod"
    assert any("smoke ok" in l for l in j["log"]) and any("traffic split" in l for l in j["log"])
    assert VALUE not in json.dumps(j) and VALUE not in demo.get(f"/ui/jobs/{job['id']}").text


def test_rebalance_ip_rules_sa_refresh(demo, fk):
    r = demo.post("/api/v1/groups/demo/zones/a/rebalance")
    assert r.status_code == 200 and r.json()["applied"] is False and "not provisioned" in r.json()["note"]
    fk.alb()
    r = demo.post("/api/v1/groups/demo/zones/a/rebalance").json()
    assert r["ok"] and r["applied"] and r["alb"].endswith(".elb.amazonaws.com")
    r = demo.put("/api/v1/groups/demo/zones/a/ip-rules", json={"cidrs": ["10.0.0.0/8"]}).json()
    assert r["policy"] == "ramen-demo" and r["attached"] is True
    assert fk.k8s.objs[("Secret", "ramen-demo-a", "ramen-deploy")]["stringData"]["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"
    sa = demo.post("/api/v1/groups/demo/zones/a/service-account").json()
    assert sa["name"].endswith(":role/ramen/ramen-demo-a")
    r = demo.post("/api/v1/refresh").json()
    assert r["zones"][0]["namespace"] == "ramen-demo-a" and r["service_accounts"] == [sa["name"]]
    w = demo.get("/api/v1/groups/demo/zones/a/workers").json()
    assert w["live"][0]["load"] == "even" and w["service_account"] == sa["name"]
    c = demo.get("/api/v1/config").json()
    assert c["secrets_backend"] == "aws" and c["env"]["RAMEN_CLOUD"] == "aws" and c["env"]["RAMEN_GROUPS_BUCKET"] == BUCKET
    assert "ramen-demo-a" in demo.get("/ui/groups/demo/zones/a/workers").text


def test_group_delete_detaches_aws(demo, fk):
    demo.post("/api/v1/groups/demo/zones/a/service-account")
    assert demo.delete("/api/v1/groups/demo").status_code == 200
    assert ("Namespace", None, "ramen-demo-a") not in fk.k8s.objs
    assert fk.iam.list_roles(PathPrefix="/ramen/")["Roles"] == []


def test_logs_endpoint(demo, fk):
    fk.logs.results = [{"@timestamp": "2026-09-28 01:00:00.000", "kubernetes.pod_name": "worker-0", "stream": "stdout", "log": "hello"}]
    r = demo.get("/api/v1/logs?group=demo&zone=a")
    assert r.status_code == 200 and "worker-0 hello" in r.text
