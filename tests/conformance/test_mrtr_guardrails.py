"""CONTRACTS §23.4 × §21.2 (v1.0.0): guardrails around a multi-round call, on real nodes and both transports.

`mrtr_group` guards `ask_confirm` with the group's own `policy.py`, which records every payload it is shown. The
contract: **pre runs on every round** — the first call and each retry, where the engine sees the arguments plus
`"$inputResponses"` — before the tool body runs again; **post runs on the final (`complete`) result only**; a blocked
round is the usual `isError` result with `_meta.ramen.guardrail`; and fail-closed still holds on a retry."""

import json
import shutil
import time
from pathlib import Path

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import ALL_INPUT, Modern, assert_envelope

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
IGNORE = shutil.ignore_patterns("__pycache__")
GUARD = {"stage": "pre", "engine": "policy"}


def answer(ok=True, **content) -> dict:
    return {"confirm": {"action": "accept", "content": {"ok": ok, **content}}}


def records(log: Path, tool: str = "ask_confirm") -> list[dict]:
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["tool"] == tool]


def stages(log: Path, tool: str = "ask_confirm") -> list[str]:
    return [r["stage"] for r in records(log, tool)]


def guardrail(result: dict) -> dict | None:
    return ((result.get("_meta") or {}).get("ramen") or {}).get("guardrail")


@pytest.fixture(params=TRANSPORTS, ids=TRANSPORTS)
def guarded(request, tmp_path):
    """A node per test: the policy log then holds exactly this test's hook runs."""
    log = tmp_path / "policy.jsonl"
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", env={"RAMEN_TEST_POLICY_LOG": str(log)}) as n:
        n.wait_serving()
        n.policy_log = log
        n.m = Modern(n.for_transport(request.param), ALL_INPUT)
        n.transport = request.param
        yield n


def first_round(node, item: str) -> str:
    first = node.m.call("ask_confirm", {"item": item})
    assert first["resultType"] == "input_required", first
    return first["requestState"]


def test_pre_fires_on_the_first_call_and_post_does_not(guarded):
    first_round(guarded, "note-1")
    rows = records(guarded.policy_log)
    assert [r["stage"] for r in rows] == ["pre"], rows
    assert rows[0]["payload"] == {"item": "note-1"}, "no $inputResponses on the first round"


def test_pre_fires_again_on_the_resumed_call_and_sees_the_answers(guarded):
    state = first_round(guarded, "note-1")
    done = guarded.m.call(
        "ask_confirm", {"item": "note-1"}, inputResponses=answer(why="spring cleaning"), requestState=state
    )
    assert text_of(done) == "deleted note-1" and guardrail(done) is None, done
    rows = records(guarded.policy_log)
    assert [r["stage"] for r in rows] == ["pre", "pre", "post"], rows
    assert rows[1]["payload"] == {"item": "note-1", "$inputResponses": answer(why="spring cleaning")}, rows[1]
    assert "deleted note-1" in json.dumps(rows[2]["payload"]), "post judges the final result"


def test_pre_can_block_the_first_call(guarded):
    r = guarded.m.call("ask_confirm", {"item": "drop table notes"})
    assert_envelope(r)
    assert r["isError"] is True and text_of(r) == "guardrail blocked: pre: arguments mention a blocked statement", r
    assert guardrail(r) == GUARD and "inputRequests" not in r, "a blocked call never asks the user anything"
    assert stages(guarded.policy_log) == ["pre"]


def test_pre_can_block_a_resume_by_inspecting_the_input_responses(guarded):
    state = first_round(guarded, "note-1")
    r = guarded.m.call(
        "ask_confirm", {"item": "note-1"}, inputResponses=answer(why="said yes under duress"), requestState=state
    )
    assert r["resultType"] == "complete" and r["isError"] is True, r
    assert text_of(r) == "guardrail blocked: pre: the answer may not be used", r
    assert guardrail(r) == GUARD
    assert "deleted" not in json.dumps(r), "the tool body did not run again"
    assert stages(guarded.policy_log) == ["pre", "pre"], "no post after a blocked pre"
    retry = guarded.m.call("ask_confirm", {"item": "note-1"}, inputResponses=answer(), requestState=state)
    assert text_of(retry) == "deleted note-1", "the block is per round: a clean answer with the same state goes through"


