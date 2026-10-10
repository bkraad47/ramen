"""CONTRACTS §21.3 D50 (0.7.5): identity providers managed on the Config page — store doc `config/oauth_providers`
(secret encrypted at rest, never read back), the API, env precedence, and a registry that follows every change at
once so the login page and `/auth/<name>/login` work without a restart."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console.auth import providers
from ramen_console.auth.oauth import OAuthRegistry
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_flow import ISSUER, FakeIdp, start

URL = "/api/v1/config/oauth-providers"
ENTRA = {"issuer": ISSUER, "client_id": "cid", "client_secret": "s3cr3t-value-XYZ", "scopes": "openid email profile"}


def wire(demo, idp: FakeIdp):
    demo.app.state.oauth_transport = httpx.MockTransport(idp.handler)


def test_clean_validates_a_provider():
    out = providers.clean("entra", {**ENTRA, "groups_source": "lookup"}, None)
    assert out == {
        "issuer": ISSUER,
        "client_id": "cid",
        "client_secret": "s3cr3t-value-XYZ",
        "scopes": "openid email profile",
        "groups_source": "lookup",
    }
    kept = providers.clean("entra", {"issuer": ISSUER, "client_id": "cid"}, {"client_secret": "old"})
    assert kept["client_secret"] == "old" and kept["groups_source"] == "claim"
    for name in ("login", "logout", "reset", "magic", "oauth", "Bad Name", "", "a/b"):
        with pytest.raises(ValueError):
            providers.clean(name, ENTRA, None)
    with pytest.raises(ValueError, match="issuer"):
        providers.clean("entra", {**ENTRA, "issuer": ""}, None)
    with pytest.raises(ValueError, match="client_id"):
        providers.clean("entra", {**ENTRA, "client_id": " "}, None)
    with pytest.raises(ValueError, match="client_secret"):
        providers.clean("entra", {"issuer": ISSUER, "client_id": "cid"}, None)
    with pytest.raises(ValueError, match="lookup"):
        providers.clean("okta", {**ENTRA, "groups_source": "lookup"}, None)
    with pytest.raises(ValueError, match="claim or lookup"):
        providers.clean("entra", {**ENTRA, "groups_source": "graph"}, None)


def test_put_get_delete_and_the_secret_never_comes_back(demo):
    idp = FakeIdp({})
    wire(demo, idp)
    assert demo.get(URL).json() == {}
    r = demo.put(f"{URL}/entra", json={**ENTRA, "groups_source": "lookup"})
    assert r.status_code == 200, r.text
    listed = r.json()["entra"]
    assert listed == {
        "issuer": ISSUER,
        "client_id": "cid",
        "scopes": "openid email profile",
        "groups_source": "lookup",
        "source": "config",
        "has_secret": True,
    }
    assert "s3cr3t" not in r.text and "client_secret" not in r.text
    assert demo.get(URL).json()["entra"] == listed
    # the secret is Fernet-encrypted at rest (the test store is wrapped like production with RAMEN_FERNET_KEY)
    st = demo.app.state
    raw = asyncio.run(st.store.inner.get("config", "oauth_providers"))
    assert raw["entra"]["client_secret"].startswith("enc:") and raw["entra"]["client_id"] == "cid"
    assert asyncio.run(st.store.get("config", "oauth_providers"))["entra"]["client_secret"] == "s3cr3t-value-XYZ"
    # editing without a secret keeps the stored one; a new one replaces it
    r = demo.put(f"{URL}/entra", json={"issuer": ISSUER, "client_id": "cid-2"})
    assert r.status_code == 200 and r.json()["entra"]["client_id"] == "cid-2" and r.json()["entra"]["has_secret"]
    assert asyncio.run(st.store.get("config", "oauth_providers"))["entra"]["client_secret"] == "s3cr3t-value-XYZ"
    demo.put(f"{URL}/entra", json={"issuer": ISSUER, "client_id": "cid-2", "client_secret": "s3cr3t-2"})
    assert asyncio.run(st.store.get("config", "oauth_providers"))["entra"]["client_secret"] == "s3cr3t-2"
    # negatives
    assert demo.put(f"{URL}/okta", json={"issuer": ISSUER, "client_id": "x"}).status_code == 422  # no secret yet
    assert demo.put(f"{URL}/okta", json={**ENTRA, "groups_source": "lookup"}).status_code == 422
    assert demo.put(f"{URL}/login", json=ENTRA).status_code == 422
    assert demo.put(f"{URL}/entra", json={"client_id": "cid"}).status_code == 422
    assert demo.delete(f"{URL}/nope").status_code == 404
    assert demo.delete(f"{URL}/entra").status_code == 200 and demo.get(URL).json() == {}
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "config.oauth_provider"]
    assert any("provider:entra" in a["tags"] and "secret:set" in a["tags"] for a in rows)
    assert any("provider:entra" in a["tags"] and "removed" in a["tags"] for a in rows)
    assert not any("s3cr3t" in t for a in rows for t in a["tags"])
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as c:
        login(c, "ga@x", PW)
        assert c.get(URL).status_code == 403 and c.put(f"{URL}/entra", json=ENTRA).status_code == 403


def test_registry_follows_a_change_at_once_login_page_and_login_redirect(demo):
    idp = FakeIdp({"email": "dev@corp.test", "email_verified": True})
    wire(demo, idp)
    with TestClient(demo.app) as anon:
        assert "/auth/entra/login" not in anon.get("/login").text
        assert anon.get("/auth/entra/login").status_code == 404
    assert demo.put(f"{URL}/entra", json=ENTRA).status_code == 200
    assert demo.app.state.oauth.providers() == ["entra"]
    with TestClient(demo.app) as anon:
        assert "/auth/entra/login" in anon.get("/login").text
        state = start(anon, idp, "entra")  # redirects to the configured issuer with the configured client id
        r = anon.get(f"/auth/entra/callback?code=good-code&state={state}", follow_redirects=False)
        assert r.status_code == 303 and anon.get("/api/v1/me").json()["email"] == "dev@corp.test"
    assert demo.get("/api/v1/config/auth").json()["providers"] == ["entra"]
    assert demo.delete(f"{URL}/entra").status_code == 200
    with TestClient(demo.app) as anon:
        assert "/auth/entra/login" not in anon.get("/login").text
        assert anon.get("/auth/entra/login").status_code == 404


def test_env_providers_are_listed_as_env_the_doc_wins_per_name_and_env_cannot_be_deleted(demo, monkeypatch):
    idp = FakeIdp({})
    wire(demo, idp)
    monkeypatch.setenv("RAMEN_OAUTH_GOOGLE_ISSUER", ISSUER)
    monkeypatch.setenv("RAMEN_OAUTH_GOOGLE_CLIENT_ID", "env-cid")
    monkeypatch.setenv("RAMEN_OAUTH_GOOGLE_CLIENT_SECRET", "env-s3cr3t-XYZ")
    assert demo.post("/api/v1/config/reload").status_code == 200
    listed = demo.get(URL).json()
    assert listed["google"]["source"] == "env" and listed["google"]["client_id"] == "env-cid"
    assert listed["google"]["has_secret"] is True
    assert "env-s3cr3t" not in demo.get(URL).text
    r = demo.delete(f"{URL}/google")
    assert r.status_code == 409 and "environment" in r.json()["detail"]
    # the doc wins for the same name; removing the doc entry falls back to the environment
    assert demo.put(f"{URL}/google", json={**ENTRA, "client_id": "doc-cid"}).status_code == 200
    assert demo.get(URL).json()["google"]["client_id"] == "doc-cid"
    assert demo.get(URL).json()["google"]["source"] == "config"
    assert demo.app.state.oauth.providers() == ["google"]
    assert demo.delete(f"{URL}/google").status_code == 200
    assert demo.get(URL).json()["google"] == {
        "issuer": ISSUER,
        "client_id": "env-cid",
        "scopes": "openid email profile",
        "groups_source": "claim",
        "source": "env",
        "has_secret": True,
    }
    assert demo.app.state.oauth.providers() == ["google"]


def test_groups_source_is_shared_between_the_provider_doc_and_the_auth_doc(demo):
    idp = FakeIdp({})
    wire(demo, idp)
    demo.put(f"{URL}/entra", json={**ENTRA, "groups_source": "lookup"})
    assert demo.get("/api/v1/config/auth").json()["groups_source"] == {"entra": "lookup"}
    assert "User.Read" in demo.app.state.oauth.scope("entra").split()
    assert demo.put("/api/v1/config/auth/groups-source/entra", json={"source": "claim"}).status_code == 200
    assert demo.get(URL).json()["entra"]["groups_source"] == "claim"
    assert "User.Read" not in demo.app.state.oauth.scope("entra")
    # a provider doc entry is re-read by other replicas within the TTL: a fresh registry build sees it
    st = demo.app.state
    reg = asyncio.run(providers.build(st.store, {}, transport=st.oauth_transport))
    assert isinstance(reg, OAuthRegistry) and reg.providers() == ["entra"] and reg.groups_source("entra") == "claim"


def test_config_page_has_one_card_per_provider_with_group_source_and_role_mapping(demo):
    idp = FakeIdp({})
    wire(demo, idp)
    demo.put(f"{URL}/entra", json=ENTRA)
    page = demo.get("/config").text
    assert "Identity providers (Entra ID, Google Workspace)" in page
    assert (
        'hx-post="/api/v1/config/oauth-providers"' in page
        and 'hx-delete="/api/v1/config/oauth-providers/entra"' in page
    )
    assert "s3cr3t" not in page
    assert 'name="client_secret"' in page and 'name="issuer"' in page and 'name="source"' in page
    assert 'name="allow_unverified"' not in page  # D51: no such switch any more
    assert "OAuth role mapping" in page and 'name="claim"' in page  # the rules sit with the provider
    assert page.count("Identity providers (Entra ID, Google Workspace)") == 1
