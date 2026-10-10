"""CONTRACTS §21.3 + §21.4 (0.7.5, D47/D48) end to end on real processes: a fake Entra ID and a fake Google (OIDC),
a fake Microsoft Graph (`/v1.0/me/memberOf`, paged) and a fake Cloud Identity (`searchDirectGroups`), a real
console with both providers on `lookup`, a real node sharing the console's secret.

The chain under test (§21.5-2/3): IdP group → role-mapping rule → custom role `analyst` (`oauth_only`) → the
person's console token carries `role: analyst` → the worker lists/calls per a tool-access entry naming `analyst`,
over Streamable HTTP and through `ramen-mcp-bridge --oauth`; the same person's password login is 403; removed from
the IdP group, the membership is gone at the next login unless an admin set one by hand; a Graph failure is a 502
and nobody is signed in."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from ramen_tests import env as E
from ramen_tests.console import Console
from ramen_tests.localconsole import LocalConsole, pkce
from ramen_tests.localnode import LocalNode, free_port
from ramen_tests.mcp_client import JsonRpcError, bridge_command, bridge_session, sdk_text, text_of
from ramen_tests.tokens import claims_of

pytestmark = pytest.mark.conformance
CALC = "demo_calculator_tool"
ARGS = {"var1": 2, "var2": 3, "func": "add"}
GID_ANALYSTS, GID_OTHER, GID_PAGE2 = "0b7f-analysts", "9c1e-other", "2d44-page-two"
G_ANALYSTS = "analysts@corp.test"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})
ANA, GAL, BOB = "ana@corp.test", "gal@corp.test", "bob@corp.test"
REDIRECT = "http://127.0.0.1:9999/callback"


class FakeIdp:
    """One HTTP server playing Entra ID (`/entra/*`), Google (`/google/*`), Microsoft Graph (`/graph/*`) and Cloud
    Identity (`/ci/*`). The test sets `login_as` and `nonce[provider]` before driving the console's callback."""

    def __init__(self):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.users: dict[str, dict] = {}  # email → {"groups": [...], "google_groups": [...]}
        self.nonce: dict[str, str] = {}
        self.login_as = ""
        self.graph_calls: list[dict] = []
        self.ci_calls: list[str] = []
        self.authorize_scopes: dict[str, str] = {}
        self.fail_graph_for: set[str] = set()
        self.tokens: dict[str, str] = {}  # access token → email
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, code: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urlsplit(self.path)
                parts = u.path.strip("/").split("/")
                provider = parts[0]
                if provider in ("entra", "google") and u.path.endswith("/.well-known/openid-configuration"):
                    iss = f"{fake.base}/{provider}"
                    return self._json(
                        200,
                        {
                            "issuer": iss,
                            "authorization_endpoint": f"{iss}/authorize",
                            "token_endpoint": f"{iss}/token",
                            "userinfo_endpoint": f"{iss}/userinfo",
                            "jwks_uri": f"{iss}/jwks",
                            "id_token_signing_alg_values_supported": ["RS256"],
                        },
                    )
                if provider in ("entra", "google") and parts[-1] == "jwks":
                    return self._json(200, {"keys": [KEY.as_dict(private=False)]})
                if provider in ("entra", "google") and parts[-1] == "userinfo":
                    email = fake.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "), "")
                    return self._json(200, {"sub": f"sub-{email}", "email": email, "email_verified": True})
                if u.path == "/graph/v1.0/me/memberOf":
                    email = fake.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "), "")
                    fake.graph_calls.append({"email": email, "query": u.query})
                    if email in fake.fail_graph_for:
                        return self._json(500, {"error": {"code": "InternalServerError"}})
                    groups = fake.users.get(email, {}).get("groups", [])
                    q = parse_qs(u.query)
                    page = q.get("page", ["1"])[0]
                    if page == "1":
                        body = {
                            "value": [
                                {"@odata.type": "#microsoft.graph.group", "id": g, "displayName": f"name of {g}"}
                                for g in groups[:1]
                            ]
                        }
                        if len(groups) > 1:
                            body["@odata.nextLink"] = f"{fake.base}/graph/v1.0/me/memberOf?page=2"
                        return self._json(200, body)
                    return self._json(
                        200,
                        {
                            "value": [
                                {"@odata.type": "#microsoft.graph.group", "id": g, "displayName": f"name of {g}"}
                                for g in groups[1:]
                            ]
                        },
                    )
                if u.path == "/ci/v1/groups/-/memberships:searchDirectGroups":
                    email = fake.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "), "")
                    fake.ci_calls.append(u.query)
                    groups = fake.users.get(email, {}).get("google_groups", [])
                    return self._json(
                        200,
                        {
                            "memberships": [
                                {"group": f"groups/{i}", "groupKey": {"id": g}, "displayName": g.split("@")[0]}
                                for i, g in enumerate(groups)
                            ]
                        },
                    )
                self._json(404, {"error": "not found", "path": self.path})

            def do_POST(self):
                u = urlsplit(self.path)
                parts = u.path.strip("/").split("/")
                provider = parts[0]
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                if provider in ("entra", "google") and parts[-1] == "token":
                    if form.get("code") != ["good-code"]:
                        return self._json(400, {"error": "invalid_grant"})
                    email = fake.login_as
                    at = f"at-{provider}-{email}-{time.time_ns()}"
                    fake.tokens[at] = email
                    now = int(time.time())
                    claims = {
                        "iss": f"{fake.base}/{provider}",
                        "sub": f"sub-{email}",
                        "aud": f"{provider}-cid",
                        "iat": now,
                        "exp": now + 300,
                        "nonce": fake.nonce.get(provider),
                        "email": email,
                        "email_verified": True,
                    }
                    if provider == "google":
                        claims["hd"] = "corp.test"
                    body = {
                        "access_token": at,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY),
                    }
                    return self._json(200, body)
                self._json(404, {"error": "not found", "path": self.path})

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()


