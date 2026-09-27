import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.cloud.local import LocalCloud
from ramen_console.storage import make_store

SECRET_VALUE = "sup3r-s3cret-value-XYZ"


@pytest.fixture
def cloud(tmp_path):
    def handler(req: httpx.Request):
        if req.url.path == "/metrics":
            load = "high" if req.url.host == "hot" else "low"
            return httpx.Response(200, json={"inflight": 1, "total": 5, "errors": 0, "load": load})
        if req.url.path == "/admin/reload":
            return httpx.Response(200, json={"tools": [{"name": "calc"}], "resources": [], "prompts": [], "errors": []})
        return httpx.Response(404)
    return LocalCloud(tmp_path / "buckets", tmp_path / "logs",
                      {"demo/zone-a": ["http://w1:8080"], "demo/zone-b": ["http://hot:8080"]},
                      "http://default:8080", "adm", httpx.MockTransport(handler))


@pytest.fixture
def app(monkeypatch, tmp_path, cloud):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.local")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setenv("RAMEN_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("RAMEN_BUCKET_ROOT", str(tmp_path / "buckets"))
    monkeypatch.setenv("RAMEN_SECRET_SHOULD_MASK", "mask-me")
    return create_app(store=make_store(), cloud=cloud)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def login(client, email, pw):
    r = client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


@pytest.fixture
def root(client):
    return login(client, "root@ramen.local", "rootpw")


@pytest.fixture
def demo(root):
    r = root.post("/api/v1/groups", json={"name": "demo", "repo_url": "https://example.com/demo.git", "ref": "main"})
    assert r.status_code == 201, r.text
    assert root.post("/api/v1/groups", json={"name": "other"}).status_code == 201
    for z in ("zone-a", "zone-b"):
        assert root.post("/api/v1/zones", json={"name": z, "provider": "local", "region": "local"}).status_code == 201
    r = root.post("/api/v1/groups/demo/environments", json={"name": "prod", "ref": "main", "zones": ["zone-a", "zone-b"]})
    assert r.status_code == 201, r.text
    return root


def make_user(root, email, role, groups, pw="pw"):
    r = root.post("/api/v1/users", json={"email": email, "password": pw, "role": role, "groups": groups})
    assert r.status_code == 201, r.text
    return r.json()


