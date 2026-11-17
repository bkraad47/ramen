"""CONTRACTS §29 (1.0.0c Phase 1) on a real console process (memory store, local cloud adapter) wired to one real
worker, with a group repo the test owns: the evals file is written, changed and removed by the test, so every branch
of §29.2 is driven here — absent, passed, failed with the gate off (warning + badge) and on (a deploy error like a
failed golden case), an engine error — through a fake evals runner that stands in for the promptfoo Job.

The fake runner is what part A's `LocalCloud.evals_runner` takes: an async callable
`run(group, zone, spec) -> {"exit_code": 0 | 100 | 1, "log": "…RAMEN_EVALS_RESULTS_BEGIN <results.json>
RAMEN_EVALS_RESULTS_END…"}` (the builder's log pattern, §24.2); `spec` is the Job spec (`configmap`, `secret` with the
injected env, `job`). The test installs it through a `sitecustomize.py` on the console's PYTHONPATH. Until the console
has the deploy integration, the cases that need it are xfail(strict=False); the worker gate (§29.3, part B) likewise
until the node binary knows `-32026`.

§29.1: the LLM providers card round trip (set / not set / clear, audited by provider tag, never a key in a response).
§29.3: `evals` in the token answer, `/.well-known/ramen-policy`, the worker gate on both eras."""

import json
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from ramen_tests import env as E
from ramen_tests.console import Console, evals_of, items
from ramen_tests.localconsole import LocalConsole
from ramen_tests.localnode import LocalNode, node_binary
from ramen_tests.mcp_client import EVALS_META, EVALS_REQUIRED, JsonRpcError, text_of
from ramen_tests.modern import ALL_INPUT, Modern

pytestmark = pytest.mark.conformance
PW = secrets.token_hex(10) + "Aa1!"
WORKER_ADMIN = secrets.token_hex(16)
EVALS_PATH = "mcp/evals/promptfooconfig.yaml"
CONSOLE_SRC = E.RAMEN_DIR / "console" / "src" / "ramen_console"
CALC = {"var1": 2, "var2": 3, "func": "add"}
STALE = "000000000000"

CONFIG = """# the demo file, minus the comments (what the test owns)
description: evals owned by the test
providers:
  - id: mcp
    config:
      enabled: true
      server:
        url: "{{env.RAMEN_MCP_URL}}"
        headers:
          Authorization: "Bearer {{env.RAMEN_MCP_KEY}}"
prompts:
  - "{{prompt}}"
tests:
  - description: calculator adds
    vars: {prompt: '{"tool": "demo_calculator_tool", "args": {"var1": 2, "var2": 3, "func": "add"}}'}
    assert: [{type: contains, value: "5"}]
  - description: word_count counts
    vars: {prompt: '{"tool": "word_count", "args": {"text": "a b c"}}'}
    assert: [{type: contains, value: '"words": 3'}]
  - description: unit_convert converts
    vars: {prompt: '{"tool": "unit_convert", "args": {"value": 1, "from_unit": "km", "to_unit": "m"}}'}
    assert: [{type: contains, value: "1000"}]
"""

