"""CONTRACTS §24.4 (v1.0.0): every sign-in path, walked against a live deployment, one recorded verdict per path.

The run's output ends with an `auth matrix` section (and `RAMEN_AUTH_MATRIX_OUT=<file>` writes it as a table), so a
cloud pass can show at a glance that each way in was exercised on that cloud:

  console password · console API key (devops) at the console and refused by a worker · MCP key (agent) at a worker
  over HTTP and gRPC and refused by the console API · a person's OAuth token over Streamable HTTP on both protocol
  eras · the same through the stdio bridge (`RAMEN_BRIDGE_CMD`) · refresh rotation · an OAuth-only role refusing a
  password · a garbage / expired token → 401 with the RFC 9728 challenge · the IP allow-list refusal
  (`RAMEN_EXPECT_CIDR_DENIED=1`) · an SSO provider on the login page (`RAMEN_EXPECT_SSO=<name>[,<name>]`).

The browser half of an SSO sign-in (Entra with group lookup, Google Workspace) is done by a person; here only what an
HTTP client can see is asserted: the redirect to the provider with the right `redirect_uri`, `client_id` and scopes.
The fine print of each path lives where it always did and is not repeated: role × group × key type in
`conformance/test_security_matrix.py`, custom roles / tool access / the bridge in `test_release_features.py` (whose
`mint_as` and `bridge_oauth_args` are used here), expiry and refresh in `conformance/test_auth_expiry.py`, the
allow-list in `test_ip_rules.py` and `test_forwarded_for.py`, federation in `conformance/test_identity_federation.py`.

Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL (the edge) and the admin credentials; RAMEN_E2E_* name the group/env/zone."""

import base64
import json
import time
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import grpc
import httpx
import pytest
from _pytest.outcomes import Skipped

from ramen_tests import env as E
from ramen_tests.console import Console
from ramen_tests.localconsole import REDIRECT
from ramen_tests.mcp_client import HttpNode, Node, bridge_command, bridge_session, sdk_text, text_of
from ramen_tests.modern import MODERN, Modern
from ramen_tests.tokens import claims_of

from .conftest import AUTH_MATRIX, ENV, GROUP, PING, ZONE, ok
from .test_release_features import bridge_oauth_args, mint_as

pytestmark = pytest.mark.cloud
S = grpc.StatusCode
CALC = "demo_calculator_tool"
ADD = {"var1": 2, "var2": 3, "func": "add"}
TRANSIENT = (S.UNAVAILABLE, S.DEADLINE_EXCEEDED)
SCOPE = f"mcp:{GROUP}:{ZONE}"
CONSENT = "ramen_telemetry_consent"


@contextmanager
def path(name: str):
    """Record the verdict of one sign-in path. The assertion error (or skip) still reaches pytest."""
    try:
        yield
    except Skipped as e:
        AUTH_MATRIX.append((name, "SKIP", str(e)[:120]))
        raise
    except BaseException as e:
        AUTH_MATRIX.append((name, "FAIL", f"{type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}"))
        raise
    AUTH_MATRIX.append((name, "PASS", ""))


def settled(fn):
    """`fn()` → a gRPC status, retried while the edge answers UNAVAILABLE (pods draining says nothing about a key)."""
    code = None
    for attempt in range(6):
        code = fn()
        if code not in TRANSIENT:
            break
        time.sleep(5 * (attempt + 1))
    return code


def grpc_node(key: str | None) -> Node:
    return Node.from_env(key, group=GROUP, zone=ZONE)


def http_node(key: str | None, timeout: float = 60) -> HttpNode:
    return HttpNode.from_env(key, group=GROUP, zone=ZONE, timeout=timeout)


def served(result: dict) -> bool:
    """A `tools/call` that authenticated: its answer, or the telemetry-consent question (§24.3) — which is only ever
    put to a caller the worker accepted."""
    if result.get("resultType") == "input_required":
        return CONSENT in result.get("inputRequests", {})
    return result.get("isError") is not True and float(text_of(result)) == 5


