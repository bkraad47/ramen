"""CONTRACTS §16.3 (D34): the console as OAuth 2.1 authorization server for end users of a worker.

Pre-registered public clients, authorization code + PKCE S256, HS256 access tokens the node verifies with the zone's
session secret, refresh tokens rotated on use and revoked by the user's session epoch."""

import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

CLIENT = {"name": "Claude Desktop", "redirect_uris": ["http://127.0.0.1:9999/callback"]}
RESOURCE = "mcp:demo:zone-a"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def node_verify(token: str, session_secret: str) -> dict:
    """Exactly what node-rs/src/token.rs does: HS256 with key = HMAC-SHA256(session secret, b"oauth")."""
    h, p, s = token.split(".")
    key = hmac.new(session_secret.encode(), b"oauth", hashlib.sha256).digest()
    want = hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest()
    assert hmac.compare_digest(b64url(want), s), "signature must verify with the node's derivation"
    header = json.loads(base64.urlsafe_b64decode(h + "=="))
    assert header["alg"] == "HS256"
    return json.loads(base64.urlsafe_b64decode(p + "=="))


def register(root) -> str:
    r = root.post("/api/v1/oauth/clients", json=CLIENT)
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize(user, client_id, challenge, state="xyz", scope=RESOURCE, redirect=CLIENT["redirect_uris"][0]):
    return user.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": scope,
        },
        follow_redirects=False,
    )


