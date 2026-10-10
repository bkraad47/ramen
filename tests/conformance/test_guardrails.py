"""CONTRACTS §21.2 (0.7.5, D46): guardrails inside the runtime, on real node processes and both transports.

`guarded_group` opts three tools into deterministic NeMo rails (no model): an input rail that blocks "drop table", an
output rail that blocks "secret". `policy_group` uses the team's own `policy.py` (engine `policy`), which also records
every payload it saw to `$RAMEN_TEST_POLICY_LOG`, so the tests can prove a hook ran, did not run, or never saw a
secret value. `broken_guarded_group` has a NeMo config that cannot load: every opted-in tool must answer blocked.
§21.5-1 is the combination with 0.7.2 tool access on the same tool."""

import json
import secrets
import shutil
import time
from pathlib import Path

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, text_of
from ramen_tests.tokens import user_token

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
SECRET, ISSUER, PUBLIC = "conformance-secret", "https://console.example", "https://mcp.example"
IGNORE = shutil.ignore_patterns("__pycache__")
BLOCKED_TEXT = "please DROP TABLE users"
SECRET_VALUE = "conformance-" + secrets.token_hex(6)  # generated: never a literal a scanner could flag


def meta(result: dict) -> dict | None:
    return ((result.get("_meta") or {}).get("ramen") or {}).get("guardrail")


def policy_copy(root: Path, fail: str) -> Path:
    """`policy_group` with its `fail:` line flipped (closed/open)."""
    dst = root / f"policy_{fail}"
    shutil.copytree(E.FIXTURES / "policy_group", dst, ignore=IGNORE)
    y = dst / "mcp" / "guardrails.yaml"
    y.write_text(y.read_text().replace("fail: closed", f"fail: {fail}"))
    return dst