def sso_login(con: LocalConsole, fake: FakeIdp, provider: str, email: str) -> tuple[Console, object]:
    """Drive the console's `/auth/<provider>/login` → fake IdP → `/auth/<provider>/callback` as a browser would.
    → (a Console client holding whatever cookies the callback set, the callback response)."""
    c = Console(con.url)
    r = c.http.get(f"/auth/{provider}/login", follow_redirects=False)
    assert r.status_code in (302, 303), f"{provider} login: {r.status_code} {r.text[:200]}"
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(f"{fake.base}/{provider}/authorize?"), r.headers["location"]
    fake.nonce[provider] = q["nonce"][0]
    fake.authorize_scopes[provider] = q.get("scope", [""])[0]
    fake.login_as = email
    cb = c.http.get(
        f"/auth/{provider}/callback", params={"code": "good-code", "state": q["state"][0]}, follow_redirects=False
    )
    return c, cb


def me(c: Console) -> dict:
    r = c.http.get("/api/v1/me")
    assert r.status_code == 200, f"/me {r.status_code}: {r.text[:200]}"
    return r.json()


def mint_as(con: LocalConsole, user: Console, client_id: str, group="demo", zone="local"):
    """LocalConsole.mint, but as `user` instead of the admin → the token response, or the consent page's status."""
    verifier, challenge = pkce()
    scope = f"mcp:{group}:{zone}"
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": scope,
    }
    page = user.http.get(f"{con.url}/oauth/authorize", params=q, follow_redirects=False)
    if page.status_code != 200:
        return page.status_code
    r = user.http.post(
        f"{con.url}/oauth/authorize",
        data={**q, "decision": "allow", "csrf_token": user.http.cookies.get("ramen_csrf", "")},
        follow_redirects=False,
    )
    if r.status_code != 303:
        return r.status_code
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    return con.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }
    )


@pytest.fixture(scope="module")
def world():
    with FakeIdp() as fake:
        fake.users[ANA] = {"groups": [GID_ANALYSTS, GID_OTHER, GID_PAGE2]}
        fake.users[GAL] = {"google_groups": [G_ANALYSTS]}
        fake.users[BOB] = {"groups": [GID_ANALYSTS]}
        env = {
            "RAMEN_OAUTH_ENTRA_ISSUER": f"{fake.base}/entra",
            "RAMEN_OAUTH_ENTRA_CLIENT_ID": "entra-cid",
            "RAMEN_OAUTH_ENTRA_CLIENT_SECRET": "entra-sec",
            "RAMEN_OAUTH_ENTRA_GROUPS": "lookup",
            "RAMEN_OAUTH_ENTRA_GROUPS_URL": f"{fake.base}/graph/v1.0",  # the base includes the API version
            "RAMEN_OAUTH_GOOGLE_ISSUER": f"{fake.base}/google",
            "RAMEN_OAUTH_GOOGLE_CLIENT_ID": "google-cid",
            "RAMEN_OAUTH_GOOGLE_CLIENT_SECRET": "google-sec",
            "RAMEN_OAUTH_GOOGLE_GROUPS": "lookup",
            "RAMEN_OAUTH_GOOGLE_GROUPS_URL": f"{fake.base}/ci/v1",
        }
        with LocalConsole(env=env) as con:
            con.seed_group()
            cid = con.register_client()
            node_env = {
                "RAMEN_SESSION_SECRET": con.session_secret,
                "RAMEN_OAUTH_ISSUER": con.url,
                "RAMEN_ROLES": json.dumps({"analyst": "viewer"}, separators=(",", ":")),
                "RAMEN_TOOL_ACCESS": json.dumps(
                    {CALC: {"list": ["analyst"], "call": ["analyst"]}}, separators=(",", ":")
                ),
            }
            with LocalNode(bucket=E.FIXTURES / "demo_group", env=node_env) as n:
                n.wait_serving()
                yield fake, con, n, cid