def test_login_flow(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/me").status_code == 401
    r = client.post("/login", data={"email": "root@ramen.local", "password": "bad"})
    assert r.status_code == 401 and "Invalid" in r.text
    login(client, "root@ramen.local", "rootpw")
    me = client.get("/api/v1/me").json()
    assert me["role"] == "super_admin" and "password" not in json.dumps(me)
    assert client.get("/").status_code == 200
    client.get("/logout", follow_redirects=False)
    assert client.get("/api/v1/me").status_code == 401
    client.cookies.set("ramen_session", "tampered")
    assert client.get("/api/v1/me").status_code == 401


def test_pages_render(demo):
    for path in ("/", "/groups", "/groups/demo", "/environments", "/zones", "/secrets", "/users", "/api-keys",
                 "/logs", "/audit", "/backups", "/config", "/login"):
        r = demo.get(path)
        assert r.status_code == 200, path
        assert "F26B3A" in r.text or path == "/login" or "logo.png" in r.text
    assert demo.get("/static/logo.png").status_code == 200
    assert demo.get("/static/htmx.min.js").status_code == 200
    assert demo.get("/groups/nope").status_code == 404
    assert demo.get("/ui/dashboard").status_code == 200


def test_groups_crud_and_rbac(demo):
    assert demo.post("/api/v1/groups", json={"name": "demo"}).status_code == 409
    assert demo.post("/api/v1/groups", json={"name": "bad name!"}).status_code == 422
    r = demo.put("/api/v1/groups/demo", json={"repo_url": "https://example.com/x.git", "ref": "dev", "mcp_auth": {"mode": "bearer"}})
    assert r.status_code == 200 and r.json()["ref"] == "dev"
    assert demo.get("/api/v1/groups/nope").status_code == 404
    groups = demo.get("/api/v1/groups").json()
    assert [g["name"] for g in groups] == ["demo", "other"]
    csv = demo.get("/api/v1/groups?format=csv")
    assert csv.headers["content-type"].startswith("text/csv") and "demo" in csv.text
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "other@x", "viewer", ["other"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        assert ga.post("/api/v1/groups", json={"name": "g2"}).status_code == 403
        assert ga.delete("/api/v1/groups/demo").status_code == 403
        assert ga.put("/api/v1/groups/demo", json={"ref": "main"}).status_code == 200
        assert ga.get("/groups/demo").status_code == 200
    with TestClient(demo.app) as v:
        login(v, "v@x", "pw")
        assert v.put("/api/v1/groups/demo", json={"ref": "main"}).status_code == 403
        assert [g["name"] for g in v.get("/api/v1/groups").json()] == ["demo"]
    with TestClient(demo.app) as o:
        login(o, "other@x", "pw")
        assert o.get("/api/v1/groups/demo").status_code == 403
        assert [g["name"] for g in o.get("/api/v1/groups").json()] == ["other"]
    assert demo.delete("/api/v1/groups/demo").status_code == 200
    assert demo.get("/api/v1/groups/demo").status_code == 404
    assert demo.get("/api/v1/environments").json() == []
    assert demo.delete("/api/v1/groups/demo").status_code == 404


def test_environments(demo):
    envs = demo.get("/api/v1/environments?group=demo").json()
    assert len(envs) == 1 and envs[0]["verbose"] is False
    assert demo.post("/api/v1/groups/demo/environments", json={"name": "prod"}).status_code == 409
    assert demo.post("/api/v1/groups/demo/environments", json={"name": "x", "zones": ["nozone"]}).status_code == 422
    r = demo.put("/api/v1/groups/demo/environments/prod", json={"verbose": True, "zones": ["zone-a"]})
    assert r.status_code == 200 and r.json()["verbose"] is True and r.json()["zones"] == ["zone-a"]
    assert demo.post("/api/v1/groups/demo/environments/prod/verbose", json={"verbose": False}).json()["verbose"] is False
    assert demo.put("/api/v1/groups/demo/environments/nope", json={}).status_code == 404
    assert demo.get("/api/v1/environments?format=json").status_code == 200
    assert demo.delete("/api/v1/groups/demo/environments/prod").status_code == 200
    assert demo.delete("/api/v1/groups/demo/environments/prod").status_code == 404


def test_zones_workers(demo):
    assert demo.post("/api/v1/zones", json={"name": "zone-a"}).status_code == 409
    assert [z["name"] for z in demo.get("/api/v1/zones").json()] == ["zone-a", "zone-b"]
    r = demo.get("/api/v1/groups/demo/zones/zone-a/workers")
    assert r.status_code == 200 and r.json()["live"][0]["load"] == "low" and r.json()["count"] == 1
    r = demo.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 3, "size": "small"})
    assert r.status_code == 200 and r.json()["count"] == 3 and r.json()["size"] == "small"
    assert demo.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 0}).status_code == 422
    assert demo.post("/api/v1/groups/demo/zones/zone-a/rebalance").json()["ok"] is True
    assert demo.put("/api/v1/groups/demo/zones/zone-a/ip-rules", json={"cidrs": ["10.0.0.0/8"]}).json()["cidrs"] == ["10.0.0.0/8"]
    assert demo.put("/api/v1/groups/demo/zones/zone-a/ip-rules", json={"cidrs": ["nope"]}).status_code == 422
    assert demo.post("/api/v1/groups/demo/zones/zone-a/service-account").json()["name"].startswith("local-sa")
    assert demo.get("/api/v1/groups/demo/zones/nozone/workers").status_code == 404
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        assert ga.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 2}).status_code == 200
        assert ga.put("/api/v1/groups/demo/zones/zone-a/workers", json={"size": "big"}).status_code == 403
        assert ga.post("/api/v1/zones", json={"name": "z9"}).status_code == 403
    assert demo.delete("/api/v1/zones/zone-b").status_code == 200
    assert demo.delete("/api/v1/zones/zone-b").status_code == 404
    assert demo.get("/api/v1/environments?group=demo").json()[0]["zones"] == ["zone-a"]


