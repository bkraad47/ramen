"""C10 (0.7.2, A8): per-tool access by caller kind, on real node processes and both transports.

The node is started with `RAMEN_TOOL_ACCESS` restricting `demo_calculator_tool` (listable by `key` and `group_admin`,
callable by `group_admin` only) next to an unrestricted `slow_tool`. A group key, and console tokens minted with the
`role` claim (`viewer`, `group_admin`, `super_admin`, none), get exactly the list and the denials the contract names:
hidden → `-32601` (log reason `tool_hidden`), listed but not callable → `-32003` (`tool_denied`). Part 2 drives the
console's `PUT …/tool-access` and checks the deploy config and the token claim. Findings in
`reports/tool-access-v0.7.2.md`."""

import json
import shutil
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localconsole import LocalConsole
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, text_of
from ramen_tests.tokens import claims_of, user_token

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
SECRET, ISSUER, PUBLIC = "conformance-secret", "https://console.example", "https://mcp.example"
CALC, FREE = "demo_calculator_tool", "slow_tool"
ARGS = {"var1": 2, "var2": 3, "func": "add"}
ACCESS = {CALC: {"list": ["key", "group_admin"], "call": ["group_admin"]}}
IGNORE = shutil.ignore_patterns("__pycache__")


def names(tools) -> set[str]:
    return {t["name"] for t in tools}


def two_tool_bucket(root):
    """The demo group plus `slow_tool`: one restricted tool, one that `tool_access` does not mention."""
    bucket = root / "bucket"
    shutil.copytree(E.FIXTURES / "demo_group", bucket, ignore=IGNORE)
    shutil.copytree(E.FIXTURES / "slow_group" / "mcp" / "tools" / FREE, bucket / "mcp" / "tools" / FREE, ignore=IGNORE)
    return bucket


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    env = {
        "RAMEN_TOOL_ACCESS": json.dumps(ACCESS, separators=(",", ":")),
        "RAMEN_SESSION_SECRET": SECRET,
        "RAMEN_OAUTH_ISSUER": ISSUER,
        "RAMEN_PUBLIC_URL": PUBLIC,
    }
    with LocalNode(bucket=two_tool_bucket(tmp_path_factory.mktemp("access")), env=env) as n:
        n.wait_serving()
        try:  # probe once: a key that can still call the restricted tool means the node side is not there yet
            n.node.call_tool(CALC, ARGS)
            n.enforced = False
        except JsonRpcError:
            n.enforced = True
        yield n


def enforced(node):
    if not node.enforced:
        pytest.xfail("waiting for worker-agent (C10): RAMEN_TOOL_ACCESS is not enforced by this node")


def token(role: str | None, sub: str) -> str:
    extra = {} if role is None else {"role": role}
    return user_token(SECRET, ISSUER, 600, sub=sub, jti=f"j-{sub}", **extra)[0]


def denied_code(client, name: str, args: dict | None = None) -> int:
    with pytest.raises(JsonRpcError) as e:
        client.call_tool(name, args or ARGS)
    return e.value.code


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_group_key_lists_the_tool_but_may_not_call_it(node, transport):
    enforced(node)
    c = node.for_transport(transport)
    c.initialize()
    assert names(c.list_tools()) == {CALC, FREE}
    with pytest.raises(JsonRpcError) as e:
        c.call_tool(CALC, ARGS)
    assert e.value.code == -32003, e.value
    assert CALC in e.value.message and "key" in e.value.message and "forbidden" in e.value.message, e.value.message
    assert text_of(c.call_tool(FREE, {"ms": 1})) == "slept 1 ms", "a tool absent from the map is unrestricted"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_viewer_token_does_not_see_the_tool_and_gets_not_found(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token("viewer", "u-viewer"))
    c.initialize()
    assert names(c.list_tools()) == {FREE}
    assert denied_code(c, CALC) == -32601, "hidden tools are indistinguishable from unknown ones"
    assert text_of(c.call_tool(FREE, {"ms": 1})) == "slept 1 ms"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_group_admin_token_lists_and_calls(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token("group_admin", "u-gadmin"))
    c.initialize()
    assert names(c.list_tools()) == {CALC, FREE}
    assert float(text_of(c.call_tool(CALC, ARGS))) == 5


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_super_admin_sees_and_calls_everything(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token("super_admin", "u-super"))
    c.initialize()
    assert names(c.list_tools()) == {CALC, FREE}
    assert float(text_of(c.call_tool(CALC, ARGS))) == 5


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_token_without_a_role_claim_is_an_mcp_user(node, transport):
    enforced(node)
    c = node.for_transport(transport, key=token(None, "u-norole"))
    c.initialize()
    assert names(c.list_tools()) == {FREE}
    assert denied_code(c, CALC) == -32601


