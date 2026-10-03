"""N5: GitHub App installation tokens — a group needs no stored PAT once a console-wide App is configured."""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ramen_console import github_app
from tests.test_api import PW, app, client, cloud, login, make_user, root  # noqa: F401 - fixtures


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture
def pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def test_installation_token_calls_github_and_returns_the_token(pem, monkeypatch):
    seen = {}

    async def fake_post(self, url, headers=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return httpx.Response(200, json={"token": "ghs_minted"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tok = await github_app.installation_token("1", pem, "99")
    assert tok == "ghs_minted"
    assert seen["url"] == "https://api.github.com/app/installations/99/access_tokens"
    assert seen["auth"].startswith("Bearer ")


def test_app_jwt_is_signed_rs256_with_the_app_as_issuer(pem):
    token = github_app.app_jwt("12345", pem)
    header_b64, payload_b64, _ = token.split(".")
    assert json.loads(b64url_decode(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(b64url_decode(payload_b64))
    assert payload["iss"] == "12345" and payload["exp"] - payload["iat"] <= 600


async def test_resolve_token_is_none_without_installation_id_or_app_config(root, pem):
    store = root.app.state.store
    assert await github_app.resolve_token(store, {}) is None
    assert await github_app.resolve_token(store, {"github_app_installation_id": "99"}) is None
    await github_app.set_app_config(store, app_id="1", private_key=pem)
    assert await github_app.resolve_token(store, {}) is None  # app configured, but this group has no installation


async def test_resolve_token_mints_an_installation_token(root, pem, monkeypatch):
    store = root.app.state.store
    await github_app.set_app_config(store, app_id="1", private_key=pem)

    async def fake(app_id, private_key, installation_id):
        return f"ghs_{app_id}_{installation_id}"

    monkeypatch.setattr(github_app, "installation_token", fake)
    tok = await github_app.resolve_token(store, {"github_app_installation_id": "99"})
    assert tok == "ghs_1_99"


def test_github_app_config_round_trips_masks_and_is_super_admin_gated(root, pem):
    r = root.get("/api/v1/config/github-app")
    assert r.status_code == 200 and r.json() == {"app_id": "", "private_key_set": False}

    r = root.put("/api/v1/config/github-app", json={"app_id": "42", "private_key": pem})
    assert r.status_code == 200
    body = r.json()
    assert body["app_id"] == "42" and body["private_key_set"] is True and "private_key" not in body

    make_user(root, "v@x", "viewer", [])
    from fastapi.testclient import TestClient

    with TestClient(root.app) as v:
        login(v, "v@x", PW)
        assert v.get("/api/v1/config/github-app").status_code == 403


async def test_private_key_is_encrypted_at_rest(root, pem):
    root.put("/api/v1/config/github-app", json={"app_id": "42", "private_key": pem})
    raw = await root.app.state.store.inner.get("config", "github_app")
    assert raw["github_app_private_key"] != pem and raw["github_app_private_key"].startswith("enc:")


def test_create_group_accepts_installation_id_and_does_not_hide_it(root):
    r = root.post(
        "/api/v1/groups",
        json={"name": "appauth", "repo_url": "https://github.com/org/appauth.git", "github_app_installation_id": "7"},
    )
    assert r.status_code == 201 and r.json()["github_app_installation_id"] == "7"


def test_update_group_can_add_installation_id_later(root):
    root.post("/api/v1/groups", json={"name": "later", "repo_url": "https://github.com/org/later.git"})
    r = root.put("/api/v1/groups/later", json={"github_app_installation_id": "8"})
    assert r.status_code == 200 and r.json()["github_app_installation_id"] == "8"


async def test_deploy_prefers_the_app_token_over_a_stored_pat(root, pem, monkeypatch):
    store = root.app.state.store
    await github_app.set_app_config(store, app_id="1", private_key=pem)

    async def fake_token(*a):
        return "app-minted-token"

    monkeypatch.setattr(github_app, "installation_token", fake_token)

    seen = {}

    async def fake_sync(group, repo_url, ref, token):
        seen["token"] = token
        return "/tmp/fake"

    root.app.state.cloud.sync_repo = fake_sync
    root.post(
        "/api/v1/groups",
        json={
            "name": "both",
            "repo_url": "https://github.com/org/both.git",
            "github_token": "should-not-be-used",
            "github_app_installation_id": "9",
        },
    )
    root.post("/api/v1/groups/both/environments", json={"name": "prod", "zones": []})
    jid = root.post("/api/v1/groups/both/environments/prod/deploy", json={"canary": False}).json()["id"]
    for _ in range(100):
        import time

        if root.get(f"/api/v1/jobs/{jid}").json()["status"] != "running":
            break
        time.sleep(0.05)
    assert seen["token"] == "app-minted-token"