# The stand-in for promptfoo, installed into the console process by `sitecustomize.py` (the console is a subprocess;
# `LocalCloud.evals_runner(group, zone, spec)` is the console's injection point): exit 0 / 100 / 1 as promptfoo does,
# results.json between the markers in the log, the Job spec's env and config recorded for the test to read.
FAKE_RUNNER = """
import json, os, pathlib
BEGIN, END = "RAMEN_EVALS_RESULTS_BEGIN", "RAMEN_EVALS_RESULTS_END"

def _results(passed, failed, errors):
    rows = [{"success": True, "testCase": {"description": "calculator adds"}}] * passed
    rows += [{"success": False, "failureReason": "assertion", "error": "Expected output to contain \\"5\\"",
              "testCase": {"description": "word_count counts"}}] * failed
    return {"evalId": "fake-eval", "results": {"version": 3, "results": rows,
            "stats": {"successes": passed, "failures": failed, "errors": errors}}, "shareableUrl": None}

async def run(group, zone, spec):
    ctl = pathlib.Path(os.environ["RAMEN_EVALS_FAKE_CTL"])
    mode = ctl.read_text().strip() if ctl.exists() else "pass"
    env = (spec.get("secret") or {}).get("stringData") or spec.get("env") or {}
    config = (spec.get("configmap") or {}).get("data") or {}
    (ctl.parent / "last_spec.json").write_text(json.dumps(
        {"group": group, "zone": zone, "env": env, "config": config, "spec": spec}, default=str))
    if mode == "error":
        return {"exit_code": 1, "log": "Error: ECONNREFUSED " + str(env.get("RAMEN_MCP_URL", ""))}
    body = _results(2, 1, 0) if mode == "fail" else _results(3, 0, 0)
    log = "promptfoo eval\\n" + BEGIN + "\\n" + json.dumps(body) + "\\n" + END + "\\n"
    return {"exit_code": 100 if mode == "fail" else 0, "log": log}
"""
SITECUSTOMIZE = """
try:
    import ramen_evals_fake
    from ramen_console.cloud import local as _local
    _local.LocalCloud.evals_runner = staticmethod(ramen_evals_fake.run)
except Exception:  # noqa: BLE001 - a console without the hook simply runs no evals
    pass
"""
CONSOLE_PY = CONSOLE_SRC / "deploy.py"
WEB = CONSOLE_SRC / "web"


PROBES = {  # part A's pieces land file by file: one probe per feature over the console sources
    "deploy": (CONSOLE_PY, "evals."),
    "llm": (WEB, "llm-providers"),
    "group-evals": (WEB, "/evals"),
    "policy": (WEB, "ramen-policy"),
    "token": (CONSOLE_SRC / "oauth_server.py", "evals"),
    "llm-page": (CONSOLE_SRC / "templates" / "config.html", "LLM providers"),
}


def console_has(*features: str) -> bool:
    if not (CONSOLE_SRC / "evals.py").exists():
        return False
    for f in features:
        where, needle = PROBES[f]
        files = list(where.glob("*.py")) if where.is_dir() else [where]
        if not any(needle in p.read_text() for p in files if p.exists()):
            return False
    return True


def local_has_runner() -> bool:
    p = CONSOLE_SRC / "cloud" / "local.py"
    return p.exists() and "evals_runner" in p.read_text() and console_has("deploy")


def node_has_gate() -> bool:
    b = node_binary()
    return bool(b) and b"evals config required" in Path(b).read_bytes()


def part_a(*needles: str):
    return pytest.mark.xfail(not console_has(*needles), reason=f"part A (console {needles}) not landed", strict=False)


part_a_runner = pytest.mark.xfail(
    not local_has_runner(), reason="part A's LocalCloud.evals_runner / deploy integration not landed", strict=False
)
part_b = pytest.mark.xfail(not node_has_gate(), reason="part B (node -32026 gate) not in the node binary", strict=False)


# --- the repo the test owns ------------------------------------------------------------------------------------------
def git(repo: Path, *args) -> str:
    r = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "-c", "user.email=tests@ramen.local", "-c", "user.name=ramen-tests", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr[:300]}"
    return r.stdout


def make_repo(root: Path) -> Path:
    """A git repo on branch `main` with the demo group's `mcp/` (the submodule checkout beside `ramen/`, else the
    one-tool fixture), minus the evals file and the §31 `llm-config/` directory: each test plants what it drives."""
    demo = E.RAMEN_DIR.parent / "ramen-demo-mcp"
    src = demo if (demo / "mcp" / "tools" / "word_count").exists() else E.FIXTURES / "demo_group"
    repo = root / "repo"
    shutil.copytree(src / "mcp", repo / "mcp", ignore=shutil.ignore_patterns("__pycache__", "evals", "llm-config"))
    static_rails(repo / "mcp" / "guardrails.yaml")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "demo group")
    return repo


