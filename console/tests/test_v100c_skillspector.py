"""CONTRACTS §30 console side: NVIDIA SkillSpector scans of the group repo's `mcp/skills/*` plus a skill synthesized
from what the canary serves, run as a Job after the evals (report first, gate per group); the report page, the group
settings, the token answer's `skillspector`, the workers' RAMEN_SKILLSPECTOR_* and the public policy document; §30.0
chart `skillspector.engine` and the pinned tag."""

import base64
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console import skillspector as sk
from ramen_console.cloud.base import Cloud
from ramen_console.cloud.local import LocalCloud
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixtures
from tests.test_oauth_server import consent, exchange, pkce, register
from tests.test_v100c_evals import CHART, by_kind, console_env, needs_helm, template

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
SETTINGS = "/api/v1/groups/demo/skillspector"
SKILL = "---\nname: noodle-math\ndescription: adds noodles\n---\n# Noodle math\nUse the `calc` tool.\n"
ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/bkraad47/ramen-skillspector:2.13.4"


def issue(rule="PI-001", sev="HIGH", file="SKILL.md", line=3, msg="instruction overrides the system prompt"):
    return {"id": rule, "severity": sev, "location": {"file": file, "start_line": line}, "explanation": msg}


def skill_row(name, score, issues=()):
    sev = "LOW" if score <= 20 else ("MEDIUM" if score <= 50 else "HIGH")
    return {
        "name": name,
        "path": name,
        "risk_score": score,
        "risk_severity": sev,
        "finding_count": len(issues),
        "execution_successful": True,
        "skill": {"name": name, "source": f"/scan/{name}"},
        "risk_assessment": {"score": score, "severity": sev, "recommendation": sk.recommendation(score)},
        "issues": list(issues),
    }


def report(*rows, ok=True):
    score = max((r["risk_score"] for r in rows), default=0)
    return json.dumps(
        {
            "multi_skill": True,
            "skill_count": len(rows),
            "max_risk_score": score,
            "risk_severity": max((r["risk_severity"] for r in rows), default="LOW"),
            "execution_successful": ok,
            "risk_recommendation": sk.recommendation(score) if ok else "DO_NOT_INSTALL",
            "analysis_completeness": {"is_complete": True},
            "skills_scanned": len(rows),
            "skills": list(rows),
        }
    )


def single(name="only", score=7, issues=()):
    return json.dumps(
        {
            "skill": {"name": name, "source": f"/scan/{name}"},
            "risk_assessment": {"score": score, "severity": "LOW", "recommendation": sk.recommendation(score)},
            "issues": list(issues),
            "execution_successful": True,
        }
    )


def wrap(json_text, head="Scanning\n"):
    return f"{head}{sk.BEGIN}\n{json_text}\n{sk.END}\n"


class Runner:
    def __init__(self):
        self.specs: list[tuple] = []
        self.out = {"exit_code": 0, "log": wrap(report(skill_row("noodle-math", 4)))}

    async def __call__(self, group, zone, spec):
        self.specs.append((group, zone, spec))
        if isinstance(self.out, Exception):
            raise self.out
        return dict(self.out)


@pytest.fixture
def runner(demo, ws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_SKILLSPECTOR_IMAGE", IMAGE)
    monkeypatch.delenv("RAMEN_EVALS_IMAGE", raising=False)
    r = Runner()
    demo.app.state.cloud.scan_runner = r
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    return r


def write_skill(tmp_path, name="noodle-math", text=SKILL, extra=()):
    d = tmp_path / "buckets" / "demo" / sk.PATH / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text)
    for rel, body in extra:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(body if isinstance(body, bytes) else body.encode())


def write_baseline(tmp_path, text="version: 1\nrules: []\n"):
    p = tmp_path / "buckets" / "demo" / sk.BASELINE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


def last_deploy(c):
    return c.get("/api/v1/groups/demo/environments/prod").json()["last_deploy"]


# pure functions ---------------------------------------------------------------------------------------------------


def test_version_settings_and_recommendation():
    v = sk.version(False, "", "")
    assert len(v) == 12 and int(v, 16) >= 0
    assert sk.version(True, "", "") != v and sk.version(False, "openai", "") != v and sk.version(False, "", "x") != v
    assert sk.settings({}) == {"gate": False, "required": False, "llm": ""}
    assert sk.settings({"skillspector": {"gate": True, "llm": "openai"}}) == {
        "gate": True,
        "required": False,
        "llm": "openai",
    }
    assert (sk.recommendation(0), sk.recommendation(20), sk.recommendation(21), sk.recommendation(51)) == (
        "SAFE",
        "SAFE",
        "CAUTION",
        "DO_NOT_INSTALL",
    )


