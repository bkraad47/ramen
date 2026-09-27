import io
import json
import subprocess
import sys

import pytest

from ramen_runtime.rpc import Server


def run(server, *msgs):
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs) + "\n{bad json\n"), out)
    return [json.loads(l) for l in out.getvalue().splitlines()]


def test_full_session(demo_bucket, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    s = Server(demo_bucket)
    res = run(
        s,
        {"jsonrpc": "2.0", "id": 1, "method": "runtime.ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "runtime.call_tool", "params": {"name": "demo_calculator_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "runtime.load", "params": {"bucket": str(demo_bucket)}},
        {"jsonrpc": "2.0", "id": 4, "method": "runtime.call_tool", "params": {"name": "demo_calculator_tool", "arguments": {"var1": 6, "var2": 7, "func": "multiply"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "runtime.read_resource", "params": {"uri": "ramen://demo/readme"}},
        {"jsonrpc": "2.0", "id": 6, "method": "runtime.get_prompt", "params": {"name": "get_calculation_prompt", "arguments": {"request": "1+1"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "runtime.call_tool", "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 8, "method": "runtime.get_prompt", "params": {"name": "get_calculation_prompt", "arguments": {}}},
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
    res = run(s, {"jsonrpc": "2.0", "id": 1, "method": "runtime.call_tool", "params": {"name": "demo_calculator_tool", "arguments": {"var1": 1, "var2": 1, "func": "add"}}})
    assert res[0]["result"]["content"][0]["text"] == "2"


@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
def test_main_subprocess(demo_bucket):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "runtime.load", "params": {"bucket": str(demo_bucket)}}, {"jsonrpc": "2.0", "id": 2, "method": "runtime.shutdown", "params": {}}]
    p = subprocess.run([sys.executable, "-m", "ramen_runtime", "--bucket", str(demo_bucket)], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    lines = [json.loads(l) for l in p.stdout.splitlines()]
    assert lines[0]["result"]["tools"][0]["name"] == "demo_calculator_tool"
    assert lines[1]["result"] == {"ok": True}
    logs = [json.loads(l) for l in p.stderr.splitlines()]
    assert all("ts" in l and "level" in l and "msg" in l for l in logs)
    assert any(l["msg"] == "loaded" for l in logs)


def test_main_bad_bucket_arg():
    p = subprocess.run([sys.executable, "-m", "ramen_runtime"], capture_output=True, text=True)
    assert p.returncode == 2


def test_main_inprocess(demo_bucket, monkeypatch, capsys):
    from ramen_runtime.__main__ import main

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "runtime.ping", "params": {}}) + "\n"))
    assert main(["--bucket", str(demo_bucket), "--load"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"ok": True}
