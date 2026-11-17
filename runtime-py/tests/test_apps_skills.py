"""CONTRACTS §33.1 MCP Apps and §33.2 Skills over MCP: load, list, read, `_meta`, limits and refusals."""

import base64
import io
import json
import os

import pytest
from conftest import write_pkg

from ramen_runtime import context
from ramen_runtime.apps import MIME
from ramen_runtime.executor import Executor
from ramen_runtime.loader import load
from ramen_runtime.rpc import Server

TOOL = {"type": "tool", "name": "t", "description": "d", "callable": "f", "input": {}}


def errors(reg) -> dict:
    return {e["package"]: e["reason"] for e in reg.errors}


def test_apps_are_resources_and_tools_point_at_them(ui_bucket):
    d = load(ui_bucket).describe()
    assert d["errors"] == []
    by = {r["uri"]: r for r in d["resources"]}
    chart = by["ui://g1/chart"]
    assert chart["mimeType"] == MIME and chart["name"] == "chart" and chart["title"] == "Histogram"
    assert chart["_meta"] == {"ui/csp": {"connectDomains": [], "resourceDomains": ["https://cdn.example"]}}
    assert "_meta" not in by["ui://g1/plain"] and "title" not in by["ui://g1/plain"]
    assert d["apps"] == [
        {
            "name": "chart",
            "uri": "ui://g1/chart",
            "title": "Histogram",
            "description": "Bars from the chart_data result.",
        },
        {"name": "plain", "uri": "ui://g1/plain", "title": "", "description": ""},
    ]
    assert d["tools"][0]["_meta"] == {"ui/resourceUri": "ui://g1/chart"}


def test_skills_are_listed_with_sorted_files_and_a_sha12_version(ui_bucket):
    d = load(ui_bucket).describe()
    (s,) = d["skills"]
    assert s["name"] == "iris-analyst" and s["description"].startswith("Analyse the Iris data")
    assert s["files"] == ["SKILL.md", "assets/logo.bin", "references/notes.md"]
    assert len(s["version"]) == 12 and int(s["version"], 16) >= 0
    assert d["skills"] == load(ui_bucket).describe()["skills"]  # stable
    (ui_bucket / "mcp/skills/iris-analyst/references/notes.md").write_text("# changed")
    assert load(ui_bucket).describe()["skills"][0]["version"] != s["version"]


def test_hash_covers_apps_and_skills(ui_bucket):
    h = load(ui_bucket).describe()["hash"]
    (ui_bucket / "mcp/apps/plain/index.html").write_text("<p>other</p>")
    assert load(ui_bucket).describe()["hash"] == h  # the HTML body is not part of the manifest
    (ui_bucket / "mcp/apps/chart/app.yaml").write_text("title: Other\n")
    h2 = load(ui_bucket).describe()["hash"]
    assert h2 != h
    (ui_bucket / "mcp/skills/iris-analyst/extra.txt").write_text("x")
    assert load(ui_bucket).describe()["hash"] != h2


def test_group_defaults_and_pycache_and_symlinks_are_skipped(ui_bucket, monkeypatch):
    monkeypatch.delenv("RAMEN_GROUP")
    sk = ui_bucket / "mcp/skills/iris-analyst"
    (sk / "__pycache__").mkdir()
    (sk / "__pycache__/x.pyc").write_bytes(b"\0")
    os.symlink(ui_bucket / "mcp/apps/plain/index.html", sk / "link.html")
    reg = load(ui_bucket)
    assert reg.apps[0].uri == "ui://default/chart"
    assert reg.skills[0].files == ["SKILL.md", "assets/logo.bin", "references/notes.md"]


