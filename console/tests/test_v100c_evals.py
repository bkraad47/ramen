"""CONTRACTS §29.2 / §29.3 console side: promptfoo evals from the group repo run as a Job after the golden cases
(report first, gate per group), the results in the job and on the group page, the token answer's `evals`, the
workers' RAMEN_EVALS_* and the public policy document; §29.2 chart `evals.engine`."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ramen_console import evals
from ramen_console.cloud.base import Cloud
from ramen_console.cloud.local import LocalCloud
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixtures
from tests.test_oauth_server import consent, exchange, pkce, register

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
SETTINGS = "/api/v1/groups/demo/evals"
CONFIG = "providers:\n  - id: mcp\n    config: {server: {url: '{{env.RAMEN_MCP_URL}}'}}\ntests:\n  - vars: {q: 1}\n"
CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "ramen"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def results(passed=2, failures=(), errors=0, eval_id="eval-abc", share=None):
    rows = [{"success": True, "testCase": {"description": f"case {i}"}} for i in range(passed)]
    for name, why in failures:
        rows.append({"success": False, "testCase": {"description": name}, "gradingResult": {"reason": why}})
    rows += [{"success": False, "error": "provider exploded", "testCase": {"vars": {"q": "2+2"}}}] * errors
    doc = {
        "evalId": eval_id,
        "results": {"results": rows, "stats": {"successes": passed, "failures": len(failures), "errors": errors}},
        "shareableUrl": share,
    }
    return json.dumps(doc)


def wrap(json_text, head="promptfoo eval\n"):
    return f"{head}{evals.BEGIN}\n{json_text}\n{evals.END}\n"


class Runner:
    def __init__(self):
        self.specs: list[tuple] = []
        self.out = {"exit_code": 0, "log": wrap(results())}

    async def __call__(self, group, zone, spec):
        self.specs.append((group, zone, spec))
        if isinstance(self.out, Exception):
            raise self.out
        return dict(self.out)


@pytest.fixture
def runner(demo, ws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_EVALS_IMAGE", "ghcr.io/promptfoo/promptfoo:0.125.1")
    monkeypatch.delenv("RAMEN_EVALS_API_URL", raising=False)
    r = Runner()
    demo.app.state.cloud.evals_runner = r
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    return r


def write_evals(tmp_path, text=CONFIG):
    p = tmp_path / "buckets" / "demo" / evals.PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


def last_deploy(c):
    return c.get("/api/v1/groups/demo/environments/prod").json()["last_deploy"]


# pure functions ---------------------------------------------------------------------------------------------------


def test_version_is_12_hex_over_config_and_required():
    v = evals.version(CONFIG, False)
    assert len(v) == 12 and int(v, 16) >= 0
    assert evals.version(CONFIG, True) != v and evals.version(CONFIG + "#", False) != v
    assert evals.version(CONFIG, False) == v


def test_parse_results_is_tolerant_of_promptfoo_shapes():
    p = evals.parse_results(results(2, [("adds", "expected 5")], 1, share="http://pf/eval/eval-abc"))
    assert p["passed"] == 2 and p["failed"] == 1 and p["errors"] == 1 and p["url"] == "http://pf/eval/eval-abc"
    assert p["id"] == "eval-abc" and p["failures"] == [("adds", "expected 5"), ("q=2+2", "provider exploded")]
    flat = {"stats": {"successes": 1, "failures": 0, "errors": 0}, "results": [{"success": True}]}
    assert evals.parse_results(json.dumps(flat))["passed"] == 1
    counted = {"results": [{"success": False, "failureReason": "bad"}, {"success": True}]}  # no stats: count
    p = evals.parse_results(json.dumps(counted))
    assert (p["passed"], p["failed"], p["errors"]) == (1, 1, 0) and p["failures"] == [("test 1", "bad")]
    with pytest.raises(ValueError):
        evals.parse_results("not json")
    with pytest.raises(ValueError):
        evals.parse_results("[1]")
    assert evals.extract(wrap(results())) == results()
    assert evals.extract("no markers") is None
    assert evals.extract(f"{evals.BEGIN}\n") is None


def test_parse_results_keeps_every_case_for_the_report_page():
    # the shapes of promptfoo 0.125.1's results.json rows, as read off the GKE server (1.0.0c)
    rows = [
        {
            "success": True,
            "failureReason": 0,
            "latencyMs": 12,
            "testCase": {"description": "calculator adds"},
            "prompt": {"raw": '{"tool":"demo_calculator_tool","args":{"var1":2}}'},
            "response": {"output": '[{"type":"text","text":"5"}]'},
            "gradingResult": {"pass": True, "reason": "All assertions passed"},
        },
        {
            "success": False,
            "failureReason": 1,
            "testCase": {"description": "counts words"},
            "prompt": {"raw": "p"},
            "response": {"output": {"words": 5}},
            "gradingResult": {"pass": False, "reason": "Custom function returned false"},
            "error": "Custom function returned false",
        },
        {
            "success": False,
            "failureReason": 2,
            "testCase": {"description": "word_count"},
            "prompt": {"raw": "p2"},
            "response": {"error": "MCP Provider error: Tool word_count not found"},
            "error": "MCP Provider error: Tool word_count not found",
        },
        {"success": False, "testCase": {"description": "old shape"}, "error": "x" * 2000},
    ]
    doc = {"evalId": "eval-1", "results": {"results": rows, "stats": {"successes": 1, "failures": 1, "errors": 2}}}
    p = evals.parse_results(json.dumps(doc))
    cases = p["cases"]
    assert [c["status"] for c in cases] == ["pass", "fail", "error", "error"]
    assert cases[0] == {
        "name": "calculator adds",
        "status": "pass",
        "input": '{"tool":"demo_calculator_tool","args":{"var1":2}}',
        "output": '[{"type":"text","text":"5"}]',
        "latency_ms": 12,
    }
    assert cases[1]["reason"] == "Custom function returned false" and cases[1]["output"] == '{"words": 5}'
    assert cases[2]["reason"].startswith("MCP Provider error") and cases[2]["output"].startswith("MCP Provider")
    assert len(cases[3]["reason"]) <= evals.MAX_TEXT + 1 and cases[3]["reason"].endswith("…")
    assert (p["passed"], p["failed"], p["errors"]) == (1, 1, 2)


def test_result_of_links_the_console_page_and_names_the_run(monkeypatch):
    parsed = evals.parse_results(
        results(2, [("adds", "no")], share="https://promptfoo.ramen-system:3000/eval/?evalId=eval-srv")
    )
    url = "/groups/demo/evals/j1/zone-a"
    # the shared URL is the in-cluster host a browser cannot open: never the link (1.0.0c, GKE); its id is the run's
    assert evals.result_of(parsed, url) == {"passed": 2, "failed": 1, "errors": 0, "url": url, "run": "eval-srv"}
    monkeypatch.delenv("RAMEN_EVALS_API_URL", raising=False)
    monkeypatch.delenv("RAMEN_EVALS_UI_URL", raising=False)
    assert evals.ui_url() == "" and evals.run_ui("eval-abc") is None  # no server: nothing to open
    monkeypatch.setenv("RAMEN_EVALS_API_URL", "https://promptfoo.ramen-system:3000")
    assert evals.ui_url() == "https://localhost:3443"  # the port-forward, not the in-cluster name
    assert evals.run_ui("eval-abc") == "https://localhost:3443/eval/eval-abc"
    assert evals.PORT_FORWARD == "kubectl -n ramen-system port-forward deploy/promptfoo 3443:3443"
    # a chart from before 1.0.0c's run page set the UI to the in-cluster API address: that is never shown
    monkeypatch.setenv("RAMEN_EVALS_UI_URL", "https://promptfoo.ramen-system:3000/")
    assert evals.ui_url() == "https://localhost:3443"
    monkeypatch.setenv("RAMEN_EVALS_UI_URL", "https://evals.example/")
    assert evals.run_ui("a b") == "https://evals.example/eval/a%20b"


def test_stored_report_is_capped():
    many = evals.parse_results(results(evals.MAX_CASES + 5))
    rep = evals.stored_report(many)
    assert len(rep["cases"]) == evals.MAX_CASES and rep["truncated"] is True and rep["id"] == "eval-abc"
    assert (rep["passed"], rep["failed"], rep["errors"]) == (evals.MAX_CASES + 5, 0, 0)
    assert "failures" not in rep and "url" not in rep
    small = evals.stored_report(evals.parse_results(results(1)))
    assert "truncated" not in small and small["cases"][0]["name"] == "case 0"


def test_job_spec_shape(monkeypatch):
    env = {
        "RAMEN_MCP_URL": "http://10.1.0.1:8080/mcp",
        "RAMEN_MCP_KEY": "rmk_x",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
    }
    spec = evals.job_spec("demo", "zone-a", CONFIG, env, image="ghcr.io/promptfoo/promptfoo:0.125.1")
    name = spec["name"]
    assert name.startswith("ramen-evals-demo-zone-a-") and len(name) <= 63
    cm, sec, job = spec["configmap"], spec["secret"], spec["job"]
    assert cm["metadata"] == {"name": name, "namespace": evals.NS, "labels": cm["metadata"]["labels"]}
    assert cm["data"] == {"promptfooconfig.yaml": CONFIG.replace("{{env.RAMEN_MCP_URL}}", env["RAMEN_MCP_URL"])}
    assert sec["kind"] == "Secret" and sec["stringData"] == env and sec["metadata"]["name"] == name
    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == evals.NS and job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 600 and pod["serviceAccountName"] == "ramen-evals"  # §33.4a
    assert pod["automountServiceAccountToken"] is False and cm["metadata"]["labels"]["ramen.io/job"] == "evals"
    assert job["spec"]["template"]["metadata"]["labels"]["ramen.io/job"] == "evals"
    c = pod["containers"][0]
    assert c["image"] == "ghcr.io/promptfoo/promptfoo:0.125.1" and c["command"][:2] == ["sh", "-c"]
    script = c["command"][2]
    assert "promptfoo eval -c /repo/mcp/evals/promptfooconfig.yaml -o /out/results.json" in script
    assert "--no-cache --no-progress-bar --no-table --no-write" in script and "--share" not in script
    assert f"echo {evals.BEGIN}; cat /out/results.json; echo; echo {evals.END}; exit $rc" in script
    assert {m["mountPath"] for m in c["volumeMounts"]} == {"/repo/mcp/evals", "/out"}
    assert c["envFrom"] == [{"secretRef": {"name": name}}]
    names = {e["name"] for e in c["env"]}
    assert "PROMPTFOO_REMOTE_API_BASE_URL" not in names and "RAMEN_GROUP" not in names  # env travels in the Secret
    assert json.dumps(job).count("test-anthropic-key") == 0  # the key is in the Secret only
    monkeypatch.setenv("RAMEN_EVALS_TIMEOUT_SECS", "42")
    monkeypatch.setenv("RAMEN_EVALS_SA", "evals-x")
    spec = evals.job_spec("demo", "z", CONFIG, env, image="i", api_url="http://promptfoo.ramen-system:3000")
    c = spec["job"]["spec"]["template"]["spec"]["containers"][0]
    assert spec["job"]["spec"]["activeDeadlineSeconds"] == 42
    assert spec["job"]["spec"]["template"]["spec"]["serviceAccountName"] == "evals-x"
    assert "--share" in c["command"][2] and "--no-write" not in c["command"][2]
    envs = {e["name"]: e["value"] for e in c["env"]}
    assert envs["PROMPTFOO_REMOTE_API_BASE_URL"] == "http://promptfoo.ramen-system:3000"
    # promptfoo's own share link names where a person opens the UI (the port-forward), not the in-cluster host
    assert envs["PROMPTFOO_REMOTE_APP_BASE_URL"] == "https://localhost:3443"
    assert "NODE_EXTRA_CA_CERTS" not in envs
    # D86: an https promptfoo server is verified against the install CA even without a canary alias
    spec = evals.job_spec("demo", "z", CONFIG, env, image="i", api_url="https://promptfoo.ramen-system:3000")
    pod = spec["job"]["spec"]["template"]["spec"]
    envs = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert envs["NODE_EXTRA_CA_CERTS"] == "/etc/ramen/ca/ca.crt" and "hostAliases" not in pod
    assert {"name": "ramen-ca", "configMap": {"name": "ramen-ca"}} in pod["volumes"]
    spec = evals.job_spec("demo", "z", CONFIG, env, image="i", api_url="https://p:3000", worker_ip="10.0.0.9")
    pod = spec["job"]["spec"]["template"]["spec"]
    assert [v["name"] for v in pod["volumes"]].count("ramen-ca") == 1 and pod["hostAliases"][0]["ip"] == "10.0.0.9"
    assert [e["name"] for e in pod["containers"][0]["env"]].count("NODE_EXTRA_CA_CERTS") == 1
    long = evals.job_spec("a" * 40, "b" * 30, CONFIG, {}, image="i")["name"]
    assert len(long) <= 63 and long.startswith("ramen-evals-")


def test_job_spec_renders_the_ramen_env_into_the_config():
    # promptfoo leaves `{{env.X}}` alone inside a provider's `server.url` (GCP, 1.0.0c: "Failed to connect to MCP
    # server {{env.RAMEN_MCP_URL}}: Invalid URL"), so the console renders the RAMEN_* names itself; provider keys stay
    # in the Secret and are promptfoo's to read.
    keep = 'key: "{{env.OPENAI_API_KEY}}"\nx: {{env.RAMEN_NOPE}}\n'
    text = 'url: "{{env.RAMEN_MCP_URL}}"\nauth: "Bearer {{ env.RAMEN_MCP_KEY }}"\n' + keep
    env = {"RAMEN_MCP_URL": "http://10.1.0.1:8080/mcp", "RAMEN_MCP_KEY": "rmk_x", "OPENAI_API_KEY": "sk-1"}
    out = evals.render(text, env)
    assert out == 'url: "http://10.1.0.1:8080/mcp"\nauth: "Bearer rmk_x"\n' + keep
    spec = evals.job_spec("demo", "a", text, env, image="i")
    assert spec["configmap"]["data"]["promptfooconfig.yaml"] == out


def test_settings_and_client_info():
    assert evals.settings({}) == {"gate": False, "required": False}
    assert evals.settings({"evals": {"gate": True}}) == {"gate": True, "required": False}
    assert evals.worker_env(None, {}) == {}
    g = {"evals": {"required": True}}
    assert evals.worker_env(CONFIG, g) == {
        "RAMEN_EVALS_VERSION": evals.version(CONFIG, True),
        "RAMEN_EVALS_REQUIRED": "1",
    }
    assert evals.client_info(CONFIG, {}) == {
        "version": evals.version(CONFIG, False),
        "required": False,
        "config": CONFIG,
    }
    assert evals.client_info(None, g) is None
    big = "x" * (evals.MAX_CONFIG + 1)
    info = evals.client_info(big, g)
    assert "config" not in info and info["version"] == evals.version(big, True) and "64" in info["error"]


# deploy -------------------------------------------------------------------------------------------------------------


def test_no_file_means_nothing_runs_and_no_evals_key(demo, runner):
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert "evals" not in job["result"]["zone-a"] and runner.specs == []
    assert not any("evals" in line for line in job["log"])


def test_no_engine_is_skipped_once_per_deploy(demo, runner, tmp_path, monkeypatch):
    monkeypatch.delenv("RAMEN_EVALS_IMAGE")
    write_evals(tmp_path)
    job = wait_job(demo, demo.post(DEPLOY, json={}).json()["id"])  # both zones
    assert job["status"] == "ok", job
    for z in ("zone-a", "zone-b"):
        assert job["result"][z]["evals"] == {"skipped": "no evals engine"}
    assert sum("no evals engine" in line for line in job["log"]) == 1 and runner.specs == []


def test_passed_evals_are_recorded_with_the_canary_url_and_provider_env(demo, runner, tmp_path, ws, caplog):  # noqa: F811
    demo.put("/api/v1/config/llm-providers", json={"anthropic": {"api_key": "anthropic-test-key-not-real"}})
    runner.out = {"exit_code": 0, "log": wrap(results(3, share="http://promptfoo.ramen-system:3000/eval/eval-abc"))}
    write_evals(tmp_path)
    job = deploy(demo)
    assert job["status"] == "ok", job
    url = f"/groups/demo/evals/{job['id']}/zone-a"
    assert job["result"]["zone-a"]["evals"] == {"passed": 3, "failed": 0, "errors": 0, "url": url, "run": "eval-abc"}
    assert [c["name"] for c in job["reports"]["zone-a"]["evals"]["cases"]] == ["case 0", "case 1", "case 2"]
    group, zone, spec = runner.specs[0]
    assert (group, zone) == ("demo", "zone-a")
    env = spec["secret"]["stringData"]
    assert env["RAMEN_MCP_URL"] == f"http://{ws['cold'].target}/mcp" and env["RAMEN_MCP_KEY"].startswith("rek_")
    # §31.4a: the Job's key is the deploy's RAMEN_EVALS_KEY — in the zone's worker config, never a group key
    worker_env = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert f"RAMEN_EVALS_KEY={env['RAMEN_MCP_KEY']}\n" in worker_env and "rmk_" not in env["RAMEN_MCP_KEY"]
    assert (env["RAMEN_GROUP"], env["RAMEN_ENV"], env["RAMEN_ZONE"]) == ("demo", "prod", "zone-a")
    assert env["ANTHROPIC_API_KEY"] == "anthropic-test-key-not-real" and "OPENAI_API_KEY" not in env
    assert spec["configmap"]["data"]["promptfooconfig.yaml"] == CONFIG.replace(
        "{{env.RAMEN_MCP_URL}}", env["RAMEN_MCP_URL"]
    )
    text = json.dumps(job) + caplog.text + demo.get("/groups/demo").text
    assert "anthropic-test-key-not-real" not in text and env["RAMEN_MCP_KEY"] not in text
    assert any("evals: 3 passed, 0 failed, 0 errors" in line for line in job["log"])
    assert last_deploy(demo)["evals"] == {"zone-a": job["result"]["zone-a"]["evals"]}
    page = demo.get("/groups/demo").text
    assert "evals: 3 passed" in page and f'href="{url}"' in page and "promptfoo.ramen-system" not in page
    deploy(demo)  # rotated: the next run gets its own key
    assert runner.specs[1][2]["secret"]["stringData"]["RAMEN_MCP_KEY"] != env["RAMEN_MCP_KEY"]


def test_failures_are_a_report_unless_the_group_gates(demo, runner, tmp_path, caplog):
    write_evals(tmp_path)
    fails = [(f"case {i}", f"expected {i}") for i in range(25)]
    runner.out = {"exit_code": 100, "log": wrap(results(1, fails))}
    job = deploy(demo)
    assert job["status"] == "ok", job
    url = f"/groups/demo/evals/{job['id']}/zone-a"
    assert job["result"]["zone-a"]["evals"] == {"passed": 1, "failed": 25, "errors": 0, "url": url, "run": "eval-abc"}
    lines = [line for line in job["log"] if "evals " in line or "evals:" in line]
    assert sum("FAILED" in line for line in lines) == 20 and any("5 more" in line for line in lines)
    assert any("warning" in line and "25 failed" in line for line in lines)
    assert "evals: 25 failed" in demo.get("/groups/demo").text
    assert demo.put(SETTINGS, json={"gate": True}).json()["gate"] is True
    job = deploy(demo)
    assert job["status"] == "error", job
    assert "evals: 25 failed" in job["error"] and job["result"]["zone-a"]["evals"]["failed"] == 25
    assert job["result"]["zone-a"]["workers"][0]["status"] == "GATE"  # torn down like a failed golden case


def test_an_error_run_is_recorded_and_gated(demo, runner, tmp_path):
    write_evals(tmp_path)
    runner.out = {"exit_code": 1, "log": "promptfoo eval\nError: provider mcp: ECONNREFUSED\n"}
    job = deploy(demo)
    assert job["status"] == "ok", job
    ev = job["result"]["zone-a"]["evals"]
    assert set(ev) == {"error"} and "exit 1" in ev["error"] and "ECONNREFUSED" in ev["error"]
    assert "evals: error" in demo.get("/groups/demo").text
    runner.out = RuntimeError("evals job timed out after 600s")
    job = deploy(demo)
    assert job["status"] == "ok" and "timed out" in job["result"]["zone-a"]["evals"]["error"]
    runner.out = {"exit_code": 0, "log": "no markers at all"}
    job = deploy(demo)
    assert "no results" in job["result"]["zone-a"]["evals"]["error"]
    # the Job echoed the markers but promptfoo wrote no file: the error names what the pod logged before them
    runner.out = {
        "exit_code": 100,
        "log": wrap("cat: can't open '/out/results.json': No such file", "MCP Provider error\nInvalid URL\n"),
    }
    job = deploy(demo)
    err = job["result"]["zone-a"]["evals"]["error"]
    assert "not JSON" in err and "Invalid URL" in err and "can't open" in err
    demo.put(SETTINGS, json={"gate": True})
    runner.out = {"exit_code": 1, "log": "boom"}
    job = deploy(demo)
    assert job["status"] == "error" and "evals error" in job["error"]


def test_no_mcp_key_skips_the_evals(demo, ws, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_EVALS_IMAGE", "i")
    demo.app.state.cloud.evals_runner = Runner()
    write_evals(tmp_path)
    job = deploy(demo)
    assert job["status"] == "ok" and job["result"]["zone-a"]["evals"] == {"skipped": "no MCP key"}


def test_a_broken_golden_case_runs_no_evals(demo, runner, tmp_path, ws):  # noqa: F811
    from tests.test_deploy_gates import write_cases

    write_cases(tmp_path, "cases:\n  - tool: calc\n    expect: {text_contains: nope}\n")
    write_evals(tmp_path)
    job = deploy(demo)
    assert job["status"] == "error" and "golden cases failed" in job["error"]
    assert "evals" not in job["result"]["zone-a"] and runner.specs == []


# group settings, workers, token, policy -----------------------------------------------------------------------------


def test_group_settings_round_trip_rbac_audit_and_push(demo, runner, tmp_path):
    write_evals(tmp_path)
    assert demo.get("/api/v1/groups/demo").json().get("evals") is None
    job = deploy(demo)
    v0 = evals.version(CONFIG, False)
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert f"RAMEN_EVALS_VERSION={v0}\n" in env_file and "RAMEN_EVALS_REQUIRED=0\n" in env_file
    r = demo.put(SETTINGS, json={"required": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"gate": False, "required": True, "pushed": {"demo/zone-a": "ok"}}
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert f"RAMEN_EVALS_VERSION={evals.version(CONFIG, True)}\n" in env_file and "RAMEN_EVALS_REQUIRED=1\n" in env_file
    assert demo.put(SETTINGS, json={"gate": True}).json() == {"gate": True, "required": True, "pushed": {}}  # unchanged
    assert demo.get("/api/v1/groups/demo").json()["evals"] == {"gate": True, "required": True}
    assert demo.put(SETTINGS, json={"gate": "maybe"}).status_code == 422
    assert demo.put("/api/v1/groups/nope/evals", json={"gate": True}).status_code == 404
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "group.evals"]
    assert a and {"group:demo", "required:True"} <= set(a[-1]["tags"])
    page = demo.get("/groups/demo").text
    assert "Evals" in page and 'hx-put="/api/v1/groups/demo/evals"' in page
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as u:
        login(u, "ga@x", PW)
        assert u.put(SETTINGS, json={"gate": False}).status_code == 200
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.put(SETTINGS, json={"gate": False}).status_code == 403


def test_workers_are_told_nothing_without_the_file(demo, runner):
    job = deploy(demo)
    text = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert "RAMEN_EVALS_VERSION" not in text and "RAMEN_EVALS_REQUIRED" not in text
    assert "RAMEN_EVALS_KEY=rek_" in text  # §31.4a: the deploy's own key is there whatever the repo carries


def test_the_token_answer_carries_the_evals_policy(demo, tmp_path):
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert "evals" not in t
    write_evals(tmp_path)
    demo.put(SETTINGS, json={"required": True})
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert t["evals"] == {"version": evals.version(CONFIG, True), "required": True, "config": CONFIG}
    write_evals(tmp_path, "x" * (evals.MAX_CONFIG + 1))
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert "config" not in t["evals"] and t["evals"]["required"] is True


def test_the_policy_document_is_public_and_404s_unknown_targets(demo, tmp_path):
    from ramen_console import otel

    write_evals(tmp_path)
    demo.put(SETTINGS, json={"required": True})
    demo.put("/api/v1/config/otel", json={"enabled": True, "endpoint": "http://j:4318"})
    with TestClient(demo.app) as anon:
        r = anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a")
        assert r.status_code == 200, r.text
        import asyncio

        tv = otel.telemetry_version(asyncio.run(otel.get_config(demo.app.state.store)))
        assert r.json() == {
            "telemetry": {"version": tv, "required": False},
            "evals": {"version": evals.version(CONFIG, True), "required": True},
        }
        assert "config" not in json.dumps(r.json()) and "x" * 10 not in r.text
        assert anon.get("/.well-known/ramen-policy?group=nope&zone=zone-a").status_code == 404
        assert anon.get("/.well-known/ramen-policy?group=demo&zone=zone-x").status_code == 404
        assert anon.get("/.well-known/ramen-policy").status_code == 404
        r = anon.get("/.well-known/ramen-policy?group=other&zone=zone-a")  # bound to no zone
        assert r.status_code == 404
    (tmp_path / "buckets" / "demo" / evals.PATH).unlink()
    r = demo.get("/.well-known/ramen-policy?group=demo&zone=zone-a")
    assert r.json()["evals"] is None


def test_the_config_page_names_the_evals_ui(demo, monkeypatch):
    assert "Evals UI" not in demo.get("/config").text
    monkeypatch.setenv("RAMEN_EVALS_API_URL", "https://promptfoo.ramen-system:3000")
    page = demo.get("/config").text
    # the address a person can open after the port-forward, and the command — never the in-cluster host as a link
    assert "Evals UI" in page and "https://localhost:3443" in page and evals.PORT_FORWARD in page
    assert 'href="https://promptfoo.ramen-system:3000' not in page


def test_the_evals_report_page(demo, runner, tmp_path, monkeypatch):
    monkeypatch.setenv("RAMEN_EVALS_API_URL", "https://promptfoo.ramen-system:3000")
    write_evals(tmp_path)
    doc = json.loads(results(1, [("counts <words>", "expected 6")], 1))
    doc["results"]["results"][0]["response"] = {"output": "five"}
    runner.out = {"exit_code": 100, "log": wrap(json.dumps(doc))}
    job = deploy(demo)
    url = f"/groups/demo/evals/{job['id']}/zone-a"
    assert job["result"]["zone-a"]["evals"]["url"] == url
    page = demo.get("/groups/demo").text
    assert f'href="{url}"' in page and "evals: 1 failed" in page
    r = demo.get(url)
    assert r.status_code == 200, r.text
    for text in ("case 0", "counts &lt;words&gt;", "expected 6", "provider exploded", "five", "eval-abc"):
        assert text in r.text, text
    assert "1 passed, 1 failed, 1 errors" in r.text
    assert r.text.count("<h1") == 1 and "<h1>Evals — demo / zone-a</h1>" in r.text  # N10: the title once
    assert "https://localhost:3443/eval/eval-abc" in r.text and evals.PORT_FORWARD in r.text
    assert "promptfoo.ramen-system" not in r.text
    assert demo.get(f"/groups/demo/evals/{job['id']}/zone-b").status_code == 404
    assert demo.get("/groups/demo/evals/nope/zone-a").status_code == 404
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.get(url).status_code == 200
    with TestClient(demo.app) as u:
        login(u, "o@x", PW)
        assert u.get(url).status_code == 403
    monkeypatch.delenv("RAMEN_EVALS_API_URL")  # Jobs only (`--no-write`): the cases, no promptfoo link
    page = demo.get(url).text
    assert "case 0" in page and "localhost:3443" not in page


# adapters -----------------------------------------------------------------------------------------------------------


async def test_the_base_adapter_and_the_local_one_without_a_runner_refuse(tmp_path):
    class Bare(Cloud):
        async def sync_repo(self, *a):
            return ""

        async def deploy(self, *a, **k):
            return {}

        async def rebalance(self, *a):
            return {}

        async def workers(self, *a):
            return []

        async def logs(self, *a, **k):
            return ""

        async def set_ip_rules(self, *a):
            return {}

        async def create_service_account(self, *a):
            return {}

        async def refresh(self):
            return {}

    with pytest.raises(NotImplementedError):
        await Bare().run_evals_job("g", "z", {})
    with pytest.raises(NotImplementedError):
        await LocalCloud(tmp_path).run_evals_job("g", "z", {})


async def test_the_k8s_adapter_runs_the_job_in_ramen_system_and_cleans_up(monkeypatch):
    from ramen_console.cloud.gcp import GcpCloud
    from tests.fakes_gcp import FakeClients

    fk = FakeClients()
    cloud = GcpCloud(project="p1", region="us-central1", bucket="b", image="i", clients=fk, wait_secs=1, poll=0)
    k8s = fk.k8s
    spec = evals.job_spec("demo", "a", CONFIG, {"RAMEN_MCP_KEY": "rmk_1"}, image="i")
    name = spec["name"]
    k8s.job_exit_code = 100
    k8s.pod_logs[("ramen-system", f"{name}-pod")] = wrap(results(1, [("c", "why")])).splitlines()
    out = await cloud.run_evals_job("demo", "a", spec)
    assert out["exit_code"] == 100 and evals.extract(out["log"]) == results(1, [("c", "why")])
    created = [c for c in k8s.calls if c[0] == "create" and c[2] == "ramen-system"]
    assert {(c[1], c[3]) for c in created} == {("ConfigMap", name), ("Secret", name), ("Job", name)}
    assert not any(k[1] == "ramen-system" for k in k8s.objs)  # Job, ConfigMap and Secret removed afterwards
    k8s.job_exit_code = None
    k8s.job_outcome = "succeeded"
    assert (await cloud.run_evals_job("demo", "a", evals.job_spec("demo", "a", CONFIG, {}, image="i")))[
        "exit_code"
    ] == 0
    k8s.job_outcome = "failed"
    assert (await cloud.run_evals_job("demo", "a", evals.job_spec("demo", "a", CONFIG, {}, image="i")))[
        "exit_code"
    ] == 1
    k8s.job_outcome = "running"
    monkeypatch.setenv("RAMEN_EVALS_TIMEOUT_SECS", "0")
    with pytest.raises(RuntimeError, match="timed out"):
        await cloud.run_evals_job("demo", "a", evals.job_spec("demo", "a", CONFIG, {}, image="i"))
    assert not any(k[1] == "ramen-system" for k in k8s.objs)


# chart ----------------------------------------------------------------------------------------------------------------


def template(*sets):
    flags = [x for s in sets for x in ("--set", s)]
    r = subprocess.run(
        ["helm", "template", "ramen", str(CHART), "-n", "ramen-system", "--set", "project=p", *flags],
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stderr, [d for d in yaml.safe_load_all(r.stdout) if d] if r.returncode == 0 else []


def console_env(docs):
    c = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "console")
    return {e["name"]: e.get("value") for e in c["spec"]["template"]["spec"]["containers"][0]["env"]}


def by_kind(docs, kind, name):
    return next((d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name), None)


@needs_helm
def test_promptfoo_engine_adds_the_server_and_tells_the_console():
    rc, err, docs = template("evals.engine=promptfoo")
    assert rc == 0, err
    dep = by_kind(docs, "Deployment", "promptfoo")
    assert dep["spec"]["replicas"] == 1
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "ghcr.io/promptfoo/promptfoo:0.125.1"  # no containerPort: 3000 is loopback-only now
    tls = dep["spec"]["template"]["spec"]["containers"][1]
    assert {p["containerPort"] for p in tls["ports"]} == {3443}  # the pod's one reachable port: the TLS front
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["PROMPTFOO_CONFIG_DIR"] == "/home/promptfoo/.promptfoo"
    assert c["volumeMounts"][0]["mountPath"] == "/home/promptfoo/.promptfoo"
    assert dep["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    pvc = by_kind(docs, "PersistentVolumeClaim", "promptfoo")
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"
    svc = by_kind(docs, "Service", "promptfoo")
    assert [p["port"] for p in svc["spec"]["ports"]] == [3000]
    assert not any(d["kind"] in ("Ingress", "HTTPRoute") and "promptfoo" in d["metadata"]["name"] for d in docs)
    env = console_env(docs)
    assert env["RAMEN_EVALS_IMAGE"] == "ghcr.io/promptfoo/promptfoo:0.125.1"
    assert env["RAMEN_EVALS_API_URL"] == "https://promptfoo.ramen-system:3000"  # D86: behind the TLS front
    assert env["RAMEN_EVALS_UI_URL"] == "https://localhost:3443"  # what a person opens after the port-forward
    srv = {e["name"]: e["value"] for e in c["env"]}
    assert srv["PROMPTFOO_REMOTE_APP_BASE_URL"] == "https://localhost:3443"
    # promptfoo binds 127.0.0.1 only (a mounted Node preload): plaintext never leaves the pod, NetworkPolicy or not
    assert srv["NODE_OPTIONS"] == "--require=/ramen/loopback.js"
    assert {"name": "loopback", "mountPath": "/ramen", "readOnly": True} in c["volumeMounts"]
    assert "ports" not in c and "exec" in c["readinessProbe"] and "127.0.0.1:3000/health" in str(c["readinessProbe"])
    shim = by_kind(docs, "ConfigMap", "promptfoo-loopback")["data"]["loopback.js"]
    assert 'host: "127.0.0.1"' in shim and "net.Server.prototype.listen" in shim
    role = by_kind(docs, "Role", "ramen-console-build")  # no builder identity, still the Job's RBAC and SA
    assert role and any("secrets" in r["resources"] and "create" in r["verbs"] for r in role["rules"])
    # §33.4a: the Jobs' own identity, bound to nothing; the builder's SA only exists with a builder
    sa = by_kind(docs, "ServiceAccount", "ramen-evals")
    assert sa and sa["automountServiceAccountToken"] is False and not sa["metadata"].get("annotations")
    assert by_kind(docs, "ServiceAccount", "ramen-builder") is None
    assert not any(d["kind"] == "RoleBinding" and "ramen-evals" in json.dumps(d["subjects"]) for d in docs)
    assert env["RAMEN_EVALS_SA"] == "ramen-evals"


@needs_helm
def test_engine_none_runs_jobs_only_and_anything_else_is_refused():
    rc, err, docs = template()
    assert rc == 0, err
    env = console_env(docs)
    assert env["RAMEN_EVALS_IMAGE"] == "ghcr.io/promptfoo/promptfoo:0.125.1"
    assert "RAMEN_EVALS_API_URL" not in env and "RAMEN_EVALS_UI_URL" not in env
    assert (
        by_kind(docs, "Deployment", "promptfoo") is None and by_kind(docs, "PersistentVolumeClaim", "promptfoo") is None
    )
    rc, _, docs = template("evals.image=")
    assert rc == 0 and "RAMEN_EVALS_IMAGE" not in console_env(docs)
    assert by_kind(docs, "Role", "ramen-console-build") is None  # no builder, no evals: no Job RBAC
    rc, err, _ = template("evals.engine=braintrust")
    assert rc != 0 and "none or promptfoo" in err


@needs_helm
def test_helm_lint_with_the_engine_on():
    r = subprocess.run(
        ["helm", "lint", str(CHART), "--set", "project=p", "--set", "evals.engine=promptfoo"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    "share",
    [
        "https://localhost:3443/eval/eval-CPI-2026-11-17T02:56:46",
        "https://localhost:3443/eval/?evalId=eval-CPI-2026-11-17T02:56:46",
        "https://promptfoo.ramen-system:3000/eval/?evalId=eval-CPI-2026-11-17T02:56:46&x=1",
    ],
)
def test_the_run_id_is_the_servers_when_the_run_was_shared(share):
    # GKE 1.0.0b37: `--share` uploads the run under a NEW id; the Job's local evalId (eval-LvI-…) does not exist on the
    # server, so the run page's promptfoo link answered "404 Eval not found"
    parsed = evals.parse_results(results(eval_id="eval-LvI-2026-11-17T02:56:46", share=share))
    assert parsed["id"] == "eval-CPI-2026-11-17T02:56:46"
    assert evals.parse_results(results(eval_id="eval-LvI-x", share=None))["id"] == "eval-LvI-x"  # not shared: local id
