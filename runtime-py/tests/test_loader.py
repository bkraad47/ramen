import json

from conftest import write_pkg

from ramen_runtime.loader import load

TOOL = {
    "type": "tool",
    "name": "x",
    "description": "d",
    "callable": "f",
    "input": {"a": {"type": "string"}},
    "output": {"type": "string"},
    "error": {"type": "string"},
}


def test_loads_demo(demo_bucket):
    reg = load(demo_bucket)
    assert [t.name for t in reg.tools] == ["demo_calculator_tool"]
    assert [r.name for r in reg.resources] == ["demo_readme"]
    assert [p.name for p in reg.prompts] == ["get_calculation_prompt"]
    assert reg.errors == []
    d = reg.describe()
    assert d["tools"][0]["inputSchema"]["required"] == ["var1", "var2", "func"]
    assert d["resources"][0] == {
        "uri": "ramen://demo/readme",
        "name": "demo_readme",
        "description": "README of the demo group repo.",
        "mimeType": "text/markdown",
    }
    assert d["prompts"][0]["arguments"] == [
        {"name": "request", "description": "the user's arithmetic question", "required": True}
    ]
    assert d["prompts"][0]["_meta"]["settings"]["max_tool_calls"] == 10
    assert d["errors"] == []


def test_utils_on_sys_path_and_callable_bound(demo_bucket):
    reg = load(demo_bucket)
    assert reg.tools[0].func(1, 2, "add") == 3


def test_missing_mcp_dir(tmp_path):
    reg = load(tmp_path)
    assert reg.tools == [] and reg.errors == [{"package": "mcp", "reason": "missing mcp/ directory"}]


def test_errors_are_per_package(tmp_path):
    write_pkg(tmp_path, "tools", "good", {**TOOL, "name": "good"}, "def f(a):\n    return a\n")
    write_pkg(
        tmp_path, "tools", "badtype", {**TOOL, "name": "badtype", "type": "resource"}, "def f(a):\n    return a\n"
    )
    write_pkg(tmp_path, "tools", "nojson", {}, "def f(a): pass\n", stem="wrongstem")
    write_pkg(tmp_path, "tools", "nofunc", {**TOOL, "name": "nofunc"}, "y = 1\n")
    write_pkg(tmp_path, "tools", "nopy", {**TOOL, "name": "nopy"})
    write_pkg(tmp_path, "tools", "syntax", {**TOOL, "name": "syntax"}, "def f(:\n")
    (tmp_path / "mcp" / "tools" / "badjson").mkdir()
    (tmp_path / "mcp" / "tools" / "badjson" / "badjson.json").write_text("{nope")
    (tmp_path / "mcp" / "tools" / "stray.txt").write_text("")
    reg = load(tmp_path)
    assert [t.name for t in reg.tools] == ["good"]
    by = {e["package"]: e["reason"] for e in reg.errors}
    assert set(by) == {"tools/badtype", "tools/nojson", "tools/nofunc", "tools/nopy", "tools/syntax", "tools/badjson"}
    assert "folder" in by["tools/badtype"]
    assert "callable" in by["tools/nofunc"]
    assert "json" in by["tools/badjson"].lower()


def test_prompt_missing_skill_file(tmp_path):
    write_pkg(
        tmp_path,
        "prompts",
        "p",
        {
            "type": "prompt",
            "name": "p",
            "description": "d",
            "input": {},
            "skill": "SKILL.md",
            "settings": "settings.json",
        },
    )
    reg = load(tmp_path)
    assert reg.prompts == [] and "SKILL.md" in reg.errors[0]["reason"]


def test_prompt_without_settings_file_ok(tmp_path):
    d = write_pkg(
        tmp_path, "prompts", "p", {"type": "prompt", "name": "p", "description": "d", "input": {}, "skill": "SKILL.md"}
    )
    (d / "SKILL.md").write_text("hi")
    reg = load(tmp_path)
    assert reg.prompts[0].settings == {}


def test_reload_replaces_module(tmp_path):
    write_pkg(tmp_path, "tools", "x", TOOL, "def f(a):\n    return 'v1'\n")
    assert load(tmp_path).tools[0].func("") == "v1"
    write_pkg(tmp_path, "tools", "x", TOOL, "def f(a):\n    return 'v2'\n")
    assert load(tmp_path).tools[0].func("") == "v2"
    assert json.loads(json.dumps(load(tmp_path).describe()))
