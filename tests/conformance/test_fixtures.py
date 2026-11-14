"""Fixtures are self-consistent with CONTRACTS §1 and §23.3 (no env needed; always runs)."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ramen_tests.env import FIXTURES

SCHEMA = json.loads((FIXTURES / "proto.schema.json").read_text())
PKG_DIRS = ("tools", "resources", "prompts")


def protos(group: str) -> list[Path]:
    root = FIXTURES / group / "mcp"
    return sorted(
        p for d in PKG_DIRS if (root / d).exists() for p in (root / d).glob("*/*.json") if p.stem == p.parent.name
    )


def test_schema_is_valid_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize(
    "path",
    protos("demo_group")
    + protos("secrets_group")
    + protos("numpy_group")
    + protos("schema_group")
    + protos("guarded_group")
    + protos("policy_group")
    + protos("broken_guarded_group")
    + protos("conformance_group")
    + protos("mrtr_group"),
    ids=lambda p: p.parent.name,
)
def test_valid_fixture_matches_schema_and_layout(path: Path):
    doc = json.loads(path.read_text())
    Draft202012Validator(SCHEMA).validate(doc)
    assert path.stem == doc["name"] == path.parent.name
    assert doc["type"] + "s" == path.parent.parent.name
    assert doc["description"].strip(), "the official suite fails a tool or prompt without a description"
    if doc["type"] == "prompt" and "skill" in doc:
        assert (path.parent / doc["skill"]).exists() and (path.parent / doc["settings"]).exists()
        assert "callable" not in doc
    else:  # tools, resources and (23.3) callable prompts
        assert (path.parent / f"{doc['name']}.py").exists()


def test_demo_group_mirrors_upstream_layout():
    root = FIXTURES / "demo_group"
    assert (root / "README.md").exists()
    assert (root / "mcp/requirements.txt").exists()
    assert (root / "mcp/tools/demo_calculator_tool/utils/calculator_utils.py").exists()
    assert (root / "mcp/prompts/get_calculation_prompt/SKILL.md").exists()


@pytest.mark.parametrize(
    "pkg,schema_rejects",
    [
        ("bad_type_tool", True),  # type "widget"
        ("no_callable_tool", False),  # structurally fine; loader must fail at import (callable missing)
        ("wrong_type_tool", False),  # valid resource proto in tools/ → loader folder/type mismatch
        ("misnamed_tool", False),  # valid proto; folder ≠ name → loader error
    ],
)
def test_broken_fixture_shape(pkg: str, schema_rejects: bool):
    folder = FIXTURES / "broken_group" / "mcp" / "tools" / pkg
    [path] = list(folder.glob("*.json"))
    doc = json.loads(path.read_text())
    errors = list(Draft202012Validator(SCHEMA).iter_errors(doc))
    assert bool(errors) == schema_rejects, errors
    layout_ok = path.stem == doc.get("name") == folder.name and doc.get("type") == "tool"
    if pkg == "no_callable_tool":
        src = (folder / f"{pkg}.py").read_text()
        assert f"def {doc['callable']}" not in src
    else:
        assert not (layout_ok and not errors), "fixture is not actually broken"


def test_broken_group_keeps_one_valid_tool():
    doc = json.loads((FIXTURES / "broken_group/mcp/tools/demo_calculator_tool/demo_calculator_tool.json").read_text())
    Draft202012Validator(SCHEMA).validate(doc)


def test_a_header_parameter_must_be_a_token_and_not_a_number():
    """§23.2: `header` is an RFC 9110 token on a string, integer, boolean or enum parameter."""
    base = {"type": "tool", "name": "t", "description": "d", "callable": "run"}
    ok = {
        **base,
        "input": {"region": {"type": "string", "header": "Region"}, "mode": {"enum": ["a"], "header": "Mode"}},
    }
    Draft202012Validator(SCHEMA).validate(ok)
    for bad in ({"type": "string", "header": "Has Space"}, {"type": "number", "header": "Amount"}):
        assert list(Draft202012Validator(SCHEMA).iter_errors({**base, "input": {"p": bad}})), bad


def test_conformance_group_has_every_fixture_the_official_suite_calls():
    """Names from the suite at the pinned version (digest sections 1, 2, 3, 4.9, 6)."""
    have = {p.stem: json.loads(p.read_text()) for p in protos("conformance_group")}
    tools = {
        "test_simple_text", "test_image_content", "test_audio_content", "test_embedded_resource",
        "test_multiple_content_types", "test_error_handling", "test_tool_with_progress", "test_tool_with_logging",
        "test_sampling", "test_elicitation", "test_missing_capability", "test_streaming_elicitation",
        "test_logging_tool", "test_trigger_tool_change", "test_trigger_prompt_change", "json_schema_2020_12_tool",
        "greet", "slow_compute", "failing_job", "protocol_error_job", "confirm_delete", "multi_input",
        "test_tool_with_task",
        *(f"test_input_required_result_{x}" for x in (
            "elicitation", "sampling", "list_roots", "request_state", "multiple_inputs", "multi_round",
            "tampered_state", "capabilities",
        )),
    }  # fmt: skip
    prompts = {
        "test_simple_prompt", "test_prompt_with_arguments", "test_prompt_with_embedded_resource",
        "test_prompt_with_image", "test_input_required_result_prompt",
    }  # fmt: skip
    assert tools <= {n for n, d in have.items() if d["type"] == "tool"}
    assert prompts <= {n for n, d in have.items() if d["type"] == "prompt"}
    uris = {d["uri"] for d in have.values() if d["type"] == "resource"}
    assert uris >= {
        "test://static-text",
        "test://static-binary",
        "test://template/{id}/data",
        "test://watched-resource",
    }
    assert {n for n in tools if have[n].get("task")} >= {"slow_compute", "failing_job", "confirm_delete", "multi_input"}
    assert any("header" in p for d in have.values() for p in d["input"].values()), "one x-mcp-header parameter"
