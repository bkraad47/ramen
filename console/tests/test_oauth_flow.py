"""CONTRACTS §9 OAuth/OIDC: full code flow against a fake OIDC provider (discovery, token with RS256 id_token,
userinfo), user link/create by verified email, role_claim/role_map."""

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from ramen_console.auth.oauth import OAuthRegistry
from tests.test_api import app, client, cloud, demo, make_user, root  # noqa: F401 - pytest fixtures

ISSUER = "https://idp.test"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})


class FakeIdp:
    """State machine for one provider: remembers the nonce from the authorize redirect, mints tokens."""

    def __init__(self, claims, id_token=True):
        self.claims, self.id_token, self.nonce, self.token_calls = claims, id_token, None, []
        self.challenge = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if path == "/token":
            form = parse_qs(req.content.decode())
            self.token_calls.append(form)
            if form.get("code") != ["good-code"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if self.challenge:  # PKCE: the verifier must hash to the challenge sent on /authorize
                digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
                assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenge
            body = {"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600}
            if self.id_token:
                now = int(time.time())
                claims = {
                    "iss": ISSUER,
                    "sub": "sub-1",
                    "aud": "cid",
                    "iat": now,
                    "exp": now + 300,
                    "nonce": self.nonce,
                    **self.claims,
                }
                body["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY)
            return httpx.Response(200, json=body)
        if path == "/userinfo":
            assert req.headers["Authorization"] == "Bearer at-1"
            return httpx.Response(200, json={"sub": "sub-1", **self.claims})
        return httpx.Response(404)


def registry(idp: FakeIdp, **extra):
    env = {
        "RAMEN_OAUTH_IDP_ISSUER": ISSUER,
        "RAMEN_OAUTH_IDP_CLIENT_ID": "cid",
        "RAMEN_OAUTH_IDP_CLIENT_SECRET": "sec",
        **extra,
    }
    return OAuthRegistry.from_env(env, transport=httpx.MockTransport(idp.handler))


def start(client: TestClient, idp: FakeIdp, name="idp") -> str:
    r = client.get(f"/auth/{name}/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(f"{ISSUER}/authorize?") and q["client_id"] == ["cid"]
    assert q["redirect_uri"] == [f"http://testserver/auth/{name}/callback"] and q["response_type"] == ["code"]
    idp.nonce = q["nonce"][0]  # openid is always requested (SEC-06), so a nonce is always issued and verified
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 43  # PKCE
    idp.challenge = q["code_challenge"][0]
    return q["state"][0]


def test_oidc_creates_user_with_mapped_role(demo, monkeypatch):
    idp = FakeIdp({"email": "Dev@Corp.test", "email_verified": True, "groups": ["devs", "ops"]})
    demo.app.state.oauth = registry(idp)
    monkeypatch.setenv("RAMEN_AUTH_OAUTH_IDP_ROLE_CLAIM", "groups")
    monkeypatch.setenv("RAMEN_AUTH_OAUTH_IDP_ROLE_MAP", "devs=group_admin:demo;ops=viewer:other")
    demo.app.state.auth_env = demo.app.state.auth_env.from_env()
    with TestClient(demo.app) as anon:
        assert "Sign in with idp" in anon.get("/login").text and "/auth/idp/login" in anon.get("/login").text
        state = start(anon, idp)
        assert anon.get("/auth/idp/callback?code=good-code&state=wrong", follow_redirects=False).status_code == 401
        state = start(anon, idp)
        r = anon.get(f"/auth/idp/callback?code=good-code&state={state}", follow_redirects=False)
        assert r.status_code == 303 and anon.cookies.get("ramen_session") and anon.cookies.get("ramen_csrf")
        me = anon.get("/api/v1/me").json()
        assert me["email"] == "dev@corp.test" and me["role"] == "group_admin" and me["groups"] == ["demo", "other"]
        assert idp.token_calls[-1]["redirect_uri"] == ["http://testserver/auth/idp/callback"]
    users = {u["email"]: u for u in demo.get("/api/v1/users").json()}
    assert users["dev@corp.test"]["provider"] == "idp"
    audit = demo.get("/api/v1/audit").json()
    assert any(
        a["action"] == "login.oauth" and a["user"] == "dev@corp.test" and a["ok"] and "provider:idp" in a["tags"]
        for a in audit
    )
    assert any(a["action"] == "login.oauth" and not a["ok"] for a in audit)  # the bad-state attempt


def test_oauth_links_existing_user_and_keeps_role(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    idp = FakeIdp({"email": "ga@x", "email_verified": True}, id_token=False)  # plain OAuth2: userinfo path
    demo.app.state.oauth = registry(idp, RAMEN_OAUTH_IDP_SCOPES="email")
    with TestClient(demo.app) as anon:
        state = start(anon, idp)
        r = anon.get(f"/auth/oauth/idp/callback?code=good-code&state={state}", follow_redirects=False)  # legacy alias
        assert r.status_code == 303
        assert anon.get("/api/v1/me").json()["role"] == "group_admin"
    assert len([u for u in demo.get("/api/v1/users").json() if u["email"] == "ga@x"]) == 1


def test_oauth_rejects_unverified_or_missing_email_and_bad_code(demo):
    idp = FakeIdp({"email": "x@y", "email_verified": False})
    demo.app.state.oauth = registry(idp)
    with TestClient(demo.app) as anon:
        state = start(anon, idp)
        r = anon.get(f"/auth/idp/callback?code=good-code&state={state}", follow_redirects=False)
        assert r.status_code == 403 and "verified" in r.text
        state = start(anon, idp)
        assert anon.get(f"/auth/idp/callback?code=bad&state={state}", follow_redirects=False).status_code == 401
        assert anon.get("/auth/nope/login").status_code == 404
        assert anon.get("/auth/nope/callback?code=1&state=2").status_code == 404
        assert anon.get("/auth/idp/callback?error=access_denied&state=x", follow_redirects=False).status_code == 401
    idp2 = FakeIdp({"name": "no email"})
    demo.app.state.oauth = registry(idp2)
    with TestClient(demo.app) as anon:
        state = start(anon, idp2)
        assert anon.get(f"/auth/idp/callback?code=good-code&state={state}", follow_redirects=False).status_code == 403
    assert not [u for u in demo.get("/api/v1/users").json() if u["email"] == "x@y"]
