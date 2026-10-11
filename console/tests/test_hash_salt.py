"""0.7.5: every stored secret hash is salted. Passwords were already argon2 (salted); API/MCP key secrets now carry a
per-key random salt, OAuth codes and refresh tokens a per-install salt (their hash is the lookup id). Hashes written
before 0.7.5 still verify, and a legacy key hash is rewritten salted on its next successful use."""

import asyncio
import hashlib

from ramen_console import oauth_server
from ramen_console.auth import apikeys
from ramen_console.auth.passwords import hash_password
from ramen_console.storage.memory import MemoryStore
from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - pytest fixtures


def test_password_hashes_are_salted():
    assert hash_password("Same-Pass-123!") != hash_password("Same-Pass-123!")


def test_key_hash_is_salted_and_verifies():
    a, b = apikeys.hash_secret("secret-1"), apikeys.hash_secret("secret-1")
    assert a != b and a.startswith("s1$") and b.startswith("s1$")
    assert apikeys.verify("secret-1", a) and not apikeys.verify("secret-2", a)
    assert not apikeys.needs_upgrade(a)


def test_mint_stores_a_salted_hash():
    raw, kid, h = apikeys.mint("agent")
    _, sec, _ = apikeys.parse(raw)
    assert h.startswith("s1$") and apikeys.verify(sec, h)


def test_legacy_unsalted_key_hash_still_verifies_and_is_flagged():
    legacy = hashlib.sha256(b"old-secret").hexdigest()
    assert apikeys.verify("old-secret", legacy) and not apikeys.verify("other", legacy)
    assert apikeys.needs_upgrade(legacy)


def test_malformed_hash_never_verifies():
    assert not apikeys.verify("x", "s1$zz") and not apikeys.verify("x", "") and not apikeys.verify("x", "s1$$")


def test_token_id_is_salted_per_install():
    store = MemoryStore()
    salt = asyncio.run(oauth_server.token_salt(store))
    assert salt and salt == asyncio.run(oauth_server.token_salt(store))  # generated once, then reused
    assert oauth_server.token_id("t", salt) != oauth_server.token_id("t", "other-salt")
    assert oauth_server.token_id("t", salt) != hashlib.sha256(b"t").hexdigest()
    assert oauth_server.token_ids("t", salt)[-1] == hashlib.sha256(b"t").hexdigest()  # legacy id still looked up


def test_legacy_key_hash_is_salted_on_next_use(demo):  # noqa: F811
    r = demo.post("/api/v1/api-keys", json={"name": "old"})
    key, kid = r.json()["key"], r.json()["id"]
    _, sec, _ = apikeys.parse(key)
    store = demo.app.state.store
    doc = asyncio.run(store.get("api_keys", kid))
    asyncio.run(store.put("api_keys", kid, {**doc, "secret_hash": hashlib.sha256(sec.encode()).hexdigest()}))
    from fastapi.testclient import TestClient

    with TestClient(demo.app) as anon:
        assert anon.get("/api/v1/me", headers={"X-Ramen-Api-Key": key}).status_code == 200
        assert asyncio.run(store.get("api_keys", kid))["secret_hash"].startswith("s1$")
        assert anon.get("/api/v1/me", headers={"X-Ramen-Api-Key": key}).status_code == 200