def records(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def wait_reasons(node: LocalNode, tool: str, wanted: set[str], timeout: float = 5) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    by_reason: dict[str, dict] = {}
    while time.monotonic() < deadline:
        calls = [x for x in node.log_lines() if x.get("msg") == "mcp" and x.get("method") == "tools/call"]
        by_reason = {x["reason"]: x for x in calls if x.get("name") == tool and x.get("reason")}
        if wanted <= set(by_reason):
            break
        time.sleep(0.2)
    return by_reason


# --- nemo ------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def nemo():
    with LocalNode(bucket=E.FIXTURES / "guarded_group") as n:
        n.wait_serving()
        r = n.node.call_tool("echo_tool", {"text": "the secret is x"})  # probe: a post rail must block this
        n.enforced = bool(r.get("isError"))
        yield n


def enforced(node):
    if not node.enforced:
        pytest.xfail("waiting for worker-agent (§21.2): mcp/guardrails.yaml is not enforced by this runtime")


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_pre_rail_blocks_the_call_before_the_tool_runs(nemo, transport):
    enforced(nemo)
    c = nemo.for_transport(transport)
    c.initialize()
    r = c.call_tool("word_count", {"text": BLOCKED_TEXT})
    assert r["isError"] is True, r
    assert text_of(r).startswith("guardrail blocked: pre: ") and "blocked by policy" in text_of(r), r
    assert "structuredContent" not in r, "a blocked call never carries the tool's output"
    assert meta(r) == {"stage": "pre", "engine": "nemo"}, r


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_allowed_payload_passes_unchanged(nemo, transport):
    enforced(nemo)
    c = nemo.for_transport(transport)
    c.initialize()
    r = c.call_tool("word_count", {"text": "ramen is a bowl"})
    assert r["isError"] is False and r["structuredContent"]["words"] == 4, r
    assert meta(r) is None, "an allowed call carries no guardrail marker"
    assert float(text_of(c.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"}))) == 5


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_post_rail_blocks_and_never_returns_the_output(nemo, transport):
    enforced(nemo)
    c = nemo.for_transport(transport)
    c.initialize()
    r = c.call_tool("echo_tool", {"text": "the secret is x"})
    assert r["isError"] is True, r
    assert text_of(r).startswith("guardrail blocked: post: ") and "blocked by policy" in text_of(r), r
    assert "the secret is x" not in text_of(r) and "structuredContent" not in r, r
    assert meta(r) == {"stage": "post", "engine": "nemo"}, r
    assert text_of(c.call_tool("echo_tool", {"text": "plain words"})) == "plain words"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_tool_not_listed_in_guardrails_yaml_is_never_checked(nemo, transport):
    enforced(nemo)
    c = nemo.for_transport(transport)
    c.initialize()
    r = c.call_tool("plain_tool", {"text": "DROP TABLE secret"})
    assert r["isError"] is False and text_of(r) == "DROP TABLE secret" and meta(r) is None, r


def test_access_log_names_guardrail_pre_and_guardrail_post(nemo):
    enforced(nemo)
    c = nemo.http_client()
    c.initialize()
    assert c.call_tool("word_count", {"text": BLOCKED_TEXT})["isError"]
    assert c.call_tool("echo_tool", {"text": "a secret"})["isError"]
    pre = wait_reasons(nemo, "word_count", {"guardrail_pre"})
    post = wait_reasons(nemo, "echo_tool", {"guardrail_post"})
    assert "guardrail_pre" in pre, [x for x in nemo.log_lines() if x.get("method") == "tools/call"][-4:]
    assert "guardrail_post" in post, [x for x in nemo.log_lines() if x.get("method") == "tools/call"][-4:]
    assert BLOCKED_TEXT not in nemo.log.read_text(), "payloads never reach the access log"


def test_reload_result_describes_the_guardrails_outside_the_manifest_hash(nemo):
    enforced(nemo)
    r = nemo.node.admin_reload()
    g = r.get("guardrails")
    assert g and g["engine"] == "nemo" and g["fail"] == "closed", r
    assert g["tools"] == {
        "word_count": ["pre", "post"],
        "echo_tool": ["pre", "post"],
        "demo_calculator_tool": ["pre"],
    }, g
    with LocalNode(bucket=E.FIXTURES / "guarded_group", env={"RAMEN_LOG_FILE": str(nemo.dir / "w2.log")}) as n2:
        n2.wait_serving()
        plain = n2.node.admin_reload()
    assert plain["hash"] == r["hash"], "same tools → same hash, whatever the guardrails say"


# --- broken engine -------------------------------------------------------------------------------------------------
def test_an_engine_that_cannot_load_blocks_its_tools_whatever_fail_says():
    """`fail: open` governs call-time errors only: a config that never loaded means blocked, not unguarded."""
    with LocalNode(bucket=E.FIXTURES / "broken_guarded_group") as n:
        n.wait_serving()
        r = n.node.call_tool("echo_tool", {"text": "hello"})
        if not r.get("isError"):
            pytest.xfail("waiting for worker-agent (§21.2): broken guardrails config leaves the tool unguarded")
        assert text_of(r).startswith("guardrail unavailable: "), r
        assert "hello" not in text_of(r)
        assert text_of(n.node.call_tool("plain_tool", {"text": "hello"})) == "hello", "unlisted tools unaffected"
        load = n.node.admin_reload()
        assert any(e.get("package") == "guardrails" for e in load.get("errors", [])), load.get("errors")
        assert load["guardrails"]["engine"] == "nemo", load.get("guardrails")


# --- policy engine ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def policy_closed(tmp_path_factory):
    root = tmp_path_factory.mktemp("policy")
    log = root / "closed.jsonl"
    with LocalNode(bucket=policy_copy(root, "closed"), env={"RAMEN_TEST_POLICY_LOG": str(log)}) as n:
        n.wait_serving()
        n.policy_log = log
        n.enforced = bool(n.node.call_tool("echo_tool", {"text": BLOCKED_TEXT}).get("isError"))
        yield n


@pytest.fixture(scope="module")
def policy_open(tmp_path_factory):
    root = tmp_path_factory.mktemp("policy-open")
    log = root / "open.jsonl"
    with LocalNode(bucket=policy_copy(root, "open"), env={"RAMEN_TEST_POLICY_LOG": str(log)}) as n:
        n.wait_serving()
        n.policy_log = log
        n.enforced = bool(n.node.call_tool("echo_tool", {"text": BLOCKED_TEXT}).get("isError"))
        yield n


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_policy_pre_and_post_block_with_the_policys_own_message(policy_closed, transport):
    enforced(policy_closed)
    c = policy_closed.for_transport(transport)
    c.initialize()
    r = c.call_tool("echo_tool", {"text": BLOCKED_TEXT})
    assert r["isError"] and text_of(r) == "guardrail blocked: pre: arguments mention a blocked statement", r
    assert meta(r) == {"stage": "pre", "engine": "policy"}
    r = c.call_tool("echo_tool", {"text": "my secret"})
    assert r["isError"] and text_of(r) == "guardrail blocked: post: output mentions a secret", r
    assert meta(r) == {"stage": "post", "engine": "policy"}
    seen = records(policy_closed.policy_log)
    assert any(x["stage"] == "pre" and x["payload"] == {"text": BLOCKED_TEXT} for x in seen), seen[-3:]
    # post payload = the result text, or structuredContent when the tool declares an output (echo_tool → {"result"})
    assert any(x["stage"] == "post" and x["payload"] in ("my secret", {"result": "my secret"}) for x in seen), seen[-3:]


def test_fail_closed_turns_a_hook_error_into_unavailable(policy_closed):
    enforced(policy_closed)
    r = policy_closed.node.call_tool("echo_tool", {"text": "boom"})
    assert r["isError"] and text_of(r).startswith("guardrail unavailable: "), r
    assert "boom" not in text_of(r) or "policy exploded" in text_of(r)


def test_fail_open_lets_the_call_proceed_when_the_hook_errors(policy_open):
    enforced(policy_open)
    r = policy_open.node.call_tool("echo_tool", {"text": "boom"})
    assert r["isError"] is False and text_of(r) == "boom", r
    assert policy_open.node.call_tool("echo_tool", {"text": BLOCKED_TEXT})["isError"], "a verdict still counts"
    r = policy_open.node.call_tool("echo_tool", {"text": "kaboom"})  # post hook error, fail open → the output
    assert r["isError"] is False and text_of(r) == "kaboom", r


def test_the_pre_payload_carries_the_secret_reference_never_its_value(tmp_path):
    """§21.2: pre runs on the *redacted* arguments — `{{$group.VAR}}` as written, the value stays in the runtime."""
    log = tmp_path / "secret.jsonl"
    env = {"RAMEN_TEST_POLICY_LOG": str(log), "RAMEN_SECRET_DEMO__TOKEN": SECRET_VALUE, "RAMEN_GROUP": "demo"}
    with LocalNode(bucket=policy_copy(tmp_path, "closed"), env=env) as n:
        n.wait_serving()
        r = n.node.call_tool("echo_tool", {"text": "token={{$demo.TOKEN}}"})
        seen = [x for x in records(log) if x["stage"] == "pre"]
        if not seen:
            pytest.xfail("waiting for worker-agent (§21.2): the policy hook never ran")
        assert seen[-1]["payload"] == {"text": "token={{$demo.TOKEN}}"}, seen[-1]
        assert SECRET_VALUE not in log.read_text()
        assert r["isError"] is False, r  # the tool itself saw the resolved value (0.5.x secrets behaviour)


# --- §21.5-1: tool access first, then the hook ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def policy_access(tmp_path_factory):
    root = tmp_path_factory.mktemp("policy-access")
    log = root / "access.jsonl"
    env = {
        "RAMEN_TEST_POLICY_LOG": str(log),
        "RAMEN_TOOL_ACCESS": json.dumps({"echo_tool": {"list": ["key", "viewer"], "call": ["viewer"]}}),
        "RAMEN_SESSION_SECRET": SECRET,
        "RAMEN_OAUTH_ISSUER": ISSUER,
        "RAMEN_PUBLIC_URL": PUBLIC,
    }
    with LocalNode(bucket=policy_copy(root, "closed"), env=env) as n:
        n.wait_serving()
        n.policy_log = log
        viewer = user_token(SECRET, ISSUER, 600, sub="u-probe", jti="j-probe", role="viewer")[0]
        n.enforced = bool(n.http_client(key=viewer).call_tool("echo_tool", {"text": BLOCKED_TEXT}).get("isError"))
        yield n


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_kind_that_may_not_call_is_denied_before_any_hook_runs(policy_access, transport):
    enforced(policy_access)
    log = policy_access.policy_log
    before = len(records(log))
    c = policy_access.for_transport(transport)  # the group key: may list echo_tool, may not call it
    c.initialize()
    with pytest.raises(JsonRpcError) as e:
        c.call_tool("echo_tool", {"text": BLOCKED_TEXT})
    assert e.value.code == -32003 and "forbidden" in e.value.message, e.value
    time.sleep(0.5)
    assert len(records(log)) == before, "tool access is decided first: the policy never saw the call"
    tok = user_token(SECRET, ISSUER, 600, sub=f"u-{transport}", jti=f"j-{transport}", role="viewer")[0]
    v = policy_access.for_transport(transport, key=tok)
    v.initialize()
    r = v.call_tool("echo_tool", {"text": BLOCKED_TEXT})
    assert r["isError"] and text_of(r).startswith("guardrail blocked: pre: "), r
    assert text_of(v.call_tool("echo_tool", {"text": "fine"})) == "fine"
    by_reason = wait_reasons(policy_access, "echo_tool", {"tool_denied", "guardrail_pre"})
    assert {"tool_denied", "guardrail_pre"} <= set(by_reason), by_reason
    assert by_reason["tool_denied"]["key_id"] != f"user:u-{transport}"
    assert by_reason["guardrail_pre"]["key_id"] == f"user:u-{transport}"