def test_dashboard(demo):
    d = demo.get("/api/v1/dashboard").json()
    assert d["cells"]["zone-a"]["demo"]["color"] == "blue"
    assert d["cells"]["zone-b"]["demo"]["color"] == "red"
    html = demo.get("/ui/dashboard").text
    assert "blue" in html and "red" in html


def test_secrets_never_leak(demo):
    r = demo.post("/api/v1/groups/demo/secrets", json={"name": "GITHUB_TOKEN", "value": SECRET_VALUE, "env": "prod", "zone": "zone-a"})
    assert r.status_code == 201 and SECRET_VALUE not in r.text and r.json()["name"] == "GITHUB_TOKEN"
    sid = r.json()["id"]
    assert demo.post("/api/v1/groups/demo/secrets", json={"name": "bad-name", "value": "x"}).status_code == 422
    assert demo.post("/api/v1/groups/demo/secrets", json={"name": "GITHUB_TOKEN", "value": "x", "env": "prod", "zone": "zone-a"}).status_code == 409
    listing = demo.get("/api/v1/groups/demo/secrets")
    assert listing.status_code == 200 and SECRET_VALUE not in listing.text and "value" not in listing.json()[0]
    assert SECRET_VALUE not in demo.get("/api/v1/groups/demo/secrets?format=csv").text
    for path in ("/secrets", "/secrets?group=demo", "/groups/demo", "/audit", "/api/v1/audit", "/api/v1/groups/demo",
                 "/api/v1/backups", "/config", "/api/v1/config"):
        assert SECRET_VALUE not in demo.get(path).text, path
    assert demo.post("/api/v1/backups", json={"target": "local"}).status_code == 201
    bid = demo.get("/api/v1/backups").json()[0]["id"]
    assert SECRET_VALUE not in demo.get(f"/api/v1/backups/{bid}/download").text
    assert "mask-me" not in demo.get("/api/v1/config").text and "mask-me" not in demo.get("/config").text
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", "pw")
        r = v.get("/api/v1/groups/demo/secrets")
        assert r.status_code == 200 and SECRET_VALUE not in r.text
        assert v.post("/api/v1/groups/demo/secrets", json={"name": "X", "value": "y"}).status_code == 403
        assert v.delete(f"/api/v1/groups/demo/secrets/{sid}").status_code == 403
    assert demo.delete(f"/api/v1/groups/demo/secrets/{sid}").status_code == 200
    assert demo.delete(f"/api/v1/groups/demo/secrets/{sid}").status_code == 404


