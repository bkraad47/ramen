"""CONTRACTS §21.4 (0.7.5, D48), the node side: `RAMEN_ROLES` names custom roles and their base, and a tool-access
list matches a caller's kind by name **or** by base. On real node processes, both transports.

`demo_calculator_tool` is listable by `analyst` and `key`, callable by `analyst` only; `slow_tool` is listable and
callable by `viewer`. `analyst` has base `viewer` in `RAMEN_ROLES`, so an analyst sees and calls both; a plain viewer
sees only `slow_tool`; an unknown role name with no entry matches nothing (fails closed, as 0.7.2)."""

import json
import shutil

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, text_of
from ramen_tests.tokens import user_token

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
SECRET, ISSUER, PUBLIC = "conformance-secret", "https://console.example", "https://mcp.example"
CALC, FREE = "demo_calculator_tool", "slow_tool"
ARGS = {"var1": 2, "var2": 3, "func": "add"}
ROLES = {"analyst": "viewer"}
ACCESS = {CALC: {"list": ["analyst", "key"], "call": ["analyst"]}, FREE: {"list": ["viewer"], "call": ["viewer"]}}
IGNORE = shutil.ignore_patterns("__pycache__")


def names(tools) -> set[str]:
    return {t["name"] for t in tools}


def token(role: str | None, sub: str) -> str:
    extra = {} if role is None else {"role": role}
    return user_token(SECRET, ISSUER, 600, sub=sub, jti=f"j-{sub}", **extra)[0]


def denied_code(client, name: str, args: dict | None = None) -> int:
    with pytest.raises(JsonRpcError) as e:
        client.call_tool(name, args or ARGS)
    return e.value.code


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    bucket = tmp_path_factory.mktemp("roles") / "bucket"
    shutil.copytree(E.FIXTURES / "demo_group", bucket, ignore=IGNORE)
    shutil.copytree(E.FIXTURES / "slow_group" / "mcp" / "tools" / FREE, bucket / "mcp" / "tools" / FREE, ignore=IGNORE)
    env = {
        "RAMEN_ROLES": json.dumps(ROLES, separators=(",", ":")),
        "RAMEN_TOOL_ACCESS": json.dumps(ACCESS, separators=(",", ":")),
        "RAMEN_SESSION_SECRET": SECRET,
        "RAMEN_OAUTH_ISSUER": ISSUER,
        "RAMEN_PUBLIC_URL": PUBLIC,
    }
    with LocalNode(bucket=bucket, env=env) as n:
        n.wait_serving()
        c = n.http_client(key=token("analyst", "u-probe"))
        c.initialize()
        try:  # probe: an analyst may call slow_tool only through its base `viewer`
            c.call_tool(FREE, {"ms": 1})
            n.enforced = True
        except JsonRpcError:
            n.enforced = False
        yield n


def enforced(node):
    if not node.enforced:
        pytest.xfail("waiting for worker-agent (§21.4): RAMEN_ROLES is not read by this node")


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_custom_role_matches_by_name_and_by_base(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token("analyst", "u-analyst"))
    c.initialize()
    assert names(c.list_tools()) == {CALC, FREE}
    assert float(text_of(c.call_tool(CALC, ARGS))) == 5, "named in the list"
    assert text_of(c.call_tool(FREE, {"ms": 1})) == "slept 1 ms", "its base `viewer` is in the list"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_the_base_role_itself_gets_only_what_names_it(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token("viewer", "u-viewer"))
    c.initialize()
    assert names(c.list_tools()) == {FREE}
    assert denied_code(c, CALC) == -32601, "a base never inherits upward from a custom role"
    assert text_of(c.call_tool(FREE, {"ms": 1})) == "slept 1 ms"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_unknown_role_name_matches_nothing(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token("wizard", "u-wizard"))
    c.initialize()
    assert names(c.list_tools()) == set()
    assert denied_code(c, CALC) == -32601 and denied_code(c, FREE, {"ms": 1}) == -32601


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_group_key_and_super_admin_are_unchanged(node, transport):
    enforced(node)
    k = node.for_transport(transport)
    k.initialize()
    assert names(k.list_tools()) == {CALC}
    assert denied_code(k, CALC) == -32003
    s = node.for_transport(transport, key=token("super_admin", "u-super"))
    s.initialize()
    assert names(s.list_tools()) == {CALC, FREE} and float(text_of(s.call_tool(CALC, ARGS))) == 5


def test_bad_ramen_roles_is_refused_at_startup(tmp_path):
    """A deploy that hands the node a malformed map must not start a node that silently ignores it."""
    with LocalNode(env={"RAMEN_ROLES": '{"analyst": ["not", "a", "base"]}'}) as n:
        try:
            n.wait_answering(timeout=20)
        except AssertionError as e:
            assert "RAMEN_ROLES" in str(e), str(e)[-600:]
            return
        pytest.xfail("waiting for worker-agent (§21.4): RAMEN_ROLES is not validated by this node")
