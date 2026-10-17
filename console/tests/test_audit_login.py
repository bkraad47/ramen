from fastapi.testclient import TestClient

from ramen_console.app import create_app


def test_login_audit_records_email(monkeypatch):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.test")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw-root-1")
    with TestClient(create_app()) as c:
        r = c.post("/login", data={"email": "root@ramen.test", "password": "pw-root-1"}, follow_redirects=False)
        assert r.status_code == 303
        rows = c.get("/api/v1/audit").json()
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        login = [e for e in rows if e["action"] == "login"]
        assert login and login[0]["user"] == "root@ramen.test" and login[0]["ok"] is True


def test_me_exposes_email(monkeypatch):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.test")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw-root-1")
    with TestClient(create_app()) as c:
        c.post("/login", data={"email": "root@ramen.test", "password": "pw-root-1"}, follow_redirects=False)
        me = c.get("/api/v1/me").json()
        assert me["email"] == "root@ramen.test" and me["role"] == "super_admin"


def test_audit_ip_is_the_trusted_hop_not_the_leftmost_forwarded_for(monkeypatch):
    """0.8.0 SC6: `x-forwarded-for` is counted from the right with RAMEN_TRUST_PROXY_HOPS (as the node does); the
    left-most entry is whatever the client wrote. The login rate limiter keys on that same address."""
    from starlette.requests import Request

    from ramen_console.audit import client_ip

    def req(xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": ("10.0.0.9", 1)})

    monkeypatch.delenv("RAMEN_TRUST_PROXY_HOPS", raising=False)
    assert client_ip(req("1.1.1.1, 2.2.2.2, 3.3.3.3")) == "3.3.3.3", "default: one trusted proxy"
    assert client_ip(req(None)) == "10.0.0.9"
    monkeypatch.setenv("RAMEN_TRUST_PROXY_HOPS", "2")
    assert client_ip(req("1.1.1.1, 2.2.2.2, 3.3.3.3")) == "2.2.2.2"
    assert client_ip(req("3.3.3.3")) == "10.0.0.9", "fewer hops than trusted: the peer, never a forged entry"
    monkeypatch.setenv("RAMEN_TRUST_PROXY_HOPS", "0")
    assert client_ip(req("1.1.1.1")) == "10.0.0.9", "0: no proxy is trusted"

    monkeypatch.delenv("RAMEN_TRUST_PROXY_HOPS", raising=False)
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.test")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw-root-1")
    monkeypatch.setenv("RAMEN_LOGIN_RATE_LIMIT", "1")
    with TestClient(create_app()) as c:
        bad = {"email": "root@ramen.test", "password": "wrong"}
        assert (
            c.post("/login", data=bad, headers={"x-forwarded-for": "5.5.5.5"}, follow_redirects=False).status_code
            == 401
        )
        assert (
            c.post("/login", data=bad, headers={"x-forwarded-for": "6.6.6.6"}, follow_redirects=False).status_code
            == 401
        )
        assert (
            c.post("/login", data=bad, headers={"x-forwarded-for": "5.5.5.5"}, follow_redirects=False).status_code
            == 429
        )
        rows = c.get("/api/v1/audit", headers={"x-forwarded-for": "7.7.7.7, 8.8.8.8"}).json()
        c.post(
            "/login",
            data={"email": "root@ramen.test", "password": "pw-root-1"},
            headers={"x-forwarded-for": "7.7.7.7, 8.8.8.8"},
            follow_redirects=False,
        )
        rows = c.get("/api/v1/audit").json()
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        assert [e["ip"] for e in rows if e["action"] == "login" and e["ok"]][0] == "8.8.8.8"
