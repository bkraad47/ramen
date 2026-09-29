"""U7/U8/U9 + D21 / CONTRACTS §12.1: API keys carry an enforced client type.

`devops` keys (`rmn_`) drive `/api/v1/*` and never reach a worker; `agent` keys (`rmk_`) are written into the
zones' deploy config so workers accept them, and are refused by the console API.
"""

import time

import pytest
from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.auth import apikeys
from ramen_console.cloud.local import LocalCloud
from ramen_console.grpcclient import Client
from ramen_console.storage import make_store

PW = "Passw0rd!-for-tests"


@pytest.fixture
def app(monkeypatch, tmp_path, workers):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.local")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setenv("RAMEN_BUCKET_ROOT", str(tmp_path / "buckets"))
    cloud = LocalCloud(
        tmp_path / "buckets",
        tmp_path / "logs",
        {"demo/zone-a": [workers["cold"].target]},
        workers["cold"].target,
        "adm",
        Client(deadline=2),
    )
    return create_app(store=make_store(), cloud=cloud)


@pytest.fixture(autouse=True)
def fake_sync(app, monkeypatch):
    async def sync(group, repo_url, ref, token):
        return "/tmp/fake"

    monkeypatch.setattr(app.state.cloud, "sync_repo", sync)


def deploy(client, zone_file_of):
    jid = client.post("/api/v1/groups/demo/environments/dev/deploy", json={"canary": False}).json()["id"]
    for _ in range(100):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "ok", j
    return zone_file_of(j)


def env_file(job):
    from pathlib import Path

    return Path(job["result"]["zone-a"]["env_file"]).read_text()


@pytest.fixture
def root(app):
    with TestClient(app) as c:
        assert c.post("/login", data={"email": "root@ramen.local", "password": "rootpw"}).status_code == 200
        assert c.post("/api/v1/groups", json={"name": "demo"}).status_code == 201
        assert c.post("/api/v1/groups", json={"name": "other"}).status_code == 201
        assert c.post("/api/v1/zones", json={"name": "zone-a", "provider": "local"}).status_code == 201
        assert c.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-a"]}).status_code == 201
        yield c


def mint(client, **body):
    r = client.post("/api/v1/api-keys", json={"name": "k", "groups": ["demo"], **body})
    assert r.status_code == 201, r.text
    return r.json()


# --- shape -----------------------------------------------------------------
def test_devops_is_the_default_and_uses_the_rmn_prefix(root):
    doc = mint(root)
    assert doc["client_type"] == "devops"
    assert doc["key"].startswith("rmn_")


def test_an_agent_key_uses_the_rmk_prefix(root):
    doc = mint(root, client_type="agent")
    assert doc["client_type"] == "agent"
    assert doc["key"].startswith("rmk_")


def test_an_unknown_client_type_is_rejected(root):
    r = root.post("/api/v1/api-keys", json={"name": "k", "client_type": "robot"})
    assert r.status_code == 422
    assert "client type" in r.json()["detail"].lower()


def test_an_agent_key_must_name_at_least_one_group(root):
    r = root.post("/api/v1/api-keys", json={"name": "k", "client_type": "agent", "groups": []})
    assert r.status_code == 422
    assert "group" in r.json()["detail"].lower()


def test_generated_key_secrets_satisfy_the_strength_rule(root):
    from ramen_console.security import password_problem

    for ct in ("devops", "agent"):
        secret = mint(root, client_type=ct)["key"].split("_", 2)[2]
        assert password_problem(secret) is None
        assert "_" not in secret and "," not in secret


# --- enforcement -----------------------------------------------------------
def test_a_devops_key_drives_the_console_api(app, root):
    key = mint(root)["key"]
    with TestClient(app) as c:
        r = c.get("/api/v1/me", headers={apikeys.HEADER: key})
        assert r.status_code == 200
        assert r.json()["client_type"] == "devops"


def test_an_agent_key_is_refused_by_the_console_api(app, root):
    key = mint(root, client_type="agent")["key"]
    with TestClient(app) as c:
        r = c.get("/api/v1/me", headers={apikeys.HEADER: key})
        assert r.status_code == 403
        assert r.json()["detail"] == "Agent key cannot call the console API"


def test_an_agent_key_is_refused_on_html_pages_too(app, root):
    key = mint(root, client_type="agent")["key"]
    with TestClient(app) as c:
        assert c.get("/groups", headers={apikeys.HEADER: key}, follow_redirects=False).status_code == 403


