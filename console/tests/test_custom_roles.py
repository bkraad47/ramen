"""CONTRACTS §21.4 (0.7.5, D48): custom roles — a named role with a built-in base, usable as a membership, a role-map
rule and a tool-access kind; the token carries the name, the worker gets `RAMEN_ROLES`; `oauth_only` roles refuse
password and magic-link sign-in."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console import rbac, roles, toolaccess
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures
from tests.test_tool_access import _claims

ROLES_URL = "/api/v1/config/roles"
TA_URL = "/api/v1/groups/demo/environments/prod/tool-access"


@pytest.fixture(autouse=True)
def _clean_registry():
    rbac.set_custom_roles({})
    yield
    rbac.set_custom_roles({})


# --- rbac resolves a custom name to its base -----------------------------------------------------------------------


def test_custom_roles_rank_and_label_as_their_base():
    rbac.set_custom_roles({"analyst": {"base": "viewer", "label": "Analyst", "oauth_only": False}})
    assert rbac.base_of("analyst") == "viewer" and rbac.base_of("viewer") == "viewer" and rbac.base_of("nope") is None
    assert rbac.rank("analyst") == rbac.RANK["viewer"] and rbac.is_group_role("analyst") and rbac.is_role("analyst")
    assert not rbac.is_group_role("super_admin") and rbac.is_role("super_admin") and not rbac.is_role("nope")
    assert rbac.role_label("analyst") == "Analyst" and rbac.highest(["mcp_user", "analyst"]) == "analyst"
    assert rbac.group_roles() == ("group_admin", "viewer", "mcp_user", "analyst")
    p = rbac.principal_from({"id": "u", "email": "a@x", "manual_memberships": {"demo": "analyst"}})
    assert p.role == "analyst" and p.role_in("demo") == "analyst"
    assert rbac.can(p, "viewer", "demo") and not rbac.can(p, "group_admin", "demo")
    assert rbac.can(p, "analyst", "demo")  # a role name is a rank too
    rbac.set_custom_roles({})
    assert rbac.memberships_of({"manual_memberships": {"demo": "analyst"}}) == {}  # unknown → dropped (fails closed)


def test_clean_validates_name_base_and_flags():
    assert roles.clean("analyst", {"base": "viewer"}) == {"base": "viewer", "label": "analyst", "oauth_only": False}
    assert roles.clean("ops_1", {"base": "group_admin", "label": "Ops", "oauth_only": True})["oauth_only"] is True
    for bad in ("Viewer", "1x", "a", "x" * 33, "a-b", "super_admin", "viewer", "key"):
        with pytest.raises(ValueError):
            roles.clean(bad, {"base": "viewer"})
    with pytest.raises(ValueError, match="base"):
        roles.clean("analyst", {"base": "super_admin"})
    with pytest.raises(ValueError, match="base"):
        roles.clean("analyst", {})
    assert roles.compact({}) == "{}" and roles.compact({"b": {"base": "viewer"}, "a": {"base": "mcp_user"}}) == (
        '{"a":"mcp_user","b":"viewer"}'
    )


# --- API ------------------------------------------------------------------------------------------------------------


def test_super_admin_creates_lists_and_removes_custom_roles(demo):
    assert demo.get(ROLES_URL).json() == {"roles": {}, "builtin": list(rbac.ROLES)}
    r = demo.put(f"{ROLES_URL}/analyst", json={"base": "viewer", "label": "Analyst"})
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == {"analyst": {"base": "viewer", "label": "Analyst", "oauth_only": False}}
    assert demo.put(f"{ROLES_URL}/viewer", json={"base": "viewer"}).status_code == 422  # built-in
    assert demo.put(f"{ROLES_URL}/Bad-Name", json={"base": "viewer"}).status_code == 422
    assert demo.put(f"{ROLES_URL}/x", json={"base": "super_admin"}).status_code == 422
    assert demo.put(f"{ROLES_URL}/key", json={"base": "viewer"}).status_code == 422  # a tool-access kind already
    assert (
        demo.put(f"{ROLES_URL}/analyst", json={"base": "mcp_user", "oauth_only": True}).status_code == 200
    )  # replaces
    assert demo.get(ROLES_URL).json()["roles"]["analyst"] == {
        "base": "mcp_user",
        "label": "analyst",
        "oauth_only": True,
    }
    assert demo.delete(f"{ROLES_URL}/analyst").status_code == 200
    assert demo.get(ROLES_URL).json()["roles"] == {}
    assert demo.delete(f"{ROLES_URL}/analyst").status_code == 404
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "config.roles"]
    assert any("role:analyst" in a["tags"] and "base:viewer" in a["tags"] for a in rows)
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as c:
        login(c, "ga@x", PW)
        assert c.get(ROLES_URL).status_code == 403
        assert c.put(f"{ROLES_URL}/x", json={"base": "viewer"}).status_code == 403


def test_a_role_in_use_cannot_be_deleted(demo):
    demo.put(f"{ROLES_URL}/analyst", json={"base": "viewer"})
    u = demo.post("/api/v1/users", json={"email": "an@x", "password": PW, "memberships": {"demo": "analyst"}})
    assert u.status_code == 201, u.text
    r = demo.delete(f"{ROLES_URL}/analyst")
    assert r.status_code == 409 and "an@x" in r.json()["detail"]
    uid = u.json()["id"]
    assert demo.put(f"/api/v1/users/{uid}", json={"memberships": {"demo": "viewer"}}).status_code == 200
    assert demo.put(TA_URL, json={"calc": {"list": ["key", "analyst"], "call": ["analyst"]}}).status_code == 200
    r = demo.delete(f"{ROLES_URL}/analyst")
    assert r.status_code == 409 and "tool access" in r.json()["detail"]
    demo.put(TA_URL, json={})
    assert demo.delete(f"{ROLES_URL}/analyst").status_code == 200


def test_custom_role_is_a_membership_a_tool_access_kind_and_a_token_claim(demo):
    demo.put(f"{ROLES_URL}/analyst", json={"base": "mcp_user", "label": "Analyst"})
    assert toolaccess.kinds() == ("key", "group_admin", "viewer", "mcp_user", "analyst")
    assert toolaccess.clean({"calc": {"list": ["analyst", "key"], "call": ["analyst"]}}) == {
        "calc": {"list": ["key", "analyst"], "call": ["analyst"]}
    }
    u = demo.post("/api/v1/users", json={"email": "an@x", "password": PW, "memberships": {"demo": "analyst"}}).json()
    assert u["memberships"] == {"demo": "analyst"} and u["role"] == "analyst"
    # the members API takes it too, and a group admin may grant it (it ranks as mcp_user)
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    vid = next(x["id"] for x in demo.get("/api/v1/users").json() if x["email"] == "v@x")
    with TestClient(demo.app) as c:
        login(c, "ga@x", PW)
        assert c.put(f"/api/v1/groups/demo/members/{vid}", json={"role": "analyst"}).status_code == 200
        assert c.put(f"/api/v1/groups/demo/members/{vid}", json={"role": "nope"}).status_code == 422
    members = {m["email"]: m["role"] for m in demo.get("/api/v1/groups/demo/members").json()}
    assert members["an@x"] == "analyst" and members["v@x"] == "analyst"
    with TestClient(demo.app) as c:
        login(c, "an@x", PW)
        assert c.get("/groups/demo", follow_redirects=False).status_code == 403  # base mcp_user: no console
        assert _claims(demo, c)["role"] == "analyst"
    # the role-map accepts it as a rule
    from ramen_console.auth.settings import parse_role

    assert parse_role("analyst:demo") == ("analyst", ["demo"])
    with pytest.raises(ValueError):
        parse_role("nope:demo")


def test_deploy_hands_every_zone_ramen_roles(demo):
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert "RAMEN_ROLES={}\n" in Path(job["result"]["zone-a"]["env_file"]).read_text()
    demo.put(f"{ROLES_URL}/analyst", json={"base": "viewer"})
    demo.put(f"{ROLES_URL}/bot", json={"base": "mcp_user"})
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok", job
    for z in ("zone-a", "zone-b"):
        assert 'RAMEN_ROLES={"analyst":"viewer","bot":"mcp_user"}\n' in Path(job["result"][z]["env_file"]).read_text()


# --- oauth_only -----------------------------------------------------------------------------------------------------


def test_oauth_only_role_refuses_password_and_magic_link_sign_in(demo, monkeypatch):
    demo.put(f"{ROLES_URL}/sso_user", json={"base": "mcp_user", "oauth_only": True})
    demo.put(f"{ROLES_URL}/plain", json={"base": "viewer"})
    assert demo.put(f"{ROLES_URL}/viewer", json={"base": "viewer", "oauth_only": True}).status_code == 422
    demo.post("/api/v1/users", json={"email": "sso@x", "password": PW, "memberships": {"other": "sso_user"}})
    demo.post("/api/v1/users", json={"email": "ok@x", "password": PW, "memberships": {"demo": "plain"}})
    with TestClient(demo.app) as c:
        r = c.post("/login", data={"email": "sso@x", "password": PW}, follow_redirects=False)
        assert r.status_code == 403 and "signs in with" in r.text
        assert c.get("/api/v1/me").status_code == 401
        r = c.post("/login", data={"email": "sso@x", "password": "wrong"}, follow_redirects=False)
        assert r.status_code == 401  # a wrong password is still a wrong password: no account policy is revealed
        assert c.post("/login", data={"email": "ok@x", "password": PW}, follow_redirects=False).status_code == 303
    # magic link: the token is minted but redeeming it is refused
    demo.put("/api/v1/config/auth", json={"magic_link": True})
    st = demo.app.state
    user = asyncio.run(st.store.list("users", {"email": "sso@x"}))[0]
    _, nonce = asyncio.run(st.accounts.start_token("sso@x", "magic"))
    token = st.tokens.issue("magic", user["id"], nonce)
    with TestClient(demo.app) as c:
        r = c.get(f"/auth/magic/{token}", follow_redirects=False)
        assert r.status_code == 403 and "signs in with" in r.text
    # the break-glass bootstrap admin is exempt even if someone hands it such a role
    root_id = next(x["id"] for x in demo.get("/api/v1/users").json() if x["email"] == "root@ramen.local")
    assert root_id
    with TestClient(demo.app) as c:
        assert (
            c.post(
                "/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False
            ).status_code
            == 303
        )
    audit = demo.get("/api/v1/audit").json()
    assert any(a["action"] == "login" and "oauth_only" in a["tags"] for a in audit)


# --- pages ----------------------------------------------------------------------------------------------------------


def test_config_users_and_group_pages_offer_custom_roles(demo):
    demo.put(f"{ROLES_URL}/analyst", json={"base": "viewer", "label": "Analyst"})
    config = demo.get("/config").text
    assert "<h2" in config and "Roles</h2>" in config and "<code>analyst</code>" in config
    assert 'hx-delete="/api/v1/config/roles/analyst"' in config and 'hx-post="/api/v1/config/roles"' in config
    users = demo.get("/users").text
    assert "Analyst" in users
    wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    group = demo.get("/groups/demo").text
    assert "Analyst" in group  # a tool-access kind