def consent(user, client_id, challenge, scope=RESOURCE, redirect=CLIENT["redirect_uris"][0], state="xyz") -> str:
    r = user.post(
        "/oauth/authorize",
        data={  # what the consent page carries as hidden fields: the whole authorize query
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": scope,
            "decision": "allow",
            "csrf_token": user.cookies.get("ramen_csrf", ""),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = urlparse(r.headers["location"])
    assert loc.scheme == "http" and loc.netloc == "127.0.0.1:9999"
    q = parse_qs(loc.query)
    assert q["state"] == [state]
    return q["code"][0]


def test_discovery_metadata(demo):
    r = demo.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    m = r.json()
    assert m["issuer"] == "http://testserver"
    assert m["authorization_endpoint"] == "http://testserver/oauth/authorize"
    assert m["token_endpoint"] == "http://testserver/oauth/token"
    assert m["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in m["grant_types_supported"] and "refresh_token" in m["grant_types_supported"]
    assert m["token_endpoint_auth_methods_supported"] == ["none"]
    assert "scopes_supported" not in m  # it would list every group and zone to the internet (review 0.5.0 L7)
    assert demo.app.state.oauth_server is not None


def test_clients_are_pre_registered_by_a_super_admin_only(demo):
    cid = register(demo)
    assert cid and "client_secret" not in demo.get("/api/v1/oauth/clients").text
    listed = demo.get("/api/v1/oauth/clients").json()
    assert [c["client_id"] for c in listed] == [cid] and listed[0]["name"] == CLIENT["name"]
    assert demo.post("/api/v1/oauth/clients", json={"name": "x", "redirect_uris": ["ftp://bad"]}).status_code == 422
    assert demo.post("/api/v1/oauth/clients", json={"name": "x", "redirect_uris": []}).status_code == 422
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.post("/api/v1/oauth/clients", json=CLIENT).status_code == 403
        assert ga.get("/api/v1/oauth/clients").status_code == 403
    page = demo.get("/config").text  # 0.5.92: OAuth clients live on the Config page, not next to API keys
    assert "OAuth clients" in page and CLIENT["name"] in page and cid in page
    assert "OAuth clients" not in demo.get("/api-keys").text
    assert demo.delete(f"/api/v1/oauth/clients/{cid}").status_code == 200
    assert demo.get("/api/v1/oauth/clients").json() == []


def test_authorize_requires_login_then_consent(demo):
    cid = register(demo)
    _, challenge = pkce()
    with TestClient(demo.app) as anon:
        r = authorize(anon, cid, challenge)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Foauth%2Fauthorize")
    r = authorize(demo, cid, challenge)
    assert r.status_code == 200
    page = r.text
    assert CLIENT["name"] in page and "demo" in page and "zone-a" in page and "Allow" in page and "Deny" in page
    # a client, redirect or scope that was not registered never reaches consent
    assert authorize(demo, "nope", challenge).status_code == 400
    assert authorize(demo, cid, challenge, redirect="http://evil.example/cb").status_code == 400
    assert authorize(demo, cid, challenge, scope="mcp:demo:nozone").status_code == 400
    assert authorize(demo, cid, challenge, scope="mcp:demo:zone-a mcp:demo:zone-b").status_code == 400
    # RFC 8707: the resource indicator may be the worker URL (what RFC 9728 clients such as Claude Code send)
    base = {"response_type": "code", "client_id": cid, "redirect_uri": CLIENT["redirect_uris"][0], "scope": RESOURCE}
    base |= {"code_challenge": challenge, "code_challenge_method": "S256"}
    ok = demo.get("/oauth/authorize", params={**base, "resource": "https://lb.example/mcp"}, follow_redirects=False)
    assert ok.status_code == 200 and "Allow" in ok.text
    for bad in ("mcp:other:zone-a", "ftp://lb.example/mcp", "lb.example/mcp"):
        assert demo.get("/oauth/authorize", params={**base, "resource": bad}, follow_redirects=False).status_code == 400
    r = demo.get("/oauth/authorize", params={"client_id": cid, "response_type": "token"}, follow_redirects=False)
    assert r.status_code == 400
    # PKCE is not optional and only S256 counts
    r = demo.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "scope": RESOURCE,
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    r = demo.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "scope": RESOURCE,
            "code_challenge": challenge,
            "code_challenge_method": "plain",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_a_user_without_the_group_is_refused_at_consent(demo):
    cid = register(demo)
    _, challenge = pkce()
    make_user(demo, "out@x", "viewer", ["other"])
    with TestClient(demo.app) as out:
        login(out, "out@x", PW)
        assert authorize(out, cid, challenge).status_code == 403


def test_code_exchange_mints_a_token_the_node_will_accept(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_PUBLIC_URL", "https://console.example")
    cid = register(demo)
    verifier, challenge = pkce()
    code = consent(demo, cid, challenge)

    # the wrong verifier is refused and burns nothing that the right one could then use... except the code itself
    r = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "code_verifier": "wrong",
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    code = consent(demo, cid, challenge)
    r = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["scope"] == RESOURCE and 0 < tok["expires_in"] <= 3600
    assert r.headers["cache-control"] == "no-store"

    secret = demo.app.state.services.store  # the zone secret the deploy hands the worker
    group = __import__("asyncio").run(secret.get("groups", "demo"))
    claims = node_verify(tok["access_token"], group["session_secret"])
    assert claims["iss"] == "https://console.example"
    assert claims["aud"] == RESOURCE and claims["scope"] == RESOURCE
    assert claims["group"] == "demo" and claims["zone"] == "zone-a"
    assert claims["email"] == "root@ramen.local" and claims["sub"] and claims["jti"]
    assert claims["exp"] - claims["iat"] == 3600

    # a code is single use
    r = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

    # refresh rotates: the old refresh token dies, the new one works
    r = demo.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid}
    )
    assert r.status_code == 200, r.text
    tok2 = r.json()
    assert tok2["refresh_token"] != tok["refresh_token"] and tok2["access_token"] != tok["access_token"]
    r = demo.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid}
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # ...and a refresh token belongs to its client
    other = demo.post(
        "/api/v1/oauth/clients", json={"name": "Other", "redirect_uris": ["http://127.0.0.1:1/cb"]}
    ).json()["client_id"]
    r = demo.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok2["refresh_token"], "client_id": other}
    )
    assert r.status_code == 400
    audit = [a["action"] for a in demo.get("/api/v1/audit").json()]
    assert "oauth.token" in audit and "oauth.authorize" in audit


