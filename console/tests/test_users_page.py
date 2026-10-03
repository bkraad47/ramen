"""Users page (0.5.92): SSO users have no password to reset; a user's groups are picked from checkboxes."""

import asyncio
import re

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

SSO = {"email": "sso@x", "role": "viewer", "groups": ["demo"], "provider": "oidc", "password_hash": ""}


def row_of(page: str, email: str) -> str:
    return re.search(rf"<tr>(?:(?!<tr>).)*?{re.escape(email)}.*?</tr>", page, re.S).group(0)


def test_sso_users_have_no_password_reset(demo):
    asyncio.run(demo.app.state.store.put("users", "sso1", {**SSO, "created": "2026-10-03T00:00:00+00:00"}))
    local = make_user(demo, "pw@x", "viewer", ["demo"])
    page = demo.get("/users").text
    sso_row, pw_row = row_of(page, "sso@x"), row_of(page, "pw@x")
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


def test_groups_are_edited_with_checkboxes(demo):
    u = make_user(demo, "pw@x", "viewer", ["demo"])
    row = row_of(demo.get("/users").text, "pw@x")
    boxes = re.findall(r'<input type="checkbox" name="groups" value="([a-z-]+)"( checked)?', row)
    assert dict(boxes) == {"demo": " checked", "other": ""}
    assert 'type="hidden" name="groups" value=""' in row  # so clearing every box still sends groups
    assert "Groups (comma)" not in row
    # what the form sends: the hidden empty field plus one value per checked box
    assert demo.put(f"/api/v1/users/{u['id']}", json={"groups": ["", "other"]}).json()["groups"] == ["other"]
    assert demo.put(f"/api/v1/users/{u['id']}", json={"groups": ""}).json()["groups"] == []