def static_rails(path: Path) -> None:
    """§36.4: the demo's `iris_*` tools name LLM rails (`pre: [self_check_input, …]`), which a deploy refuses without
    an LLM provider on the group or the org. These suites test evals / scans / configs, not rails: every stage that
    names rails is turned back into `true` so the static flows alone run (test_console_guardrails.py owns the rails)."""
    if not path.exists():
        return
    doc = yaml.safe_load(path.read_text()) or {}
    for spec in (doc.get("tools") or {}).values():
        for stage in ("pre", "post"):
            if isinstance(spec, dict) and isinstance(spec.get(stage), list | dict):
                spec[stage] = True
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def set_evals_file(repo: Path, text: str | None) -> None:
    p = repo / EVALS_PATH
    if text is None:
        if p.exists():
            p.unlink()
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    if git(repo, "status", "--porcelain").strip():
        git(repo, "commit", "-q", "-m", "evals file" if text else "no evals file")


# --- the world ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    """A console with a fake evals engine, one real worker for zone `local` reached by the deploy file in its bucket
    (the `wired` pattern of test_console_sleep_otel.py), the group `demo` on the test's own repo, one MCP key."""
    root = Path(__import__("tempfile").mkdtemp(prefix="ramen-evals-"))
    repo = make_repo(root)
    fake = root / "fake"
    fake.mkdir()
    (fake / "ramen_evals_fake.py").write_text(FAKE_RUNNER)
    (fake / "sitecustomize.py").write_text(SITECUSTOMIZE)
    ctl = fake / "mode"
    ctl.write_text("pass")
    env = {
        "RAMEN_ADMIN_KEY": WORKER_ADMIN,
        "RAMEN_EVALS_IMAGE": "fake/promptfoo:test",  # §29.2: no image → nothing runs
        "RAMEN_EVALS_FAKE_CTL": str(ctl),
        "RAMEN_EVALS_UI_URL": "http://promptfoo.test:3000",
        "PYTHONPATH": str(fake),
    }
    con = LocalConsole(env=env)
    bucket = con.dir / "buckets" / "demo"
    bucket.mkdir(parents=True)
    node = LocalNode(bucket=bucket, admin_key=WORKER_ADMIN, env={"RAMEN_SESSION_SECRET": con.session_secret})
    con.env["RAMEN_WORKER_URL"] = node.target
    node.env["RAMEN_OAUTH_ISSUER"] = con.url
    with con, node:
        a = con.admin
        assert a.create_zone("local", provider="local", region="local").status_code in (201, 409)
        assert a.create_group("demo", str(repo)).status_code in (201, 409)
        assert a.create_environment("demo", "dev", ["local"]).status_code in (201, 409)
        r = a.create_mcp_key("demo", "evals")
        assert r.status_code == 201, r.text
        users = {}
        for name, role, groups in (("gadmin", "group_admin", ["demo"]), ("viewer", "viewer", ["demo"])):
            email = f"{name}-{secrets.token_hex(3)}@ramen.test"
            assert a.create_user(email, PW, role, groups).status_code == 201
            c = Console(con.url)
            assert c.login(email, PW).status_code in (200, 303)
            users[name] = c
        node.wait_serving()
        yield {"con": con, "node": node, "repo": repo, "ctl": ctl, "fake": fake, "key": r.json()["key"], "users": users}
        for c in users.values():
            c.close()


def deploy(world) -> dict:
    con = world["con"]
    return con.admin.wait_job(con.admin.deploy("demo", "dev").json()["id"], timeout=300)


def mode(world, value: str) -> None:
    world["ctl"].write_text(value)


def log_text(job: dict) -> str:
    return "\n".join(job.get("log") or [])


def env_file(world) -> dict:
    path = world["con"].dir / "buckets" / "demo" / ".ramen" / "env-local"
    assert path.exists(), world["con"].output()
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def audit(world, action: str) -> list[dict]:
    return [e for e in items(world["con"].admin.audit()) if e.get("action") == action]


@pytest.fixture
def no_file(world):
    set_evals_file(world["repo"], None)
    return world


