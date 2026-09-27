"""CONTRACTS §9 CSRF: ramen_csrf cookie + X-Ramen-CSRF header / hidden field; API keys exempt."""
from fastapi.testclient import TestClient

from tests.test_api import app, client, cloud, demo, root, login  # noqa: F401 - pytest fixtures


def bare(app):
    c = TestClient(app)
    c.event_hooks = {}  # no automatic header
    return c


def test_cookie_authenticated_api_mutation_needs_header(demo):
    c = bare(demo.app)
    r = c.post("/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False)
    assert r.status_code == 303 and c.cookies.get("ramen_csrf")
    assert c.get("/api/v1/me").status_code == 200  # reads never need it
    r = c.post("/api/v1/zones", json={"name": "z-csrf"})
    assert r.status_code == 403 and "csrf" in r.json()["detail"]
    assert c.post("/api/v1/zones", json={"name": "z-csrf"}, headers={"X-Ramen-CSRF": "wrong"}).status_code == 403
    tok = c.cookies.get("ramen_csrf")
    assert c.post("/api/v1/zones", json={"name": "z-csrf"}, headers={"X-Ramen-CSRF": tok}).status_code == 201
    rows = demo.get("/api/v1/audit").json()  # rejected attempts are audited (generic action: the route never ran)
    assert sum(1 for a in rows if a["action"] == "POST /api/v1/zones" and not a["ok"] and a["user"] == "root@ramen.local") == 2
    assert any(a["action"] == "zone.create" and a["ok"] for a in rows)
    assert c.post("/api/v1/config/auth", headers={"X-Ramen-CSRF": tok}).status_code == 405


def test_api_key_is_exempt(demo):
    key = demo.post("/api/v1/api-keys", json={"name": "ci"}).json()["key"]
    c = bare(demo.app)
    r = c.post("/api/v1/zones", json={"name": "z-key"}, headers={"X-Ramen-Api-Key": key})
    assert r.status_code == 201, r.text
    assert "ramen_csrf" not in c.cookies


def test_login_form_double_submit(client):
    c = bare(client.app)
    page = c.get("/login")
    assert page.status_code == 200 and c.cookies.get("ramen_csrf") and 'name="csrf_token"' in page.text
    tok = c.cookies.get("ramen_csrf")
    assert tok in page.text
    r = c.post("/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False)
    assert r.status_code == 403  # cookie present, field missing
    r = c.post("/login", data={"email": "root@ramen.local", "password": "rootpw", "csrf_token": "nope"}, follow_redirects=False)
    assert r.status_code == 403
    r = c.post("/login", data={"email": "root@ramen.local", "password": "rootpw", "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/logout", follow_redirects=False).status_code == 303
    assert not c.cookies.get("ramen_session")


def test_htmx_page_carries_header_script(root):
    html = root.get("/").text
    assert "X-Ramen-CSRF" in html and "ramen_csrf" in html