@pytest.fixture(scope="module")
def people(admin, console_url, world, suffix):
    """A password user with a custom role, an OAuth-only user, both key types, an OAuth client — and a deploy when
    the worker does not know the new MCP key yet."""
    E.require("RAMEN_NODE_URL")
    base = console_url.rstrip("/")
    ok(admin.http.put(f"{base}/api/v1/config/roles/analyst", json={"base": "viewer", "label": "Analyst"}), 200)
    ok(admin.http.put(f"{base}/api/v1/config/roles/analyst_sso", json={"base": "viewer", "oauth_only": True}), 200)
    pw = f"Am-{uuid.uuid4().hex[:12]}-Aa1!"
    person, sso = f"am-person-{suffix}@ramen.local", f"am-sso-{suffix}@ramen.local"
    for email, role in ((person, "analyst"), (sso, "analyst_sso")):
        r = admin.http.post(f"{base}/api/v1/users", json={"email": email, "password": pw, "memberships": {GROUP: role}})
        ok(r, 201)
    devops = ok(admin.create_api_key(f"am-devops-{suffix}", groups=[GROUP], role="viewer", client_type="devops"), 201)
    mcp = ok(admin.create_mcp_key(GROUP, f"am-agent-{suffix}"), 201).json()["key"]
    cid = ok(
        admin.http.post(f"{base}/api/v1/oauth/clients", json={"name": f"am-{suffix}", "redirect_uris": [REDIRECT]}), 201
    ).json()["client_id"]
    with grpc_node(mcp) as n:
        if settled(lambda: n.status(PING)) == S.UNAUTHENTICATED:  # keys reach a worker with a deploy
            job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=True), 202).json()["id"], timeout=1800)
            assert job["status"] == "ok", {k: job.get(k) for k in ("status", "error")}
    out = {"base": base, "pw": pw, "person": person, "sso": sso, "devops": devops.json()["key"], "mcp": mcp, "cid": cid}
    c = Console(base)
    assert c.login(person, pw).status_code in (200, 303)
    out["person_console"] = c
    consent = "telemetry_consent" in c.http.get("/oauth/authorize", params=authorize_query(cid)).text
    out["tokens"] = mint_as(c, cid, SCOPE, form={"telemetry_consent": "1"} if consent else None)
    yield out
    c.close()


def authorize_query(cid: str) -> dict:
    return {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "scope": SCOPE,
        "state": "am",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",  # RFC 7636 appendix B; never exchanged
        "code_challenge_method": "S256",
        "resource": SCOPE,
    }


# --- the console ------------------------------------------------------------------------------------------------------
def test_console_password_sign_in(people, admin_creds):
    with path("console: password sign-in (bootstrap admin)"):
        with Console(people["base"]) as c:
            assert c.login(*admin_creds).status_code in (200, 303)
            me = ok(c.me()).json()
            assert me.get("role") == "super_admin" and me.get("kind", "session") != "apikey", me
    with path("console: password sign-in (custom-role user)"):
        me = ok(people["person_console"].me()).json()
        assert me.get("email", me.get("name")) == people["person"], me
    with path("console: wrong password refused"):
        with Console(people["base"]) as c:
            r = c.login(people["person"], people["pw"] + "x")
            assert r.status_code in (401, 403) and c.me().status_code == 401, r.status_code


def test_console_api_key_is_for_the_console_only(people):
    with path("console API key (devops): accepted by the console API"):
        with Console(people["base"], api_key=people["devops"]) as c:
            assert ok(c.me()).json().get("kind") == "apikey"
            ok(c.get("group", group=GROUP))
    with path("console API key (devops): refused by a worker over gRPC"):
        with grpc_node(people["devops"]) as n:
            code = settled(lambda: n.status(PING))
        assert code == S.UNAUTHENTICATED, f"a worker accepted a console key: {code.name}"
    with path("console API key (devops): refused by a worker over HTTP"):
        with http_node(people["devops"]) as h:
            r = h.post(PING)
        assert r.status_code == 401 and "WWW-Authenticate" in r.headers, r.status_code


