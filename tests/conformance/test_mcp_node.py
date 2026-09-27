"""CONTRACTS §3: node HTTP surface, via the official mcp client. Needs RAMEN_NODE_URL + RAMEN_MCP_KEY."""
import json

import httpx
import pytest

from ramen_tests import env as E
from ramen_tests.mcp_client import INIT_BODY, MCP_HEADERS, endpoint, session, text_of

pytestmark = pytest.mark.conformance
TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "', "raise ")


@pytest.fixture
def http(node_url):
    with httpx.Client(base_url=node_url, verify=E.tls_verify(), timeout=30) as c:
        yield c


async def test_initialize_reports_contract_protocol(node_url, mcp_key):
    async with session(node_url, mcp_key) as s:
        assert s.protocol_version == "2025-06-18"


async def test_list_tools_exposes_demo_calculator_with_derived_schema(node_url, mcp_key):
    async with session(node_url, mcp_key) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    t = tools["demo_calculator_tool"]
    assert t.description
    schema = t.input_schema
    assert schema["type"] == "object"
    assert schema["properties"]["var1"]["type"] == "number"
    assert schema["properties"]["var2"]["type"] == "number"
    assert schema["properties"]["func"]["enum"] == ["add", "subtract", "multiply", "divide"]
    assert sorted(schema["required"]) == ["func", "var1", "var2"]


async def test_list_resources_and_prompts(node_url, mcp_key):
    async with session(node_url, mcp_key) as s:
        res = (await s.list_resources()).resources
        prompts = (await s.list_prompts()).prompts
    r = next(x for x in res if str(x.uri) == "ramen://demo/readme")
    assert r.mime_type == "text/markdown"
    p = next(x for x in prompts if x.name == "get_calculation_prompt")
    args = {a.name: a for a in (p.arguments or [])}
    assert args["request"].required is True


@pytest.mark.parametrize("func,expected", [("add", 5), ("subtract", -1), ("multiply", 6), ("divide", 2 / 3)])
async def test_calculator_operations(node_url, mcp_key, func, expected):
    async with session(node_url, mcp_key) as s:
        r = await s.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": func})
    assert not r.is_error, text_of(r)
    assert float(text_of(r)) == pytest.approx(expected)


async def test_divide_by_zero_is_error_without_traceback(node_url, mcp_key):
    async with session(node_url, mcp_key) as s:
        r = await s.call_tool("demo_calculator_tool", {"var1": 1, "var2": 0, "func": "divide"})
    assert r.is_error
    text = text_of(r)
    assert "division by zero" in text.lower()
    assert not any(m in text for m in TRACEBACK_MARKERS), text


async def test_read_demo_readme_resource(node_url, mcp_key):
    async with session(node_url, mcp_key) as s:
        r = await s.read_resource("ramen://demo/readme")
    c = r.contents[0]
    assert str(c.uri) == "ramen://demo/readme"
    assert c.mime_type == "text/markdown"
    assert "ramen-demo-mcp-group" in c.text


async def test_get_calculation_prompt_substitutes_request(node_url, mcp_key):
    async with session(node_url, mcp_key) as s:
        r = await s.get_prompt("get_calculation_prompt", {"request": "what is 2 plus 3"})
    m = r.messages[0]
    assert m.role == "user"
    assert "what is 2 plus 3" in m.content.text
    assert "{{request}}" not in m.content.text
    assert "demo_calculator_tool" in m.content.text


def test_unauthenticated_initialize_is_401_jsonrpc_error(http, node_url):
    r = http.post(endpoint(node_url), json=INIT_BODY, headers=MCP_HEADERS)
    assert r.status_code == 401
    body = r.json()
    assert body.get("jsonrpc") == "2.0" and "error" in body


def test_wrong_key_is_401(http, node_url):
    r = http.post(endpoint(node_url), json=INIT_BODY, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_health_ready_metrics(http, node_admin_url):
    assert http.get("/healthz").status_code == 200
    assert http.get("/readyz").status_code == 200
    m = http.get("/metrics").json()
    for k in ("inflight", "total", "errors", "load", "sidecar_alive", "loaded_at", "packages"):
        assert k in m, m
    assert m["load"] in ("low", "even", "high")
    assert set(m["packages"]) >= {"tools", "resources", "prompts", "errors"}


def test_metrics_counts_calls(http, node_url, mcp_key, node_admin_url):
    before = http.get("/metrics").json()["total"]
    r = http.post(endpoint(node_url), json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                  headers={**MCP_HEADERS, "Authorization": f"Bearer {mcp_key}"})
    assert r.status_code == 200
    assert http.get("/metrics").json()["total"] >= before + 1


def test_cidr_lock(http, node_url, mcp_key):
    """RAMEN_ALLOWED_CIDRS excludes the caller → 403 (JSON-RPC error). Only assertable when the target
    node is deliberately configured that way; set RAMEN_EXPECT_CIDR_403=1 to enable."""
    if E.env("RAMEN_EXPECT_CIDR_403") != "1":
        pytest.skip("node not configured with an excluding RAMEN_ALLOWED_CIDRS (set RAMEN_EXPECT_CIDR_403=1)")
    r = http.post(endpoint(node_url), json=INIT_BODY, headers={**MCP_HEADERS, "Authorization": f"Bearer {mcp_key}"})
    assert r.status_code == 403
    assert "error" in r.json()


def test_admin_reload_requires_admin_key(http, node_admin_url):
    r = http.post("/admin/reload")
    assert r.status_code in (401, 403)
    key = E.env("RAMEN_ADMIN_KEY")
    if not key:
        pytest.skip("RAMEN_ADMIN_KEY not set; skipping authorised reload")
    r = http.post("/admin/reload", headers={"X-Ramen-Admin-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert {"tools", "resources", "prompts", "errors"} <= set(body)
    assert json.dumps(body)  # JSON-serialisable load result
