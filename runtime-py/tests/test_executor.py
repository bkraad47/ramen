import pytest
from conftest import write_pkg

from ramen_runtime.executor import Executor
from ramen_runtime.loader import load

TOOL = {
    "type": "tool",
    "name": "t",
    "description": "d",
    "callable": "f",
    "input": {"a": {"type": "string"}},
    "output": {"type": "string"},
    "error": {"type": "string"},
}


@pytest.fixture
def demo(demo_bucket):
    return Executor(load(demo_bucket))


def test_call_tool_ok(demo):
    r = demo.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
    assert r == {"content": [{"type": "text", "text": "5"}], "structuredContent": {"result": 5}, "isError": False}


def test_call_tool_exception_is_error_without_traceback(demo):
    r = demo.call_tool("demo_calculator_tool", {"var1": 1, "var2": 0, "func": "divide"})
    assert r["isError"] is True
    assert r["content"][0]["text"] == "ZeroDivisionError: division by zero"


def test_call_tool_invalid_args(demo):
    r = demo.call_tool("demo_calculator_tool", {"var1": "x", "var2": 0, "func": "add"})
    assert r["isError"] is True and "var1" in r["content"][0]["text"]
    r = demo.call_tool("demo_calculator_tool", {"var1": 1, "var2": 0})
    assert r["isError"] is True and "func" in r["content"][0]["text"]


def test_call_unknown_tool(demo):
    with pytest.raises(KeyError):
        demo.call_tool("nope", {})


def test_dict_result_is_json(tmp_path):
    plain = {k: v for k, v in TOOL.items() if k != "output"}
    write_pkg(tmp_path, "tools", "t", plain, "def f(a):\n    return {'a': a, 'n': 1.5}\n")
    r = Executor(load(tmp_path)).call_tool("t", {"a": "z"})
    assert r["content"][0]["text"] == '{"a": "z", "n": 1.5}'


def test_secret_substituted_and_never_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMEN_SECRET_G__K", "hunter2")
    write_pkg(
        tmp_path, "tools", "t", TOOL, "def f(a):\n    assert a == 'hunter2'\n    raise RuntimeError('boom ' + a)\n"
    )
    ex = Executor(load(tmp_path))
    r = ex.call_tool("t", {"a": "{{$g.k}}"})
    assert r["isError"] and "hunter2" not in r["content"][0]["text"] and "***" in r["content"][0]["text"]
    r = ex.call_tool("t", {"a": "{{$g.missing}}"})
    assert r["isError"] and "g.MISSING" in r["content"][0]["text"]


def test_read_resource(demo):
    r = demo.read_resource("ramen://demo/readme")
    assert r["contents"][0]["uri"] == "ramen://demo/readme"
    assert r["contents"][0]["mimeType"] == "text/markdown"
    assert r["contents"][0]["text"].startswith("# ramen-demo-mcp-group")


def test_read_resource_unknown(demo):
    with pytest.raises(KeyError):
        demo.read_resource("ramen://nope")


def test_read_resource_error(tmp_path):
    write_pkg(
        tmp_path,
        "resources",
        "r",
        {
            "type": "resource",
            "name": "r",
            "description": "d",
            "uri": "u://r",
            "mime_type": "text/plain",
            "callable": "f",
            "input": {},
            "output": {"type": "string"},
            "error": {"type": "string"},
        },
        "def f():\n    raise ValueError('bad')\n",
    )
    with pytest.raises(RuntimeError, match="ValueError: bad"):
        Executor(load(tmp_path)).read_resource("u://r")


def test_get_prompt_renders_skill_and_settings(demo):
    r = demo.get_prompt("get_calculation_prompt", {"request": "2+2"})
    text = r["messages"][0]["content"]["text"]
    assert r["messages"][0]["role"] == "user"
    assert "Request: 2+2" in text
    assert not text.startswith("---")
    assert '"max_tool_calls": 10' in text


def test_get_prompt_missing_arg(demo):
    with pytest.raises(ValueError, match="request"):
        demo.get_prompt("get_calculation_prompt", {})
    with pytest.raises(KeyError):
        demo.get_prompt("nope", {})


def test_scalar_output_is_wrapped_as_structured_content(demo):
    r = demo.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
    assert r == {
        "content": [{"type": "text", "text": "5"}],
        "structuredContent": {"result": 5},
        "isError": False,
    }


def test_object_output_is_validated_and_passed_through(tmp_path):
    obj = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}
    code = "def f(a):\n    return {'n': int(a)} if a.isdigit() else {'n': a}\n"
    write_pkg(tmp_path, "tools", "t", {**TOOL, "output": obj}, code)
    ex = Executor(load(tmp_path))
    r = ex.call_tool("t", {"a": "7"})
    assert r["structuredContent"] == {"n": 7} and r["isError"] is False
    assert r["content"][0]["text"] == '{"n": 7}'
    r = ex.call_tool("t", {"a": "x"})
    assert r["isError"] is True
    assert r["content"][0]["text"] == "invalid output: n: 'x' is not of type 'integer'"
    assert "structuredContent" not in r


def test_scalar_output_mismatch_is_a_tool_error(tmp_path):
    write_pkg(tmp_path, "tools", "t", {**TOOL, "output": {"type": "number"}}, "def f(a):\n    return a\n")
    r = Executor(load(tmp_path)).call_tool("t", {"a": "nope"})
    assert r["isError"] is True and r["content"][0]["text"] == "invalid output: result: 'nope' is not of type 'number'"


def test_no_output_schema_means_no_structured_content(tmp_path):
    plain = {k: v for k, v in TOOL.items() if k != "output"}
    write_pkg(tmp_path, "tools", "t", plain, "def f(a):\n    return {'a': a}\n")
    r = Executor(load(tmp_path)).call_tool("t", {"a": "z"})
    assert r == {"content": [{"type": "text", "text": '{"a": "z"}'}], "isError": False}