def test_mcp_key_is_for_the_workers_only(people):
    with path("MCP key (agent): accepted by a worker over gRPC"):
        with grpc_node(people["mcp"]) as n:
            assert settled(lambda: n.status(PING)) == S.OK
            n.initialize()
            assert CALC in {t["name"] for t in n.list_tools()}
            assert served(n.request("tools/call", {"name": CALC, "arguments": ADD}))
    with path("MCP key (agent): accepted by a worker over Streamable HTTP (2025-06-18)"):
        with http_node(people["mcp"]) as h:
            h.initialize()
            assert h.session_id and CALC in {t["name"] for t in h.list_tools()}
    with path("MCP key (agent): accepted by a worker over Streamable HTTP (2026-07-28)"):
        with http_node(people["mcp"]) as h:
            m = Modern(h, {"elicitation": {"form": {}}})
            assert m.discover()["supportedVersions"][0] == MODERN
            assert served(m.call(CALC, ADD))
    with path("MCP key (agent): refused by the console API"):
        with Console(people["base"], api_key=people["mcp"]) as c:
            r = c.me()
            assert r.status_code in (401, 403), f"the console API accepted an MCP key: {r.status_code}"
            assert c.get("group", group=GROUP).status_code in (401, 403)


def test_no_credential_is_refused_everywhere(people):
    with path("no credential: console API 401, worker 401 / UNAUTHENTICATED"):
        with Console(people["base"]) as c:
            assert c.me().status_code == 401
        with grpc_node(None) as n:
            assert settled(lambda: n.status(PING)) == S.UNAUTHENTICATED
        with http_node(None) as h:
            assert h.post(PING).status_code == 401


# --- a person's OAuth token -------------------------------------------------------------------------------------------
def test_a_persons_oauth_token_over_streamable_http_on_both_eras(people):
    token = people["tokens"]["access_token"]
    claims = claims_of(token)
    with path("OAuth: authorization code + PKCE mints a token for the person"):
        assert (
            claims.get("email", claims.get("sub")) and claims.get("scope") == SCOPE and claims.get("role") == "analyst"
        )
    with path("OAuth token: Streamable HTTP, 2025-06-18 (initialize + session)"):
        with http_node(token) as h:
            h.initialize()
            assert h.session_id and CALC in {t["name"] for t in h.list_tools()}
            assert served(h.call_tool(CALC, ADD))
    with path("OAuth token: Streamable HTTP, 2026-07-28 (no handshake)"):
        with http_node(token) as h:
            m = Modern(h, {"elicitation": {"form": {}}})
            assert CALC in {t["name"] for t in m.request("tools/list")["tools"]}
            assert served(m.call(CALC, ADD))
    with path("OAuth token: gRPC"):
        with grpc_node(token) as n:
            assert settled(lambda: n.status(PING)) == S.OK
    with path("OAuth token: refused by the console API (it is a worker credential)"):
        r = httpx.get(
            f"{people['base']}/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},
            verify=E.tls_verify(),
            timeout=30,
        )
        assert r.status_code == 401, r.status_code


async def test_the_same_person_through_the_stdio_bridge(people, tmp_path):
    with path("OAuth token: through the stdio bridge (RAMEN_BRIDGE_CMD)"):
        if not bridge_command():
            pytest.skip("ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH)")
        node = grpc_node(people["mcp"])
        args = bridge_oauth_args(node, people["base"], people["cid"], people["tokens"], tmp_path)
        try:
            async with bridge_session(node, args=args) as s:
                assert CALC in {t.name for t in (await s.list_tools()).tools}
                r = await s.call_tool(CALC, ADD)
                assert not r.is_error and float(sdk_text(r)) == 5
        finally:
            node.close()
        kept = json.loads((tmp_path / "tokens.json").read_text())
        people["tokens"].update({k: kept[k] for k in ("access_token", "refresh_token") if k in kept})


async def test_the_mcp_key_through_the_stdio_bridge(people):
    with path("MCP key (agent): through the stdio bridge"):
        if not bridge_command():
            pytest.skip("ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH)")
        node = grpc_node(people["mcp"])
        try:
            async with bridge_session(node) as s:
                assert CALC in {t.name for t in (await s.list_tools()).tools}
        finally:
            node.close()


