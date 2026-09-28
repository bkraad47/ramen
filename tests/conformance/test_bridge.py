"""CONTRACTS §11 bridge: `ramen-mcp-bridge` as a stdio MCP server driven by the official `mcp` SDK client — the path
Claude Desktop / Cursor use. Needs RAMEN_NODE_URL + RAMEN_MCP_KEY (or the e2e-minted key) and the bridge
(RAMEN_BRIDGE_CMD, `ramen-mcp-bridge` on PATH, or ../runtime-py/.venv)."""

import asyncio
import io

import pytest

from ramen_tests.mcp_client import PROTOCOL, bridge_args, bridge_command, bridge_session, sdk_text

pytestmark = pytest.mark.conformance


@pytest.fixture(scope="module", autouse=True)
def _bridge_present():
    if not bridge_command():
        pytest.skip("ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH / ../runtime-py/.venv)")


def test_bridge_flags_follow_contract():
    a = bridge_args("h:1", "rmk_x", "g", "z", tls=True, ca="/ca.pem")
    assert a == ["--target", "h:1", "--key", "rmk_x", "--group", "g", "--zone", "z", "--tls", "--ca", "/ca.pem"]
    assert bridge_args("h:1", "k", "g", "z")[-1] == "--insecure"


async def test_initialize_through_bridge(node):
    async with bridge_session(node) as s:
        assert s.protocol_version == PROTOCOL
        assert await s.send_ping()


async def test_full_mcp_flow_through_bridge(node):
    async with bridge_session(node) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert tools["demo_calculator_tool"].input_schema["properties"]["func"]["enum"] == [
            "add",
            "subtract",
            "multiply",
            "divide",
        ]
        r = await s.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "multiply"})
        assert not r.is_error and float(sdk_text(r)) == 6
        bad = await s.call_tool("demo_calculator_tool", {"var1": 1, "var2": 0, "func": "divide"})
        assert bad.is_error and "division by zero" in sdk_text(bad).lower()
        res = await s.read_resource("ramen://demo/readme")
        assert "ramen-demo-mcp-group" in res.contents[0].text
        p = await s.get_prompt("get_calculation_prompt", {"request": "2+3"})
        assert "2+3" in p.messages[0].content.text
        assert {x.name for x in (await s.list_prompts()).prompts} >= {"get_calculation_prompt"}
        assert {str(x.uri) for x in (await s.list_resources()).resources} >= {"ramen://demo/readme"}


async def test_unknown_tool_is_mcp_error_not_bridge_crash(node):
    async with bridge_session(node) as s:
        try:
            r = await s.call_tool("no_such_tool", {})
            assert r.is_error
        except Exception as e:  # noqa: BLE001 - McpError for a JSON-RPC error is the other legal outcome
            assert "32601" in str(e) or "not found" in str(e).lower() or "unknown" in str(e).lower(), e
        assert await s.send_ping()  # the bridge is still alive


async def test_bad_key_through_bridge_fails_cleanly(node):
    """UNAUTHENTICATED from the node must surface as an error to the stdio client (no hang, no traceback on stdout)."""
    err = io.StringIO()
    with pytest.raises(Exception):  # noqa: B017 - McpError / timeout / closed stream are all acceptable
        async with asyncio.timeout(30):
            async with bridge_session(node, key="nope", timeout=20, errlog=err) as s:
                await s.list_tools()
    assert "nope" not in err.getvalue()
