"""B3 / C5 (0.7.0): schema compatibility between a canary's manifest and the stable track's last stored one."""

import hashlib
import json

import pytest

from ramen_console import compat


def tool(name, props=None, required=(), **extra):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": "", "inputSchema": schema, **extra}


def manifest(tools=(), resources=(), prompts=()):
    return {"tools": list(tools), "resources": list(resources), "prompts": list(prompts), "errors": []}


OLD = manifest(
    tools=[tool("calc", {"a": {"type": "number"}, "op": {"enum": ["add", "sub"]}}, ["a"])],
    resources=[{"uri": "ramen://demo/readme", "name": "readme", "mimeType": "text/plain"}],
    prompts=[{"name": "greet", "arguments": [{"name": "who", "required": True}], "_meta": {"settings": {}}}],
)


def test_hash_is_the_contract_c1_canonical_sha256():
    body = {
        "tools": OLD["tools"],
        "resources": OLD["resources"],
        "prompts": [{"name": "greet", "arguments": OLD["prompts"][0]["arguments"]}],
    }
    want = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert compat.manifest_hash(OLD) == want
    assert compat.manifest_hash({"tools": OLD["tools"], "resources": [], "prompts": []}) != want
    assert compat.manifest_hash({}) == compat.manifest_hash({"tools": [], "resources": [], "prompts": []})


def test_manifest_of_keeps_definitions_and_prefers_the_workers_hash():
    m = compat.manifest_of(OLD)
    assert set(m) == {"hash", "tools", "resources", "prompts"} and m["hash"] == compat.manifest_hash(OLD)
    assert m["tools"] == OLD["tools"] and "errors" not in m
    assert compat.manifest_of({**OLD, "hash": "abc"})["hash"] == "abc"  # the runtime's own hash wins (C1)
    assert compat.manifest_of(None) is None and compat.manifest_of({"errors": ["x"]}) is None


def test_identical_manifests_are_compatible():
    d = compat.diff(OLD, OLD)
    assert d == {"breaking": [], "additive": []} and compat.is_breaking(d) is False


@pytest.mark.parametrize(
    "new, expect",
    [
        (manifest(resources=OLD["resources"], prompts=OLD["prompts"]), "removed tool calc"),
        (manifest(tools=OLD["tools"], prompts=OLD["prompts"]), "removed resource ramen://demo/readme"),
        (manifest(tools=OLD["tools"], resources=OLD["resources"]), "removed prompt greet"),
        ({**OLD, "tools": [tool("calc", {"a": {"type": "number"}}, ["a"])]}, "tool calc: removed property op"),
        (
            {
                **OLD,
                "tools": [
                    tool("calc", {**OLD["tools"][0]["inputSchema"]["properties"], "b": {"type": "number"}}, ["a", "b"])
                ],
            },
            "tool calc: new required property b",
        ),
        (
            {**OLD, "tools": [tool("calc", {"a": {"type": "string"}, "op": {"enum": ["add", "sub"]}}, ["a"])]},
            "tool calc: property a type number -> string",
        ),
        (
            {**OLD, "tools": [tool("calc", {"a": {"type": "number"}, "op": {"enum": ["add"]}}, ["a"])]},
            "tool calc: property op enum narrowed, removed sub",
        ),
        (
            {**OLD, "tools": [tool("calc", {"a": {"type": "number"}, "op": {"type": "string"}}, ["a", "op"])]},
            "tool calc: property op is now required",
        ),
        ({**OLD, "prompts": [{"name": "greet", "arguments": []}]}, "prompt greet: removed argument who"),
        (
            {
                **OLD,
                "prompts": [
                    {
                        "name": "greet",
                        "arguments": OLD["prompts"][0]["arguments"] + [{"name": "tone", "required": True}],
                    }
                ],
            },
            "prompt greet: new required argument tone",
        ),
    ],
)
def test_breaking_changes(new, expect):
    d = compat.diff(OLD, new)
    assert expect in d["breaking"], d
    assert compat.is_breaking(d)