def test_deploy_job(demo, tmp_path, monkeypatch):
    async def fake_sync(group, repo_url, ref, token):
        (tmp_path / "buckets" / group).mkdir(parents=True, exist_ok=True)
        fake_sync.calls.append((repo_url, ref, token))
        return str(tmp_path / "buckets" / group)
    fake_sync.calls = []
    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", fake_sync)
    demo.post("/api/v1/groups/demo/secrets", json={"name": "GITHUB_TOKEN", "value": "ghp_tok"})
    demo.post("/api/v1/groups/demo/secrets", json={"name": "API", "value": SECRET_VALUE, "env": "prod", "zone": "zone-a"})
    demo.post("/api/v1/groups/demo/secrets", json={"name": "OTHER", "value": "no", "env": "staging"})
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "k1"})
    r = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True})
    assert r.status_code == 202, r.text
    job = r.json()
    for _ in range(50):
        job = demo.get(f"/api/v1/jobs/{job['id']}").json()
        if job["status"] != "running":
            break
    assert job["status"] == "ok", job
    assert fake_sync.calls == [("https://example.com/demo.git", "main", "ghp_tok")]
    env = (tmp_path / "buckets" / "demo" / ".ramen" / "env-zone-a").read_text()
    assert f"RAMEN_SECRET_DEMO__API={SECRET_VALUE}" in env
    assert SECRET_VALUE not in (tmp_path / "buckets" / "demo" / ".ramen" / "env-zone-b").read_text() and "OTHER" not in env and "RAMEN_MCP_KEYS=rmk_" in env
    assert "RAMEN_VERBOSE=0" in env
    assert SECRET_VALUE not in json.dumps(job)
    assert demo.get("/api/v1/environments?group=demo").json()[0]["last_deploy"]["status"] == "ok"
    html = demo.get(f"/ui/jobs/{job['id']}")
    assert html.status_code == 200 and "ok" in html.text
    assert demo.get("/api/v1/jobs/nope").status_code == 404
    r = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"zone": "zone-a"}, headers={"HX-Request": "true"})
    assert r.status_code == 202 and "hx-get" in r.text
    r = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"zone": "nozone"})
    assert r.status_code == 422
    assert "job" in demo.get("/groups/demo").text
    assert demo.post("/api/v1/groups/demo/environments/nope/deploy", json={}).status_code == 404


def test_deploy_error_surfaces(demo, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("git failed: nope")
    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", boom)
    job = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={}).json()
    for _ in range(50):
        job = demo.get(f"/api/v1/jobs/{job['id']}").json()
        if job["status"] != "running":
            break
    assert job["status"] == "error" and "git failed" in job["error"]
    assert "error" in demo.get(f"/ui/jobs/{job['id']}").text
    audit = demo.get("/api/v1/audit").json()
    assert any(a["action"] == "deploy" and a["ok"] is False for a in audit)


def test_mcp_keys(demo):
    r = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "k1"})
    assert r.status_code == 201 and r.json()["key"].startswith("rmk_")
    kid = r.json()["id"]
    lst = demo.get("/api/v1/groups/demo/mcp-keys").json()
    assert lst[0]["name"] == "k1" and "key" not in lst[0] and "value" not in lst[0]
    assert demo.delete(f"/api/v1/groups/demo/mcp-keys/{kid}").status_code == 200
    assert demo.delete(f"/api/v1/groups/demo/mcp-keys/{kid}").status_code == 404


def test_sa_rules(demo):
    assert demo.get("/api/v1/config/sa-rules").json() == {"rules": []}
    r = demo.put("/api/v1/config/sa-rules", json={"rules": [{"effect": "deny", "permission": "iam.*"}]})
    assert r.status_code == 200
    assert demo.put("/api/v1/config/sa-rules", json={"rules": [{"bad": 1}]}).status_code == 422
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        assert ga.put("/api/v1/config/sa-rules", json={"rules": []}).status_code == 403
        ok = ga.put("/api/v1/groups/demo/sa-restrictions", json={"rules": [{"effect": "allow", "permission": "storage.get"}]})
        assert ok.status_code == 200
        clash = ga.put("/api/v1/groups/demo/sa-restrictions", json={"rules": [{"effect": "allow", "permission": "iam.roles.create"}]})
        assert clash.status_code == 409 and "denies" in clash.text
    assert demo.get("/api/v1/groups/demo").json()["sa_restrictions"][0]["permission"] == "storage.get"


