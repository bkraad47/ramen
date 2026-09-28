"""CONTRACTS §2: node ↔ runtime sidecar protocol, exercised directly against `python -m ramen_runtime`."""

import json

import pytest

from ramen_tests.env import FIXTURES
from ramen_tests.sidecar import Sidecar, SidecarError, runtime_importable

pytestmark = pytest.mark.conformance
DEMO = FIXTURES / "demo_group"
BROKEN = FIXTURES / "broken_group"
SECRETS = FIXTURES / "secrets_group"
SECRET_VALUE = "tok-3f9a1c-do-not-log"


@pytest.fixture(scope="module", autouse=True)
def _need_runtime():
    if not runtime_importable():
        pytest.skip("ramen_runtime not importable (set RAMEN_RUNTIME_PYTHON or uv sync runtime-py)")


@pytest.fixture
def demo():
    with Sidecar(DEMO) as s:
        s.loaded = s.call("runtime.load", {"bucket": str(DEMO)})
        yield s


def text(result) -> str:
    return "".join(c.get("text", "") for c in result["content"])


def test_load_shape(demo):
    r = demo.loaded
    assert r["errors"] == []
    tool = next(t for t in r["tools"] if t["name"] == "demo_calculator_tool")
    assert tool["description"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["var1"]["type"] == "number"
    assert schema["properties"]["var2"]["type"] == "number"
    assert schema["properties"]["func"]["enum"] == ["add", "subtract", "multiply", "divide"]
    assert sorted(schema["required"]) == ["func", "var1", "var2"]
    res = next(x for x in r["resources"] if x["uri"] == "ramen://demo/readme")
    assert res["name"] == "demo_readme" and res["mimeType"] == "text/markdown" and res["description"]
    p = next(x for x in r["prompts"] if x["name"] == "get_calculation_prompt")
    assert p["arguments"] == [{"name": "request", "description": "the user's arithmetic question", "required": True}]


@pytest.mark.parametrize("func,expected", [("add", 5), ("subtract", -1), ("multiply", 6), ("divide", 2 / 3)])
def test_call_tool(demo, func, expected):
    r = demo.call(
        "runtime.call_tool", {"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": func}}
    )
    assert r["isError"] is False
    assert r["content"][0]["type"] == "text"
    assert float(text(r)) == pytest.approx(expected)


def test_call_tool_error_is_flagged_not_raised(demo):
    r = demo.call(
        "runtime.call_tool", {"name": "demo_calculator_tool", "arguments": {"var1": 1, "var2": 0, "func": "divide"}}
    )
    assert r["isError"] is True
    assert "division by zero" in text(r).lower()
    assert "Traceback" not in text(r)


def test_call_unknown_tool(demo):
    try:
        r = demo.call("runtime.call_tool", {"name": "nope", "arguments": {}})
    except SidecarError as e:
        assert e.error["code"] < 0
    else:
        assert r["isError"] is True


def test_read_resource(demo):
    r = demo.call("runtime.read_resource", {"uri": "ramen://demo/readme"})
    c = r["contents"][0]
    assert c["uri"] == "ramen://demo/readme" and c["mimeType"] == "text/markdown"
    assert "ramen-demo-mcp-group" in c["text"]


def test_get_prompt(demo):
    r = demo.call("runtime.get_prompt", {"name": "get_calculation_prompt", "arguments": {"request": "7 times 6"}})
    m = r["messages"][0]
    assert m["role"] == "user" and m["content"]["type"] == "text"
    assert "7 times 6" in m["content"]["text"] and "{{request}}" not in m["content"]["text"]


def test_ping_and_unknown_method(demo):
    assert demo.call("runtime.ping", {}) == {"ok": True}
    with pytest.raises(SidecarError) as ei:
        demo.call("runtime.nope", {})
    assert ei.value.error["code"] == -32601


def test_malformed_line_does_not_kill_process(demo):
    demo.proc.stdin.write("this is not json\n")
    demo.proc.stdin.flush()
    assert demo.call("runtime.ping", {}) == {"ok": True}


def test_shutdown_exits(demo):
    assert demo.call("runtime.shutdown", {}) == {"ok": True}
    assert demo.wait_exit(10) is not None


def test_stderr_is_json_lines(demo):
    demo.call("runtime.ping", {})
    lines = [x for x in demo.stderr.splitlines() if x.strip()]
    for x in lines:
        try:
            json.loads(x)
        except ValueError as e:
            raise AssertionError(f"non-JSON stderr line: {x[:300]!r}") from e


def test_broken_packages_reported_valid_still_load():
    with Sidecar(BROKEN) as s:
        r = s.call("runtime.load", {"bucket": str(BROKEN)})
    names = {t["name"] for t in r["tools"]}
    assert "demo_calculator_tool" in names
    assert names.isdisjoint({"wrong_type_tool", "other_name", "misnamed_tool", "no_callable_tool", "bad_type_tool"})
    pkgs = {e["package"] for e in r["errors"]}
    for e in r["errors"]:
        assert e["reason"]
    for broken in ("wrong_type_tool", "misnamed_tool", "no_callable_tool", "bad_type_tool"):
        assert any(broken in p for p in pkgs), (broken, pkgs)


def test_secret_substitution_from_env():
    with Sidecar(SECRETS, {"RAMEN_SECRET_DEMO__TOKEN": SECRET_VALUE, "RAMEN_GROUP": "demo"}) as s:
        r = s.call("runtime.load", {"bucket": str(SECRETS)})
        assert r["errors"] == [], r["errors"]
        echoed = s.call("runtime.call_tool", {"name": "echo_secret_tool", "arguments": {"text": "t={{$demo.TOKEN}}"}})
        assert text(echoed) == f"t={SECRET_VALUE}"
        resolved = s.call("runtime.call_tool", {"name": "resolve_secret_tool", "arguments": {"var": "TOKEN"}})
        assert text(resolved) == f"value={SECRET_VALUE}"
        s.call("runtime.shutdown", {})
        s.wait_exit()
        assert SECRET_VALUE not in s.stderr


def test_unknown_secret_is_error_not_leak():
    with Sidecar(SECRETS) as s:
        s.call("runtime.load", {"bucket": str(SECRETS)})
        r = s.call("runtime.call_tool", {"name": "echo_secret_tool", "arguments": {"text": "{{$demo.MISSING}}"}})
        # either substituted to error or left/flagged; must never crash the sidecar
        assert isinstance(r["isError"], bool)
        assert s.call("runtime.ping", {}) == {"ok": True}
