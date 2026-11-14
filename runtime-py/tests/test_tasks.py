"""CONTRACTS §23.5: runtime.task_start / task_poll / task_update / task_cancel."""

import json
import time

import pytest
from conftest import write_pkg

from ramen_runtime.rpc import Server
from ramen_runtime.tasks import KEEP_S, Tasks

TOOL = {
    "type": "tool",
    "name": "t",
    "description": "d",
    "callable": "f",
    "input": {"a": {"type": "string"}},
    "task": True,
}
HEAD = "import threading, time\nfrom ramen_runtime import *\nGATE = threading.Event()\n"
STEPS = HEAD + (
    "def f(a):\n"
    "    progress(1)\n"
    "    r = input_responses()\n"
    "    if 'step1' not in r:\n"
    "        raise InputRequired({'step1': elicit('name?', {'properties': {'name': {}}})}, state={'n': 1})\n"
    "    if 'step2' not in r:\n"
    "        raise InputRequired({'step2': list_roots()}, state={'n': request_state()['n'] + 1})\n"
    "    return {'a': a, 'state': request_state(), 'keys': sorted(r), 'caps': client_capabilities()}\n"
)


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})

    def _make(code, extra: dict | None = None):
        write_pkg(tmp_path, "tools", "t", TOOL, code)
        for rel, text in (extra or {}).items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(text)
        return Server(tmp_path, load_on_start=True)

    return _make


def rpc(server, method, **params):
    r = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": f"runtime.{method}", "params": params}))
    return r.get("result", r.get("error"))


def wait(server, tid, *, until=("completed", "failed", "cancelled", "input_required")):
    for _ in range(400):
        r = rpc(server, "task_poll", taskId=tid)
        if r["status"] in until:
            return r
        time.sleep(0.01)
    raise AssertionError(f"task stuck: {r}")


def gate(server):
    return server.exe.reg.tools[0].func.__globals__["GATE"]


def test_task_runs_on_a_thread_and_the_result_is_polled(make):
    s = make(HEAD + "def f(a):\n    GATE.wait(5)\n    return 'done ' + a\n")
    assert rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"}) == {"status": "working"}
    assert rpc(s, "task_poll", taskId="aa") == {"status": "working"}  # start returned at once
    assert rpc(s, "ping") == {"ok": True}
    gate(s).set()
    assert wait(s, "aa") == {
        "status": "completed",
        "result": {"content": [{"type": "text", "text": "done x"}], "isError": False},
    }
    assert rpc(s, "task_poll", taskId="aa")["status"] == "completed"  # still there for a re-poll


def test_start_errors(make):
    s = make(HEAD + "def f(a):\n    return a\n")
    assert rpc(s, "task_start", taskId="aa", name="nope", arguments={})["code"] == -32004
    assert rpc(s, "task_start", taskId=5, name="t", arguments={})["code"] == -32602
    assert rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"}) == {"status": "working"}
    assert "already exists" in rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"})["message"]
    for m in ("task_poll", "task_cancel"):
        assert rpc(s, m, taskId="zz") == {"code": -32004, "message": "not found: task zz"}
    assert rpc(s, "task_update", taskId="zz", inputResponses={})["code"] == -32004
    assert rpc(Server(s.bucket), "task_start", taskId="b", name="t", arguments={})["code"] == -32002


def test_tool_error_and_invalid_arguments_complete_the_task_with_is_error(make):
    s = make(HEAD + "def f(a):\n    raise RuntimeError('boom')\n")
    rpc(s, "task_start", taskId="a1", name="t", arguments={"a": "x"})
    assert wait(s, "a1")["result"] == {"content": [{"type": "text", "text": "RuntimeError: boom"}], "isError": True}
    rpc(s, "task_start", taskId="a2", name="t", arguments={})
    r = wait(s, "a2")
    assert (
        r["status"] == "completed"
        and r["result"]["isError"]
        and "invalid arguments" in r["result"]["content"][0]["text"]
    )


def test_input_required_then_update_reruns_with_accumulated_responses_and_state(make):
    s = make(STEPS)
    rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"}, client={"capabilities": {"elicitation": {}}})
    r = wait(s, "aa")
    assert r["status"] == "input_required" and list(r["inputRequests"]) == ["step1"] and "state" not in r
    # a response for a key that is not outstanding is ignored: nothing re-runs
    assert rpc(s, "task_update", taskId="aa", inputResponses={"other": {"x": 1}}) == {"ok": True}
    assert rpc(s, "task_poll", taskId="aa")["status"] == "input_required"
    assert rpc(s, "task_update", taskId="aa", inputResponses={"step1": {"action": "accept"}}) == {"ok": True}
    s.tasks._tasks["aa"].thread.join(5)
    r = rpc(s, "task_poll", taskId="aa")
    assert r["status"] == "input_required" and list(r["inputRequests"]) == ["step2"]
    rpc(s, "task_update", taskId="aa", inputResponses={"step2": {"roots": []}})
    r = wait(s, "aa", until=("completed",))
    assert json.loads(r["result"]["content"][0]["text"]) == {
        "a": "x",
        "state": {"n": 2},
        "keys": ["step1", "step2"],
        "caps": {"elicitation": {}},
    }
    assert rpc(s, "task_update", taskId="aa", inputResponses={"step2": {}}) == {"ok": True}  # terminal: ignored
    assert rpc(s, "task_update", taskId="aa", inputResponses=[1])["code"] == -32602