@pytest.fixture(scope="module")
def configured(world):
    """The custom role and the two mapping rules, through the console's API (xfail while the ui-agent builds them)."""
    fake, con, _, _ = world
    a = con.admin.http
    r = a.put(f"{con.url}/api/v1/config/roles/analyst", json={"base": "viewer", "label": "Analyst", "oauth_only": True})
    if r.status_code in (404, 405):
        pytest.xfail(f"waiting for ui-agent (§21.4): PUT /api/v1/config/roles answers {r.status_code}")
    assert r.status_code in (200, 201), r.text
    for provider, value in (("entra", GID_ANALYSTS), ("google", G_ANALYSTS)):
        r = a.put(f"{con.url}/api/v1/config/auth/role-map/{provider}", json={"claim": "groups"})
        assert r.status_code in (200, 204), r.text
        r = a.put(
            f"{con.url}/api/v1/config/auth/role-map/{provider}",
            json={"value": value, "role": "analyst", "groups": ["demo"]},
        )
        if r.status_code == 422:
            pytest.xfail("waiting for ui-agent (§21.4): role-mapping rules refuse a custom role name")
        assert r.status_code in (200, 204), r.text
    probe, cb = sso_login(con, fake, "entra", ANA)  # probe: does the console look the groups up at all?
    if cb.status_code == 303 and not fake.graph_calls and not me(probe).get("memberships"):
        pytest.xfail("waiting for ui-agent (§21.3): the console never asked Graph for the person's groups")
    return world


def test_both_providers_are_offered_on_the_login_page(world):
    _, con, _, _ = world
    page = con.admin.http.get(f"{con.url}/login").text
    assert "/auth/entra/login" in page and "/auth/google/login" in page, "any IdP on any adapter (here: local)"


def test_entra_groups_are_looked_up_in_graph_and_mapped_to_the_custom_role(configured):
    fake, con, _, _ = configured
    user, cb = sso_login(con, fake, "entra", ANA)
    assert cb.status_code == 303 and user.http.cookies.get("ramen_session"), f"{cb.status_code} {cb.text[:300]}"
    info = me(user)
    if not fake.graph_calls and not info.get("memberships"):
        pytest.xfail("waiting for ui-agent (§21.3): the console never asked Graph for the person's groups")
    assert [c["email"] for c in fake.graph_calls][-2:] == [ANA, ANA], "two pages: @odata.nextLink was followed"
    assert info["memberships"] == {"demo": "analyst"}, info
    assert "User.Read" in fake.authorize_scopes["entra"], fake.authorize_scopes
    users = {u["email"]: u for u in con.admin.get("users").json()}
    assert users[ANA]["provider"] == "entra"


def test_google_groups_are_looked_up_in_cloud_identity(configured):
    fake, con, _, _ = configured
    user, cb = sso_login(con, fake, "google", GAL)
    assert cb.status_code == 303, f"{cb.status_code} {cb.text[:300]}"
    info = me(user)
    assert fake.ci_calls and GAL in unquote(fake.ci_calls[-1]), fake.ci_calls
    assert info["memberships"] == {"demo": "analyst"}, info
    assert "cloud-identity.groups.readonly" in fake.authorize_scopes["google"], fake.authorize_scopes


def test_the_token_carries_the_custom_role_and_the_worker_enforces_it_over_http(configured):
    fake, con, node, cid = configured
    user, _ = sso_login(con, fake, "entra", ANA)
    tok = mint_as(con, user, cid)
    assert isinstance(tok, dict), f"consent refused: {tok}"
    claims = claims_of(tok["access_token"])
    assert claims["role"] == "analyst", claims
    c = node.http_client(key=tok["access_token"])
    c.initialize()
    assert {t["name"] for t in c.list_tools()} == {CALC}
    assert float(text_of(c.call_tool(CALC, ARGS))) == 5
    # the same person through gRPC
    g = node.client(key=tok["access_token"])
    g.initialize()
    assert float(text_of(g.call_tool(CALC, ARGS))) == 5


