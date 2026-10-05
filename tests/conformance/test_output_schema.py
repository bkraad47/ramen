"""B5 / C9 (0.7.0): output schemas are enforced on the way out, on real node processes. A tool package's `output`
is published as `outputSchema`, a conforming result carries `structuredContent`, and a result that breaks the schema
comes back as an MCP tool error (`isError: true`), never as a transport error or a silent pass."""

import json

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import http_session, text_of

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
SCHEMA = json.loads((E.FIXTURES / "schema_group" / "mcp" / "tools" / "schema_tool" / "schema_tool.json").read_text())


@pytest.fixture(scope="module")
def schema_node():
    with LocalNode(bucket=E.FIXTURES / "schema_group") as n:
        n.wait_serving()
        yield n


@pytest.fixture(scope="module")
def demo_node():
    with LocalNode() as n:
        n.wait_serving()
        yield n


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_output_schema_is_listed_verbatim_and_enforced(schema_node, transport):
    c = schema_node.for_transport(transport)
    c.initialize()
    tools = {t["name"]: t for t in c.list_tools()}
    assert tools["schema_tool"]["outputSchema"] == SCHEMA["output"]
    good = c.call_tool("schema_tool", {"a": "7"})
    assert not good.get("isError") and good["structuredContent"] == {"n": 7}, good
    bad = c.call_tool("schema_tool", {"a": "x"})
    assert bad["isError"] is True, bad
    assert text_of(bad).startswith("invalid output: n: "), text_of(bad)
    assert "structuredContent" not in bad or bad["structuredContent"] != {"n": "x"}


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_scalar_output_is_wrapped_as_result(demo_node, transport):
    """A proto's `"output": {"type": "number"}` becomes an object schema with one `result` property, and the
    structured content follows the same shape — what an SDK client validates against."""
    c = demo_node.for_transport(transport)
    c.initialize()
    tools = {t["name"]: t for t in c.list_tools()}
    assert tools["demo_calculator_tool"]["outputSchema"] == {
        "type": "object",
        "properties": {"result": {"type": "number"}},
        "required": ["result"],
    }
    r = c.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
    assert r["structuredContent"] == {"result": 5} and float(text_of(r)) == 5


async def test_sdk_client_validates_structured_content_against_the_schema(schema_node):
    """The official SDK checks `structuredContent` against the server's `outputSchema` itself: a conforming result
    parses, the violating one is already an `isError` from the runtime so the SDK never sees bad structured data."""
    async with http_session(schema_node.http) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert tools["schema_tool"].output_schema == SCHEMA["output"]
        good = await s.call_tool("schema_tool", {"a": "7"})
        assert not good.is_error and good.structured_content == {"n": 7}
        bad = await s.call_tool("schema_tool", {"a": "x"})
        assert bad.is_error and "invalid output" in "".join(getattr(c, "text", "") for c in bad.content)
