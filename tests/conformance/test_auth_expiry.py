"""A1/A2/A4/D2 (GitHub bkraad47/ramen#1, the r/selfhosted thread): what happens to an MCP session when the access
token expires mid-task — on real processes.

Part 1 needs only a node: tokens minted with the console's own derivation and a 2–3 s life. Part 2 runs a real console
with `RAMEN_OAUTH_ACCESS_TTL=3` (C8) and a node sharing its secret: the real `/oauth/token` code + refresh flow, the
SDK Streamable HTTP client, and `ramen-mcp-bridge --oauth` with a seeded token file. Findings in plain words:
`reports/auth-expiry-v0.7.0.md`."""

import asyncio
import json
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localconsole import LocalConsole, supports_access_ttl
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import (
    HttpError,
    bridge_command,
    bridge_session,
    http_session,
    sdk_http_client,
    sdk_text,
    text_of,
)
from ramen_tests.tokens import claims_of, user_token

pytestmark = pytest.mark.conformance
SECRET, ISSUER, PUBLIC = "conformance-secret", "https://console.example", "https://mcp.example"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
META = '/.well-known/oauth-protected-resource"'


def wait_until(exp: float, margin: float = 0.6) -> None:
    """Sleep until `exp` (unix seconds) is `margin` behind us; the node compares whole seconds."""
    time.sleep(max(0.0, exp + margin - time.time()))


# --- part 1: the node alone -------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def node():
    env = {"RAMEN_SESSION_SECRET": SECRET, "RAMEN_OAUTH_ISSUER": ISSUER, "RAMEN_PUBLIC_URL": PUBLIC}
    with LocalNode(bucket=E.FIXTURES / "slow_group", env=env) as n:
        n.wait_serving()
        yield n


def token(ttl: float, **kw) -> tuple[str, float]:
    return user_token(SECRET, ISSUER, ttl, **kw)


def test_expired_token_gets_the_same_401_challenge_as_no_token(node):
    """A4: a client that sees this 401 restarts sign-in exactly as it would with no token at all."""
    t, exp = token(2)
    c = node.http_client(key=t)
    c.initialize()
    wait_until(exp)
    r = c.post(PING)
    assert r.status_code == 401, r.text
    anon = c.post(PING, key=None)
    assert r.headers["WWW-Authenticate"] == anon.headers["WWW-Authenticate"]
    assert f'resource_metadata="{PUBLIC}{META}' in r.headers["WWW-Authenticate"]
    assert 'scope="mcp:demo:local"' in r.headers["WWW-Authenticate"]
    assert t not in r.text and node.client(key=t).outcome(PING) == "UNAUTHENTICATED"  # gRPC says the same


def test_session_minted_for_a_user_survives_a_new_token_for_the_same_user(node):
    """A2: the session id is bound to `user:<sub>`, not to the token, so a refreshed token keeps the session."""
    t1, _ = token(600, jti="j1")
    c = node.http_client(key=t1)
    c.initialize()
    sid = c.session_id
    assert sid
    t2, _ = token(600, jti="j2")  # a later token for the same person
    assert t2 != t1
    assert c.status(PING, key=t2) == 200
    assert text_of(c.call_tool("slow_tool", {"ms": 1}, key=t2)) == "slept 1 ms"
    assert c.session_id == sid
    other, _ = token(600, sub="u-43", jti="j3")  # someone else's token cannot ride that session
    assert c.status(PING, key=other) == 404
    assert c.status(PING, key=node.key) == 404, "nor can a group key"


def test_a_call_started_before_expiry_finishes_after_it(node):
    """D2: auth is checked when a call enters; a tool that runs past the token's `exp` still returns its result."""
    t, exp = token(2)
    c = node.http_client(key=t)
    c.initialize()
    r = c.request("tools/call", {"name": "slow_tool", "arguments": {"ms": 3500}}, timeout=30)
    assert text_of(r) == "slept 3500 ms"
    assert time.time() > exp, "the call ended after the token had expired"
    assert c.status(PING) == 401, "and the very next call is refused"


