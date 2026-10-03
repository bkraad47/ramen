"""Users page (0.5.93–0.5.95): SSO users have no password to reset; roles are set per group in the member tables."""

import asyncio
import re

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

SSO = {"email": "sso@x", "role": "viewer", "groups": ["demo"], "provider": "oidc", "password_hash": ""}


def users_table(page: str) -> str:
    return page.split('<h2 style="margin-top:0">Users</h2>', 1)[1]


def row_of(html: str, email: str) -> str:
    return re.search(rf"<tr>(?:(?!<tr>).)*?{re.escape(email)}.*?</tr>", html, re.S).group(0)


def test_sso_users_have_no_password_reset(demo):
    asyncio.run(demo.app.state.store.put("users", "sso1", {**SSO, "created": "2026-10-03T00:00:00+00:00"}))
    local = make_user(demo, "pw@x", "viewer", ["demo"])
    table = users_table(demo.get("/users").text)
    sso_row, pw_row = row_of(table, "sso@x"), row_of(table, "pw@x")
    assert "Reset password" in pw_row and "Reset password" not in sso_row
    assert "Signs in through oidc" in sso_row
    r = demo.post("/api/v1/users/sso1/password", json={"password": "Another-Passw0rd!"})
    assert r.status_code == 422 and "oidc" in r.json()["detail"]
    assert demo.post(f"/api/v1/users/{local['id']}/password", json={"password": "Another-Passw0rd!"}).status_code == 200
    with TestClient(demo.app) as sso:  # nor can they give themselves one
        sso.cookies.set("ramen_session", demo.app.state.signer.sign({"uid": "sso1", "ep": 0}))
        assert sso.get("/api/v1/me").json()["name"] == "sso@x"
        sso.cookies.set("ramen_csrf", "t0ken")  # double-submit: a cookie-authenticated mutation echoes the cookie
        h = {"X-Ramen-CSRF": "t0ken"}
        assert (
            sso.post("/api/v1/users/me/password", json={"password": "Another-Passw0rd!"}, headers=h).status_code == 422
        )


def test_roles_are_picked_per_group_in_the_member_tables(demo):
    u = make_user(demo, "pw@x", "viewer", ["demo"])
    page = demo.get("/users").text
    assert "Groups (comma)" not in page
    row = row_of(page.split("<h3", 1)[1], "pw@x")  # the first member table (group demo)
    assert f'hx-put="/api/v1/groups/demo/members/{u["id"]}"' in row
    assert '<input type="radio" name="role" value="viewer" checked>' in row
    assert '<input type="radio" name="role" value="group_admin">' in row  # a super admin may hand it out
    assert f'hx-delete="/api/v1/groups/demo/members/{u["id"]}"' in row
    assert demo.put(f"/api/v1/groups/other/members/{u['id']}", json={"role": "mcp_user"}).status_code == 200
    me = next(x for x in demo.get("/api/v1/users").json() if x["email"] == "pw@x")
    assert me["memberships"] == {"demo": "viewer", "other": "mcp_user"}
    r = demo.post("/api/v1/groups/other/members", json={"email": "pw@x", "role": "viewer"})  # the Add-member form
    assert r.status_code == 200 and r.json()["memberships"]["other"] == "viewer"
    assert demo.post("/api/v1/groups/other/members", json={"email": "nobody@x", "role": "viewer"}).status_code == 404
