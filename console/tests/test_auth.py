import pytest

from ramen_console.auth import apikeys, passwords
from ramen_console.auth.bootstrap import ensure_super_admin
from ramen_console.auth.oauth import OAuthRegistry
from ramen_console.auth.sessions import SessionSigner
from ramen_console.storage.memory import MemoryStore


def test_passwords():
    h = passwords.hash_password("pw")
    assert h != "pw" and passwords.verify_password("pw", h)
    assert not passwords.verify_password("nope", h)
    assert not passwords.verify_password("pw", "garbage")


def test_sessions():
    s = SessionSigner("secret")
    tok = s.sign({"uid": "u1"})
    assert s.load(tok) == {"uid": "u1"}
    assert s.load(tok + "x") is None
    assert s.load(None) is None
    assert s.load(tok, max_age=-1) is None


def test_apikeys():
    key, kid, h = apikeys.mint()
    assert key.startswith("rmn_") and key.count("_") == 2
    parsed = apikeys.parse(key)
    assert parsed and parsed[0] == kid
    assert apikeys.verify(parsed[1], h)
    assert not apikeys.verify("bad", h)
    assert apikeys.parse("nope") is None
    assert apikeys.parse("abc_def_ghi") is None


async def test_bootstrap(monkeypatch):
    store = MemoryStore()
    monkeypatch.delenv("RAMEN_ADMIN_EMAIL", raising=False)
    assert await ensure_super_admin(store) is None
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@x")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw")
    u = await ensure_super_admin(store)
    assert u["role"] == "super_admin" and u["email"] == "root@x"
    assert passwords.verify_password("pw", u["password_hash"])
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw2")
    u2 = await ensure_super_admin(store)
    assert u2["id"] == u["id"] and passwords.verify_password("pw2", u2["password_hash"])
    assert len(await store.list("users")) == 1


def test_oauth_registry_from_env(monkeypatch):
    assert OAuthRegistry.from_env().providers() == []
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_SECRET", "sec")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_METADATA_URL", "https://issuer/.well-known/openid-configuration")
    monkeypatch.setenv("RAMEN_OAUTH_HALF_CLIENT_ID", "cid")
    reg = OAuthRegistry.from_env()
    assert reg.providers() == ["oidc"]
    assert reg.client("oidc") is not None
    assert reg.client("half") is None
    assert reg.enabled
