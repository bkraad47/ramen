from ramen_console.util import is_cidr


def test_is_cidr_accepts_v4_and_v6():
    assert is_cidr("10.0.0.0/8") and is_cidr("::/0") and is_cidr("2001:db8::/32") and is_cidr("172.18.0.4/32")


def test_is_cidr_rejects_garbage():
    assert not is_cidr("10.0.0.0") and not is_cidr("300.1.1.1/8") and not is_cidr("abc/12") and not is_cidr("")


def test_rebalance_unknown_zone_is_404(monkeypatch):
    from fastapi.testclient import TestClient

    from ramen_console.app import create_app

    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_CLOUD", "local")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.test")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw-root-1")
    with TestClient(create_app()) as c:
        c.post("/login", data={"email": "root@ramen.test", "password": "pw-root-1"}, follow_redirects=False)
        c.post("/api/v1/groups", json={"name": "g1", "repo_url": "https://example.com/r.git", "ref": "main"})
        assert c.post("/api/v1/groups/g1/zones/nope/rebalance").status_code == 404
        assert c.post("/api/v1/groups/nope/zones/nope/rebalance").status_code == 404