def test_a_key_presented_with_the_wrong_prefix_is_not_accepted(app, root):
    key = mint(root)["key"]
    with TestClient(app) as c:
        swapped = "rmk_" + key.split("_", 1)[1]
        assert c.get("/api/v1/me", headers={apikeys.HEADER: swapped}).status_code == 401


# --- reaching (and not reaching) workers ------------------------------------
def test_an_agent_key_reaches_workers_but_a_devops_key_does_not(root):
    agent = mint(root, client_type="agent", name="llm")["key"]
    devops = mint(root, name="ci")["key"]
    text = deploy(root, env_file)
    keys = next(ln for ln in text.splitlines() if ln.startswith("RAMEN_MCP_KEYS="))
    assert agent in keys
    assert devops not in keys


def test_an_agent_key_only_reaches_the_groups_it_names(root):
    mint(root, client_type="agent", name="llm", groups=["other"])
    assert "RAMEN_MCP_KEYS=" not in deploy(root, env_file)


def test_revoking_an_agent_key_stops_it_reaching_workers(root):
    doc = mint(root, client_type="agent", name="llm")
    assert root.delete(f"/api/v1/api-keys/{doc['id']}").status_code == 200
    assert doc["key"] not in deploy(root, env_file)


# --- listing and migration --------------------------------------------------
def test_the_listing_shows_the_client_type(root):
    mint(root, name="ci")
    mint(root, name="llm", client_type="agent")
    rows = {k["name"]: k["client_type"] for k in root.get("/api/v1/api-keys").json()}
    assert rows == {"ci": "devops", "llm": "agent"}


def test_pre_0_4_0_keys_default_by_their_prefix():
    assert apikeys.client_type_of({}, "rmn") == "devops"
    assert apikeys.client_type_of({}, "rmk") == "agent"
    assert apikeys.client_type_of({}) == "devops"  # only rmn_ existed before 0.4.0
    assert apikeys.client_type_of({"client_type": "agent"}, "rmn") == "agent"


def test_a_pre_0_4_0_stored_key_still_authenticates(app, root):
    """A doc written before 0.4.0 carries no client_type and no prefix; it must keep working as devops."""
    import anyio

    raw, kid, h = apikeys.mint()
    doc = {"name": "legacy", "role": "viewer", "groups": ["demo"], "secret_hash": h, "owner": "x", "created": "t"}
    anyio.run(app.state.store.put, "api_keys", kid, doc)
    r = root.get("/api/v1/me", headers={apikeys.HEADER: raw})
    assert r.status_code == 200, r.text
    assert r.json()["client_type"] == "devops"


def test_the_group_mcp_key_form_is_the_agent_control(root):
    r = root.post("/api/v1/groups/demo/mcp-keys", json={"name": "llm"})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["client_type"] == "agent"
    assert doc["key"].startswith("rmk_")


def test_a_group_mcp_key_is_not_accepted_by_the_console_api(app, root):
    key = root.post("/api/v1/groups/demo/mcp-keys", json={"name": "llm"}).json()["key"]
    with TestClient(app) as c:
        assert c.get("/api/v1/me", headers={apikeys.HEADER: key}).status_code == 401


def test_the_page_offers_the_caller_groups_as_a_multi_select(root):
    html = root.get("/api-keys").text
    assert '<select name="groups" multiple' in html
    assert '<option value="demo">demo</option>' in html
    assert '<option value="other">other</option>' in html


def test_the_listing_shows_group_names_not_a_json_array(root):
    mint(root, name="ci", groups=["demo", "other"])
    html = root.get("/api-keys").text
    assert "<td>demo, other</td>" in html
    assert '["demo"' not in html


def test_the_listing_writes_the_role_out(root):
    mint(root, name="ci", role="group_admin")
    assert "<td>Group Admin</td>" in root.get("/api-keys").text


def test_an_empty_group_list_is_a_dash_not_an_empty_json_array(root):
    """U8: no page shows a JSON array where a person expects names."""
    import re

    mint(root, name="mine", groups=[])
    cells = re.findall(r"<td>(.*?)</td>", root.get("/api-keys").text, re.S)
    assert cells
    assert not any("[" in c for c in cells)
    assert any(c.strip() == "—" for c in cells)
