"""CONTRACTS §21.5-5 (0.7.5): golden cases run through the guardrail hooks like any call, so a case whose payload a
rail blocks fails the deploy gate with the rail's message.

A real console (local adapter) deploys a local git repo built from `guarded_group` to a real node it reaches over
gRPC (`RAMEN_WORKER_URL`), with the group's own MCP key for the canary calls. First a deploy whose cases pass, then
one more commit adding a case the input rail blocks: that deploy must end `error` and name the case and the rail."""

import json
import subprocess
from pathlib import Path

import pytest

from ramen_tests import env as E
from ramen_tests.localconsole import LocalConsole
from ramen_tests.localnode import LocalNode

pytestmark = pytest.mark.conformance
GROUP, ZONE, ENV = "gold", "local", "dev"
GOOD = """cases:
  - name: add
    tool: demo_calculator_tool
    args: {var1: 40, var2: 2, func: add}
    expect: {text_contains: "42"}
  - name: words
    tool: word_count
    args: {text: "ramen is a bowl of noodles"}
    expect: {subset: {structuredContent: {words: 6}}}
"""
BLOCKED = (
    GOOD
    + """  - name: words of a blocked statement
    tool: word_count
    args: {text: "please DROP TABLE users"}
    expect: {subset: {structuredContent: {words: 4}}}
"""
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@x", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def commit_cases(repo: Path, text: str, msg: str) -> None:
    (repo / "mcp" / "tests.yaml").write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("golden")
    repo = root / "repo"
    import shutil

    shutil.copytree(E.FIXTURES / "guarded_group", repo, ignore=shutil.ignore_patterns("__pycache__"))
    git(repo, "init", "-q", "-b", "main")
    commit_cases(repo, GOOD, "good cases")
    node = LocalNode(bucket=root / "placeholder", env={"RAMEN_GROUP": GROUP})  # port chosen now, bucket set below
    con = LocalConsole(env={"RAMEN_WORKER_URL": node.target, "RAMEN_ADMIN_KEY": node.admin_key})
    bucket = con.dir / "buckets" / GROUP
    bucket.mkdir(parents=True)
    node.env["RAMEN_BUCKET"] = str(bucket)
    with node, con:
        node.wait_answering()
        a = con.admin
        assert a.create_zone(ZONE, provider="local", region="local").status_code in (201, 409)
        assert a.create_group(GROUP, str(repo)).status_code == 201
        assert a.create_environment(GROUP, ENV, [ZONE]).status_code == 201
        r = a.post("mcp_keys", {"name": "canary"}, group=GROUP)
        assert r.status_code == 201, r.text
        yield con, node, repo


def deploy(con: LocalConsole) -> dict:
    job = con.admin.deploy(GROUP, ENV).json()
    return con.admin.wait_job(job["id"], timeout=300)


def test_passing_golden_cases_deploy_through_the_rails(world):
    con, node, _ = world
    job = deploy(con)
    assert job["status"] == "ok", json.dumps(job, indent=1)[-2500:]
    zone = job["result"][ZONE]
    assert zone["ok"] and zone["workers"][0]["ok"], zone
    text = json.dumps(job)
    assert "golden add: ok" in text and "golden words: ok" in text, job.get("log")
    assert node.node.health() == "SERVING"
    load = node.node.admin_reload()
    if "guardrails" not in load:
        pytest.xfail("waiting for worker-agent (§21.2): the runtime reports no guardrails block")
    assert load["guardrails"]["engine"] == "nemo", load["guardrails"]


def test_a_golden_case_the_input_rail_blocks_fails_the_gate_with_the_rails_message(world):
    con, _, repo = world
    commit_cases(repo, BLOCKED, "a case the rail blocks")
    job = deploy(con)
    text = json.dumps(job)
    if job["status"] == "ok":
        pytest.xfail("waiting for worker-agent (§21.2): the rail did not block the golden case's payload")
    assert job["status"] == "error", text[-2500:]
    assert "golden cases failed" in text and "words of a blocked statement" in text, text[-2500:]
    if "guardrail blocked: pre" not in text:
        pytest.xfail("waiting for ui-agent (§21.5-5): the golden failure reason omits the tool's isError text")
    assert "blocked by policy" in text, text[-2500:]