async def test_sdk_client_hits_401_at_expiry_and_a_new_connection_keeps_the_old_session(node):
    """A1 on the official SDK client: the call after expiry fails with the 401; swapping the bearer on the same
    session continues (the SDK keeps sending the `Mcp-Session-Id` it was given, the node accepts it for the user)."""
    t1, exp = token(3, jti="j1")
    client = sdk_http_client(node.http, key=t1, timeout=15)
    try:
        async with http_session(node.http, http_client=client, timeout=15) as s:
            assert sdk_text(await s.call_tool("slow_tool", {"ms": 1})) == "slept 1 ms"
            wait_until(exp)
            with pytest.raises(Exception) as e:  # noqa: PT011 - the SDK's own error type for a 401 is recorded below
                await s.call_tool("slow_tool", {"ms": 1})
            # observed (mcp 2.x, no auth provider): the 401 surfaces as MCPError(-32603, "Server returned an error
            # response") — the status and the WWW-Authenticate challenge are not passed up to the caller
            assert "-32603" in str(e.value) or "error response" in str(e.value), repr(e.value)
            t2, _ = token(600, jti="j2")
            client.headers["Authorization"] = f"Bearer {t2}"
            r = await s.call_tool("slow_tool", {"ms": 1})
            assert sdk_text(r) == "slept 1 ms"
            assert names(await s.list_tools()) >= {"slow_tool"}
    finally:
        await client.aclose()


def names(listing) -> set[str]:
    return {t.name for t in listing.tools}


def test_access_log_names_the_reason_for_an_expired_token(node):
    """D3/C6: the per-call log line says why a call was refused, so an operator can tell expiry from a bad key."""
    t, exp = token(1, sub="u-log")
    c = node.http_client(key=t)
    wait_until(exp)
    assert c.status(PING) == 401
    denied = [x for x in node.log_lines() if x.get("msg") == "mcp" and x.get("reason")]
    assert {x["reason"] for x in denied} >= {"token_expired"}, denied[-3:]


# --- part 2: a real console mints, expires and refreshes ------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    """Console (3 s access tokens) + node sharing its secret and issuer + one registered public client."""
    with LocalConsole(env={"RAMEN_OAUTH_ACCESS_TTL": "3"}) as con:
        con.seed_group()
        cid = con.register_client()
        env = {"RAMEN_SESSION_SECRET": con.session_secret, "RAMEN_OAUTH_ISSUER": con.url}
        with LocalNode(bucket=E.FIXTURES / "slow_group", env=env) as n:
            n.wait_serving()
            yield con, n, cid


def short_tokens(world) -> dict:
    con, _, cid = world
    tok = con.mint(cid)
    if tok["expires_in"] != 3:
        if supports_access_ttl():
            raise AssertionError(f"RAMEN_OAUTH_ACCESS_TTL=3 but expires_in={tok['expires_in']}")
        pytest.xfail(f"waiting for ui-agent (C8): RAMEN_OAUTH_ACCESS_TTL not read yet, expires_in={tok['expires_in']}")
    return tok


