"""Fixtures are self-consistent with CONTRACTS §1 (no env needed; always runs)."""

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
    + protos("broken_guarded_group"),
    ids=lambda p: p.parent.name,
)
def test_valid_fixture_matches_schema_and_layout(path: Path):
    doc = json.loads(path.read_text())
    Draft202012Validator(SCHEMA).validate(doc)
    assert path.stem == doc["name"] == path.parent.name
    assert doc["type"] + "s" == path.parent.parent.name
    if doc["type"] == "prompt":
        assert (path.parent / doc["skill"]).exists() and (path.parent / doc["settings"]).exists()
        assert "callable" not in doc
    else:
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