def test_client_info_and_worker_env_need_the_engine_or_required(monkeypatch):
    monkeypatch.delenv("RAMEN_SKILLSPECTOR_IMAGE", raising=False)
    assert sk.client_info(None, {}) is None and sk.worker_env(None, {}) == {}
    g = {"skillspector": {"required": True, "llm": "anthropic"}}
    v = sk.version(True, "anthropic", "")
    assert sk.client_info(None, g) == {"version": v, "required": True, "llm": "anthropic", "baseline": ""}
    assert sk.worker_env(None, g) == {"RAMEN_SKILLSPECTOR_VERSION": v, "RAMEN_SKILLSPECTOR_REQUIRED": "1"}
    monkeypatch.setenv("RAMEN_SKILLSPECTOR_IMAGE", IMAGE)
    info = sk.client_info("b: 1\n", {})
    assert info == {"version": sk.version(False, "", "b: 1\n"), "required": False, "llm": "", "baseline": "b: 1\n"}
    assert sk.worker_env("b: 1\n", {})["RAMEN_SKILLSPECTOR_REQUIRED"] == "0"
    big = "x" * (sk.MAX_BASELINE + 1)
    info = sk.client_info(big, {})
    assert "baseline" not in info and "64" in info["error"] and info["version"] == sk.version(False, "", big)


def test_synthesize_skill_names_every_tool_and_prompt():
    tools = [
        {"name": "calc", "title": "Calculator", "description": "adds", "inputSchema": {"type": "object"}},
        {"name": "greet"},
    ]
    prompts = [{"name": "hello", "description": "says hi", "arguments": [{"name": "who", "required": True}]}]
    text = sk.synthesize_skill(tools, prompts, "zone-a")
    assert text.startswith("---\nname: ramen-served-zone-a\ndescription: ")
    assert "## Tool `calc` — Calculator" in text and "adds" in text and '```json\n{\n  "type": "object"\n}\n```' in text
    assert "## Tool `greet`" in text and "## Prompt `hello`" in text and '"who"' in text
    assert sk.synthesize_skill([], [], "z") is None


def test_bundle_groups_skill_dirs_and_refuses_a_large_tree():
    listing = [
        "mcp/skills/a/SKILL.md",
        "mcp/skills/a/scripts/run.sh",
        "mcp/skills/no-skill-md/readme.txt",
        "mcp/skills/../etc/passwd",
        "mcp/skills/b/SKILL.md",
        "mcp/skills/stray.txt",
    ]
    assert sk.skill_files(listing) == {
        "a": ["mcp/skills/a/SKILL.md", "mcp/skills/a/scripts/run.sh"],
        "b": ["mcp/skills/b/SKILL.md"],
    }
    files = {"a/SKILL.md": b"# a", "a/scripts/run.sh": b"\x00\x01", "ramen-served-z/SKILL.md": b"# s"}
    packed = sk.bundle(files)
    assert json.loads(packed)["a/scripts/run.sh"] == base64.b64encode(b"\x00\x01").decode()
    with pytest.raises(ValueError, match="KiB"):
        sk.bundle({"a/SKILL.md": b"x" * (sk.MAX_DIR + 1)})


def test_job_spec_shape(monkeypatch):
    env = {"RAMEN_GROUP": "demo", "ANTHROPIC_API_KEY": "test-anthropic-key"}
    files = {"noodle-math/SKILL.md": SKILL.encode(), "ramen-served-zone-a/SKILL.md": b"# s"}
    spec = sk.job_spec("demo", "zone-a", files, env, image=IMAGE)
    name = spec["name"]
    assert name.startswith("ramen-scan-demo-zone-a-") and len(name) <= 63
    cm, sec, job = spec["configmap"], spec["secret"], spec["job"]
    assert cm["metadata"] == {"name": name, "namespace": sk.NS, "labels": cm["metadata"]["labels"]}
    assert set(cm["data"]) == {"files.json", "ramen_scan.py"} and json.loads(cm["data"]["files.json"]) == {
        k: base64.b64encode(v).decode() for k, v in files.items()
    }
    assert sec["stringData"] == env and "SKILLSPECTOR_PROVIDER" not in sec["stringData"]
    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == sk.NS and job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 600 and pod["serviceAccountName"] == "ramen-evals"  # §33.4a
    assert pod["automountServiceAccountToken"] is False and cm["metadata"]["labels"]["ramen.io/job"] == "scan"
    c = pod["containers"][0]
    assert c["image"] == IMAGE and c["command"][:2] == ["sh", "-c"]
    script = c["command"][2]
    assert "python /cm/ramen_scan.py unpack /cm/files.json /scan" in script
    # v2.13.4 has no `--max-skills` (it caps at 32 itself); §30's flag is therefore not passed
    assert "skillspector scan /scan --recursive --format json --no-llm -o /out/report.json; rc=$?" in script
    assert f"echo {sk.BEGIN}; cat /out/report.json; echo; echo {sk.END}; exit $rc" in script
    assert {m["mountPath"] for m in c["volumeMounts"]} == {"/cm", "/scan", "/out"}
    assert c["envFrom"] == [{"secretRef": {"name": name}}] and c["securityContext"]["runAsNonRoot"] is True
    assert json.dumps(job).count("test-anthropic-key") == 0
    # an LLM: the provider name travels in the Secret, --no-llm goes; a baseline: skill by skill, then merged
    monkeypatch.setenv("RAMEN_SKILLSPECTOR_TIMEOUT_SECS", "42")
    spec = sk.job_spec("demo", "z", files, env, image="i", llm="anthropic", baseline="version: 1\n")
    c = spec["job"]["spec"]["template"]["spec"]["containers"][0]
    assert spec["job"]["spec"]["activeDeadlineSeconds"] == 42
    assert spec["secret"]["stringData"]["SKILLSPECTOR_PROVIDER"] == "anthropic"
    assert "--no-llm" not in c["command"][2] and "--recursive" not in c["command"][2]
    assert "--baseline /cm/baseline.yaml" in c["command"][2] and "ramen_scan.py merge" in c["command"][2]
    assert spec["configmap"]["data"]["baseline.yaml"] == "version: 1\n"
    assert len(sk.job_spec("a" * 40, "b" * 30, files, {}, image="i")["name"]) <= 63


