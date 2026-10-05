import pytest

from ramen_runtime.proto import ProtoError, input_schema, validate_proto

BASE = {
    "type": "tool",
    "name": "t",
    "description": "d",
    "callable": "f",
    "input": {},
    "output": {"type": "number"},
    "error": {"type": "string"},
}


def test_valid_tool():
    validate_proto(BASE, "tools", "t", "t")


def test_type_not_allowed():
    with pytest.raises(ProtoError, match="type"):
        validate_proto({**BASE, "type": "widget"}, "tools", "t", "t")


def test_type_mismatch_with_folder():
    with pytest.raises(ProtoError, match="folder"):
        validate_proto({**BASE, "type": "resource", "uri": "x", "mime_type": "y"}, "tools", "t", "t")


def test_name_mismatch():
    with pytest.raises(ProtoError, match="name"):
        validate_proto({**BASE, "name": "other"}, "tools", "t", "t")


def test_stem_mismatch():
    with pytest.raises(ProtoError, match="name"):
        validate_proto(BASE, "tools", "t", "other")


def test_missing_callable():
    p = {k: v for k, v in BASE.items() if k != "callable"}
    with pytest.raises(ProtoError, match="callable"):
        validate_proto(p, "tools", "t", "t")


def test_resource_requires_uri():
    with pytest.raises(ProtoError, match="uri"):
        validate_proto({**BASE, "type": "resource"}, "resources", "t", "t")


def test_prompt_has_no_callable_but_skill():
    p = {
        "type": "prompt",
        "name": "p",
        "description": "d",
        "input": {},
        "skill": "SKILL.md",
        "settings": "settings.json",
    }
    validate_proto(p, "prompts", "p", "p")
    with pytest.raises(ProtoError, match="skill"):
        validate_proto({k: v for k, v in p.items() if k != "skill"}, "prompts", "p", "p")


def test_bad_param_type():
    with pytest.raises(ProtoError):
        validate_proto({**BASE, "input": {"x": {"type": "blob"}}}, "tools", "t", "t")


def test_input_schema_maps_types():
    s = input_schema(
        {
            "a": {"type": "number", "description": "A"},
            "b": {"type": "string"},
            "c": {"type": "integer"},
            "d": {"type": "boolean"},
            "e": {"enum": ["x", "y"], "description": "E"},
        }
    )
    assert s["type"] == "object"
    assert s["properties"]["a"] == {"type": "number", "description": "A"}
    assert s["properties"]["b"] == {"type": "string"}
    assert s["properties"]["c"] == {"type": "integer"}
    assert s["properties"]["d"] == {"type": "boolean"}
    assert s["properties"]["e"] == {"type": "string", "enum": ["x", "y"], "description": "E"}
    assert s["required"] == ["a", "b", "c", "d", "e"]
    assert s["additionalProperties"] is False


def test_output_must_be_a_valid_json_schema():
    with pytest.raises(ProtoError, match="output"):
        validate_proto({**BASE, "output": {"type": "float"}}, "tools", "t", "t")
    validate_proto({**BASE, "output": {"type": "object", "properties": {"n": {"type": "integer"}}}}, "tools", "t", "t")