def test_console_token_expires_refresh_rotates_and_the_same_session_continues(world):
    con, node, cid = world
    tok = short_tokens(world)
    c = node.http_client(key=tok["access_token"])
    c.initialize()
    sid = c.session_id
    assert text_of(c.call_tool("slow_tool", {"ms": 1})) == "slept 1 ms"
    wait_until(claims_of(tok["access_token"])["exp"])
    r = c.post(PING)
    assert r.status_code == 401 and f'resource_metadata="{META}' in r.headers["WWW-Authenticate"]
    new = con.refresh(tok["refresh_token"], cid)  # what an OAuth-capable client does on that 401, no browser
    assert new["access_token"] != tok["access_token"] and new["refresh_token"] != tok["refresh_token"]
    assert claims_of(new["access_token"])["sub"] == claims_of(tok["access_token"])["sub"]
    c.key = new["access_token"]
    assert c.session_id == sid and c.status(PING) == 200
    assert text_of(c.call_tool("slow_tool", {"ms": 1})) == "slept 1 ms"
    # the rotated-out refresh token is dead; presenting it again revokes the whole chain (M3)
    r = con.token_response({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    sub = claims_of(tok["access_token"])["sub"]
    assert any(x.get("key_id") == f"user:{sub}" for x in node.log_lines() if x.get("msg") == "mcp")


async def test_sdk_client_refreshes_through_the_console_and_keeps_its_session(world):
    con, node, cid = world
    tok = short_tokens(world)
    client = sdk_http_client(node.http, key=tok["access_token"], timeout=15)
    try:
        async with http_session(node.http, http_client=client, timeout=15) as s:
            assert sdk_text(await s.call_tool("slow_tool", {"ms": 1})) == "slept 1 ms"
            wait_until(claims_of(tok["access_token"])["exp"])
            with pytest.raises(Exception, match="-32603|error response"):  # the SDK's view of the 401
                await s.call_tool("slow_tool", {"ms": 1})
            new = con.refresh(tok["refresh_token"], cid)
            client.headers["Authorization"] = f"Bearer {new['access_token']}"
            assert sdk_text(await s.call_tool("slow_tool", {"ms": 1})) == "slept 1 ms"
    finally:
        await client.aclose()


@pytest.mark.skipif(not bridge_command(), reason="ramen-mcp-bridge not found")
async def test_bridge_oauth_refreshes_silently_with_a_seeded_token_file(world, tmp_path):
    """A5: the bridge holds the refresh token; an expiring access token is renewed before/after the worker's
    UNAUTHENTICATED without a browser (`--no-browser` would print the sign-in URL — it must not)."""
    con, node, cid = world
    tok = short_tokens(world)
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({**tok, "expires_at": time.time() + tok["expires_in"]}))
    args = [
        "--target", node.target, "--group", "demo", "--zone", "local", "--insecure",
        "--oauth", con.url, "--client-id", cid, "--token-file", str(path), "--no-browser",
    ]  # fmt: skip
    err = open(tmp_path / "bridge.err", "w+")  # noqa: SIM115 - Popen needs a real fileno
    async with asyncio.timeout(90):
        async with bridge_session(node.node, args=args, timeout=30, errlog=err) as s:
            assert names(await s.list_tools()) >= {"slow_tool"}
            wait_until(claims_of(tok["access_token"])["exp"])
            r = await s.call_tool("slow_tool", {"ms": 1})
            assert not r.is_error and sdk_text(r) == "slept 1 ms"
            await asyncio.sleep(4)  # a second expiry while the bridge is up
            assert sdk_text(await s.call_tool("slow_tool", {"ms": 1})) == "slept 1 ms"
    kept = json.loads(path.read_text())
    assert kept["access_token"] != tok["access_token"] and kept["refresh_token"] != tok["refresh_token"]
    text = (tmp_path / "bridge.err").read_text()
    err.close()
    assert "oauth/authorize" not in text and "signing in" not in text, text


@pytest.mark.skipif(not bridge_command(), reason="ramen-mcp-bridge not found")
async def test_bridge_with_a_dead_refresh_token_would_need_the_browser(world, tmp_path):
    """The one case a person is asked to sign in again: no usable refresh token. With `--no-browser` the bridge prints
    the sign-in URL and waits for the callback instead of serving — so the SDK handshake does not complete."""
    con, node, cid = world
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"access_token": "stale", "refresh_token": "revoked", "expires_at": 0}))
    args = [
        "--target", node.target, "--group", "demo", "--zone", "local", "--insecure",
        "--oauth", con.url, "--client-id", cid, "--token-file", str(path), "--no-browser",
    ]  # fmt: skip
    err = open(tmp_path / "bridge.err", "w+")  # noqa: SIM115 - Popen needs a real fileno
    with pytest.raises(Exception):  # noqa: B017 - timeout / closed stream: the bridge is waiting on a person
        async with asyncio.timeout(8):
            async with bridge_session(node.node, args=args, timeout=5, errlog=err) as s:
                await s.list_tools()
    err.close()
    text = (tmp_path / "bridge.err").read_text()
    assert "refresh failed" in text and "/oauth/authorize" in text, text


def test_http_error_from_console_does_not_leak_tokens(world):
    con, _, cid = world
    r = con.token_response({"grant_type": "refresh_token", "refresh_token": "nope-secret", "client_id": cid})
    assert r.status_code == 400 and "nope-secret" not in r.text


def test_harness_raises_on_non_2xx(node):
    with pytest.raises(HttpError):
        node.http_client(key="bad").call_raw(PING)