def test_cancel_discards_the_result(make):
    s = make(HEAD + "def f(a):\n    GATE.wait(5)\n    return 'late'\n")
    rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"})
    assert rpc(s, "task_cancel", taskId="aa") == {"ok": True}
    assert rpc(s, "task_poll", taskId="aa") == {"status": "cancelled"}
    g = gate(s)
    thread = s.tasks._tasks["aa"].thread
    g.set()
    thread.join(5)
    assert rpc(s, "task_poll", taskId="aa") == {"status": "cancelled"}
    assert rpc(s, "task_cancel", taskId="aa") == {"ok": True}
    # cancelling a finished task changes nothing
    rpc(s, "task_start", taskId="bb", name="t", arguments={"a": "x"})
    assert wait(s, "bb")["status"] == "completed"
    rpc(s, "task_cancel", taskId="bb")
    assert rpc(s, "task_poll", taskId="bb")["status"] == "completed"


def test_missing_capability_and_internal_failure_fail_the_task_redacted(make, monkeypatch):
    s = make(HEAD + "def f(a):\n    raise MissingCapability({'sampling': {}})\n")
    rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"})
    assert wait(s, "aa") == {
        "status": "failed",
        "error": {
            "code": -32021,
            "message": "missing required client capabilities",
            "data": {"requiredCapabilities": {"sampling": {}}},
        },
    }
    monkeypatch.setenv("RAMEN_SECRET_G__K", "hunter2")
    monkeypatch.setattr(s.exe, "call_tool", lambda *a, **k: (_ for _ in ()).throw(OSError("lost hunter2")))
    rpc(s, "task_start", taskId="bb", name="t", arguments={"a": "{{$g.k}}", "b": "{{$g.unset}}"})
    assert wait(s, "bb") == {"status": "failed", "error": {"code": -32603, "message": "OSError: lost ***"}}


def test_guardrails_block_completes_the_task_and_run_before_every_rerun(make):
    policy = (
        "SEEN = []\n"
        "def pre(tool, arguments):\n"
        "    SEEN.append(arguments)\n"
        "    return 'no' if arguments['a'] == 'bad' else None\n"
        "def post(tool, arguments, result):\n"
        "    SEEN.append('post')\n"
    )
    s = make(
        STEPS,
        {
            "mcp/guardrails.yaml": "engine: policy\ntools:\n  t: {pre: true, post: true}\n",
            "mcp/guardrails/policy.py": policy,
        },
    )
    seen = s.exe.guard.engine._pre.__globals__["SEEN"]
    rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "bad"})
    r = wait(s, "aa")
    assert (
        r["status"] == "completed"
        and r["result"]["isError"]
        and r["result"]["_meta"]["ramen"]["guardrail"]["stage"] == "pre"
    )
    del seen[:]
    rpc(s, "task_start", taskId="bb", name="t", arguments={"a": "x"})
    wait(s, "bb")
    rpc(
        s, "task_update", taskId="bb", inputResponses={"step1": {"v": 1}, "step2": {"early": 1}}
    )  # step2: not asked yet
    s.tasks._tasks["bb"].thread.join(5)
    assert list(rpc(s, "task_poll", taskId="bb")["inputRequests"]) == ["step2"]
    rpc(s, "task_update", taskId="bb", inputResponses={"step2": {"v": 2}})
    assert wait(s, "bb", until=("completed",))["result"]["isError"] is False
    one = {"step1": {"v": 1}}
    assert seen == [
        {"a": "x"},
        {"a": "x", "$inputResponses": one},
        {"a": "x", "$inputResponses": one | {"step2": {"v": 2}}},
        "post",
    ]


def test_reload_leaves_running_tasks_on_the_old_code(make, tmp_path):
    s = make(HEAD + "def f(a):\n    GATE.wait(5)\n    return 'v1'\n")
    rpc(s, "task_start", taskId="aa", name="t", arguments={"a": "x"})
    old = gate(s)
    write_pkg(tmp_path, "tools", "t", TOOL, HEAD + "def f(a):\n    return 'v2'\n")
    assert rpc(s, "load")["errors"] == []
    assert rpc(s, "task_poll", taskId="aa") == {"status": "working"}
    old.set()
    assert wait(s, "aa")["result"]["content"][0]["text"] == "v1"
    rpc(s, "task_start", taskId="bb", name="t", arguments={"a": "x"})
    assert wait(s, "bb")["result"]["content"][0]["text"] == "v2"


def test_results_are_kept_until_polled_then_for_a_bounded_time():
    now = [0.0]
    tasks = Tasks(ttl_s=100, clock=lambda: now[0])
    tasks.start("a", lambda responses, state: {"content": [], "isError": False}, str)
    tasks.start("b", lambda responses, state: {"resultType": "input_required", "inputRequests": {"k": {}}}, str)
    for t in list(tasks._tasks.values()):
        t.thread.join(5)
    now[0] = 90.0  # unpolled: kept
    assert tasks.poll("a")["status"] == "completed"
    now[0] = 90.0 + KEEP_S - 1
    assert tasks.poll("a")["status"] == "completed" and tasks.poll("b")["status"] == "input_required"
    now[0] = 90.0 + KEEP_S + 1
    with pytest.raises(KeyError):
        tasks.poll("a")
    assert tasks.poll("b")["status"] == "input_required"  # not terminal, inside ttl + keep
    now[0] = 100 + KEEP_S + 1  # a task nobody finished or polled goes after ttl + keep
    with pytest.raises(KeyError):
        tasks.poll("b")
