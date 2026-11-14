"""CONTRACTS §23.1 through the stdio bridge: `ramen-mcp-bridge` → `ramen.v1.Mcp/Call`.

On gRPC the body alone selects the era, so a modern client behind the bridge works only if the bridge forwards each
message byte for byte — `_meta` included — and adds nothing of its own (no `initialize`, no session). Raw JSON lines
are written to the bridge's stdin here, because the question is what the bridge does, not what an SDK does.
Read-only for this suite: a failure is reported, the bridge is not patched from here."""

import json
import subprocess

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import bridge_args, bridge_command
from ramen_tests.modern import ALL_INPUT, MODERN, SERVER_INFO, meta

pytestmark = pytest.mark.conformance


@pytest.fixture(scope="module")
def node():
    if not bridge_command():
        pytest.skip("ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH / ../../ramen-mcp-bridge/.venv)")
    with LocalNode(bucket=E.FIXTURES / "mrtr_group") as n:
        n.wait_serving()
        yield n


def through_bridge(node: LocalNode, messages: list[dict]) -> list[dict]:
    """Start the bridge, write `messages` as JSON lines, close stdin, → the lines it wrote back."""
    cmd = bridge_command() + bridge_args(node.target, node.key, "demo", "local")
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert out.returncode == 0, out.stderr[-2000:]
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def modern(rid, method: str, params: dict | None = None, caps: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": {**(params or {}), "_meta": meta(caps)}}


def mcp_lines(node: LocalNode) -> list[dict]:
    return [x for x in node.log_lines() if x.get("msg") == "mcp"]


def test_a_modern_body_reaches_the_node_with_its_meta_and_no_session_is_synthesised(node):
    before = len(mcp_lines(node))
    replies = through_bridge(node, [modern(1, "tools/list"), modern(2, "server/discover")])
    assert [r["id"] for r in replies] == [1, 2], replies
    tools, discover = (r["result"] for r in replies)
    assert tools["resultType"] == "complete" and tools["ttlMs"] == 60000, "the node saw _meta: it answered as modern"
    assert tools["_meta"][SERVER_INFO]["name"] == "ramen-node"
    assert discover["supportedVersions"][0] == MODERN
    seen = mcp_lines(node)[before:]
    assert [x["method"] for x in seen] == ["tools/list", "server/discover"], (
        "nothing but the two requests: no initialize"
    )
    assert {x["protocol"] for x in seen} == {MODERN} and {x["transport"] for x in seen} == {"grpc"}, seen


def test_client_capabilities_and_mrtr_members_pass_through_untouched(node):
    answer = {"who": {"action": "accept", "content": {"name": "Ada"}}}
    replies = through_bridge(
        node,
        [
            modern(1, "tools/call", {"name": "caps_echo", "arguments": {}}, {"sampling": {}, "x": {"y": [1, 2]}}),
            modern(2, "tools/call", {"name": "ask_name", "arguments": {}}, ALL_INPUT),
            modern(3, "tools/call", {"name": "ask_name", "arguments": {}, "inputResponses": answer}, ALL_INPUT),
            modern(4, "tools/call", {"name": "ask_name", "arguments": {}}, {}),
            modern(5, "ping"),
        ],
    )
    by_id = {r["id"]: r for r in replies}
    assert json.loads(by_id[1]["result"]["content"][0]["text"]) == {"sampling": {}, "x": {"y": [1, 2]}}
    assert by_id[2]["result"]["resultType"] == "input_required" and "who" in by_id[2]["result"]["inputRequests"]
    assert by_id[3]["result"]["content"][0]["text"] == "Hello, Ada!"
    assert by_id[4]["error"]["code"] == -32021, "the node's modern errors come back as they are"
    assert by_id[5]["error"]["code"] == -32601, (
        "ping is removed in the modern era; the bridge does not answer it itself"
    )


def test_a_legacy_body_through_the_same_bridge_is_still_legacy(node):
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    replies = through_bridge(
        node,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ],
    )
    assert [r["id"] for r in replies] == [1, 2, 3], "a notification produces no output"
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert "resultType" not in replies[1]["result"] and replies[2]["result"] == {}
