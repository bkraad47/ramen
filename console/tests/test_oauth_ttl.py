"""A3 / C8 (0.7.0): RAMEN_OAUTH_ACCESS_TTL and RAMEN_OAUTH_REFRESH_TTL override the token lifetimes."""

import base64
import json
import time

from ramen_console import oauth_server
from ramen_console.auth.oauth import OAuthRegistry
from tests.test_api import app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_server import consent, pkce, register


def claims(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


def test_defaults_are_an_hour_and_thirty_days(monkeypatch):
    monkeypatch.delenv("RAMEN_OAUTH_ACCESS_TTL", raising=False)
    monkeypatch.delenv("RAMEN_OAUTH_REFRESH_TTL", raising=False)
    assert oauth_server.access_ttl() == 3600 and oauth_server.refresh_ttl() == 30 * 24 * 3600


def test_env_overrides_are_read_per_mint_and_bad_values_fall_back(monkeypatch, caplog):
    monkeypatch.setenv("RAMEN_OAUTH_ACCESS_TTL", "3")
    monkeypatch.setenv("RAMEN_OAUTH_REFRESH_TTL", "120")
    assert oauth_server.access_ttl() == 3 and oauth_server.refresh_ttl() == 120
    monkeypatch.setenv("RAMEN_OAUTH_ACCESS_TTL", "soon")
    monkeypatch.setenv("RAMEN_OAUTH_REFRESH_TTL", "0")
    assert oauth_server.access_ttl() == 3600 and oauth_server.refresh_ttl() == 30 * 24 * 3600
    assert any("RAMEN_OAUTH_ACCESS_TTL" in r.getMessage() for r in caplog.records)


def test_minted_tokens_carry_the_configured_lifetimes(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_OAUTH_ACCESS_TTL", "3")
    monkeypatch.setenv("RAMEN_OAUTH_REFRESH_TTL", "120")
    cid = register(demo)
    verifier, challenge = pkce()
    code = consent(demo, cid, challenge)
    r = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/callback",
            "client_id": cid,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    tok = r.json()
    c = claims(tok["access_token"])
    assert tok["expires_in"] == 3 and c["exp"] - c["iat"] == 3
    import asyncio

    (rec,) = asyncio.run(demo.app.state.store.list("oauth_refresh"))  # the one refresh token just stored
    assert 100 <= rec["exp"] - time.time() <= 120


def test_ttl_variables_are_not_mistaken_for_oauth_providers():
    env = {
        "RAMEN_OAUTH_ACCESS_TTL": "3",
        "RAMEN_OAUTH_REFRESH_TTL": "4",
        "RAMEN_OAUTH_IDP_CLIENT_ID": "x",
        "RAMEN_OAUTH_IDP_CLIENT_SECRET": "y",
        "RAMEN_OAUTH_IDP_METADATA_URL": "https://idp/.well-known/openid-configuration",
    }
    assert OAuthRegistry.from_env(env).providers() == ["idp"]
    assert OAuthRegistry.from_env({"RAMEN_OAUTH_ACCESS_TTL": "3"}).enabled is False


def test_lifetimes_show_on_the_config_page_and_api(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_OAUTH_ACCESS_TTL", "900")
    page = demo.get("/config").text
    assert "RAMEN_OAUTH_ACCESS_TTL" in page and "900" in page and "RAMEN_OAUTH_REFRESH_TTL" in page
    cfg = demo.get("/api/v1/config").json()
    assert cfg["oauth_ttl"] == {"access": 900, "refresh": 30 * 24 * 3600}
    assert cfg["env"]["RAMEN_OAUTH_ACCESS_TTL"] == "900"  # not a secret: shown unmasked in the RAMEN_* table
