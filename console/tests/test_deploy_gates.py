"""0.7.0 deploy flow: manifests recorded per worker (B2/B4, C3), the schema gate (B3, C5) and golden cases (C1, C4),
exercised through the console against the fake gRPC workers of the local adapter."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console import compat
from ramen_console.app import create_app
from ramen_console.cloud.local import LocalCloud
from ramen_console.grpcclient import Client
from ramen_console.storage import make_store
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
CALC = {
    "name": "calc",
    "description": "",
    "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]},
}
README = {"uri": "ramen://demo/readme", "name": "readme", "mimeType": "text/plain"}
GREET = {"name": "greet", "arguments": [{"name": "who", "required": True}], "_meta": {"settings": {}}}


def manifest(tools=(CALC,), resources=(README,), prompts=(GREET,), **extra):
    return {"tools": list(tools), "resources": list(resources), "prompts": list(prompts), "errors": [], **extra}


def deploy(client, **body):
    return wait_job(client, client.post(DEPLOY, json={"canary": True, **body}).json()["id"])


def env(client):
    return client.get("/api/v1/groups/demo/environments/prod").json()


@pytest.fixture
def ws(workers):
    for w in workers.values():
        w.load_result = manifest()
        w.mcp_keys = None  # any minted key is accepted
    return workers


def test_the_full_manifest_and_hash_are_recorded_per_worker(demo, ws):
    job = deploy(demo, canary=False)
    assert job["status"] == "ok", job
    ld = env(demo)["last_deploy"]
    key = f"zone-a {ws['cold'].target}"
    assert set(ld["manifest"]) == {key, f"zone-b {ws['hot'].target}"}
    m = ld["manifest"][key]
    assert set(m) == {"hash", "tools", "resources", "prompts"} and m["tools"] == [CALC] and m["prompts"] == [GREET]
    assert m["hash"] == compat.manifest_hash(manifest())  # the console hashes when the worker did not (older runtimes)
    assert ld["stable"] == {"zone-a": key, "zone-b": f"zone-b {ws['hot'].target}"}
    assert ld["packages"][key]["tools"] == [CALC]  # the pre-0.7.0 key is still there for the packages table
    ws["cold"].load_result = manifest(hash="given-by-the-runtime")
    deploy(demo, canary=False)
    assert env(demo)["last_deploy"]["manifest"][key]["hash"] == "given-by-the-runtime"


def test_group_page_shows_a_collapsed_manifest_per_zone(demo, ws):
    deploy(demo, canary=False)
    page = demo.get("/groups/demo").text
    h = compat.manifest_hash(manifest())
    assert page.count("<summary>Manifest <code") == 2  # one per zone, collapsed
    assert h[:12] in page and h in page
    assert "ramen://demo/readme" in page and "greet" in page
    assert "&#34;inputSchema&#34;" in page or '"inputSchema"' in page  # schemas inside the details


def test_a_breaking_change_aborts_the_canary_unless_breaking_is_accepted(demo, ws):
    assert deploy(demo)["status"] == "ok"
    before = env(demo)["last_deploy"]
    ws["cold"].load_result = manifest(
        tools=[{**CALC, "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}}}]
    )
    job = deploy(demo)
    assert job["status"] == "error", job
    assert "breaking" in job["error"] and "tool calc: property a type number -> string" in job["error"]
    r = job["result"]["zone-a"]
    assert r["ok"] is False and r["compat"]["breaking"] == ["tool calc: property a type number -> string"]
    assert (
        r["compat"]["against"] == before["manifest"][before["stable"]["zone-a"]]["hash"] and not r["compat"]["forced"]
    )
    assert "a no longer required" in json.dumps(r["compat"]["additive"])
    assert any("schema vs stable" in line and "breaking" in line for line in job["log"])
    assert job["result"]["zone-b"]["ok"] is True and job["result"]["zone-b"]["compat"]["breaking"] == []
    after = env(demo)["last_deploy"]
    assert after["stable"]["zone-a"] == before["stable"]["zone-a"]  # the stable manifest survives a refused canary
    assert after["manifest"][after["stable"]["zone-a"]] == before["manifest"][before["stable"]["zone-a"]]
    job = deploy(demo, breaking=True)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["compat"]["forced"] is True
    assert any("accepted" in line for line in job["log"])
    assert env(demo)["last_deploy"]["manifest"][after["stable"]["zone-a"]]["tools"][0]["inputSchema"]["properties"][
        "a"
    ] == {"type": "string"}
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "deploy.start"]
    assert any("breaking:True" in a["tags"] for a in rows)


def test_additive_changes_pass_and_are_reported(demo, ws):
    deploy(demo)
    ws["cold"].load_result = manifest(tools=[CALC, {"name": "echo", "inputSchema": {"type": "object"}}])
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["compat"] == {
        "breaking": [],
        "additive": ["added tool echo"],
        "against": compat.manifest_hash(manifest()),
        "forced": False,
    }


def test_the_first_deploy_has_nothing_to_compare_against(demo, ws):
    job = deploy(demo)
    assert job["status"] == "ok" and "compat" not in job["result"]["zone-a"]


def write_cases(tmp_path, text):
    p = tmp_path / "buckets" / "demo" / "mcp" / "tests.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_golden_cases_run_against_the_canary_and_a_failure_aborts_with_the_diff(demo, ws, tmp_path):
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    for w in ws.values():
        w.tool_results["calc"] = {"content": [{"type": "text", "text": "42"}]}
    write_cases(
        tmp_path,
        "cases:\n  - tool: calc\n    args: {a: 40, b: 2}\n    expect: {subset: {content: [{text: '42'}]}}\n"
        "  - tool: calc\n    name: text\n    expect: {text_contains: '4'}\n",
    )
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["golden"] == {"passed": 2, "failed": []}
    calls = [c for c in ws["cold"].calls if c[0] == "Mcp/Call" and c[2].get("method") == "tools/call"]
    assert calls and calls[0][2]["params"] == {"name": "calc", "arguments": {"a": 40, "b": 2}}
    assert calls[0][1]["authorization"].startswith("Bearer rmk_")
    assert any("golden calc: ok" in line for line in job["log"])
    for w in ws.values():
        w.tool_results["calc"] = {"content": [{"type": "text", "text": "41"}]}
    job = deploy(demo)
    assert job["status"] == "error", job
    assert "golden cases failed" in job["error"] and "calc: content[0].text: expected '42', got '41'" in job["error"]
    assert job["result"]["zone-a"]["golden"] == {
        "passed": 1,
        "failed": ["calc: content[0].text: expected '42', got '41'"],
    }
    assert any("golden calc: FAILED content[0].text" in line for line in job["log"])


def test_golden_args_resolve_group_secrets_and_a_missing_one_fails(demo, ws, tmp_path):
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    demo.post("/api/v1/groups/demo/secrets", json={"name": "API_TOKEN", "value": "t0k-secret"})
    write_cases(
        tmp_path,
        "cases:\n  - tool: whoami\n    args: {token: '{{$demo.API_TOKEN}}'}\n    expect: {text_contains: t0k-secret}\n",
    )
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert "t0k-secret" not in json.dumps(job["log"])  # the case passes without the value reaching the log
    write_cases(
        tmp_path, "cases:\n  - tool: whoami\n    args: {token: '{{$demo.NOPE}}'}\n    expect: {text_contains: x}\n"
    )
    job = deploy(demo)
    assert job["status"] == "error" and "secret demo.NOPE" in job["error"]


def test_golden_cases_are_skipped_with_a_warning_when_the_group_has_no_mcp_key(demo, ws, tmp_path, caplog):
    write_cases(tmp_path, "cases:\n  - tool: calc\n    expect: {text_contains: x}\n")
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["golden"] == {"skipped": "no MCP key", "cases": 1}
    assert any("golden cases skipped" in line for line in job["log"])
    assert any("no MCP key" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")


def test_a_broken_tests_yaml_fails_the_deploy_before_any_zone(demo, ws, tmp_path):
    write_cases(tmp_path, "cases: [{args: {}}]")
    job = deploy(demo)
    assert job["status"] == "error" and "case 1: `tool` is required" in job["error"]
    assert job["result"] is None


def test_a_jsonrpc_error_from_the_tool_is_a_failure(demo, ws, tmp_path):
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    ws["cold"].smoke_ok = True
    write_cases(tmp_path, "cases:\n  - tool: calc\n    expect: {exact: {content: []}}\n")
    job = deploy(demo)
    assert job["status"] == "error" and "calc: exact: expected" in job["error"]


def test_a_refused_job_offers_to_deploy_again_accepting_the_break(demo, ws):
    deploy(demo)
    assert '"breaking": true' not in demo.get("/groups/demo").text  # the environment row keeps its three buttons
    ws["cold"].load_result = manifest(tools=[])
    job = deploy(demo)
    assert job["status"] == "error"
    card = demo.get(f"/ui/jobs/{job['id']}").text
    assert "Deploy again, accept breaking changes" in card and '"breaking": true' in card
    assert 'hx-post="/api/v1/groups/demo/environments/prod/deploy"' in card
    assert "Deploy again, accept breaking changes" in demo.get("/groups/demo").text
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert "accept breaking" not in v.get(f"/ui/jobs/{job['id']}").text


@pytest.fixture
def two_worker_cloud(tmp_path, workers):
    return LocalCloud(
        tmp_path / "buckets",
        tmp_path / "logs",
        {"demo/zone-a": [workers["cold"].target, workers["hot"].target]},
        workers["cold"].target,
        "adm",
        Client(deadline=2),
    )


@pytest.fixture
def two_worker_demo(monkeypatch, tmp_path, two_worker_cloud):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.local")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setenv("RAMEN_BUCKET_ROOT", str(tmp_path / "buckets"))
    a = create_app(store=make_store(), cloud=two_worker_cloud)
    with TestClient(a) as c:
        login(c, "root@ramen.local", "rootpw")
        c.post("/api/v1/groups", json={"name": "demo"})
        c.post("/api/v1/zones", json={"name": "zone-a", "provider": "local", "region": "local"})
        c.post("/api/v1/groups/demo/environments", json={"name": "prod", "zones": ["zone-a"]})
        yield c


def test_workers_partial_shows_each_hash_and_marks_a_mismatch(two_worker_demo, workers):
    workers["cold"].metrics["manifest_hash"] = "a" * 64
    workers["hot"].metrics["manifest_hash"] = "a" * 64
    html = two_worker_demo.get("/ui/groups/demo/zones/zone-a/workers").text
    assert html.count("aaaaaaaaaaaa</code>") == 2 and "manifest mismatch" not in html
    workers["hot"].metrics["manifest_hash"] = "b" * 64
    html = two_worker_demo.get("/ui/groups/demo/zones/zone-a/workers").text
    assert "bbbbbbbbbbbb</code>" in html and "manifest mismatch" in html
    del workers["hot"].metrics["manifest_hash"]  # a pre-0.7.0 worker reports none: shown as unknown, no mismatch
    html = two_worker_demo.get("/ui/groups/demo/zones/zone-a/workers").text
    assert "manifest mismatch" not in html and "aaaaaaaaaaaa</code>" in html


def test_local_read_file_reads_the_clone_and_returns_none_when_absent(tmp_path):
    c = LocalCloud(tmp_path / "buckets")
    assert asyncio.run(c.read_file("demo", "mcp/tests.yaml")) is None
    p = tmp_path / "buckets" / "demo" / "mcp" / "tests.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("cases: []")
    assert asyncio.run(c.read_file("demo", "mcp/tests.yaml")) == b"cases: []"
    assert asyncio.run(c.read_file("demo", "mcp")) is None  # a directory is not a file
    assert Path(p).exists()
