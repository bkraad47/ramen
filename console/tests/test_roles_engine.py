"""instructions/v0.5.95.md — the roles engine (D41): roles are per group, one engine gates pages, API and tokens."""

import asyncio

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_server import authorize, pkce, register

SUPER_ONLY_PAGES = ("/config", "/api-keys", "/backups", "/audit")
SUPER_ONLY_API = ("/api/v1/audit", "/api/v1/api-keys", "/api/v1/backups", "/api/v1/config/auth")


def uid_of(root, email: str) -> str:
    return next(u["id"] for u in root.get("/api/v1/users").json() if u["email"] == email)


def test_memberships_are_per_group(demo):
    u = demo.post(
        "/api/v1/users",
        json={"email": "mix@x", "password": PW, "memberships": {"demo": "mcp_user", "other": "group_admin"}},
    ).json()
    assert u["memberships"] == {"demo": "mcp_user", "other": "group_admin"}
    assert u["role"] == "group_admin" and sorted(u["groups"]) == ["demo", "other"]  # summaries, for the old readers
    cid = register(demo)
    with TestClient(demo.app) as c:
        login(c, "mix@x", PW)
        assert c.get("/api/v1/me").json()["memberships"] == {"demo": "mcp_user", "other": "group_admin"}
        assert c.get("/groups/other").status_code == 200
        assert c.get("/groups/demo", follow_redirects=False).status_code == 403  # only an MCP user there
        assert c.get("/api/v1/groups/demo").status_code == 403
        assert c.post("/api/v1/groups/other/secrets", json={"name": "X", "value": "1"}).status_code == 201
        assert c.post("/api/v1/groups/demo/secrets", json={"name": "X", "value": "1"}).status_code == 403
        assert authorize(c, cid, pkce()[1]).status_code == 200  # mcp:demo:zone-a — an MCP user may connect
        page = c.get("/groups").text
        assert "demo" not in page.split("<main>")[1] or 'href="/groups/demo"' not in page  # not even listed
    # a legacy document (role + groups) reads as the same role in every listed group
    asyncio.run(
        demo.app.state.store.put(
            "users",
            "legacy",
            {
                "email": "old@x",
                "role": "viewer",
                "groups": ["demo"],
                "provider": "password",
                "password_hash": "",
                "created": "x",
            },
        )
    )
    old = next(u for u in demo.get("/api/v1/users").json() if u["email"] == "old@x")
    assert old["memberships"] == {"demo": "viewer"}
    # super admin: role + groups still accepted, and `memberships` edits per group
    u = make_user(demo, "v@x", "viewer", ["demo", "other"])
    assert u["memberships"] == {"demo": "viewer", "other": "viewer"}
    r = demo.put(f"/api/v1/users/{u['id']}", json={"memberships": {"demo": "group_admin"}})
    assert (
        r.status_code == 200 and r.json()["memberships"] == {"demo": "group_admin"} and r.json()["groups"] == ["demo"]
    )
    assert demo.put(f"/api/v1/users/{u['id']}", json={"memberships": {"demo": "king"}}).status_code == 422
    assert demo.put(f"/api/v1/users/{u['id']}", json={"memberships": {"nope": "viewer"}}).status_code == 422