def test_refresh_rotation(people):
    with path("OAuth: refresh rotates, the new token works at the worker, the old refresh token is dead"):
        old = dict(people["tokens"])
        form = {"grant_type": "refresh_token", "refresh_token": old["refresh_token"], "client_id": people["cid"]}
        r = httpx.post(f"{people['base']}/oauth/token", data=form, verify=E.tls_verify(), timeout=60)
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        fresh = r.json()
        assert fresh["refresh_token"] != old["refresh_token"] and fresh["access_token"] != old["access_token"]
        assert claims_of(fresh["access_token"])["sub"] == claims_of(old["access_token"])["sub"]
        with http_node(fresh["access_token"]) as h:
            assert h.status(PING) == 200
        again = httpx.post(f"{people['base']}/oauth/token", data=form, verify=E.tls_verify(), timeout=60)
        assert again.status_code == 400 and again.json().get("error") == "invalid_grant", again.text[:200]


def test_an_oauth_only_role_refuses_a_password(people):
    with path("OAuth-only role: password sign-in refused"):
        with Console(people["base"]) as c:
            r = c.login(people["sso"], people["pw"])
            assert r.status_code == 403, f"{r.status_code} {r.text[:200]}"
            assert c.me().status_code == 401, "no session came of it"


# --- tokens that are not tokens ---------------------------------------------------------------------------------------
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def forged(real: str, **claims) -> str:
    """The real token's header and claims with `claims` changed, under the old signature."""
    head, body, sig = real.split(".")
    return f"{head}.{b64url(json.dumps({**claims_of(real), **claims}).encode())}.{sig}"


def test_a_garbage_or_expired_token_gets_the_rfc_9728_challenge(people):
    real = people["tokens"]["access_token"]
    bad = {
        "garbage": "not-a-token",
        "three random parts": "aaaa.bbbb.cccc",
        "expired (exp in the past, old signature)": forged(real, exp=int(time.time()) - 3600),
        "another person (sub changed, old signature)": forged(real, sub="someone-else"),
        "signature cut": real.rsplit(".", 1)[0] + ".AAAA",
    }
    with path("bad token: 401 with the RFC 9728 challenge over HTTP"):
        with http_node(None) as h:
            anon = h.post(PING)
            assert anon.status_code == 401
            for what, token in bad.items():
                r = h.post(PING, key=token)
                assert r.status_code == 401, f"{what}: {r.status_code}"
                challenge = r.headers.get("WWW-Authenticate", "")
                assert challenge.startswith("Bearer ") and "resource_metadata=" in challenge, f"{what}: {challenge!r}"
                assert challenge == anon.headers["WWW-Authenticate"], f"{what}: a different challenge than no token"
                assert token not in r.text
            url = anon.headers["WWW-Authenticate"].split('resource_metadata="', 1)[1].split('"', 1)[0]
            assert (
                url.endswith("/.well-known/oauth-protected-resource") or "/.well-known/oauth-protected-resource" in url
            )
            # the metadata is the zone's: like every call to a shared edge it carries the routing headers
            meta = httpx.get(url, headers=dict(E.routing_metadata()), verify=E.tls_verify(), timeout=30)
            assert meta.status_code == 200, f"{url}: {meta.status_code}"
            doc = meta.json()
            assert doc.get("authorization_servers") and doc.get("resource"), doc
            issuer = doc["authorization_servers"][0].rstrip("/")
            server = httpx.get(f"{issuer}/.well-known/oauth-authorization-server", verify=E.tls_verify(), timeout=30)
            assert server.status_code == 200 and server.json().get("token_endpoint"), server.status_code
    with path("bad token: UNAUTHENTICATED over gRPC"):
        with grpc_node(None) as n:
            for what, token in bad.items():
                code = settled(lambda token=token: n.status(PING, key=token))
                assert code == S.UNAUTHENTICATED, f"{what}: {code.name}"


def test_a_token_for_another_zone_is_refused(people):
    with path("OAuth token: audience bound to its group and zone"):
        other = forged(people["tokens"]["access_token"], aud="mcp:other:zone", scope="mcp:other:zone")
        with http_node(other) as h:
            assert h.post(PING).status_code == 401


def edge_health(n) -> str:
    try:
        return n.health()
    except grpc.RpcError as e:
        return e.code().name


