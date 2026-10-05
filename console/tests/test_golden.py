"""C1 / C4 (0.7.0): golden cases from a group repo's `mcp/tests.yaml`, run against the canary before promotion."""

import pytest

from ramen_console import golden

YAML = """
cases:
  - tool: demo_calculator_tool
    args: {operation: add, a: 40, b: 2}
    expect: {subset: {content: [{type: text, text: "42"}]}}
  - tool: demo_calculator_tool
    name: division
    args: {operation: div, a: 1, b: 0}
    expect: {text_contains: "zero"}
  - tool: whoami
    args: {token: "{{$demo.API_TOKEN}}"}
    expect: {exact: {content: [{type: text, text: "ok"}], isError: false}}
"""


def test_cases_are_parsed_and_named():
    cases = golden.parse(YAML)
    assert [c["name"] for c in cases] == ["demo_calculator_tool", "division", "whoami"]
    assert cases[0]["args"] == {"operation": "add", "a": 40, "b": 2}
    assert golden.parse("") == [] and golden.parse(None) == [] and golden.parse("cases: []") == []


@pytest.mark.parametrize(
    "text, msg",
    [
        ("cases: 3", "`cases` must be a list"),
        ("cases: [{args: {}}]", "case 1: `tool` is required"),
        ("cases: [{tool: t, expect: {}}]", "case t: `expect` needs subset, exact or text_contains"),
        (
            "cases: [{tool: t, expect: {subset: {}, exact: {}}}]",
            "case t: `expect` needs exactly one of subset, exact, text_contains",
        ),
        ("cases: [{tool: t, args: 5, expect: {text_contains: x}}]", "case t: `args` must be a mapping"),
        ("- not a mapping", "tests.yaml must be a mapping with `cases`"),
        ("cases: [\n  - tool: t\n expect: {", "tests.yaml is not valid YAML"),
    ],
)
def test_bad_files_say_what_is_wrong(text, msg):
    with pytest.raises(golden.GoldenError) as e:
        golden.parse(text)
    assert msg in str(e.value)


def test_secret_references_are_resolved_before_the_call():
    args = {
        "token": "{{$demo.API_TOKEN}}",
        "n": 1,
        "nested": {"k": "{{$demo.OTHER}} and {{$demo.API_TOKEN}}"},
        "xs": ["{{$demo.OTHER}}"],
    }
    out = golden.resolve_args(args, {"demo.API_TOKEN": "t0k", "demo.OTHER": "o"}.get)
    assert out == {"token": "t0k", "n": 1, "nested": {"k": "o and t0k"}, "xs": ["o"]}
    with pytest.raises(golden.GoldenError) as e:
        golden.resolve_args({"a": "{{$demo.MISSING}}"}, {}.get)
    assert "secret demo.MISSING" in str(e.value)


def test_subset_match_recurses_into_lists_and_mappings():
    got = {"content": [{"type": "text", "text": "42"}], "isError": False}
    assert golden.check({"subset": {"content": [{"text": "42"}]}}, got) is None
    assert golden.check({"subset": {"content": [{"text": "41"}]}}, got) == "content[0].text: expected '41', got '42'"
    assert golden.check({"subset": {"content": [{}, {}]}}, got) == "content: expected at least 2 items, got 1"
    assert golden.check({"subset": {"missing": 1}}, got) == "missing: expected 1, got nothing"
    assert golden.check({"subset": {"isError": True}}, got) == "isError: expected True, got False"
    assert golden.check({"subset": {"content": {"x": 1}}}, got) == "content: expected a mapping, got a list"


def test_exact_and_text_contains():
    got = {"content": [{"type": "text", "text": "cannot divide by zero"}], "isError": True}
    assert golden.check({"exact": got}, got) is None
    r = golden.check({"exact": {"content": got["content"]}}, got)
    assert r.startswith("exact: expected ") and "isError" in r
    assert golden.check({"text_contains": "zero"}, got) is None
    assert golden.check({"text_contains": "one"}, got) == "text_contains: 'one' not in 'cannot divide by zero'"
    assert golden.check({"text_contains": "x"}, {"content": []}) == "text_contains: 'x' not in ''"


def test_a_jsonrpc_error_fails_the_case_with_its_message():
    resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "unknown tool"}}
    assert golden.check_response({"name": "c", "expect": {"text_contains": "x"}}, resp) == "error -32602: unknown tool"
    assert (
        golden.check_response({"name": "c", "expect": {"text_contains": "x"}}, {"jsonrpc": "2.0", "id": 1})
        == "no result in the response"
    )
    ok = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "x"}]}}
    assert golden.check_response({"name": "c", "expect": {"text_contains": "x"}}, ok) is None


def test_request_is_a_tools_call():
    req = golden.request({"tool": "calc", "args": {"a": 1}}, 7)
    assert req == {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "calc", "arguments": {"a": 1}}}