@pytest.fixture
def with_file(world):
    set_evals_file(world["repo"], CONFIG)
    mode(world, "pass")
    world["con"].admin.set_group_evals("demo", gate=False, required=False)
    yield world
    mode(world, "pass")
    world["con"].admin.set_group_evals("demo", gate=False, required=False)


# --- §29.1 LLM providers ---------------------------------------------------------------------------------------------
@part_a("llm", "llm-page")
def test_llm_providers_round_trip_never_shows_a_key(world):
    con, key = world["con"], "sk-ant-test-" + secrets.token_hex(12)
    a = con.admin
    before = a.llm_providers()
    assert isinstance(before, dict) and key not in json.dumps(before)
    r = a.set_llm_providers({"anthropic": {"api_key": key}, "openai": {"api_key": "sk-" + secrets.token_hex(8)}})
    assert r.status_code == 200, r.text
    doc = a.llm_providers()
    assert key not in json.dumps(doc) and "anthropic" in json.dumps(doc), doc
    assert "set" in json.dumps(doc).lower() or any(v for v in doc.values()), f"a set key reads as set: {doc}"
    for page in ("/config", "/audit"):
        text = a.page(page).text
        assert key not in text, f"{page} leaked the key"
    assert "LLM providers" in a.page("/config").text, "the card on the Config page"
    rows = audit(world, "config.llm_providers")
    assert rows and any("provider:anthropic" in e.get("tags", []) for e in rows), rows[:2]
    assert key not in json.dumps(rows)
    assert a.set_llm_providers({"anthropic": {"api_key": ""}}).status_code == 200, "an empty string clears a key"
    cleared = json.dumps(a.llm_providers()).lower()
    assert "anthropic" not in cleared or "not set" in cleared or '"anthropic": {}' in cleared or "false" in cleared
    assert a.set_llm_providers({"openai": {"api_key": ""}}).status_code == 200
    for who in ("gadmin", "viewer"):
        assert world["users"][who].get("config_llm_providers").status_code == 403, who
        assert world["users"][who].set_llm_providers({"anthropic": {"api_key": "x"}}).status_code == 403, who


# --- §29.2 the group setting -----------------------------------------------------------------------------------------
@part_a("group-evals")
def test_group_evals_settings_default_off_and_are_the_group_admins_call(world):
    con = world["con"]
    g = con.admin.get("group", group="demo").json()
    assert (g.get("evals") or {}) in ({}, {"gate": False, "required": False}), g.get("evals")
    assert world["users"]["viewer"].set_group_evals("demo", gate=True).status_code == 403
    r = world["users"]["gadmin"].set_group_evals("demo", gate=True)
    assert r.status_code == 200, r.text
    assert con.admin.get("group", group="demo").json()["evals"] == {"gate": True, "required": False}
    assert con.admin.set_group_evals("demo", gate=False).status_code == 200
    assert con.admin.get("group", group="demo").json()["evals"] == {"gate": False, "required": False}
    page = world["users"]["gadmin"].page("/groups/demo").text
    assert "evals" in page.lower(), "the group page shows the setting"


# --- §29.2 the deploy ------------------------------------------------------------------------------------------------
@part_a("deploy")
def test_no_evals_file_means_nothing_runs(no_file):
    job = deploy(no_file)
    assert job["status"] == "ok", job.get("error")
    assert evals_of(job, "local") == "absent", job.get("result")
    assert "promptfoo" not in log_text(job).lower() and "evals" not in json.dumps(job.get("result")), "nothing recorded"
    spec = no_file["fake"] / "last_spec.json"
    assert not spec.exists(), "the runner was called without a file"


