"""0.6.0: a public console (RAMEN_COOKIE_SECURE=1) never serves a page or the API over plain http."""

from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.storage import make_store
from tests.test_api import app, client, cloud  # noqa: F401 - fixtures


def test_plain_http_is_redirected_or_refused_except_probes(monkeypatch, tmp_path, cloud):  # noqa: F811
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.local")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setenv("RAMEN_BACKUP_ROOT", str(tmp_path / "b"))
    monkeypatch.setenv("RAMEN_BUCKET_ROOT", str(tmp_path / "k"))
    monkeypatch.setenv("RAMEN_COOKIE_SECURE", "1")
    with TestClient(create_app(store=make_store(), cloud=cloud), base_url="http://console.example") as c:
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "https://console.example/login"
        r = c.post("/login", data={"email": "x", "password": "y"}, follow_redirects=False)
        assert r.status_code == 403 and "HTTPS" in r.text
        assert c.get("/api/v1/me", follow_redirects=False).status_code == 301
        assert c.get("/healthz").status_code == 200 and c.get("/readyz").status_code == 200  # kubelet talks plain http
        # behind the load balancer the pod sees plain http with X-Forwarded-Proto: https — that is the real request
        r = c.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200 and "Strict-Transport-Security" in r.headers


def test_a_private_console_keeps_plain_http(client):  # noqa: F811 - the default test app has no RAMEN_COOKIE_SECURE
    assert client.get("/login").status_code == 200