def test_users(demo):
    u = make_user(demo, "ga@x", "group_admin", ["demo"])
    assert "password_hash" not in u
    assert demo.post("/api/v1/users", json={"email": "ga@x", "password": "x"}).status_code == 409
    assert demo.post("/api/v1/users", json={"email": "z@x", "password": "x", "role": "king"}).status_code == 422
    assert demo.post("/api/v1/users", json={"email": "z@x", "password": "x", "role": "viewer", "groups": ["nogroup"]}).status_code == 422
    users = demo.get("/api/v1/users").json()
    assert len(users) == 2 and all("password_hash" not in x for x in users)
    r = demo.put(f"/api/v1/users/{u['id']}", json={"role": "super_admin"})
    assert r.status_code == 200 and r.json()["role"] == "super_admin"
    demo.put(f"/api/v1/users/{u['id']}", json={"role": "group_admin", "groups": ["demo"]})
    assert demo.put("/api/v1/users/nope", json={"role": "viewer"}).status_code == 404
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        r = ga.post("/api/v1/users", json={"email": "v@x", "password": "pw", "role": "viewer", "groups": ["demo"]})
        assert r.status_code == 201
        vid = r.json()["id"]
        assert ga.post("/api/v1/users", json={"email": "v2@x", "password": "pw", "role": "group_admin", "groups": ["demo"]}).status_code == 403
        assert ga.post("/api/v1/users", json={"email": "v3@x", "password": "pw", "role": "viewer", "groups": ["other"]}).status_code == 403
        assert [x["email"] for x in ga.get("/api/v1/users").json()] == ["ga@x", "v@x"]
        assert ga.put(f"/api/v1/users/{vid}", json={"role": "viewer"}).status_code == 403
        assert ga.delete(f"/api/v1/users/{u['id']}").status_code == 403
        assert ga.post("/api/v1/users/me/password", json={"password": "newpw"}).status_code == 200
        assert ga.delete(f"/api/v1/users/{vid}").status_code == 200
        assert ga.get("/users").status_code == 200
    login(TestClient(demo.app), "ga@x", "newpw")
    assert demo.delete(f"/api/v1/users/{u['id']}").status_code == 200
    assert demo.delete(f"/api/v1/users/{u['id']}").status_code == 404
    me = demo.get("/api/v1/me").json()
    assert demo.delete(f"/api/v1/users/{me['id']}").status_code == 409