def test_the_helper_script_unpacks_and_merges(tmp_path):
    """The python the Job runs inside the image, run here with the same arguments."""
    helper = tmp_path / "ramen_scan.py"
    helper.write_text(sk.HELPER)
    files = {"a/SKILL.md": b"# a", "a/x/y.bin": b"\x00\xff", "../evil": b"no", "/abs": b"no"}
    (tmp_path / "files.json").write_text(sk.bundle({k: v for k, v in files.items() if k[0] not in "./"}))
    scan = tmp_path / "scan"
    r = subprocess.run(
        ["python3", "-I", str(helper), "unpack", str(tmp_path / "files.json"), str(scan)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (scan / "a" / "SKILL.md").read_text() == "# a" and (scan / "a" / "x" / "y.bin").read_bytes() == b"\x00\xff"
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.json").write_text(single("a", 7, [issue()]))
    (out / "b.json").write_text(single("b", 60, [issue("EX-002", "CRITICAL"), issue()]))
    (out / "c.json").write_text("not json")
    r = subprocess.run(
        ["python3", "-I", str(helper), "merge", str(out), str(out / "report.json")], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    doc = json.loads((out / "report.json").read_text())
    assert doc["multi_skill"] is True and doc["max_risk_score"] == 60 and doc["risk_recommendation"] == "DO_NOT_INSTALL"
    assert doc["skills_scanned"] == 2 and doc["execution_successful"] is False
    rows = {s["name"]: s for s in doc["skills"]}
    assert rows["b"]["finding_count"] == 2 and rows["b"]["issues"][0]["id"] == "EX-002" and "error" in rows["c"]
    assert rows["a"]["risk_score"] == 7 and rows["a"]["path"] == "a"


def test_parse_report_is_tolerant_of_both_shapes():
    p = sk.parse_report(
        report(
            skill_row("a", 7, [issue()]), skill_row("b", 33, [issue("EX-002", "CRITICAL", "run.sh", 9, "curl | sh")])
        )
    )
    assert (p["score"], p["severity"], p["recommendation"], p["skills_scanned"], p["findings"]) == (
        33,
        "MEDIUM",
        "CAUTION",
        2,
        2,
    )
    assert p["skills"][1] == {
        "name": "b",
        "path": "b",
        "score": 33,
        "severity": "MEDIUM",
        "recommendation": "CAUTION",
        "findings": [{"rule": "EX-002", "severity": "CRITICAL", "file": "run.sh", "line": 9, "message": "curl | sh"}],
    }
    assert p["skills"][0]["findings"] == [
        {
            "rule": "PI-001",
            "severity": "HIGH",
            "file": "SKILL.md",
            "line": 3,
            "message": "instruction overrides the system prompt",
        }
    ]
    p = sk.parse_report(single("only", 55, [issue()]))  # the single-skill report (a baseline scan of one skill)
    assert (p["score"], p["recommendation"], p["skills_scanned"]) == (55, "DO_NOT_INSTALL", 1)
    assert p["skills"][0]["name"] == "only" and p["findings"] == 1
    p = sk.parse_report(json.dumps({"risk_score": 12, "skills": [{"name": "x", "error": "boom"}]}))  # §30 wording
    assert p["score"] == 12 and p["skills"][0]["error"] == "boom" and p["recommendation"] == "SAFE"
    flat = {  # the conformance harness's fake: flat finding keys under `findings`
        "risk_score": 30,
        "skills": [
            {
                "name": "y",
                "risk_score": 30,
                "findings": [{"rule_id": "P1", "severity": "HIGH", "file": "SKILL.md", "line": 2, "message": "m"}],
            }
        ],
    }
    p = sk.parse_report(json.dumps(flat))
    assert p["skills"][0]["findings"] == [
        {"rule": "P1", "severity": "HIGH", "file": "SKILL.md", "line": 2, "message": "m"}
    ]
    assert p["findings"] == 1 and p["recommendation"] == "CAUTION"
    assert sk.parse_report(report(ok=False))["error"] == "the scan did not complete"
    with pytest.raises(ValueError):
        sk.parse_report("not json")
    with pytest.raises(ValueError):
        sk.parse_report("[1]")
    assert sk.extract(wrap(single())) == single() and sk.extract("no markers") is None


def test_result_and_stored_report_are_bounded():
    rows = [skill_row(f"s{i}", 3, [issue(msg="m" * 2000)] * 40) for i in range(8)]
    parsed = sk.parse_report(report(*rows))
    assert sk.result_of(parsed, "/groups/demo/scans/j/z") == {
        "score": 3,
        "severity": "LOW",
        "recommendation": "SAFE",
        "skills": 8,
        "findings": 320,
        "url": "/groups/demo/scans/j/z",
    }
    stored = sk.stored_report(parsed)
    assert stored["truncated"] is True and len(json.dumps(stored)) <= sk.MAX_REPORT
    assert stored["skills"][0]["name"] == "s0" and sum(len(s["findings"]) for s in stored["skills"]) < 320
    small = sk.stored_report(sk.parse_report(report(skill_row("a", 1))))
    assert "truncated" not in small and small["skills"][0]["findings"] == []


# deploy -------------------------------------------------------------------------------------------------------------


def test_no_skills_and_no_engine_means_no_key(demo, ws, monkeypatch):  # noqa: F811
    monkeypatch.delenv("RAMEN_SKILLSPECTOR_IMAGE", raising=False)
    job = deploy(demo)
    assert job["status"] == "ok" and "skillspector" not in job["result"]["zone-a"]
    assert not any("scan" in line for line in job["log"])


def test_no_engine_is_skipped_once_per_deploy(demo, ws, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("RAMEN_SKILLSPECTOR_IMAGE", raising=False)
    write_skill(tmp_path)
    job = wait_job(demo, demo.post(DEPLOY, json={}).json()["id"])
    assert job["status"] == "ok", job
    for z in ("zone-a", "zone-b"):
        assert job["result"][z]["skillspector"] == {"skipped": "no scan engine"}
    assert sum("no scan engine" in line for line in job["log"]) == 1


def test_a_safe_scan_is_recorded_with_the_report_page(demo, runner, tmp_path, ws, caplog):  # noqa: F811
    demo.put("/api/v1/config/llm-providers", json={"anthropic": {"api_key": "anthropic-test-key-not-real"}})
    write_skill(tmp_path, extra=[("scripts/run.sh", "echo hi\n"), ("img.bin", b"\x00\x01")])
    (tmp_path / "buckets" / "demo" / sk.PATH / "no-skill").mkdir()
    (tmp_path / "buckets" / "demo" / sk.PATH / "no-skill" / "readme.txt").write_text("not a skill")
    runner.out = {"exit_code": 0, "log": wrap(report(skill_row("noodle-math", 4), skill_row("ramen-served-zone-a", 9)))}
    job = deploy(demo)
    assert job["status"] == "ok", job
    url = f"/groups/demo/scans/{job['id']}/zone-a"
    assert job["result"]["zone-a"]["skillspector"] == {
        "score": 9,
        "severity": "LOW",
        "recommendation": "SAFE",
        "skills": 2,
        "findings": 0,
        "url": url,
    }
    group, zone, spec = runner.specs[0]
    assert (group, zone) == ("demo", "zone-a")
    files = {k: base64.b64decode(v) for k, v in json.loads(spec["configmap"]["data"]["files.json"]).items()}
    assert files["noodle-math/SKILL.md"] == SKILL.encode() and files["noodle-math/img.bin"] == b"\x00\x01"
    assert files["noodle-math/scripts/run.sh"] == b"echo hi\n" and not any(k.startswith("no-skill") for k in files)
    served = files["ramen-served-zone-a/SKILL.md"].decode()
    assert "name: ramen-served-zone-a" in served and "## Tool `calc`" in served and "## Prompt `greet`" in served
    env = spec["secret"]["stringData"]
    assert (env["RAMEN_GROUP"], env["RAMEN_ENV"], env["RAMEN_ZONE"]) == ("demo", "prod", "zone-a")
    assert env["ANTHROPIC_API_KEY"] == "anthropic-test-key-not-real" and "SKILLSPECTOR_PROVIDER" not in env
    assert "--no-llm" in spec["job"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    text = json.dumps(job) + caplog.text + demo.get("/groups/demo").text
    assert "anthropic-test-key-not-real" not in text
    assert any("scan: SAFE, score 9/100, 2 skills, 0 findings" in line for line in job["log"])
    assert job["reports"]["zone-a"]["skillspector"]["skills"][1]["name"] == "ramen-served-zone-a"
    assert last_deploy(demo)["skillspector"] == {"zone-a": job["result"]["zone-a"]["skillspector"]}
    page = demo.get("/groups/demo").text
    assert "scan: SAFE" in page and f'href="{url}"' in page and "Scan report" in page
    r = demo.get(url)
    assert r.status_code == 200 and "ramen-served-zone-a" in r.text and "SAFE" in r.text and "9/100" in r.text
    assert r.text.count("<h1") == 1 and "<h1>Scan report — demo / zone-a</h1>" in r.text  # N10: the title once
    assert demo.get(f"/groups/demo/scans/{job['id']}/zone-b").status_code == 404
    assert demo.get("/groups/demo/scans/nope/zone-a").status_code == 404
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.get(url).status_code == 200
    with TestClient(demo.app) as u:
        login(u, "o@x", PW)
        assert u.get(url).status_code == 403


def test_caution_warns_and_do_not_install_gates_when_the_group_gates(demo, runner, tmp_path):
    write_skill(tmp_path)
    bad = issue("EX-002", "CRITICAL", "scripts/run.sh", 9, "curl | sh")
    runner.out = {"exit_code": 0, "log": wrap(report(skill_row("noodle-math", 40, [bad])))}
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["skillspector"]["recommendation"] == "CAUTION"
    assert any("warning" in line and "CAUTION" in line for line in job["log"])
    assert any("noodle-math EX-002 CRITICAL scripts/run.sh:9: curl | sh" in line for line in job["log"])
    assert "scan: CAUTION" in demo.get("/groups/demo").text
    runner.out = {"exit_code": 1, "log": wrap(report(skill_row("noodle-math", 80, [bad])))}
    job = deploy(demo)  # gate off: reported, deploy ok
    assert job["status"] == "ok" and job["result"]["zone-a"]["skillspector"]["recommendation"] == "DO_NOT_INSTALL"
    assert "scan: DO_NOT_INSTALL" in demo.get("/groups/demo").text
    assert demo.put(SETTINGS, json={"gate": True}).json()["gate"] is True
    job = deploy(demo)
    assert job["status"] == "error", job
    assert "DO_NOT_INSTALL" in job["error"] and job["result"]["zone-a"]["skillspector"]["score"] == 80
    assert job["result"]["zone-a"]["workers"][0]["status"] == "GATE"
    page = demo.get(f"/groups/demo/scans/{job['id']}/zone-a").text
    assert "EX-002" in page and "scripts/run.sh:9" in page and "curl | sh" in page


def test_an_error_run_is_recorded_and_gated(demo, runner, tmp_path):
    write_skill(tmp_path)
    runner.out = {"exit_code": 2, "log": "Scanning\nError: no SKILL.md\n"}
    job = deploy(demo)
    assert job["status"] == "ok", job
    s = job["result"]["zone-a"]["skillspector"]
    assert set(s) == {"error"} and "exit 2" in s["error"] and "no SKILL.md" in s["error"]
    assert "scan: error" in demo.get("/groups/demo").text
    runner.out = RuntimeError("scan job timed out after 600s")
    job = deploy(demo)
    assert "timed out" in job["result"]["zone-a"]["skillspector"]["error"]
    runner.out = {"exit_code": 2, "log": wrap("cat: can't open '/out/report.json'", "Error: boom\n")}
    job = deploy(demo)
    assert "not JSON" in job["result"]["zone-a"]["skillspector"]["error"]
    runner.out = {"exit_code": 2, "log": wrap(report(skill_row("noodle-math", 0), ok=False))}
    job = deploy(demo)
    assert "did not complete" in job["result"]["zone-a"]["skillspector"]["error"]
    demo.put(SETTINGS, json={"gate": True})
    runner.out = {"exit_code": 2, "log": "boom"}
    job = deploy(demo)
    assert job["status"] == "error" and "scan error" in job["error"]


def test_nothing_to_scan_and_no_mcp_key(demo, ws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_SKILLSPECTOR_IMAGE", IMAGE)
    r = Runner()
    demo.app.state.cloud.scan_runner = r
    job = deploy(demo)  # no key: the served skill cannot be synthesized, no repo skills either
    assert job["status"] == "ok" and job["result"]["zone-a"]["skillspector"] == {"skipped": "no MCP key"}
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    for w in ws.values():
        w.load_result = {"tools": [], "resources": [], "prompts": [], "errors": []}
    job = deploy(demo, breaking=True)  # the empty manifest is a breaking change against the first deploy
    assert job["result"]["zone-a"]["skillspector"] == {"skipped": "nothing to scan"} and r.specs == []


def test_a_large_tree_is_an_error_naming_the_size(demo, runner, tmp_path):
    write_skill(tmp_path, extra=[("big.bin", b"x" * (sk.MAX_DIR + 10))])
    job = deploy(demo)
    err = job["result"]["zone-a"]["skillspector"]["error"]
    assert "KiB" in err and runner.specs == []


def test_a_baseline_with_one_skill_scans_skill_by_skill(demo, runner, tmp_path, ws):  # noqa: F811
    write_skill(tmp_path)
    write_baseline(tmp_path)
    for w in ws.values():
        w.load_result = {"tools": [], "resources": [], "prompts": [], "errors": []}
    runner.out = {"exit_code": 0, "log": wrap(single("noodle-math", 2))}
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["skillspector"]["skills"] == 1
    _, _, spec = runner.specs[0]
    assert spec["configmap"]["data"]["baseline.yaml"] == "version: 1\nrules: []\n"
    assert "--baseline" in spec["job"]["spec"]["template"]["spec"]["containers"][0]["command"][2]


def test_the_llm_setting_hands_the_provider_to_the_job(demo, runner, tmp_path):
    write_skill(tmp_path)
    demo.put(
        "/api/v1/config/llm-providers",
        json={"openai_compatible": {"base_url": "http://ollama:11434/v1", "api_key": "k-1"}},
    )
    r = demo.put(SETTINGS, json={"llm": "openai_compatible"})
    assert r.status_code == 200, r.text
    job = deploy(demo)
    assert job["status"] == "ok", job
    env = runner.specs[0][2]["secret"]["stringData"]
    assert env["SKILLSPECTOR_PROVIDER"] == "openai_compatible" and env["SKILLSPECTOR_COMPAT_API_KEY"] == "k-1"
    assert env["SKILLSPECTOR_COMPAT_BASE_URL"] == "http://ollama:11434/v1"
    assert "--no-llm" not in runner.specs[0][2]["job"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "k-1" not in json.dumps(job)


# group settings, workers, token, policy -----------------------------------------------------------------------------


def test_group_settings_round_trip_rbac_audit_and_push(demo, runner, tmp_path):
    assert demo.get("/api/v1/groups/demo").json().get("skillspector") is None
    job = deploy(demo)
    v0 = sk.version(False, "", "")
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert f"RAMEN_SKILLSPECTOR_VERSION={v0}\n" in env_file and "RAMEN_SKILLSPECTOR_REQUIRED=0\n" in env_file
    r = demo.put(SETTINGS, json={"required": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"gate": False, "required": True, "llm": "", "pushed": {"demo/zone-a": "ok"}}
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert f"RAMEN_SKILLSPECTOR_VERSION={sk.version(True, '', '')}\n" in env_file
    assert "RAMEN_SKILLSPECTOR_REQUIRED=1\n" in env_file
    assert demo.put(SETTINGS, json={"gate": True}).json()["pushed"] == {}  # the version did not change
    demo.put("/api/v1/config/llm-providers", json={"anthropic": {"api_key": "test-anthropic-key"}})
    assert demo.put(SETTINGS, json={"llm": "anthropic"}).json()["pushed"] == {"demo/zone-a": "ok"}  # it did
    assert demo.get("/api/v1/groups/demo").json()["skillspector"] == {
        "gate": True,
        "required": True,
        "llm": "anthropic",
    }
    assert demo.put(SETTINGS, json={"llm": "openai"}).status_code == 422  # not configured
    assert demo.put(SETTINGS, json={"llm": "gemini"}).status_code == 422  # unknown
    demo.put("/api/v1/config/llm-providers", json={"vertex": {"project": "p", "location": "us-central1"}})
    assert "SkillSpector" in demo.put(SETTINGS, json={"llm": "vertex"}).json()["detail"]  # no such provider upstream
    assert demo.put(SETTINGS, json={"llm": ""}).json()["llm"] == ""
    assert demo.put(SETTINGS, json={"gate": "maybe"}).status_code == 422
    assert demo.put("/api/v1/groups/nope/skillspector", json={"gate": True}).status_code == 404
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "group.skillspector"]
    assert a and {"group:demo", "required:True"} <= set(a[0]["tags"])
    page = demo.get("/groups/demo").text
    assert "Scans" in page and 'hx-put="/api/v1/groups/demo/skillspector"' in page
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as u:
        login(u, "ga@x", PW)
        assert u.put(SETTINGS, json={"gate": False}).status_code == 200
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.put(SETTINGS, json={"gate": False}).status_code == 403


def test_workers_are_told_nothing_without_the_engine_or_required(demo, ws, monkeypatch):  # noqa: F811
    monkeypatch.delenv("RAMEN_SKILLSPECTOR_IMAGE", raising=False)
    job = deploy(demo)
    assert "RAMEN_SKILLSPECTOR_" not in Path(job["result"]["zone-a"]["env_file"]).read_text()


def test_the_token_answer_and_the_policy_document(demo, tmp_path, monkeypatch):
    monkeypatch.delenv("RAMEN_SKILLSPECTOR_IMAGE", raising=False)
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert "skillspector" not in t
    with TestClient(demo.app) as anon:
        assert "skillspector" not in anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a").json()
    write_baseline(tmp_path, "rules: [a]\n")
    demo.put(SETTINGS, json={"required": True})
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    v = sk.version(True, "", "rules: [a]\n")
    assert t["skillspector"] == {"version": v, "required": True, "llm": "", "baseline": "rules: [a]\n"}
    with TestClient(demo.app) as anon:
        r = anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a")
        assert r.json()["skillspector"] == {"version": v, "required": True} and "rules" not in r.text


def test_the_config_page_shows_evals_and_scans(demo, monkeypatch):
    monkeypatch.delenv("RAMEN_SKILLSPECTOR_IMAGE", raising=False)
    page = demo.get("/config").text
    assert "Evals &amp; scans" in page and "SkillSpector" in page and "off" in page
    monkeypatch.setenv("RAMEN_SKILLSPECTOR_IMAGE", IMAGE)
    assert IMAGE in demo.get("/config").text


# adapters -----------------------------------------------------------------------------------------------------------


async def test_the_base_and_local_adapters(tmp_path):
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

    assert await Bare().list_files("g", "mcp/skills/") == []
    with pytest.raises(NotImplementedError):
        await Bare().run_scan_job("g", "z", {})
    local = LocalCloud(tmp_path)
    with pytest.raises(NotImplementedError):
        await local.run_scan_job("g", "z", {})
    (tmp_path / "g" / "mcp" / "skills" / "a" / "sub").mkdir(parents=True)
    (tmp_path / "g" / "mcp" / "skills" / "a" / "SKILL.md").write_text("x")
    (tmp_path / "g" / "mcp" / "skills" / "a" / "sub" / "f.txt").write_text("y")
    (tmp_path / "g" / "mcp" / "tests.yaml").write_text("z")
    assert await local.list_files("g", "mcp/skills/") == ["mcp/skills/a/SKILL.md", "mcp/skills/a/sub/f.txt"]
    assert await local.list_files("g", "mcp/") == ["mcp/skills/a/SKILL.md", "mcp/skills/a/sub/f.txt", "mcp/tests.yaml"]
    assert await local.list_files("nope", "") == []


async def test_the_k8s_adapter_runs_the_scan_job_and_lists_the_bucket(monkeypatch):
    from ramen_console.cloud.gcp import GcpCloud
    from tests.fakes_gcp import FakeClients

    fk = FakeClients()
    cloud = GcpCloud(project="p1", region="us-central1", bucket="b", image="i", clients=fk, wait_secs=1, poll=0)
    k8s = fk.k8s
    spec = sk.job_spec("demo", "a", {"x/SKILL.md": b"#"}, {"RAMEN_GROUP": "demo"}, image="i")
    name = spec["name"]
    k8s.job_exit_code = 1
    k8s.pod_logs[("ramen-system", f"{name}-pod")] = wrap(single("x", 70)).splitlines()
    out = await cloud.run_scan_job("demo", "a", spec)
    assert out["exit_code"] == 1 and sk.extract(out["log"]) == single("x", 70)
    created = [c for c in k8s.calls if c[0] == "create" and c[2] == "ramen-system"]
    assert {(c[1], c[3]) for c in created} == {("ConfigMap", name), ("Secret", name), ("Job", name)}
    assert not any(k[1] == "ramen-system" for k in k8s.objs)
    k8s.job_outcome = "running"
    monkeypatch.setenv("RAMEN_SKILLSPECTOR_TIMEOUT_SECS", "0")
    with pytest.raises(RuntimeError, match="scan job .* timed out"):
        await cloud.run_scan_job("demo", "a", sk.job_spec("demo", "a", {"x/SKILL.md": b"#"}, {}, image="i"))
    bucket = fk.storage.bucket("b")
    bucket.data.update(
        {"demo/mcp/skills/a/SKILL.md": b"#", "demo/mcp/skills/a/s.sh": b"", "other/mcp/skills/b/SKILL.md": b""}
    )
    assert await cloud.list_files("demo", "mcp/skills/") == ["mcp/skills/a/SKILL.md", "mcp/skills/a/s.sh"]


async def test_the_aws_and_azure_adapters_list_their_buckets():
    from ramen_console.cloud.aws import AwsCloud
    from ramen_console.cloud.azure import AzureCloud
    from tests.fakes_aws import FakeAwsClients
    from tests.fakes_azure import FakeAzureClients
    from tests.test_aws_cloud import aws_env

    with aws_env():
        fk = FakeAwsClients().seed(bucket="ramen-b")
        fk.s3.put_object(Bucket="ramen-b", Key="demo/mcp/skills/a/SKILL.md", Body=b"#")
        fk.s3.put_object(Bucket="ramen-b", Key="demo/mcp/skills/a/x.py", Body=b"")
        fk.s3.put_object(Bucket="ramen-b", Key="other/mcp/skills/a/SKILL.md", Body=b"")
        c = AwsCloud(region="us-east-1", bucket="ramen-b", image="i", admin_key="adm", clients=fk)
        assert await c.list_files("demo", "mcp/skills/") == ["mcp/skills/a/SKILL.md", "mcp/skills/a/x.py"]
        assert await c.list_files("demo", "nope/") == []
    fz = FakeAzureClients()
    fz.container.blobs.update({"demo/mcp/skills/a/SKILL.md": b"#", "demo/mcp/skills/b/SKILL.md": b"#", "x/y": b""})
    c = AzureCloud(
        subscription="s",
        resource_group="rg",
        location="l",
        storage_account="acct",
        key_vault="kv",
        oidc_issuer="https://i",
        image="i",
        admin_key="adm",
        clients=fz,
    )
    assert await c.list_files("demo", "mcp/skills/") == ["mcp/skills/a/SKILL.md", "mcp/skills/b/SKILL.md"]


# chart and the pinned tag ---------------------------------------------------------------------------------------------


def test_the_pinned_tag_is_one_value_everywhere():
    spec = importlib.util.spec_from_file_location("check_versions", ROOT / "scripts" / "check_versions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pins = mod.engine_pins(ROOT, "skillspector")  # §34.1 generalised the pin check to every optional engine
    assert set(pins.values()) == {"2.13.4"}, pins
    assert {"deploy/docker/skillspector/Dockerfile", "deploy/helm/ramen/values.yaml", "docs/CONTRACTS.md"} <= set(pins)
    assert mod.check_engines(ROOT) == []
    assert IMAGE.endswith(":" + pins["deploy/helm/ramen/values.yaml"])


@needs_helm
def test_the_skillspector_engine_tells_the_console_and_nothing_else_runs():
    rc, err, docs = template("skillspector.engine=skillspector")
    assert rc == 0, err
    env = console_env(docs)
    assert env["RAMEN_SKILLSPECTOR_IMAGE"] == IMAGE and env["RAMEN_SKILLSPECTOR_TIMEOUT_SECS"] == "600"
    assert env["RAMEN_SKILLSPECTOR_CPU"] == "250m" and env["RAMEN_SKILLSPECTOR_MEMORY"] == "512Mi"
    assert by_kind(docs, "Role", "ramen-console-build") is not None  # the scan Job's RBAC and SA
    assert by_kind(docs, "ServiceAccount", "ramen-evals") is not None  # §33.4a: never the builder's
    assert not any(d["kind"] in ("Deployment", "Service") and "skillspector" in d["metadata"]["name"] for d in docs)
    rc, _, docs = template("skillspector.engine=skillspector", "evals.image=", "skillspector.image=custom/ss:9")
    assert rc == 0 and console_env(docs)["RAMEN_SKILLSPECTOR_IMAGE"] == "custom/ss:9"
    assert by_kind(docs, "Role", "ramen-console-build") is not None
    rc, _, docs = template()
    assert rc == 0 and not any(k.startswith("RAMEN_SKILLSPECTOR") for k in console_env(docs))
    rc, err, _ = template("skillspector.engine=snyk")
    assert rc != 0 and "none or skillspector" in err


@needs_helm
def test_helm_lint_with_the_scan_engine_on():
    r = subprocess.run(
        ["helm", "lint", str(CHART), "--set", "project=p", "--set", "skillspector.engine=skillspector"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_dockerfile_pins_the_tag_and_runs_as_non_root():
    text = (ROOT / "deploy" / "docker" / "skillspector" / "Dockerfile").read_text()
    assert "ARG SKILLSPECTOR_REF=v2.13.4" in text and "git clone --depth 1 --branch" in text
    assert "USER skillspector" in text and "WORKDIR /scan" in text and 'ENTRYPOINT ["skillspector"]' in text
    assert "git ca-certificates" in text
