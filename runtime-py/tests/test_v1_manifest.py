"""CONTRACTS §23.3: what a v1 package adds to the load result, and that a 0.8 package describes exactly as before."""

import json

import pytest
from conftest import FIXTURES, write_pkg

from ramen_runtime.loader import load
from ramen_runtime.proto import ProtoError, input_schema, validate_proto

TOOL = {"type": "tool", "name": "t", "description": "d", "callable": "f", "input": {"a": {"type": "string"}}}
RES = {
    "type": "resource",
    "name": "r",
    "description": "d",
    "uri": "u://items/{id}/data",
    "mime_type": "application/json",
    "callable": "f",
    "input": {"id": {"type": "string"}},
}
PROMPT = {"type": "prompt", "name": "p", "description": "d", "input": {"topic": {"type": "string"}}, "callable": "f"}
# pinned before any v1 change: the 0.8.0 describe() of tests/fixtures/demo
DEMO_HASH = "8d6d4adee1658c772da2ecf3640e581abf1c03f79c5b44430abb3bcc6aad4f55"
DEMO_DESCRIBE_SHA = "26135206217209b0f3debe7d3862d0a53e4c3bb569729de3082b650183c1d975"


def test_a_0_8_package_keeps_its_manifest_hash_and_describe_bytes():
    import hashlib

    d = load(FIXTURES / "demo").describe()
    assert d["hash"] == DEMO_HASH
    assert hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest() == DEMO_DESCRIBE_SHA
    assert list(d) == ["tools", "resources", "prompts", "hash", "errors"]


def test_header_param_becomes_x_mcp_header():
    s = input_schema({"region": {"type": "string", "header": "Region"}, "n": {"type": "integer"}})
    assert s["properties"]["region"] == {"type": "string", "x-mcp-header": "Region"}
    assert "x-mcp-header" not in s["properties"]["n"]


@pytest.mark.parametrize(
    ("inp", "match"),
    [
        ({"a": {"type": "number", "header": "A"}}, "string, integer, boolean"),
        ({"a": {"type": "string", "header": "bad name"}}, "token"),
        ({"a": {"type": "string", "header": "X"}, "b": {"enum": ["1"], "header": "x"}}, "twice"),
    ],
)
def test_header_validation_fails_the_package(inp, match):
    with pytest.raises(ProtoError, match=match):
        validate_proto({**TOOL, "input": inp}, "tools", "t", "t")


def test_header_is_for_tools_only_and_valid_headers_load():
    validate_proto(
        {**TOOL, "input": {"a": {"type": "integer", "header": "A"}, "b": {"enum": ["x"], "header": "B-1"}}},
        "tools",
        "t",
        "t",
    )
    with pytest.raises(ProtoError, match="only for tool"):
        validate_proto({**PROMPT, "input": {"topic": {"type": "string", "header": "T"}}}, "prompts", "p", "p")


def test_task_flag_is_surfaced_only_where_set(tmp_path):
    write_pkg(tmp_path, "tools", "t", {**TOOL, "task": True}, "def f(a):\n    return a\n")
    write_pkg(tmp_path, "tools", "u", {**TOOL, "name": "u", "task": False}, "def f(a):\n    return a\n")
    tools = {t["name"]: t for t in load(tmp_path).describe()["tools"]}
    assert tools["t"]["task"] is True and "task" not in tools["u"]
    with pytest.raises(ProtoError, match="task"):
        validate_proto({**TOOL, "task": "yes"}, "tools", "t", "t")


def test_resource_template_is_listed_apart_with_enum_variables(tmp_path):
    write_pkg(tmp_path, "resources", "r", RES, "def f(id):\n    return {'id': id}\n")
    kinds = {**RES, "name": "k", "uri": "u://{kind}/all", "input": {"kind": {"enum": ["a", "b"], "description": "x"}}}
    write_pkg(tmp_path, "resources", "k", kinds, "def f(kind):\n    return kind\n")
    static = {**RES, "name": "s", "uri": "u://static", "input": {}}
    write_pkg(tmp_path, "resources", "s", static, "def f():\n    return 's'\n")
    d = load(tmp_path).describe()
    assert d["errors"] == []
    assert [r["uri"] for r in d["resources"]] == ["u://static"]
    assert d["resourceTemplates"] == [
        {
            "name": "k",
            "uriTemplate": "u://{kind}/all",
            "description": "d",
            "mimeType": "application/json",
            "variables": {"kind": {"enum": ["a", "b"]}},
        },
        {"name": "r", "uriTemplate": "u://items/{id}/data", "description": "d", "mimeType": "application/json"},
    ]
    assert list(d) == ["tools", "resources", "prompts", "resourceTemplates", "hash", "errors"]
    h = d["hash"]
    write_pkg(tmp_path, "resources", "r", {**RES, "description": "changed"}, "def f(id):\n    return id\n")
    assert load(tmp_path).describe()["hash"] != h  # templates are part of the manifest hash


@pytest.mark.parametrize(
    ("uri", "inp", "match"),
    [
        ("u://{id}", {}, "must match"),
        ("u://{id}", {"id": {"type": "string"}, "x": {"type": "string"}}, "must match"),
        ("u://{id}/{id}", {"id": {"type": "string"}}, "twice"),
        ("u://{1bad}", {"1bad": {"type": "string"}}, "not a variable name"),
        ("u://{id}", {"id": {"type": "integer"}}, "string or an enum"),
    ],
)
def test_resource_template_validation(uri, inp, match):
    with pytest.raises(ProtoError, match=match):
        validate_proto({**RES, "uri": uri, "input": inp}, "resources", "r", "r")


def test_prompt_takes_exactly_one_of_skill_or_callable(tmp_path):
    validate_proto(PROMPT, "prompts", "p", "p")
    with pytest.raises(ProtoError, match="skill.*callable"):
        validate_proto({**PROMPT, "skill": "SKILL.md"}, "prompts", "p", "p")
    write_pkg(tmp_path, "prompts", "p", PROMPT)  # callable prompt without p.py
    assert "missing p.py" in load(tmp_path).errors[0]["reason"]


def test_prompt_argument_enum_is_surfaced(tmp_path):
    proto = {**PROMPT, "input": {"topic": {"enum": ["cats", "dogs"], "description": "what"}, "n": {"type": "string"}}}
    d = write_pkg(tmp_path, "prompts", "p", proto, "def f(topic, n):\n    return topic\n")
    (d / "settings.json").write_text('{"k": 1}')
    p = load(tmp_path).describe()["prompts"][0]
    assert p["arguments"] == [
        {"name": "topic", "description": "what", "required": True, "enum": ["cats", "dogs"]},
        {"name": "n", "description": "", "required": True},
    ]
    assert p["_meta"] == {"settings": {"k": 1}}