@pytest.mark.skipif(not bridge_command(), reason="ramen-mcp-bridge not found")
async def test_the_bridge_with_oauth_carries_the_custom_role_to_the_worker(configured, tmp_path):
    fake, con, node, cid = configured
    user, _ = sso_login(con, fake, "entra", ANA)
    tok = mint_as(con, user, cid)
    assert isinstance(tok, dict), f"consent refused: {tok}"
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({**tok, "expires_at": time.time() + tok["expires_in"]}))
    args = [
        "--target", node.target, "--group", "demo", "--zone", "local", "--insecure",
        "--oauth", con.url, "--client-id", cid, "--token-file", str(path), "--no-browser",
    ]  # fmt: skip
    err = open(tmp_path / "bridge.err", "w+")  # noqa: SIM115 - Popen needs a real fileno
    async with asyncio.timeout(90):
        async with bridge_session(node.node, args=args, timeout=30, errlog=err) as s:
            assert {t.name for t in (await s.list_tools()).tools} == {CALC}
            r = await s.call_tool(CALC, ARGS)
            assert not r.is_error and float(sdk_text(r)) == 5
    err.close()
    assert "oauth/authorize" not in (tmp_path / "bridge.err").read_text()


def test_password_login_is_refused_for_a_person_holding_an_oauth_only_role(configured):
    """A password account that an admin puts into an `oauth_only` role (the manual side) can no longer use it."""
    _, con, _, _ = configured
    pw = "Epoch-9!long-enough-pw"
    r = con.admin.create_user("pw@corp.test", pw, "viewer", ["demo"])
    assert r.status_code == 201, r.text
    assert Console(con.url).login("pw@corp.test", pw).status_code == 303, "a plain viewer signs in with a password"
    r = con.admin.http.put(f"{con.url}/api/v1/groups/demo/members/{r.json()['id']}", json={"role": "analyst"})
    assert r.status_code in (200, 204), r.text
    r = Console(con.url).login("pw@corp.test", pw)
    if r.status_code == 303:
        pytest.xfail("waiting for ui-agent (§21.4): oauth_only is not enforced on /login")
    assert r.status_code == 403 and "entra" in r.text.lower(), f"{r.status_code} {r.text[:300]}"


def test_removed_from_the_idp_group_the_membership_is_gone_at_the_next_login(configured):
    fake, con, node, cid = configured
    fake.users[ANA]["groups"] = [GID_OTHER]
    try:
        user, cb = sso_login(con, fake, "entra", ANA)
        assert cb.status_code == 303, cb.text[:300]
        assert me(user).get("memberships") in ({}, None), me(user)
        tok = mint_as(con, user, cid)
        if isinstance(tok, dict):  # minted anyway: the token must not carry the role, and the worker must deny
            assert claims_of(tok["access_token"]).get("role") != "analyst"
            c = node.http_client(key=tok["access_token"])
            c.initialize()
            with pytest.raises(JsonRpcError) as e:
                c.call_tool(CALC, ARGS)
            assert e.value.code == -32601
        else:
            assert tok == 403, f"consent for a group the person no longer belongs to: {tok}"
    finally:
        fake.users[ANA]["groups"] = [GID_ANALYSTS, GID_OTHER, GID_PAGE2]


def test_an_admin_entry_survives_removal_from_the_idp_group(configured):
    fake, con, _, _ = configured
    users = {u["email"]: u for u in con.admin.get("users").json()}
    uid = users[ANA]["id"]
    r = con.admin.http.put(f"{con.url}/api/v1/groups/demo/members/{uid}", json={"role": "viewer"})
    assert r.status_code in (200, 204), r.text
    fake.users[ANA]["groups"] = []
    try:
        user, cb = sso_login(con, fake, "entra", ANA)
        assert cb.status_code == 303, cb.text[:300]
        assert me(user)["memberships"] == {"demo": "viewer"}, "membership from either side: the admin's entry stands"
    finally:
        fake.users[ANA]["groups"] = [GID_ANALYSTS, GID_OTHER, GID_PAGE2]
    r = con.admin.http.delete(f"{con.url}/api/v1/groups/demo/members/{uid}")
    assert r.status_code in (200, 204), r.text
    user, _ = sso_login(con, fake, "entra", ANA)
    assert me(user)["memberships"] == {"demo": "analyst"}, "admin entry removed → the IdP role is back"


def test_a_graph_failure_is_a_502_and_nobody_is_signed_in(configured):
    fake, con, _, _ = configured
    fake.fail_graph_for.add(BOB)
    try:
        user, cb = sso_login(con, fake, "entra", BOB)
    finally:
        fake.fail_graph_for.discard(BOB)
    assert cb.status_code == 502 and "groups" in cb.text.lower(), f"{cb.status_code} {cb.text[:300]}"
    assert not user.http.cookies.get("ramen_session")