def test_config_api_keys_backups_and_audit_are_super_admin_only(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        for path in SUPER_ONLY_PAGES:
            assert ga.get(path, follow_redirects=False).status_code == 403, path
        for path in SUPER_ONLY_API:
            assert ga.get(path).status_code == 403, path
        assert ga.post("/api/v1/api-keys", json={"name": "k"}).status_code == 403
        assert ga.delete("/api/v1/groups/demo").status_code == 403
        assert 'hx-delete="/api/v1/groups/demo"' not in ga.get("/groups").text
        nav = ga.get("/").text.split("<main>")[0]
        for href in ("/api-keys", "/audit", "/backups", "/config"):
            assert f'href="{href}"' not in nav, href
    assert 'hx-delete="/api/v1/groups/demo"' in demo.get("/groups").text


def test_secrets_are_for_admins_only(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.get("/secrets", follow_redirects=False).status_code == 403
        assert v.get("/api/v1/groups/demo/secrets").status_code == 403
        assert 'href="/secrets"' not in v.get("/").text
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get("/secrets").status_code == 200 and ga.get("/api/v1/groups/demo/secrets").status_code == 200


def test_group_admins_approve_others_requests_in_their_group_only(demo):
    for email, groups in (("a1@x", ["demo"]), ("a2@x", ["demo"]), ("o@x", ["other"])):
        make_user(demo, email, "group_admin", groups)
    body = {"group": "demo", "zone": "zone-a", "permission": "bucket.read"}
    with TestClient(demo.app) as a1:
        login(a1, "a1@x", PW)
        rid = a1.post("/api/v1/requests", json=body).json()["id"]
        r = a1.post(f"/api/v1/requests/{rid}/approve")
        assert r.status_code == 409 and "another" in r.json()["detail"].lower()
        assert a1.post(f"/api/v1/requests/{rid}/deny").status_code == 409
    with TestClient(demo.app) as o:
        login(o, "o@x", PW)
        assert o.post(f"/api/v1/requests/{rid}/approve").status_code == 403
        assert o.get("/api/v1/requests").status_code == 200 and o.get("/api/v1/requests").json() == []
    with TestClient(demo.app) as a2:
        login(a2, "a2@x", PW)
        page = a2.get("/groups/demo").text
        assert f'hx-post="/api/v1/requests/{rid}/approve"' in page
        assert a2.post(f"/api/v1/requests/{rid}/approve").json()["status"] == "approved"
        assert [q["id"] for q in a2.get("/api/v1/requests").json()] == [rid]
        assert a2.post(f"/api/v1/requests/{rid}/revoke").status_code == 200
    with TestClient(demo.app) as a1:  # the requester never sees approve/deny buttons for their own request
        login(a1, "a1@x", PW)
        rid2 = a1.post("/api/v1/requests", json={**body, "permission": "secrets.read"}).json()["id"]
        assert f'hx-post="/api/v1/requests/{rid2}/approve"' not in a1.get("/groups/demo").text


def test_group_admins_reset_passwords_of_their_groups_members_only(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    v, o = make_user(demo, "v@x", "viewer", ["demo"]), make_user(demo, "o@x", "viewer", ["other"])
    root_id = uid_of(demo, "root@ramen.local")
    new = "Fresh-Passw0rd-1!"
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.post(f"/api/v1/users/{v['id']}/password", json={"password": new}).status_code == 200
        assert ga.post(f"/api/v1/users/{o['id']}/password", json={"password": new}).status_code == 403
        assert ga.post(f"/api/v1/users/{root_id}/password", json={"password": new}).status_code == 403
        assert "Reset password" in ga.get("/users").text
    with TestClient(demo.app) as v2:
        login(v2, "v@x", new)


def test_viewers_list_their_groups_users_read_only(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "m@x", "mcp_user", ["demo"])
    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        page = v.get("/users").text
        assert "m@x" in page and "o@x" not in page
        assert "Create user" not in page and 'hx-delete="/api/v1/users' not in page and "Reset password" not in page
        assert {u["email"] for u in v.get("/api/v1/users").json()} == {"v@x", "m@x"}
        assert v.post("/api/v1/users", json={"email": "n@x", "password": PW, "role": "viewer"}).status_code == 403
        assert v.get("/logs?group=demo&zone=zone-a").status_code == 200
        assert v.get("/api/v1/groups/demo/mcp-keys").status_code == 200


def test_group_admins_manage_memberships_of_their_groups(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    m = make_user(demo, "m@x", "mcp_user", ["other"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        r = ga.put(f"/api/v1/groups/demo/members/{m['id']}", json={"role": "mcp_user"})
        assert r.status_code == 200 and r.json()["memberships"] == {"other": "mcp_user", "demo": "mcp_user"}
        assert ga.put(f"/api/v1/groups/other/members/{m['id']}", json={"role": "viewer"}).status_code == 403
        assert ga.put(f"/api/v1/groups/demo/members/{m['id']}", json={"role": "group_admin"}).status_code == 403
        assert ga.put(f"/api/v1/groups/demo/members/{m['id']}", json={"role": "king"}).status_code == 422
        members = ga.get("/api/v1/groups/demo/members").json()
        assert {(x["email"], x["role"]) for x in members} == {("ga@x", "group_admin"), ("m@x", "mcp_user")}
        page = ga.get("/users").text
        assert f'hx-put="/api/v1/groups/demo/members/{m["id"]}"' in page  # the per-group table with a role picker
        assert f'hx-delete="/api/v1/groups/demo/members/{m["id"]}"' in page
        assert 'hx-put="/api/v1/groups/other/members/' not in page
        ga_id = uid_of(demo, "ga@x")
        assert f'members/{ga_id}"' not in page  # a group admin cannot re-role or remove a fellow group admin
        assert ga.delete(f"/api/v1/groups/demo/members/{m['id']}").json()["memberships"] == {"other": "mcp_user"}
        assert ga.delete(f"/api/v1/groups/other/members/{m['id']}").status_code == 403
    # the super admin may hand out group_admin and sees every group's table
    r = demo.put(f"/api/v1/groups/demo/members/{m['id']}", json={"role": "group_admin"})
    assert r.status_code == 200 and r.json()["memberships"]["demo"] == "group_admin"
    page = demo.get("/users").text
    assert 'hx-put="/api/v1/groups/other/members/' in page and 'hx-put="/api/v1/groups/demo/members/' in page


def test_service_account_cards_are_for_admins(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        page = v.get("/groups/demo").text
        assert "Service-account permissions" not in page and "Service-account restrictions" not in page
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        page = ga.get("/groups/demo").text
        assert "Service-account permissions" in page and "Service-account restrictions" in page
        assert "How MCP users sign in" in page and "<details" in page  # collapsed instructions, HTTP and bridge


def test_idp_rules_build_memberships_per_group(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_SECRET", "sec")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_METADATA_URL", "https://issuer/.well-known/openid-configuration")
    from ramen_console.auth.oauth import OAuthRegistry

    demo.app.state.oauth = OAuthRegistry.from_env()
    claims = {"email": "sso@x", "email_verified": True, "roles": ["AD-Admins", "AD-MCP"]}

    class FakeClient:
        async def authorize_access_token(self, request):
            return {"userinfo": dict(claims)}

    monkeypatch.setattr(demo.app.state.oauth, "client", lambda name: FakeClient() if name == "oidc" else None)
    rm = "/api/v1/config/auth/role-map/oidc"
    assert demo.put(rm, json={"claim": "roles"}).status_code == 200
    assert demo.put(rm, json={"value": "AD-Admins", "role": "group_admin", "groups": "demo"}).status_code == 200
    assert demo.put(rm, json={"value": "AD-MCP", "role": "mcp_user", "groups": "demo, other"}).status_code == 200
    with TestClient(demo.app) as anon:
        anon.get("/auth/oauth/oidc/callback", follow_redirects=False)
        me = anon.get("/api/v1/me").json()
        assert me["memberships"] == {"demo": "group_admin", "other": "mcp_user"}  # highest role per group
    claims["roles"] = ["AD-MCP"]
    with TestClient(demo.app) as anon:
        anon.get("/auth/oauth/oidc/callback", follow_redirects=False)
        assert anon.get("/api/v1/me").json()["memberships"] == {"demo": "mcp_user", "other": "mcp_user"}


def test_an_unapproved_mcp_user_has_nothing(demo):
    make_user(demo, "none@x", "mcp_user", [])
    cid = register(demo)
    with TestClient(demo.app) as u:
        login(u, "none@x", PW)
        assert u.get("/").status_code == 200 and "no group yet" in u.get("/").text
        for path in ("/groups", "/groups/demo", "/users", "/secrets", "/logs"):
            assert u.get(path, follow_redirects=False).status_code == 403, path
        for path in ("/api/v1/groups", "/api/v1/users", "/api/v1/groups/demo/mcp-keys"):
            assert u.get(path).status_code == 403, path
        assert authorize(u, cid, pkce()[1]).status_code == 403


def test_role_matrix(demo):
    """One table, every role: what each may reach. super > group_admin > viewer > mcp_user, each scoped to its group."""
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "m@x", "mcp_user", ["demo"])
    probes = [  # (method, path, super, group_admin, viewer, mcp_user)
        ("GET", "/", 200, 200, 200, 200),
        ("GET", "/groups/demo", 200, 200, 200, 403),
        ("GET", "/groups/other", 200, 403, 403, 403),
        ("GET", "/api/v1/groups/demo", 200, 200, 200, 403),
        ("GET", "/api/v1/groups/demo/secrets", 200, 200, 403, 403),
        ("GET", "/users", 200, 200, 200, 403),
        ("GET", "/api/v1/users", 200, 200, 200, 403),
        ("GET", "/logs?group=demo&zone=zone-a", 200, 200, 200, 403),
        ("GET", "/api-keys", 200, 403, 403, 403),
        ("GET", "/audit", 200, 403, 403, 403),
        ("GET", "/backups", 200, 403, 403, 403),
        ("GET", "/config", 200, 403, 403, 403),
        ("GET", "/api/v1/audit", 200, 403, 403, 403),
        ("DELETE", "/api/v1/groups/demo", None, 403, 403, 403),  # None: not probed for super (it would delete)
        ("GET", "/api/v1/me", 200, 200, 200, 200),
        # workers and zones: scaled / removed by group admins (their group) and super admins only
        ("PUT", "/api/v1/groups/demo/zones/zone-a/workers", 200, 200, 403, 403),
        ("PUT", "/api/v1/groups/other/zones/zone-a/workers", 200, 403, 403, 403),
        ("DELETE", "/api/v1/zones/zone-b", None, 403, 403, 403),
        ("GET", "/zones", 200, 200, 200, 403),
    ]
    clients = {"super_admin": demo}
    for role, email in (("group_admin", "ga@x"), ("viewer", "v@x"), ("mcp_user", "m@x")):
        clients[role] = login(TestClient(demo.app), email, PW)
    bad = []
    for method, path, *want in probes:
        for role, expect in zip(("super_admin", "group_admin", "viewer", "mcp_user"), want, strict=True):
            if expect is None:
                continue
            body = {"count": 1} if path.endswith("/workers") else None
            got = clients[role].request(method, path, json=body, follow_redirects=False).status_code
            if got != expect:
                bad.append((role, method, path, got, expect))
    assert bad == [], bad
