"""CONTRACTS §21.2 (0.7.5, D46), the console's side: the `guardrails` block a worker reports is kept with the zone's
manifest and shown on the group page, never hashed or diffed; a golden case a rail refuses fails the gate with the
rail's own message."""

from ramen_console import compat, golden
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures
from tests.test_deploy_gates import CALC, deploy, env, manifest, write_cases, ws  # noqa: F401 - pytest fixtures

GUARD = {"engine": "nemo", "fail": "closed", "tools": {"calc": ["pre", "post"], "sum": ["pre"]}}
BLOCKED = {
    "content": [{"type": "text", "text": "guardrail blocked: pre: blocked by policy"}],
    "isError": True,
    "_meta": {"ramen": {"guardrail": {"stage": "pre", "engine": "nemo"}}},
}


def test_manifest_keeps_guardrails_outside_the_hash_and_the_diff():
    plain, guarded = manifest(), manifest(guardrails=GUARD)
    assert compat.manifest_hash(plain) == compat.manifest_hash(guarded)
    m = compat.manifest_of(guarded)
    assert m["guardrails"] == GUARD and m["hash"] == compat.manifest_hash(plain)
    assert "guardrails" not in compat.manifest_of(plain)
    assert compat.diff(compat.manifest_of(plain), m) == {"breaking": [], "additive": []}
    assert "guardrails" not in compat.manifest_of(manifest(guardrails="junk"))


def test_group_page_shows_the_guardrails_per_zone(demo, ws):
    ws["cold"].load_result = manifest(guardrails=GUARD)
    ws["hot"].load_result = manifest(guardrails={"engine": "none", "tools": {}})
    job = deploy(demo, canary=False)
    assert job["status"] == "ok", job
    ld = env(demo)["last_deploy"]
    assert ld["manifest"][f"zone-a {ws['cold'].target}"]["guardrails"] == GUARD
    page = demo.get("/groups/demo").text
    assert "Guardrails: <b>nemo</b> · fail closed · <code>calc</code> (pre, post), <code>sum</code> (pre)" in page
    assert 'Guardrails: <span class="muted">none' in page  # zone-b opted nothing in
    assert page.count("<summary>Manifest <code") == 2 and "workers differ" not in page


def test_check_response_puts_a_tool_errors_text_first_and_names_the_rail():
    case = {"tool": "calc", "name": "calc", "expect": {"text_contains": "42"}}
    why = golden.check_response(case, {"jsonrpc": "2.0", "id": 1, "result": BLOCKED})
    assert why.startswith("tool error: guardrail blocked: pre: blocked by policy (guardrail pre, nemo); text_contains")
    # a case that expects the block is a passing case
    expects_block = {**case, "expect": {"text_contains": "guardrail blocked: pre"}}
    assert golden.check_response(expects_block, {"jsonrpc": "2.0", "id": 1, "result": BLOCKED}) is None
    # an ordinary isError: its text first, then the diff (§21.5-5)
    plain = {"content": [{"type": "text", "text": "ZeroDivisionError: division by zero"}], "isError": True}
    why = golden.check_response(case, {"jsonrpc": "2.0", "id": 1, "result": plain})
    assert why.startswith("tool error: ZeroDivisionError: division by zero; text_contains")
    # a plain mismatch has no error text to put first
    ok = {"content": [{"type": "text", "text": "41"}], "isError": False}
    assert golden.check_response(case, {"jsonrpc": "2.0", "id": 1, "result": ok}).startswith("text_contains")


def test_a_golden_case_a_rail_refuses_fails_the_deploy_with_the_rails_message(demo, ws, tmp_path):
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    for w in ws.values():
        w.tool_results["calc"] = BLOCKED
    write_cases(tmp_path, "cases:\n  - tool: calc\n    args: {a: 40}\n    expect: {text_contains: '42'}\n")
    job = deploy(demo)
    assert job["status"] == "error", job
    assert "golden cases failed" in job["error"]
    assert "calc: tool error: guardrail blocked: pre: blocked by policy (guardrail pre, nemo)" in job["error"]
    assert any("golden calc: FAILED tool error: guardrail blocked: pre" in line for line in job["log"])


def test_a_canary_whose_guardrails_did_not_load_is_refused(demo, ws):
    err = {"package": "guardrails", "reason": "engine nemo: config dir mcp/guardrails has no flows"}
    ws["cold"].load_result = manifest(errors=[err], guardrails={"engine": "nemo", "fail": "closed", "tools": {}})
    job = deploy(demo)
    assert job["status"] == "error", job
    assert "guardrails did not load on the canary: engine nemo" in job["error"]
    assert any("zone zone-a: guardrails did not load" in line for line in job["log"])
    # a package error that is not the guardrails file stays what it was: reported, not a refusal
    ws["cold"].load_result = manifest(errors=[{"package": "tools/bad", "reason": "SyntaxError"}])
    assert deploy(demo)["status"] == "ok"