@pytest.mark.parametrize(
    "new, expect",
    [
        ({**OLD, "tools": OLD["tools"] + [tool("echo")]}, "added tool echo"),
        (
            {**OLD, "resources": OLD["resources"] + [{"uri": "ramen://demo/x", "name": "x"}]},
            "added resource ramen://demo/x",
        ),
        ({**OLD, "prompts": OLD["prompts"] + [{"name": "bye", "arguments": []}]}, "added prompt bye"),
        (
            {
                **OLD,
                "tools": [
                    tool("calc", {**OLD["tools"][0]["inputSchema"]["properties"], "b": {"type": "number"}}, ["a"])
                ],
            },
            "tool calc: new optional property b",
        ),
        (
            {**OLD, "tools": [tool("calc", {"a": {"type": "number"}, "op": {"enum": ["add", "sub", "mul"]}}, ["a"])]},
            "tool calc: property op enum widened, added mul",
        ),
        (
            {**OLD, "tools": [tool("calc", {"a": {"type": "number"}, "op": {"enum": ["add", "sub"]}})]},
            "tool calc: property a no longer required",
        ),
        (
            {
                **OLD,
                "prompts": [
                    {
                        "name": "greet",
                        "arguments": OLD["prompts"][0]["arguments"] + [{"name": "tone", "required": False}],
                    }
                ],
            },
            "prompt greet: new optional argument tone",
        ),
    ],
)
def test_additive_changes(new, expect):
    d = compat.diff(OLD, new)
    assert d["breaking"] == [] and expect in d["additive"], d
    assert not compat.is_breaking(d)


def test_descriptions_and_meta_do_not_matter():
    new = json.loads(json.dumps(OLD))
    new["tools"][0]["description"] = "changed"
    new["prompts"][0]["_meta"] = {"settings": {"x": 1}}
    assert compat.diff(OLD, new) == {"breaking": [], "additive": []}


def test_nested_schemas_are_compared():
    old = manifest(
        tools=[tool("t", {"q": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}})]
    )
    new = manifest(tools=[tool("t", {"q": {"type": "object", "properties": {"x": {"type": "string"}}}})])
    d = compat.diff(old, new)
    assert "tool t: property q.x type number -> string" in d["breaking"]
    assert "tool t: removed property q.y" in d["breaking"]
    items_old = manifest(tools=[tool("t", {"xs": {"type": "array", "items": {"type": "number"}}})])
    items_new = manifest(tools=[tool("t", {"xs": {"type": "array", "items": {"type": "string"}}})])
    assert "tool t: property xs[] type number -> string" in compat.diff(items_old, items_new)["breaking"]


def test_type_lists_and_missing_pieces():
    old = manifest(tools=[tool("t", {"a": {"type": ["number", "string"]}, "b": {}})])
    assert compat.diff(old, manifest(tools=[tool("t", {"a": {"type": ["string", "number"]}, "b": {}})])) == {
        "breaking": [],
        "additive": [],
    }
    d = compat.diff(old, manifest(tools=[tool("t", {"a": {"type": "number"}, "b": {"enum": [1]}})]))
    assert "tool t: property a type number, string -> number" in d["breaking"]
    assert "tool t: property b enum narrowed, removed any value" in d["breaking"]
    assert compat.diff(None, OLD) == {"breaking": [], "additive": []}  # nothing stored yet: nothing to break
    assert compat.diff({}, OLD)["additive"] == [
        "added tool calc",
        "added resource ramen://demo/readme",
        "added prompt greet",
    ]


def test_output_schema_changes_count_the_same_way():
    old = manifest(tools=[tool("t", outputSchema={"type": "object", "properties": {"n": {"type": "number"}}})])
    new = manifest(tools=[tool("t", outputSchema={"type": "object", "properties": {"n": {"type": "string"}}})])
    assert "tool t: output property n type number -> string" in compat.diff(old, new)["breaking"]
    gone = manifest(tools=[tool("t")])
    assert "tool t: output schema removed" in compat.diff(old, gone)["breaking"]
    assert "tool t: output schema added" in compat.diff(gone, old)["additive"]


def test_summary_is_one_line_per_change():
    d = {"breaking": ["removed tool calc"], "additive": ["added tool echo"]}
    assert compat.summary(d) == "breaking: removed tool calc; additive: added tool echo"
    assert compat.summary({"breaking": [], "additive": []}) == "no schema changes"