def test_permission_requests(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", "pw")
        r = v.post("/api/v1/requests", json={"role": "group_admin", "group": "demo"})
        assert r.status_code == 201
        rid = r.json()["id"]
        assert v.get("/api/v1/requests").status_code == 403
    assert demo.get("/api/v1/requests").json()[0]["status"] == "pending"
    assert demo.post(f"/api/v1/requests/{rid}/approve").status_code == 200
    assert demo.post(f"/api/v1/requests/{rid}/approve").status_code == 409
    assert demo.post("/api/v1/requests/nope/approve").status_code == 404
    users = {u["email"]: u for u in demo.get("/api/v1/users").json()}
    assert users["v@x"]["role"] == "group_admin" and users["v@x"]["groups"] == ["demo"]


def test_api_keys(demo):
    r = demo.post("/api/v1/api-keys", json={"name": "ci"})
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("rmn_")
    kid = r.json()["id"]
    lst = demo.get("/api/v1/api-keys").json()
    assert lst[0]["name"] == "ci" and "key" not in lst[0] and "secret_hash" not in lst[0]
    with TestClient(demo.app) as anon:
        h = {"X-Ramen-Api-Key": key}
        assert anon.get("/api/v1/me", headers=h).json()["kind"] == "apikey"
        assert anon.get("/api/v1/groups", headers=h).status_code == 200
        assert anon.get("/api/v1/me", headers={"X-Ramen-Api-Key": "rmn_bad_key"}).status_code == 401
        assert anon.get("/api/v1/me", headers={"X-Ramen-Api-Key": f"rmn_{kid}_wrongsecret"}).status_code == 401
        assert anon.get("/api/v1/me", headers={"X-Ramen-Api-Key": "garbage"}).status_code == 401
    r = demo.post("/api/v1/api-keys", json={"name": "scoped", "role": "viewer", "groups": ["demo"]})
    assert r.json()["role"] == "viewer"
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        assert ga.post("/api/v1/api-keys", json={"name": "esc", "role": "super_admin"}).status_code == 403
        assert ga.post("/api/v1/api-keys", json={"name": "esc", "groups": ["other"]}).status_code == 403
        own = ga.post("/api/v1/api-keys", json={"name": "mine"}).json()
        assert own["role"] == "group_admin" and own["groups"] == ["demo"]
        assert [k["name"] for k in ga.get("/api/v1/api-keys").json()] == ["mine"]
        assert ga.delete(f"/api/v1/api-keys/{kid}").status_code == 403
    assert demo.delete(f"/api/v1/api-keys/{kid}").status_code == 200
    assert demo.delete(f"/api/v1/api-keys/{kid}").status_code == 404
    with TestClient(demo.app) as anon:
        assert anon.get("/api/v1/me", headers={"X-Ramen-Api-Key": key}).status_code == 401


def test_logs(demo, tmp_path):
    p = tmp_path / "logs" / "demo" / "zone-a" / "worker.log"
    p.parent.mkdir(parents=True)
    p.write_text("a\nb\nc\n")
    r = demo.get("/api/v1/logs?group=demo&zone=zone-a&tail=2")
    assert r.status_code == 200 and r.text == "b\nc\n"
    assert demo.get("/api/v1/logs?group=demo&zone=zone-a&download=1").headers["content-disposition"].startswith("attachment")
    assert "b" in demo.get("/logs?group=demo&zone=zone-a").text
    assert demo.get("/api/v1/logs?group=nope&zone=zone-a").status_code == 404


def test_audit(demo):
    audit = demo.get("/api/v1/audit").json()
    actions = {a["action"] for a in audit}
    assert {"login", "group.create", "zone.create", "environment.create"} <= actions
    a = next(x for x in audit if x["action"] == "group.create")
    assert a["ok"] is True and a["target"] == "demo" and a["ip"] and a["user"] == "root@ramen.local" and "group:demo" in a["tags"]
    assert demo.get("/api/v1/audit?format=csv").headers["content-type"].startswith("text/csv")
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "o@x", "group_admin", ["other"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        ga.put("/api/v1/groups/demo", json={"ref": "x"})
        mine = ga.get("/api/v1/audit").json()
        assert mine and all("group:demo" in x["tags"] for x in mine)
    with TestClient(demo.app) as o:
        login(o, "o@x", "pw")
        theirs = o.get("/api/v1/audit").json()
        assert theirs and all("group:other" in x["tags"] and "group:demo" not in x["tags"] for x in theirs)


def test_backups(demo, tmp_path):
    r = demo.post("/api/v1/backups", json={"target": "local"})
    assert r.status_code == 201 and r.json()["release_version"] == "0.2.0"
    bid = r.json()["id"]
    data = demo.get(f"/api/v1/backups/{bid}/download").json()
    assert data["release_version"] == "0.2.0" and data["groups"][0]["name"] == "demo" and "secrets" not in data
    assert all("password_hash" not in u for u in data["users"])
    r2 = demo.post("/api/v1/backups", json={"target": "bucket"})
    assert r2.status_code == 201 and "_backups" in r2.json()["path"]
    assert demo.post("/api/v1/backups", json={"target": "mars"}).status_code == 422
    demo.delete("/api/v1/groups/demo")
    assert [g["name"] for g in demo.get("/api/v1/groups").json()] == ["other"]
    assert demo.post(f"/api/v1/backups/{bid}/restore").status_code == 200
    assert demo.get("/api/v1/groups").json()[0]["name"] == "demo"
    assert demo.post("/api/v1/backups/nope/restore").status_code == 404
    assert demo.get("/api/v1/backups/nope/download").status_code == 404
    assert demo.get("/api/v1/backups?format=csv").status_code == 200


def test_config_and_refresh(demo, tmp_path, monkeypatch):
    cfg = tmp_path / "ramen.yaml"
    cfg.write_text("demo_flag: yes-please\n")
    monkeypatch.setenv("RAMEN_CONFIG", str(cfg))
    r = demo.post("/api/v1/config/reload")
    assert r.status_code == 200 and "RAMEN_DEMO_FLAG" in r.json()["applied"]
    c = demo.get("/api/v1/config").json()
    assert c["env"]["RAMEN_DEMO_FLAG"] == "yes-please" and c["env"]["RAMEN_ADMIN_PASSWORD"] == "***"
    r = demo.post("/api/v1/refresh")
    assert r.status_code == 200 and "groups" in r.json()
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", "pw")
        assert ga.post("/api/v1/refresh").status_code == 403
        assert ga.get("/config").status_code == 403


def test_cloud_not_implemented_surfaces(demo, monkeypatch):
    from ramen_console.cloud.aws import AwsCloud
    demo.app.state.services.cloud = AwsCloud()
    r = demo.post("/api/v1/groups/demo/zones/zone-a/rebalance")
    assert r.status_code == 501 and "aws" in r.text


def test_htmx_mutation_headers(demo):
    r = demo.post("/api/v1/zones", json={"name": "zone-c"}, headers={"HX-Request": "true"})
    assert r.status_code == 201 and r.headers.get("HX-Refresh") == "true"
    r = demo.post("/api/v1/zones", json={"name": "zone-c"}, headers={"HX-Request": "true"})
    assert r.status_code == 409 and "exists" in r.text


def test_oauth_routes(demo, monkeypatch):
    assert demo.get("/auth/oauth/oidc/login").status_code == 404
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_SECRET", "sec")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_METADATA_URL", "https://issuer/.well-known/openid-configuration")
    from ramen_console.auth.oauth import OAuthRegistry
    demo.app.state.oauth = OAuthRegistry.from_env()

    class FakeClient:
        async def authorize_redirect(self, request, redirect_uri):
            from starlette.responses import RedirectResponse
            return RedirectResponse("https://issuer/authorize?redirect_uri=" + redirect_uri)

        async def authorize_access_token(self, request):
            return {"userinfo": {"email": "sso@x"}}

    monkeypatch.setattr(demo.app.state.oauth, "client", lambda name: FakeClient() if name == "oidc" else None)
    with TestClient(demo.app) as anon:
        assert "oidc" in anon.get("/login").text
        r = anon.get("/auth/oauth/oidc/login", follow_redirects=False)
        assert r.status_code == 307 and "callback" in r.headers["location"]
        r = anon.get("/auth/oauth/oidc/callback", follow_redirects=False)
        assert r.status_code == 303
        me = anon.get("/api/v1/me").json()
        assert me["name"] == "sso@x" and me["role"] == "viewer"
        r = anon.get("/auth/oauth/oidc/callback", follow_redirects=False)
        assert r.status_code == 303
        assert anon.get("/auth/oauth/nope/callback").status_code == 404


def test_probes(client, monkeypatch):
    assert client.get("/healthz").json()["ok"] is True
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["ok"] is True

    async def broken(*a, **k):
        raise ConnectionError("store down")
    monkeypatch.setattr(client.app.state.store, "list", broken)
    assert client.get("/readyz").status_code == 503


def test_readyz_without_admin(monkeypatch, cloud):
    monkeypatch.delenv("RAMEN_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("RAMEN_STORE", "memory")
    with TestClient(create_app(store=make_store(), cloud=cloud)) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and "super admin" in r.text


def test_deploy_worker_failure_and_no_zones(demo, tmp_path, monkeypatch):
    async def fake_sync(group, repo_url, ref, token):
        return str(tmp_path / "buckets" / group)

    async def bad_deploy(group, env, zone, canary=True, config=None, **kw):
        return {"ok": False, "workers": [{"id": "http://w1:8080", "ok": False, "error": "ConnectError: boom"}]}
    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", fake_sync)
    monkeypatch.setattr(demo.app.state.cloud, "deploy", bad_deploy)
    job = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={}).json()
    job = demo.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "error" and "zone-a http://w1:8080: ConnectError: boom" in job["error"]
    demo.put("/api/v1/groups/demo/environments/prod", json={"zones": []})
    job = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={}).json()
    assert demo.get(f"/api/v1/jobs/{job['id']}").json()["error"] == "no zones configured"
