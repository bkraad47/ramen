"""V1.4 (folded into U25) / CONTRACTS §12.1: a session carries the user's epoch and dies when it moves on.

The epoch is bumped on password change, role change, group change, delete, and any change to `config/auth`.
"""

import pytest
from fastapi.testclient import TestClient

from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - pytest fixtures

PW = "Passw0rd!-for-tests"
NEW = "N3wPassw0rd!-here"


@pytest.fixture
def member(root):
    r = root.post("/api/v1/users", json={"email": "m@x", "password": PW, "role": "viewer", "groups": []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def session_for(app, email, pw):  # noqa: F811
    c = TestClient(app)
    assert c.post("/login", data={"email": email, "password": pw}, follow_redirects=False).status_code == 303
    assert c.get("/api/v1/me").status_code == 200
    return c


def test_a_fresh_session_works(app, member):  # noqa: F811
    assert session_for(app, "m@x", PW).get("/api/v1/me").status_code == 200


def test_a_password_change_revokes_the_existing_session(app, root, member):  # noqa: F811
    live = session_for(app, "m@x", PW)
    assert root.post(f"/api/v1/users/{member}/password", json={"password": NEW}).status_code == 200
    assert live.get("/api/v1/me").status_code == 401
    assert session_for(app, "m@x", NEW).get("/api/v1/me").status_code == 200


def test_a_role_change_revokes_the_existing_session(app, root, member):  # noqa: F811
    live = session_for(app, "m@x", PW)
    assert root.put(f"/api/v1/users/{member}", json={"role": "group_admin"}).status_code == 200
    assert live.get("/api/v1/me").status_code == 401


def test_a_group_change_revokes_the_existing_session(app, root, member, demo):  # noqa: F811
    live = session_for(app, "m@x", PW)
    assert root.put(f"/api/v1/users/{member}", json={"groups": ["demo"]}).status_code == 200
    assert live.get("/api/v1/me").status_code == 401


def test_an_unchanged_update_leaves_the_session_alone(app, root, member):  # noqa: F811
    live = session_for(app, "m@x", PW)
    assert root.put(f"/api/v1/users/{member}", json={"role": "viewer", "groups": []}).status_code == 200
    assert live.get("/api/v1/me").status_code == 200


def test_deleting_the_user_revokes_the_session(app, root, member):  # noqa: F811
    live = session_for(app, "m@x", PW)
    assert root.delete(f"/api/v1/users/{member}").status_code == 200
    assert live.get("/api/v1/me").status_code == 401


def test_an_auth_config_change_revokes_every_session(app, root, member):  # noqa: F811
    """The func-tester's finding: disabling password login must not leave people signed in."""
    live, admin_live = session_for(app, "m@x", PW), session_for(app, "root@ramen.local", "rootpw")
    assert root.put("/api/v1/config/auth", json={"magic_link": True}).status_code == 200
    assert live.get("/api/v1/me").status_code == 401
    assert admin_live.get("/api/v1/me").status_code == 401


def test_a_rejected_auth_config_change_leaves_sessions_alone(app, root, member):  # noqa: F811
    live = session_for(app, "m@x", PW)
    assert root.put("/api/v1/config/auth", json={"password_login": False}).status_code == 422
    assert live.get("/api/v1/me").status_code == 200


def test_a_password_reset_revokes_the_existing_session(app, root, member):  # noqa: F811
    st = app.state
    live = session_for(app, "m@x", PW)
    import anyio

    _, nonce = anyio.run(st.accounts.start_token, "m@x", "reset")
    token = st.tokens.issue("reset", member, nonce)
    with TestClient(app) as c:
        c.get(f"/auth/reset/{token}")
        r = c.post(f"/auth/reset/{token}", data={"password": NEW}, follow_redirects=False)
        assert r.status_code == 303, r.text
    assert live.get("/api/v1/me").status_code == 401


def test_a_weak_password_is_refused_everywhere_it_can_be_set(root, member):
    for r in (
        root.post("/api/v1/users", json={"email": "w@x", "password": "short", "role": "viewer"}),
        root.post(f"/api/v1/users/{member}/password", json={"password": "short"}),
    ):
        assert r.status_code == 422, r.text
        assert "12 characters" in r.json()["detail"]
