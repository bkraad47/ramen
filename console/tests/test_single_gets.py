"""F3 (0.3.0 func report): single-item GETs for zones and environments."""

from fastapi.testclient import TestClient

from ramen_console.app import create_app


def _client(monkeypatch):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_CLOUD", "local")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.test")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw-root-1")
    c = TestClient(create_app())
    c.__enter__()
    c.post("/login", data={"email": "root@ramen.test", "password": "pw-root-1"}, follow_redirects=False)
    return c


def test_get_zone_and_environment(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/v1/zones", json={"name": "z1", "provider": "local", "region": "local"}).status_code == 201
    assert (
        c.post(
            "/api/v1/groups", json={"name": "g1", "repo_url": "https://example.com/r.git", "ref": "main"}
        ).status_code
        == 201
    )
    assert (
        c.post("/api/v1/groups/g1/environments", json={"name": "dev", "ref": "main", "zones": ["z1"]}).status_code
        == 201
    )
    z = c.get("/api/v1/zones/z1")
    assert z.status_code == 200 and z.json()["name"] == "z1" and z.json()["provider"] == "local"
    e = c.get("/api/v1/groups/g1/environments/dev")
    assert e.status_code == 200 and e.json()["name"] == "dev" and e.json()["zones"] == ["z1"]
    assert c.get("/api/v1/zones/nope").status_code == 404
    assert c.get("/api/v1/groups/g1/environments/nope").status_code == 404
    assert c.get("/api/v1/groups/nope/environments/dev").status_code in (403, 404)
    c.__exit__(None, None, None)