def test_app_load_failures(ui_bucket):
    apps = ui_bucket / "mcp/apps"
    (apps / "big").mkdir()
    (apps / "big/index.html").write_bytes(b"x" * (512 * 1024 + 1))
    (apps / "noindex").mkdir()
    (apps / "bad name").mkdir()
    (apps / "bad name/index.html").write_text("x")
    for name, yaml_ in [
        ("notmap", "- a\n"),
        ("unknown", "foo: 1\n"),
        ("badtitle", "title: [1]\n"),
        ("badcsp", "csp: {connectDomains: 'x'}\n"),
        ("badyaml", "title: [\n"),
    ]:
        (apps / name).mkdir()
        (apps / name / "index.html").write_text("x")
        (apps / name / "app.yaml").write_text(yaml_)
    write_pkg(ui_bucket, "tools", "noapp", {**TOOL, "name": "noapp", "ui": "nope"}, "def f():\n    return 1\n")
    reg = load(ui_bucket)
    by = errors(reg)
    assert [a.name for a in reg.apps] == ["chart", "plain"]
    assert by["apps/big"] == f"index.html is {512 * 1024 + 1} bytes, over the 512 KiB limit"
    assert by["apps/noindex"] == "missing index.html"
    assert "must match" in by["apps/bad name"]
    assert by["apps/notmap"] == "app.yaml must be a mapping"
    assert by["apps/unknown"] == "app.yaml: unknown key foo"
    assert by["apps/badtitle"] == "app.yaml: title must be a str"
    assert by["apps/badcsp"] == "app.yaml: csp values must be lists of strings"
    assert by["apps/badyaml"].startswith("app.yaml: invalid yaml")
    assert by["tools/noapp"] == "ui: app 'nope' not found under mcp/apps"
    assert [t.name for t in reg.tools] == ["chart_data"]


def test_skill_load_failures(ui_bucket):
    skills = ui_bucket / "mcp/skills"

    def skill(name: str, text: str, **files: bytes):
        (skills / name).mkdir()
        (skills / name / "SKILL.md").write_text(text)
        for f, data in files.items():
            (skills / name / f).write_bytes(data)

    skill("nofront", "# no frontmatter\n")
    skill("wrongname", "---\nname: other\ndescription: d\n---\n")
    skill("nodesc", "---\nname: nodesc\n---\n")
    skill("badyaml", "---\nname: [\n---\n")
    skill("notmap", "---\n- a\n---\n")
    skill("big", "---\nname: big\ndescription: d\n---\n", **{"data.bin": b"x" * (256 * 1024 + 1)})
    (skills / "empty").mkdir()
    (skills / "bad.name").mkdir()
    (skills / "bad.name/SKILL.md").write_text("---\nname: bad.name\ndescription: d\n---\n")
    by = errors(load(ui_bucket))
    assert by["skills/nofront"].startswith("SKILL.md: missing frontmatter")
    assert by["skills/wrongname"] == "SKILL.md: frontmatter name 'other' must be 'wrongname'"
    assert by["skills/nodesc"] == "SKILL.md: frontmatter needs a description"
    assert by["skills/badyaml"].startswith("SKILL.md: invalid frontmatter")
    assert by["skills/notmap"] == "SKILL.md: frontmatter must be a mapping"
    assert by["skills/big"] == f"data.bin is {256 * 1024 + 1} bytes, over the 256 KiB limit"
    assert by["skills/empty"] == "missing SKILL.md"
    assert "must match" in by["skills/bad.name"]


def test_read_app_and_skill_files(ui_bucket):
    exe = Executor(load(ui_bucket))
    (c,) = exe.read_resource("ui://g1/chart")["contents"]
    assert c["uri"] == "ui://g1/chart" and c["mimeType"] == MIME and "<svg" in c["text"]
    (c,) = exe.read_resource("skill://iris-analyst/SKILL.md")["contents"]
    assert c["mimeType"] == "text/markdown" and c["text"].startswith("---\nname: iris-analyst")
    (c,) = exe.read_resource("skill://iris-analyst/references/notes.md")["contents"]
    assert c == {"uri": "skill://iris-analyst/references/notes.md", "mimeType": "text/markdown", "text": "# Notes\n"}
    (c,) = exe.read_resource("skill://iris-analyst/assets/logo.bin")["contents"]
    assert c["mimeType"] == "application/octet-stream" and "text" not in c
    assert base64.b64decode(c["blob"]).startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "uri",
    [
        "ui://g1/nope",
        "ui://other/chart",
        "skill://nope/SKILL.md",
        "skill://iris-analyst",
        "skill://iris-analyst/",
        "skill://iris-analyst/missing.md",
        "skill://iris-analyst/assets",
    ],
)
def test_unknown_app_or_skill_file_is_not_found(ui_bucket, uri):
    with pytest.raises(KeyError):
        Executor(load(ui_bucket)).read_resource(uri)


