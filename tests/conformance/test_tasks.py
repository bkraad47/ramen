"""CONTRACTS §23.5 (v1.0.0): the tasks extension (`io.modelcontextprotocol/tasks`), modern era only, real nodes.

A tool with `"task": true` called by a client that declared the extension on that request returns a task at once;
`tasks/get` reads it, `tasks/update` answers its input requests, `tasks/cancel` stops it. Anyone else gets the
synchronous call. A task belongs to the caller that started it. Guardrails wrap it: pre before the start and before
every re-run, post on the final result. Fixture: `fixtures/mrtr_group` (`slow_job`, `confirm_job`, `fail_job`)."""

import json
import os
import re
import secrets
import time
from datetime import datetime

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import ALL_INPUT, TASKS, WITH_TASKS, Modern, assert_envelope

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
CAPS = {**ALL_INPUT, **WITH_TASKS}
TASK_FIELDS = {"taskId", "status", "createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs"}
KEY_A, KEY_B = ("rmk_" + secrets.token_hex(12) for _ in range(2))
YES = {"confirm": {"action": "accept", "content": {"ok": True}}}
NEEDS_TASKS = {"requiredCapabilities": {"extensions": {TASKS: {}}}}


def start(tmp=None, **env) -> LocalNode:
    extra = {"RAMEN_TEST_POLICY_LOG": str(tmp)} if tmp else {}
    return LocalNode(
        bucket=E.FIXTURES / "mrtr_group", key=KEY_A, env={"RAMEN_MCP_KEYS": f"{KEY_A},{KEY_B}", **extra, **env}
    )


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    log = tmp_path_factory.mktemp("policy") / "policy.jsonl"
    with start(log) as n:
        n.wait_serving()
        n.policy_log = log
        yield n


@pytest.fixture(params=TRANSPORTS, ids=TRANSPORTS)
def t(request, node) -> Modern:
    """A client that declares the tasks extension (and every input method) on each request."""
    return Modern(node.for_transport(request.param), CAPS)


def unknown_task(reply) -> bool:
    return reply.code == -32602 and reply.error["message"] == "Unknown task" and reply.status in (None, 200)


def hooks(node, label: str) -> list[str]:
    if not node.policy_log.exists():
        return []
    rows = [json.loads(x) for x in node.policy_log.read_text().splitlines() if x.strip()]
    return [r["stage"] for r in rows if label in json.dumps(r["payload"])]


def iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- who gets a task --------------------------------------------------------------------------------------------------
def test_a_declaring_client_gets_a_task_at_once(t):
    began = time.monotonic()
    created = t.call("slow_job", {"seconds": 2, "label": "lifecycle"})
    assert time.monotonic() - began < 1.5, "the call returns before the work is done"
    assert_envelope(created, "task")
    assert set(created) == {"resultType", "_meta", *TASK_FIELDS}, sorted(created)
    assert re.fullmatch(r"[0-9a-f]{32}", created["taskId"]), "128 random bits, hex"
    assert created["status"] == "working"
    assert (created["ttlMs"], created["pollIntervalMs"]) == (3600_000, 2000)
    assert abs((iso(created["createdAt"]) - datetime.now(iso(created["createdAt"]).tzinfo)).total_seconds()) < 30
    assert iso(created["lastUpdatedAt"]) >= iso(created["createdAt"])
    assert "content" not in created and "requestState" not in created and "task" not in created

    now = t.task_get(created["taskId"])  # readable immediately
    assert now["resultType"] == "complete" and now["taskId"] == created["taskId"], now
    assert now["status"] == "working" and "result" not in now and "error" not in now, now

    done = t.wait_task(created["taskId"])
    assert_envelope(done)
    assert done["status"] == "completed" and TASK_FIELDS <= set(done), done
    assert text_of(done["result"]) == "job lifecycle done" and done["result"].get("isError") is not True, done
    assert "requestState" not in done and "io.modelcontextprotocol/related-task" not in (
        done["result"].get("_meta") or {}
    )
    assert iso(done["lastUpdatedAt"]) > iso(created["createdAt"]) and done["createdAt"] == created["createdAt"]
    assert t.task_get(created["taskId"])["status"] == "completed", "a finished task stays readable for its TTL"


