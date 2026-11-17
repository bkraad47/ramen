import io
import json
import subprocess
import sys
import time

import pytest

from ramen_runtime.rpc import Server


def run(server, *msgs):
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs) + "\n{bad json\n"), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_full_session(demo_bucket, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    s = Server(demo_bucket)
    res = run(
        s,
        {"jsonrpc": "2.0", "id": 1, "method": "runtime.ping", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "runtime.call_tool",
            "params": {"name": "demo_calculator_tool", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "runtime.load", "params": {"bucket": str(demo_bucket)}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "runtime.call_tool",
            "params": {"name": "demo_calculator_tool", "arguments": {"var1": 6, "var2": 7, "func": "multiply"}},
        },
        {"jsonrpc": "2.0", "id": 5, "method": "runtime.read_resource", "params": {"uri": "ramen://demo/readme"}},
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "runtime.get_prompt",
            "params": {"name": "get_calculation_prompt", "arguments": {"request": "1+1"}},
        },
        {"jsonrpc": "2.0", "id": 7, "method": "runtime.call_tool", "params": {"name": "nope", "arguments": {}}},
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "runtime.get_prompt",
            "params": {"name": "get_calculation_prompt", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 9, "method": "nope"},
        {"jsonrpc": "2.0", "id": 10, "method": "runtime.call_tool", "params": {"name": 1}},
        {"jsonrpc": "2.0", "method": "runtime.ping"},
        {"jsonrpc": "2.0", "id": 11, "method": "runtime.shutdown", "params": {}},
        {"jsonrpc": "2.0", "id": 12, "method": "runtime.ping", "params": {}},
    )
    by = {r["id"]: r for r in res if "id" in r and r["id"] is not None}
    assert by[1]["result"] == {"ok": True}
    assert by[2]["error"]["code"] == -32002
    assert by[3]["result"]["tools"][0]["name"] == "demo_calculator_tool" and by[3]["result"]["errors"] == []
    assert len(by[3]["result"]["hash"]) == 64  # C1: manifest hash travels with the load result
    assert by[4]["result"]["content"][0]["text"] == "42"
    assert by[5]["result"]["contents"][0]["mimeType"] == "text/markdown"
    assert "1+1" in by[6]["result"]["messages"][0]["content"]["text"]
    assert by[7]["error"]["code"] == -32004
    assert by[8]["error"]["code"] == -32602
    assert by[9]["error"]["code"] == -32601
    assert by[10]["error"]["code"] == -32602
    assert by[11]["result"] == {"ok": True}
    assert 12 not in by
    nulls = [r for r in res if r.get("id") is None]
    assert nulls == [] or all(r["error"]["code"] == -32700 for r in nulls)


def test_parse_error_and_internal(demo_bucket, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: (_ for _ in ()).throw(RuntimeError("pip broke")))
    s = Server(demo_bucket)
    res = run(s, {"jsonrpc": "2.0", "id": 1, "method": "runtime.load", "params": {}}, {"jsonrpc": "2.0", "id": 2})
    by = {r.get("id"): r for r in res}
    assert by[1]["error"]["code"] == -32603 and "pip broke" in by[1]["error"]["message"]
    assert by[2]["error"]["code"] == -32600
    assert by[None]["error"]["code"] == -32700


def test_load_at_start_when_flag(demo_bucket, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    s = Server(demo_bucket, load_on_start=True)
    res = run(
        s,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "runtime.call_tool",
            "params": {"name": "demo_calculator_tool", "arguments": {"var1": 1, "var2": 1, "func": "add"}},
        },
    )
    # the reply by id: the thread pool (D83) may write a notification line before it (flaky 2 of 3 by position)
    reply = next(r for r in res if r.get("id") == 1)
    assert reply["result"]["content"][0]["text"] == "2"


@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
def test_main_subprocess(demo_bucket):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "runtime.load", "params": {"bucket": str(demo_bucket)}},
        {"jsonrpc": "2.0", "id": 2, "method": "runtime.shutdown", "params": {}},
    ]
    p = subprocess.run(
        [sys.executable, "-m", "ramen_runtime", "--bucket", str(demo_bucket)],
        input="".join(json.dumps(m) + "\n" for m in msgs),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr
    lines = [json.loads(x) for x in p.stdout.splitlines()]
    assert lines[0]["result"]["tools"][0]["name"] == "demo_calculator_tool"
    assert lines[1]["result"] == {"ok": True}
    logs = [json.loads(x) for x in p.stderr.splitlines()]
    assert all("ts" in x and "level" in x and "msg" in x for x in logs)
    assert any(x["msg"] == "loaded" for x in logs)


def test_main_bad_bucket_arg():
    p = subprocess.run([sys.executable, "-m", "ramen_runtime"], capture_output=True, text=True)
    assert p.returncode == 2


def test_main_inprocess(demo_bucket, monkeypatch, capsys):
    from ramen_runtime.__main__ import main

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "runtime.ping", "params": {}}) + "\n")
    )
    assert main(["--bucket", str(demo_bucket), "--load"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"ok": True}


# ---- request concurrency (D83) --------------------------------------------------------------------------------------
SLOW_PROTO = {
    "type": "tool",
    "name": "slow",
    "description": "d",
    "callable": "f",
    "input": {"tag": {"type": "string"}, "ms": {"type": "number"}},
}
SLOW = (
    "import threading, time\n"
    "from ramen_runtime import progress\n"
    "L, ACTIVE, PEAK, SPAN = threading.Lock(), [0], [0], {}\n"
    "def f(tag, ms):\n"
    "    with L:\n"
    "        ACTIVE[0] += 1\n"
    "        PEAK[0] = max(PEAK[0], ACTIVE[0])\n"
    "    t0 = time.monotonic()\n"
    "    progress(1, 2, tag)\n"
    "    time.sleep(ms / 1000)\n"
    "    progress(2, 2, tag)\n"
    "    with L:\n"
    "        ACTIVE[0] -= 1\n"
    "    SPAN[tag] = (t0, time.monotonic())\n"
    "    if tag == 'boom':\n"
    "        raise ValueError('bad tag')\n"
    "    return tag\n"
)


def slow(rid, tag, ms, **extra):
    p = {"name": "slow", "arguments": {"tag": tag, "ms": ms}} | extra
    return {"jsonrpc": "2.0", "id": rid, "method": "runtime.call_tool", "params": p}


@pytest.fixture
def slow_server(demo_bucket, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    from conftest import write_pkg

    write_pkg(demo_bucket, "tools", "slow", SLOW_PROTO, SLOW)
    return lambda: Server(demo_bucket, load_on_start=True)


def tool_globals(s: Server) -> dict:
    return s.exe.tool("slow").func.__globals__


def test_concurrent_calls_overlap_and_answers_keep_their_ids(slow_server):
    s = slow_server()
    assert s.concurrency == 4  # RAMEN_RUNTIME_CONCURRENCY default
    t0 = time.monotonic()
    out = run(s, *(slow(i, f"t{i}", 300) for i in range(4)))
    assert time.monotonic() - t0 < 0.9, "four 300 ms calls run together, not one after the other"
    by = {r["id"]: r for r in out if r.get("id") is not None}
    assert {i: by[i]["result"]["content"][0]["text"] for i in range(4)} == {i: f"t{i}" for i in range(4)}
    assert tool_globals(s)["PEAK"][0] == 4


def test_the_concurrency_limit_is_honoured_and_a_bad_value_means_one(slow_server, monkeypatch):
    monkeypatch.setenv("RAMEN_RUNTIME_CONCURRENCY", "2")
    s = slow_server()
    assert s.concurrency == 2
    out = run(s, *(slow(i, f"t{i}", 150) for i in range(5)))
    assert len([r for r in out if r.get("id") is not None]) == 5
    assert tool_globals(s)["PEAK"][0] == 2, "the third call waited for a free worker"
    for bad in ("0", "-3", "many"):
        monkeypatch.setenv("RAMEN_RUNTIME_CONCURRENCY", bad)
        assert slow_server().concurrency == 1 if bad != "many" else slow_server().concurrency == 4


def test_a_reload_waits_for_in_flight_calls_and_blocks_new_ones(slow_server, monkeypatch):
    s = slow_server()
    real, when = s.load, {}

    def load(params):
        when["start"] = time.monotonic()
        r = real(params)
        when["end"] = time.monotonic()
        return r

    monkeypatch.setattr(s, "load", load)
    g = tool_globals(s)  # the module object survives the reload (sys.modules), its SPAN with it
    out = run(
        s,
        slow(1, "before", 300),
        {"jsonrpc": "2.0", "id": 2, "method": "runtime.load", "params": {}},
        slow(3, "after", 10),
    )
    assert [r["id"] for r in out if r.get("id") is not None] == [1, 2, 3]
    span = g["SPAN"] if "before" in g["SPAN"] else tool_globals(s)["SPAN"]
    assert span["before"][1] <= when["start"], "the load waited for the running call"
    after = tool_globals(s)["SPAN"]["after"]
    assert when["end"] <= after[0], "the call behind the load ran on the reloaded runtime"


def test_a_tool_raising_on_one_thread_does_not_touch_another(slow_server, capsys):
    s = slow_server()
    capsys.readouterr()
    out = run(s, slow(1, "boom", 100), slow(2, "fine", 100))
    by = {r["id"]: r for r in out if r.get("id") is not None}
    assert by[1]["result"]["isError"] and by[1]["result"]["content"][0]["text"] == "ValueError: bad tag"
    assert by[2]["result"] == {"content": [{"type": "text", "text": "fine"}], "isError": False}
    lines = [json.loads(x) for x in capsys.readouterr().err.splitlines() if '"call_tool"' in x]
    assert sorted((x["name"], x["ok"]) for x in lines) == [("slow", False), ("slow", True)], "one log line per call"


def test_progress_lines_name_their_own_request_under_concurrency(slow_server):
    s = slow_server()
    out = run(s, slow("p1", "one", 150, progress=True), slow("p2", "two", 150, progress=True))
    steps = [m["params"] for m in out if m.get("method") == "runtime.progress"]
    assert len(steps) == 4
    assert {(p["id"], p["message"]) for p in steps} == {("p1", "one"), ("p2", "two")}, "an id never crosses a call"
    by = {r["id"]: r for r in out if "result" in r}
    assert by["p1"]["result"]["content"][0]["text"] == "one" and by["p2"]["result"]["content"][0]["text"] == "two"
