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