@part_a_runner
def test_evals_pass_and_the_job_records_the_counts_and_the_injected_env(with_file):
    w = with_file
    job = deploy(w)
    assert job["status"] == "ok", job.get("error")
    ev = evals_of(job, "local")
    assert isinstance(ev, dict) and (ev["passed"], ev["failed"], ev.get("errors", 0)) == (3, 0, 0), ev
    # the badge opens the console's page of the run (every case), readable by the group's members
    assert ev["url"] == f"/groups/demo/evals/{job['id']}/local", ev
    r = w["con"].admin.evals_report("demo", job["id"], "local")
    assert r.status_code == 200 and "3 passed, 0 failed, 0 errors" in r.text, r.text[:300]
    text = log_text(job)
    assert re.search(r"evals.*3 passed|3 passed.*evals|evals: 3", text, re.I), text[-1500:]
    spec = json.loads((w["fake"] / "last_spec.json").read_text())
    blob = json.dumps(spec)
    env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    for k in ("RAMEN_MCP_URL", "RAMEN_MCP_KEY", "RAMEN_GROUP", "RAMEN_ENV", "RAMEN_ZONE"):
        assert k in env or k in blob, f"{k} not injected: {sorted(env) or blob[:300]}"
    if env:
        # §31.4a (D76): the Job calls as the deploy's own `RAMEN_EVALS_KEY` (`rek_…`), never as the group key
        assert env["RAMEN_MCP_KEY"].startswith("rek_") and env["RAMEN_MCP_KEY"] != w["key"], env["RAMEN_MCP_KEY"][:4]
        assert (env["RAMEN_GROUP"], env["RAMEN_ZONE"]) == ("demo", "local"), env
        assert env["RAMEN_MCP_URL"].endswith("/mcp"), env["RAMEN_MCP_URL"]
        assert env["RAMEN_MCP_KEY"] not in text and env["RAMEN_MCP_KEY"] not in json.dumps(job.get("result"))
    assert CONFIG.splitlines()[1] in blob or EVALS_PATH in blob, "the spec carries the config"
    assert w["key"] not in text and w["key"] not in json.dumps(job.get("result")), "the key is never in the job"


@part_a_runner
def test_failed_evals_with_the_gate_off_are_a_warning_and_a_badge(with_file):
    w = with_file
    mode(w, "fail")
    job = deploy(w)
    assert job["status"] == "ok", f"gate off: the deploy goes through: {job.get('error')}"
    ev = evals_of(job, "local")
    assert (ev["passed"], ev["failed"]) == (2, 1), ev
    text = log_text(job)
    assert "word_count counts" in text, "the failed test's description is in the job log"
    assert re.search(r"1 failed", text), text[-1500:]
    page = w["con"].admin.page("/groups/demo").text
    assert re.search(r"evals:\s*1 failed", page), "the red badge on the last deploy"


@part_a_runner
def test_failed_evals_with_the_gate_on_fail_the_deploy_like_a_golden_case(with_file):
    w = with_file
    assert w["con"].admin.set_group_evals("demo", gate=True).status_code == 200
    mode(w, "fail")
    job = deploy(w)
    assert job["status"] == "error", job
    assert "evals" in (job.get("error") or "").lower(), job.get("error")
    ev = evals_of(job, "local")
    assert ev and ev["failed"] == 1, ev
    mode(w, "pass")
    job = deploy(w)
    assert job["status"] == "ok", job.get("error")
    assert evals_of(job, "local")["failed"] == 0


@part_a_runner
def test_an_engine_error_is_an_error_with_the_gate_on_and_a_warning_without(with_file):
    w = with_file
    mode(w, "error")
    job = deploy(w)
    assert job["status"] == "ok", job.get("error")
    ev = evals_of(job, "local")
    assert ev and (ev.get("errors") or ev.get("error")), ev
    assert "ECONNREFUSED" in log_text(job) or "error" in log_text(job).lower()
    assert w["con"].admin.set_group_evals("demo", gate=True).status_code == 200
    job = deploy(w)
    assert job["status"] == "error" and "evals" in (job.get("error") or "").lower(), job.get("error")


# --- §29.3 what clients are told --------------------------------------------------------------------------------------
def token_evals(world) -> dict | None:
    con = world["con"]
    return con.mint(con.register_client()).get("evals")