def test_no_declaration_means_the_synchronous_call(t, node):
    plain = t.with_caps(ALL_INPUT).call("slow_job", {"seconds": 0, "label": "sync"})
    assert plain["resultType"] == "complete" and text_of(plain) == "job sync done" and "taskId" not in plain, plain
    legacy = node.for_transport(t.transport)
    legacy.initialize()
    old = legacy.call_tool("slow_job", {"seconds": 0, "label": "legacy"})
    assert text_of(old) == "job legacy done" and "taskId" not in old and "resultType" not in old, old


def test_a_tool_without_task_true_never_becomes_a_task(t):
    r = t.call("plain_tool", {"text": "hi"}, task={"ttl": 60000, "pollInterval": 1000})  # the 2025-11 hint is ignored
    assert r["resultType"] == "complete" and text_of(r) == "hi" and "taskId" not in r, r


def test_task_methods_without_the_extension_are_32021(t):
    created = t.call("slow_job", {"seconds": 1, "label": "gate"})
    bare = t.with_caps(ALL_INPUT)
    for method, params in (
        ("tasks/get", {"taskId": created["taskId"]}),
        ("tasks/update", {"taskId": created["taskId"], "inputResponses": {}}),
        ("tasks/cancel", {"taskId": created["taskId"]}),
    ):
        r = bare.raw(method, params)
        assert r.code == -32021 and r.status in (None, 400), (method, r.message)
        assert r.error["data"] == NEEDS_TASKS, r.error
    assert t.wait_task(created["taskId"])["status"] == "completed", "the refused cancel did not cancel it"


def test_tasks_result_and_tasks_list_do_not_exist(t):
    for method in ("tasks/result", "tasks/list"):
        r = t.raw(method, {"taskId": "0" * 32})
        assert r.code == -32601 and r.status in (None, 404), (method, r.message)


# --- outcomes ---------------------------------------------------------------------------------------------------------
def test_a_tool_failure_completes_the_task_with_an_error_result(t):
    created = t.call("fail_job")
    done = t.wait_task(created["taskId"])
    assert done["status"] == "completed", "`failed` is for protocol errors, never for isError results"
    assert done["result"]["isError"] is True and "the job failed" in text_of(done["result"]), done
    assert "error" not in done


def test_input_required_inside_a_task(t):
    created = t.call("confirm_job", {"item": "task-note"})
    assert created["resultType"] == "task", "the task is created first; the question is asked through tasks/get"
    waiting = t.wait_task(created["taskId"], until=("input_required", "completed", "failed"))
    assert waiting["status"] == "input_required", waiting
    request = waiting["inputRequests"]["confirm"]
    assert request["method"] == "elicitation/create" and request["params"]["message"] == "Delete task-note?", request
    assert "result" not in waiting and "requestState" not in waiting

    ack = t.request("tasks/update", {"taskId": created["taskId"], "inputResponses": YES})
    assert ack["resultType"] == "complete" and set(ack) <= {"resultType", "_meta"}, ack
    done = t.wait_task(created["taskId"])
    assert done["status"] == "completed" and text_of(done["result"]) == "deleted task-note", done
    assert "inputRequests" not in done


def test_an_update_that_answers_nothing_keeps_the_task_waiting(t):
    created = t.call("confirm_job", {"item": "still-waiting"})
    t.wait_task(created["taskId"], until=("input_required",))
    t.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {"other": {"action": "accept"}}})
    time.sleep(1.2)
    again = t.wait_task(created["taskId"], until=("input_required", "completed"))
    assert again["status"] == "input_required" and "confirm" in again["inputRequests"], again
    bad = t.raw("tasks/update", {"taskId": created["taskId"], "inputResponses": "yes"})
    assert bad.code == -32602, bad.message
    t.request("tasks/cancel", {"taskId": created["taskId"]})


def test_cancel(t):
    created = t.call("slow_job", {"seconds": 20, "label": "cancel-me"})
    ack = t.request("tasks/cancel", {"taskId": created["taskId"]})
    assert ack["resultType"] == "complete" and set(ack) <= {"resultType", "_meta"}, ack
    gone = t.wait_task(created["taskId"], until=("cancelled", "completed", "failed"), timeout=10)
    assert gone["status"] == "cancelled" and "result" not in gone and "error" not in gone, gone
    again = t.request("tasks/cancel", {"taskId": created["taskId"]})
    assert again["resultType"] == "complete", "cancelling a terminal task is not an error"
    assert t.task_get(created["taskId"])["status"] == "cancelled", "and it stays cancelled"
    finished = t.call("slow_job", {"seconds": 0, "label": "done-first"})
    t.wait_task(finished["taskId"])
    assert t.request("tasks/cancel", {"taskId": finished["taskId"]})["resultType"] == "complete"
    assert t.task_get(finished["taskId"])["status"] == "completed", (
        "a completed task is not turned into a cancelled one"
    )