# --- the address ------------------------------------------------------------------------------------------------------
def test_the_ip_allow_list_refuses_this_address(console_url):
    with path("IP allow-list: a caller outside it is refused before its credential is read"):
        if E.env("RAMEN_EXPECT_CIDR_DENIED") != "1":
            pytest.skip("RAMEN_EXPECT_CIDR_DENIED not set (a zone deployed with an allow-list that excludes this host)")
        key = E.env("RAMEN_MCP_KEY")
        with grpc_node(key) as n:
            assert settled(lambda: n.status(PING)) == S.PERMISSION_DENIED
            # the worker itself keeps health open to everyone; an edge firewall (Envoy's policy on AKS) closes the
            # whole route to an address outside the list, health included — either is a refusal of this caller
            assert edge_health(n) in ("SERVING", "PERMISSION_DENIED"), (
                "health: open at the worker, or closed by the edge"
            )
        with http_node(key) as h:
            assert h.post(PING).status_code == 403
        with http_node(None) as h:
            assert h.post(PING).status_code == 403, "the address is checked before the credential"


# --- SSO --------------------------------------------------------------------------------------------------------------
SCOPES = {
    "entra": {"openid", "email", "profile", "User.Read"},  # group lookup through Graph (§21.3)
    "google": {"openid", "email", "profile"},
}


@pytest.mark.parametrize("provider", [p for p in (E.env("RAMEN_EXPECT_SSO") or "none").split(",") if p])
def test_the_sso_provider_is_on_the_login_page_and_redirects_correctly(console_url, provider):
    """Everything short of the browser: the provider is offered, and its link sends the browser to the IdP with this
    console's callback, a client id and the scopes the lookup needs. A person completes the sign-in itself."""
    with path(f"SSO {provider}: on the login page, redirect carries redirect_uri / client_id / scopes"):
        if provider == "none":
            pytest.skip("RAMEN_EXPECT_SSO not set (e.g. RAMEN_EXPECT_SSO=entra)")
        base = console_url.rstrip("/")
        with httpx.Client(verify=E.tls_verify(), timeout=30, follow_redirects=False) as c:
            page = c.get(f"{base}/login")
            assert page.status_code == 200 and f"/auth/{provider}/login" in page.text, (
                f"{provider} is not offered on the login page"
            )
            r = c.get(f"{base}/auth/{provider}/login")
            assert r.status_code in (302, 303, 307), f"{r.status_code} {r.text[:200]}"
            target = urlparse(r.headers["location"])
            q = {k: v[0] for k, v in parse_qs(target.query).items()}
            assert target.scheme == "https" and target.hostname and target.hostname != urlparse(base).hostname, target
            assert q.get("redirect_uri") == f"{base}/auth/{provider}/callback", q.get("redirect_uri")
            assert q.get("client_id"), "no client_id in the redirect"
            assert q.get("response_type") == "code" and q.get("state"), q
            scopes = set(q.get("scope", "").split())
            want = SCOPES.get(provider, {"openid", "email"})
            assert want <= scopes, f"scopes {sorted(scopes)} lack {sorted(want - scopes)}"
            assert "client_secret" not in r.headers["location"]
            if provider == "entra":
                assert target.hostname == "login.microsoftonline.com", target.hostname
            if provider == "google":
                assert target.hostname == "accounts.google.com", target.hostname
            # the callback refuses a visit that did not come from this redirect
            forged_cb = c.get(f"{base}/auth/{provider}/callback", params={"code": "x", "state": "forged"})
            assert forged_cb.status_code in (302, 303, 400, 401, 403), forged_cb.status_code
            assert "ramen_session" not in forged_cb.headers.get("set-cookie", ""), "a forged callback opened a session"


def test_every_path_was_recorded(console_url):
    """Last in the file: the matrix is in the run's output (and in RAMEN_AUTH_MATRIX_OUT when set)."""
    failed = [row for row in AUTH_MATRIX if row[1] == "FAIL"]
    assert AUTH_MATRIX, "no path ran"
    assert not failed, f"{len(failed)} sign-in path(s) failed: {[p for p, _, _ in failed]}"