def test_post_fires_only_on_the_final_result(guarded):
    """The interim result of this call mentions the word post blocks ("Delete classified?"). Post must not see it."""
    state = first_round(guarded, "classified")
    assert stages(guarded.policy_log) == ["pre"], "post never judges an input_required round"
    r = guarded.m.call("ask_confirm", {"item": "classified"}, inputResponses=answer(), requestState=state)
    assert r["isError"] is True and text_of(r) == "guardrail blocked: post: output mentions classified material", r
    assert guardrail(r) == {"stage": "post", "engine": "policy"}
    assert "deleted classified" not in json.dumps(r), "a post block never returns the tool's output"
    assert stages(guarded.policy_log) == ["pre", "pre", "post"]


def test_fail_closed_holds_on_a_resume(guarded):
    state = first_round(guarded, "note-1")
    r = guarded.m.call("ask_confirm", {"item": "note-1"}, inputResponses=answer(why="boom"), requestState=state)
    assert r["isError"] is True and text_of(r).startswith("guardrail unavailable: "), r
    assert guardrail(r) == GUARD and "deleted" not in json.dumps(r)
    assert stages(guarded.policy_log) == ["pre", "pre"]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_fail_open_lets_the_resume_through_and_logs_it(tmp_path, transport):
    bucket, log = tmp_path / "open", tmp_path / "policy.jsonl"
    shutil.copytree(E.FIXTURES / "mrtr_group", bucket, ignore=IGNORE)
    y = bucket / "mcp" / "guardrails.yaml"
    y.write_text(y.read_text().replace("fail: closed", "fail: open"))
    with LocalNode(bucket=bucket, env={"RAMEN_TEST_POLICY_LOG": str(log)}) as n:
        n.wait_serving()
        m = Modern(n.for_transport(transport), ALL_INPUT)
        state = m.call("ask_confirm", {"item": "note-1"})["requestState"]
        r = m.call("ask_confirm", {"item": "note-1"}, inputResponses=answer(why="boom"), requestState=state)
        assert text_of(r) == "deleted note-1" and guardrail(r) is None, r
        assert stages(log) == ["pre", "pre", "post"]
        assert "guardrail_error" in n.output()


def test_a_tool_not_listed_in_guardrails_yaml_is_never_checked_on_any_round(guarded):
    first = guarded.m.call("ask_name")
    assert first["resultType"] == "input_required"
    done = guarded.m.call("ask_name", inputResponses={"who": {"action": "accept", "content": {"name": "drop table"}}})
    assert text_of(done) == "Hello, drop table!"
    assert records(guarded.policy_log, "ask_name") == [] and records(guarded.policy_log) == []


def test_a_refused_state_never_reaches_the_hook(guarded):
    """The node verifies `requestState` before the runtime is called: a forged retry runs no pre hook."""
    state = first_round(guarded, "note-1")
    r = guarded.m.call_raw("ask_confirm", {"item": "note-1"}, inputResponses=answer(), requestState=state + "x")
    assert r.code == -32602, r.message
    assert stages(guarded.policy_log) == ["pre"], "only the first round's pre"


def test_blocked_rounds_are_logged_with_the_guardrail_reason(guarded):
    state = first_round(guarded, "note-1")
    guarded.m.call("ask_confirm", {"item": "note-1"}, inputResponses=answer(why="under duress"), requestState=state)
    state = first_round(guarded, "classified")
    guarded.m.call("ask_confirm", {"item": "classified"}, inputResponses=answer(), requestState=state)
    deadline, reasons = time.monotonic() + 5, set()
    while time.monotonic() < deadline and not {"guardrail_pre", "guardrail_post"} <= reasons:
        calls = [x for x in guarded.log_lines() if x.get("method") == "tools/call" and x.get("name") == "ask_confirm"]
        reasons = {x["reason"] for x in calls if x.get("reason") and x.get("transport") == guarded.transport}
        time.sleep(0.2)
    assert {"guardrail_pre", "guardrail_post"} <= reasons, reasons
    assert "under duress" not in guarded.log.read_text(), "the payload is never logged"


def test_a_legacy_call_runs_pre_once_and_no_post(guarded):
    """Legacy era: the input_required outcome becomes a tool error; pre ran, the tool asked, nothing was final."""
    c = guarded.for_transport(guarded.transport)
    c.initialize()
    r = c.call_tool("ask_confirm", {"item": "note-1"})
    assert r["isError"] is True and "needs more input (confirm)" in text_of(r), r
    assert stages(guarded.policy_log) == ["pre"]
    blocked = c.call_tool("ask_confirm", {"item": "drop table"})
    assert (
        text_of(blocked) == "guardrail blocked: pre: arguments mention a blocked statement"
        and guardrail(blocked) == GUARD
    )