# --- ownership --------------------------------------------------------------------------------------------------------
def test_another_caller_cannot_read_update_or_cancel_a_task(t):
    created = t.call("confirm_job", {"item": "private"})
    t.wait_task(created["taskId"], until=("input_required",))
    other = t.with_key(KEY_B)
    assert unknown_task(other.raw("tasks/get", {"taskId": created["taskId"]}))
    assert unknown_task(other.raw("tasks/update", {"taskId": created["taskId"], "inputResponses": YES}))
    assert unknown_task(other.raw("tasks/cancel", {"taskId": created["taskId"]}))
    mine = t.task_get(created["taskId"])
    assert mine["status"] == "input_required", "the stranger's update and cancel changed nothing"
    t.request("tasks/cancel", {"taskId": created["taskId"]})


@pytest.mark.parametrize("task_id", ["0" * 32, "nope", "", "../../etc/passwd"])
def test_an_unknown_task_id_is_32602(t, task_id):
    for method in ("tasks/get", "tasks/cancel"):
        assert unknown_task(t.raw(method, {"taskId": task_id})), (method, task_id)


def test_a_task_started_over_one_transport_is_readable_over_the_other(node):
    """The binding is the caller (§16.2), not the connection."""
    created = Modern(node.http, CAPS).call("slow_job", {"seconds": 0, "label": "either-way"})
    done = Modern(node.node, CAPS).wait_task(created["taskId"])
    assert text_of(done["result"]) == "job either-way done"


# --- guardrails around a task -----------------------------------------------------------------------------------------
def test_pre_runs_before_the_start_and_post_on_the_final_result(t, node):
    label = "guard-" + secrets.token_hex(4)
    created = t.call("slow_job", {"seconds": 0, "label": label})
    done = t.wait_task(created["taskId"])
    assert text_of(done["result"]) == f"job {label} done"
    assert hooks(node, label) == ["pre", "post"], hooks(node, label)


def test_a_pre_block_completes_the_task_with_the_guardrail_result(t, node):
    label = "drop table " + secrets.token_hex(4)
    created = t.call("slow_job", {"seconds": 5, "label": label})
    assert created["resultType"] == "task", created
    done = t.wait_task(created["taskId"], timeout=4)
    assert done["status"] == "completed" and done["result"]["isError"] is True, done
    assert text_of(done["result"]) == "guardrail blocked: pre: arguments mention a blocked statement"
    assert done["result"]["_meta"]["ramen"]["guardrail"] == {"stage": "pre", "engine": "policy"}
    assert hooks(node, label) == ["pre"], "blocked before the tool ran: no post"


def test_a_post_block_replaces_the_task_result(t):
    created = t.call("slow_job", {"seconds": 0, "label": "classified"})
    done = t.wait_task(created["taskId"])
    assert done["status"] == "completed" and done["result"]["isError"] is True, done
    assert text_of(done["result"]) == "guardrail blocked: post: output mentions classified material"
    assert "job classified done" not in json.dumps(done)


def test_pre_runs_again_before_the_re_run_after_an_update(t, node):
    item = "rerun-" + secrets.token_hex(4)
    created = t.call("confirm_job", {"item": item})
    t.wait_task(created["taskId"], until=("input_required",))
    assert hooks(node, item) == ["pre"], "post does not judge the question"
    duress = {"confirm": {"action": "accept", "content": {"ok": True, "why": "under duress"}}}
    t.request("tasks/update", {"taskId": created["taskId"], "inputResponses": duress})
    done = t.wait_task(created["taskId"])
    assert done["result"]["isError"] is True, done
    assert text_of(done["result"]) == "guardrail blocked: pre: the answer may not be used"
    assert hooks(node, item) == ["pre", "pre"]

    item = "rerun-ok-" + secrets.token_hex(4)
    created = t.call("confirm_job", {"item": item})
    t.wait_task(created["taskId"], until=("input_required",))
    t.request("tasks/update", {"taskId": created["taskId"], "inputResponses": YES})
    assert text_of(t.wait_task(created["taskId"])["result"]) == f"deleted {item}"
    assert hooks(node, item) == ["pre", "pre", "post"]