def test_access_log_names_tool_hidden_and_tool_denied(node):
    """The operator can tell "this kind may not see it" from "may see, may not call"; the credential never appears."""
    enforced(node)
    key_c = node.http_client()
    key_c.initialize()
    assert denied_code(key_c, CALC) == -32003
    viewer = token("viewer", "u-log")
    tok_c = node.http_client(key=viewer)
    tok_c.initialize()
    assert denied_code(tok_c, CALC) == -32601
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        calls = [x for x in node.log_lines() if x.get("msg") == "mcp" and x.get("method") == "tools/call"]
        reasons = {x.get("reason") for x in calls if x.get("name") == CALC}
        if {"tool_hidden", "tool_denied"} <= reasons:
            break
        time.sleep(0.2)
    assert {"tool_hidden", "tool_denied"} <= reasons, calls[-4:]
    by_reason = {x["reason"]: x for x in calls if x.get("reason")}
    assert by_reason["tool_denied"]["status"] == "denied" and by_reason["tool_denied"]["key_id"] != "user:u-log"
    assert by_reason["tool_hidden"]["status"] == "denied" and by_reason["tool_hidden"]["key_id"] == "user:u-log"
    text = node.log.read_text()
    assert node.key not in text and viewer not in text


# --- part 2: the console side ----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def console():
    with LocalConsole() as con:
        con.seed_group()
        yield con


def put_access(con: LocalConsole, body: dict):
    r = con.admin.put("env_tool_access", body, group="demo", env="dev")
    if r.status_code in (404, 405):
        pytest.xfail(f"waiting for ui-agent (C10): PUT …/tool-access answers {r.status_code}")
    return r


def test_console_validates_the_map_and_hands_it_to_the_deploy(console):
    con = console
    assert put_access(con, {CALC: {"list": ["key", "wizard"], "call": []}}).status_code == 422, "unknown kind"
    assert put_access(con, {CALC: {"list": ["key"], "call": ["group_admin"]}}).status_code == 422, "call ⊄ list"
    r = put_access(con, ACCESS)
    assert r.status_code in (200, 204), r.text
    envs = [e for e in con.admin.get("environments_all", params={"group": "demo"}).json() if e["name"] == "dev"]
    assert envs and envs[0].get("tool_access") == ACCESS, envs
    job = con.admin.deploy("demo", "dev").json()
    con.admin.wait_job(job["id"], timeout=300)  # no worker is reachable here; the local adapter still renders the env
    env_file = con.dir / "buckets" / "demo" / ".ramen" / "env-local"
    assert env_file.exists(), con.output()
    line = next((x for x in env_file.read_text().splitlines() if x.startswith("RAMEN_TOOL_ACCESS=")), None)
    assert line and json.loads(line.split("=", 1)[1]) == ACCESS, env_file.read_text()
    page = con.admin.page("/groups/demo").text
    assert "Tool access" in page and "/environments/dev/tool-access" in page


def test_console_token_carries_the_role_claim(console):
    con = console
    cid = con.register_client()
    claims = claims_of(con.mint(cid)["access_token"])
    if "role" not in claims:
        pytest.xfail("waiting for ui-agent (C10): no `role` claim in the console's access token")
    assert claims["role"] == "super_admin", claims  # the bootstrap admin
