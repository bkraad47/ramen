"""CONTRACTS §16.7 (D92) on a real `ramen-node`: an `Admin/Reload` carrying `revoked` makes the node refuse a revoked
`rmk_` key, a revoked token id and a person's tokens issued before the cut-off — at once, on both transports, with the
key still in its environment — and a later plain reload keeps it."""

import hashlib
import json
import time

import pytest

from ramen_tests.localconsole import SESSION_SECRET
from ramen_tests.localnode import LocalNode
from ramen_tests.tokens import user_token

pytestmark = pytest.mark.conformance
ISSUER, GONE = "https://console.example", "rmk_d92-revoked-key"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture
def node():  # one node per transport: a revocation never shrinks
    env = {
        "RAMEN_MCP_KEYS": f"test-key,{GONE}",
        "RAMEN_SESSION_SECRET": SESSION_SECRET,
        "RAMEN_OAUTH_ISSUER": ISSUER,
        "RAMEN_PUBLIC_URL": "https://mcp.example",
    }
    with LocalNode(env=env) as n:
        n.wait_serving()
        yield n


def tok(sub: str, jti: str, iat_shift: int = 0) -> str:
    t, _ = user_token(SESSION_SECRET, ISSUER, 600, sub=sub, jti=jti)
    if not iat_shift:
        return t
    from ramen_tests.tokens import claims_of, mint_console_token

    c = claims_of(t)
    return mint_console_token({**c, "iat": c["iat"] + iat_shift}, SESSION_SECRET)


@pytest.mark.parametrize("transport", ("grpc", "http"))
def test_a_pushed_revocation_is_refused_at_once(node, transport):
    t_jti, t_sub, t_other = tok("u-a", f"ja-{transport}"), tok("u-b", f"jb-{transport}"), tok("u-c", f"jc-{transport}")
    for key in (GONE, "test-key", t_jti, t_sub, t_other):
        assert node.for_transport(transport, key=key).outcome(PING) == "OK", key
    cut = int(time.time())
    revoked = {"keys": [hashlib.sha256(GONE.encode()).hexdigest()], "jti": [f"ja-{transport}"], "sub": {"u-b": cut}}
    node.node.admin_reload(revoked=json.dumps(revoked))
    for key in (GONE, t_jti, t_sub):
        assert node.for_transport(transport, key=key).outcome(PING) == "UNAUTHENTICATED", key
    for key in ("test-key", t_other, tok("u-b", "jb-new", iat_shift=5)):  # signed in again after the cut-off
        assert node.for_transport(transport, key=key).outcome(PING) == "OK", key
    node.node.admin_reload()  # a plain reload re-reads the env, which still names the key: the list stays
    assert node.for_transport(transport, key=GONE).outcome(PING) == "UNAUTHENTICATED"