def test_revoking_the_user_kills_refresh_but_not_the_running_access_token(demo):
    cid = register(demo)
    u = make_user(demo, "ada@x", "group_admin", ["demo"])
    verifier, challenge = pkce()
    with TestClient(demo.app) as ada:
        login(ada, "ada@x", PW)
        code = consent(ada, cid, challenge)
    tok = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "code_verifier": verifier,
        },
    ).json()
    assert (
        demo.post(f"/api/v1/users/{u['id']}/password", json={"password": "N3wPassw0rd!-here"}).status_code == 200
    )  # epoch bump
    r = demo.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid}
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert demo.delete(f"/api/v1/users/{u['id']}").status_code == 200
    # the access token is a bearer with a 1 h life and no revocation list: the security how-to says so (§16.3)


def test_token_endpoint_rejects_what_the_spec_rejects(demo):
    cid = register(demo)
    assert (
        demo.post("/oauth/token", data={"grant_type": "password", "client_id": cid}).json()["error"]
        == "unsupported_grant_type"
    )
    r = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "nope",
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "code_verifier": "v",
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "nope",
            "client_id": "ghost",
            "redirect_uri": "x",
            "code_verifier": "v",
        },
    )
    assert r.status_code in (400, 401) and r.json()["error"] in ("invalid_client", "invalid_grant")
    assert demo.post("/oauth/token", data={}).status_code == 400


def test_deploy_hands_the_zone_the_session_secret_and_the_issuer(demo, monkeypatch, tmp_path):
    async def fake_sync(group, repo_url, ref, token):  # the fixture repo URL is not clonable; the deploy tests stub it
        (tmp_path / "buckets" / group).mkdir(parents=True, exist_ok=True)
        return str(tmp_path / "buckets" / group)

    handed: dict[str, dict] = {}
    real_deploy = demo.app.state.cloud.deploy

    async def spy_deploy(group, env, zone, canary=True, config=None, **kw):
        handed[zone] = dict(config or {})
        return await real_deploy(group, env, zone, canary, config, **kw)

    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", fake_sync)
    monkeypatch.setattr(demo.app.state.cloud, "deploy", spy_deploy)
    monkeypatch.setenv("RAMEN_PUBLIC_URL", "https://console.example/")
    r = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False})
    assert r.status_code == 202
    assert handed["zone-a"]["RAMEN_OAUTH_ISSUER"] == "https://console.example"
    assert handed["zone-a"]["RAMEN_PUBLIC_URL"] == "https://console.example"  # absolute resource_metadata (RFC 9728)
    secret = handed["zone-a"]["RAMEN_SESSION_SECRET"]
    assert len(secret) >= 32
    # the same secret in every zone of the group and on every deploy (§16.2: any pod verifies any session)
    assert handed["zone-b"]["RAMEN_SESSION_SECRET"] == secret
    handed.clear()
    demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False})
    assert handed["zone-a"]["RAMEN_SESSION_SECRET"] == secret
    # the local adapter keeps it out of the bucket file the runtime can read (review 0.5.0 L6) ...
    env_file = tmp_path / "buckets" / "demo" / ".ramen" / "env-zone-a"
    assert secret not in env_file.read_text() and "RAMEN_OAUTH_ISSUER" not in env_file.read_text()
    # ... and it is never returned by the API and never in a backup
    assert "session_secret" not in demo.get("/api/v1/groups/demo").text
    bid = demo.post("/api/v1/backups", json={"target": "local"}).json()["id"]
    assert secret not in demo.get(f"/api/v1/backups/{bid}/download").text
    # the local stack shares one value between the worker and the console through the environment
    monkeypatch.setenv("RAMEN_LOCAL_SESSION_SECRET", "compose-shared")
    handed.clear()
    demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False})
    assert handed["zone-a"]["RAMEN_SESSION_SECRET"] == "compose-shared"


def exchange(demo, cid, code, verifier):
    return demo.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "code_verifier": verifier,
        },
    )


def refresh(demo, cid, token):
    return demo.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": cid})