@part_a("policy", "token", "deploy")
def test_the_token_answer_and_the_policy_carry_the_evals_version(with_file):
    w = with_file
    assert deploy(w)["status"] == "ok"
    told = token_evals(w)
    assert told and set(told) >= {"version", "required", "config"}, told
    assert told["config"] == CONFIG and told["required"] is False
    v0 = told["version"]
    assert re.fullmatch(r"[0-9a-f]{12}", v0), v0  # sha256 over config + required, first 12 hex (§29.3)
    pol = w["con"].admin.policy("demo", "local")
    assert pol["evals"] == {"version": v0, "required": False}, pol
    assert "telemetry" in pol and set(pol["telemetry"]) >= {"version", "required"}, pol
    assert "config" not in json.dumps(pol["evals"]) and w["key"] not in json.dumps(pol), "public, no secrets"
    assert w["con"].admin.set_group_evals("demo", required=True).status_code == 200
    told2 = token_evals(w)
    assert told2["required"] is True and told2["version"] != v0, "the version covers `required`"
    assert w["con"].admin.policy("demo", "local")["evals"] == {"version": told2["version"], "required": True}


@part_a("policy", "token")
def test_without_a_file_the_token_and_the_policy_say_nothing_about_evals(no_file):
    w = no_file
    assert deploy(w)["status"] == "ok"
    assert token_evals(w) is None
    pol = w["con"].admin.policy("demo", "local")
    assert "telemetry" in pol and not pol.get("evals"), pol


# --- §29.3 the worker gate ------------------------------------------------------------------------------------------
@part_a("deploy")
@part_b
def test_a_required_group_gates_calls_on_the_evals_version(with_file):
    w, node, key = with_file, with_file["node"], with_file["key"]
    con = w["con"]
    assert con.admin.set_group_evals("demo", required=True).status_code == 200
    job = deploy(w)
    assert job["status"] == "ok", f"the console's own golden calls pass the gate: {job.get('error')}"
    env = env_file(w)
    version = env.get("RAMEN_EVALS_VERSION")
    assert version and env.get("RAMEN_EVALS_REQUIRED") == "1", sorted(env)
    assert token_evals(w)["version"] == version
    for transport in ("grpc", "http"):
        m = Modern(node.for_transport(transport), ALL_INPUT, key=key, telemetry=None, evals=None)
        r = m.call_raw("demo_calculator_tool", CALC)
        assert (r.code, r.error["message"]) == (EVALS_REQUIRED, "evals config required"), r.message
        assert r.error["data"] == {"version": version} and r.status in (None, 428), (r.status, r.error)
        r = m.with_evals(STALE).call_raw("demo_calculator_tool", CALC)
        assert (r.code, r.error["message"]) == (EVALS_REQUIRED, "evals config changed: sign in again"), r.message
        assert r.error["data"] == {"version": version} and r.status in (None, 428)
        done = m.with_evals(version).call("demo_calculator_tool", CALC)
        assert done["resultType"] == "complete" and text_of(done) in ("5", "5.0"), done
        assert m.request("tools/list")["tools"], "lists are never gated"
        assert EVALS_META in json.dumps(m.with_evals(version).body("tools/call", {"name": "x", "arguments": {}}))
    # a 2025-06-18 body through the bridge: the version as `params.evals`
    legacy = node.http_client(key)
    legacy.initialize()
    with pytest.raises(JsonRpcError) as e:
        legacy.call_tool("demo_calculator_tool", CALC)
    assert e.value.code == EVALS_REQUIRED, e.value
    legacy.evals = version
    assert text_of(legacy.call_tool("demo_calculator_tool", CALC)) in ("5", "5.0")
    legacy.close()
    lines = node.log.read_text()
    assert "evals_required" in lines and "evals_stale" in lines, "access-log reasons"
    assert con.admin.set_group_evals("demo", required=False).status_code == 200
    assert deploy(w)["status"] == "ok"
    deadline = time.monotonic() + 30
    while True:
        r = Modern(node.http, ALL_INPUT, key=key, telemetry=None, evals=None).call_raw("demo_calculator_tool", CALC)
        if r.result or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    assert r.result and r.result["resultType"] == "complete", f"the requirement is lifted by the deploy: {r.message}"