@pytest.mark.parametrize("rel", ["../SKILL.md", "references/../../x", "/etc/passwd", "a\\..\\b"])
def test_traversal_is_refused(ui_bucket, rel):
    with pytest.raises(ValueError, match="path refused"):
        Executor(load(ui_bucket)).read_resource(f"skill://iris-analyst/{rel}")


def test_read_time_size_limit(ui_bucket):
    exe = Executor(load(ui_bucket))
    (ui_bucket / "mcp/skills/iris-analyst/references/notes.md").write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="over the 256 KiB limit"):
        exe.read_resource("skill://iris-analyst/references/notes.md")


def test_tool_results_carry_the_app_uri(ui_bucket):
    code = (
        "from ramen_runtime import InputRequired, elicit\ndef f():\n    raise InputRequired({'q': elicit('m', {})})\n"
    )
    write_pkg(ui_bucket, "tools", "asker", {**TOOL, "name": "asker", "ui": "plain"}, code)
    exe = Executor(load(ui_bucket))
    ok = exe.call_tool("chart_data", {"n": 2})
    assert ok["content"][0]["text"] == "[0, 10]" and ok["_meta"] == {"ui/resourceUri": "ui://g1/chart"}
    bad = exe.call_tool("chart_data", {"n": "x"})
    assert bad["isError"] and bad["_meta"] == {"ui/resourceUri": "ui://g1/chart"}
    exe.guard.pre = lambda name, args: {"content": [], "isError": True, "_meta": {"ramen": {"guardrail": {}}}}
    blocked = exe.call_tool("chart_data", {"n": 1})
    assert blocked["_meta"] == {"ramen": {"guardrail": {}}, "ui/resourceUri": "ui://g1/chart"}
    exe.guard.pre = lambda name, args: None
    asked = exe.call_tool("asker", {}, client={"elicitation": {}})
    assert asked["resultType"] == "input_required" and "_meta" not in asked
    assert "_meta" not in Executor(load(ui_bucket)).call_tool("asker", {}, responses={"q": {"action": "accept"}})
    assert context  # the §23.4 types above are the public ones


def test_rpc_round_trip(ui_bucket, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    s = Server(ui_bucket)
    out = io.StringIO()
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "runtime.load", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "runtime.read_resource", "params": {"uri": "ui://g1/plain"}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "runtime.read_resource",
            "params": {"uri": "skill://iris-analyst/SKILL.md"},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "runtime.read_resource", "params": {"uri": "skill://iris-analyst/../x"}},
        {"jsonrpc": "2.0", "id": 5, "method": "runtime.read_resource", "params": {"uri": "skill://iris-analyst/nope"}},
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "runtime.call_tool",
            "params": {"name": "chart_data", "arguments": {"n": 1}},
        },
    ]
    s.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    by = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert [a["name"] for a in by[1]["result"]["apps"]] == ["chart", "plain"]
    assert by[1]["result"]["skills"][0]["files"][0] == "SKILL.md"
    assert by[2]["result"]["contents"][0]["text"] == "<!doctype html><p>plain</p>\n"
    assert by[3]["result"]["contents"][0]["mimeType"] == "text/markdown"
    assert by[4]["error"]["code"] == -32602 and "refused" in by[4]["error"]["message"]
    assert by[5]["error"]["code"] == -32004
    assert by[6]["result"]["_meta"] == {"ui/resourceUri": "ui://g1/chart"}