def test_refresh_reuse_revokes_the_whole_grant_and_tokens_are_hashed_at_rest(demo):
    """Security review 0.5.0 M3 and M5."""
    import asyncio

    cid = register(demo)
    verifier, challenge = pkce()
    code = consent(demo, cid, challenge)
    store = demo.app.state.services.store
    # neither the code nor a refresh token appears in the store as itself
    assert asyncio.run(store.get("oauth_codes", code)) is None
    assert asyncio.run(store.list("oauth_codes"))
    t1 = exchange(demo, cid, code, verifier).json()
    assert asyncio.run(store.get("oauth_refresh", t1["refresh_token"])) is None
    t2 = refresh(demo, cid, t1["refresh_token"]).json()
    t3 = refresh(demo, cid, t2["refresh_token"]).json()
    assert "refresh_token" in t3
    # the rotated-out t2 presented again: someone else holds the chain — t3 dies with it
    r = refresh(demo, cid, t2["refresh_token"])
    assert r.status_code == 400 and "reuse" in r.json()["error_description"]
    assert refresh(demo, cid, t3["refresh_token"]).status_code == 400
    assert asyncio.run(store.list("oauth_refresh")) == []
    # and a fresh grant is unaffected
    verifier, challenge = pkce()
    t4 = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert refresh(demo, cid, t4["refresh_token"]).status_code == 200


def test_every_mint_rechecks_that_the_user_is_still_allowed(demo):
    """Security review 0.5.0 M2: what was true at consent is checked again at exchange and at refresh."""
    cid = register(demo)
    u = make_user(demo, "bo@x", "viewer", ["demo"])
    verifier, challenge = pkce()
    with TestClient(demo.app) as bo:
        login(bo, "bo@x", PW)
        code = consent(bo, cid, challenge)
        # the group is taken away between consent and exchange
        assert demo.put(f"/api/v1/users/{u['id']}", json={"role": "viewer", "groups": ["other"]}).status_code == 200
        r = exchange(demo, cid, code, verifier)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        assert demo.put(f"/api/v1/users/{u['id']}", json={"role": "viewer", "groups": ["demo"]}).status_code == 200
        login(bo, "bo@x", PW)  # the role change moved the epoch; a new consent is needed
        verifier, challenge = pkce()
        tok = exchange(demo, cid, consent(bo, cid, challenge), verifier).json()
    assert demo.put(f"/api/v1/users/{u['id']}", json={"role": "viewer", "groups": ["other"]}).status_code == 200
    assert refresh(demo, cid, tok["refresh_token"]).status_code == 400


def test_api_keys_cannot_authorize_and_loopback_clients_may_pick_a_port(demo):
    """Security review 0.5.0 I9 and I7."""
    key = demo.post("/api/v1/api-keys", json={"name": "ci"}).json()["key"]
    cid = register(demo)
    _, challenge = pkce()
    with TestClient(demo.app) as anon:
        r = anon.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CLIENT["redirect_uris"][0],
                "scope": RESOURCE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            headers={"X-Ramen-Api-Key": key},
            follow_redirects=False,
        )
        assert r.status_code == 403
    # RFC 8252 §7.3: a native app registered on http://127.0.0.1:9999/callback may come back on any port
    assert authorize(demo, cid, challenge, redirect="http://127.0.0.1:43111/callback").status_code == 200
    assert authorize(demo, cid, challenge, redirect="http://127.0.0.1:43111/other").status_code == 400
    # ...and on any loopback name (0.5.95): localhost, 127.0.0.1 and ::1 are one interface
    assert authorize(demo, cid, challenge, redirect="http://localhost:43111/callback").status_code == 200
    assert authorize(demo, cid, challenge, redirect="http://[::1]:43111/callback").status_code == 200
    assert authorize(demo, cid, challenge, redirect="http://10.0.0.5:43111/callback").status_code == 400
    assert authorize(demo, cid, challenge, redirect="https://127.0.0.1:43111/callback").status_code == 400
    # a redirect URI that already carries a query keeps it
    other = demo.post(
        "/api/v1/oauth/clients", json={"name": "Q", "redirect_uris": ["http://localhost:1/cb?app=1"]}
    ).json()["client_id"]
    r = demo.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": other,
            "redirect_uri": "http://localhost:7777/cb?app=1",
            "scope": RESOURCE,
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "decision": "deny",
            "csrf_token": demo.cookies.get("ramen_csrf", ""),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q == {"app": ["1"], "state": ["s"], "error": ["access_denied"]}