# --- limits, expiry, bookkeeping --------------------------------------------------------------------------------------
def test_a_task_expires_after_its_ttl():
    with start(RAMEN_TASK_TTL_SECS="2") as n:
        n.wait_serving()
        t = Modern(n.http, CAPS)
        created = t.call("slow_job", {"seconds": 0, "label": "short-lived"})
        assert created["ttlMs"] == 2000
        assert t.wait_task(created["taskId"], timeout=1.8)["status"] == "completed"
        time.sleep(3.5)
        assert unknown_task(t.raw("tasks/get", {"taskId": created["taskId"]})), "expired = unknown"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_running_tasks_are_capped_per_pod(transport):
    with start(RAMEN_MAX_TASKS="1") as n:
        n.wait_serving()
        t = Modern(n.for_transport(transport), CAPS)
        first = t.call("slow_job", {"seconds": 20, "label": "holds-the-slot"})
        if transport == "http":
            assert t.call_raw("slow_job", {"seconds": 1, "label": "refused"}).status == 429
        else:
            with pytest.raises(grpc.RpcError) as e:
                t.call_raw("slow_job", {"seconds": 1, "label": "refused"})
            assert e.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert n.node.admin_metrics()["tasks"] == 1
        sync = t.with_caps(ALL_INPUT).call("slow_job", {"seconds": 0, "label": "sync"})
        assert text_of(sync) == "job sync done", "the cap is on tasks; a synchronous call is not a task"
        t.request("tasks/cancel", {"taskId": first["taskId"]})
        t.wait_task(first["taskId"], until=("cancelled",), timeout=10)
        deadline = time.monotonic() + 10
        while n.node.admin_metrics()["tasks"] and time.monotonic() < deadline:
            time.sleep(0.3)
        again = t.call("slow_job", {"seconds": 0, "label": "after"})
        assert again["resultType"] == "task", "the slot came back"


def test_task_lines_in_the_access_log_name_the_tool_and_the_task(node):
    t = Modern(node.http, CAPS)
    created = t.call("slow_job", {"seconds": 0, "label": "logged"})
    t.wait_task(created["taskId"])
    t.request("tasks/cancel", {"taskId": created["taskId"]})
    deadline, lines = time.monotonic() + 5, []
    while time.monotonic() < deadline:
        lines = [x for x in node.log_lines() if x.get("msg") == "mcp" and x.get("task") == created["taskId"]]
        if {"tasks/get", "tasks/cancel"} <= {x.get("method") for x in lines}:
            break
        time.sleep(0.2)
    assert {"tasks/get", "tasks/cancel"} <= {x.get("method") for x in lines}, lines
    assert all(x["name"] == "slow_job" for x in lines if x["method"].startswith("tasks/")), lines
    assert KEY_A not in node.log.read_text()


# --- two pods, one store ----------------------------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("RAMEN_TEST_REDIS_URL"), reason="needs a real Redis: set RAMEN_TEST_REDIS_URL")
def test_another_pod_reads_updates_and_cancels_a_task_through_the_shared_store():
    """§23.5: the owner writes the record to Redis; any pod of the zone answers `tasks/get`, and an update or a
    cancel that reaches another pod is queued there for the owner."""
    redis = {"RAMEN_REDIS_SCOPE_URL": os.environ["RAMEN_TEST_REDIS_URL"]}
    with start(**redis) as a, start(**redis) as b:
        a.wait_serving()
        b.wait_serving()
        owner, other = Modern(a.http, CAPS), Modern(b.http, CAPS)
        created = owner.call("slow_job", {"seconds": 1, "label": "two-pods"})
        done = other.wait_task(created["taskId"])
        assert done["status"] == "completed" and "two-pods" in text_of(done["result"]), done
        # another caller on the other pod still cannot see it
        assert unknown_task(other.with_key(KEY_B).raw("tasks/get", {"taskId": created["taskId"]}))
        asking = owner.call("confirm_job", {"item": "two-pods"})
        parked = other.wait_task(asking["taskId"], until=("input_required",))
        assert set(parked["inputRequests"]) == {"confirm"}, parked
        other.request("tasks/update", {"taskId": asking["taskId"], "inputResponses": YES})
        assert other.wait_task(asking["taskId"])["status"] == "completed"
        long = owner.call("slow_job", {"seconds": 20, "label": "cancel-me"})
        other.request("tasks/cancel", {"taskId": long["taskId"]})
        assert other.wait_task(long["taskId"], timeout=15)["status"] == "cancelled"
