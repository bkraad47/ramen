"""ramen-mcp-bridge against an in-process grpcio fake of ramen.v1.Mcp (+ grpc.health.v1)."""

import io
import json
import os
import subprocess
import sys
from concurrent import futures

import grpc
import pytest
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ramen_proto.ramen.v1 import mcp_pb2, mcp_pb2_grpc
from ramen_runtime import bridge

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class FakeMcp(mcp_pb2_grpc.McpServicer):
    """Minimal MCP over gRPC: bearer `good` required, ping/initialize/tools/*; records metadata."""

    def __init__(self):
        self.seen: list[dict] = []

    def Call(self, request, context):  # noqa: N802
        md = dict(context.invocation_metadata())
        self.seen.append(md)
        if md.get("authorization") != "Bearer good":
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
        msg = json.loads(request.body)
        rid, method = msg.get("id"), msg.get("method", "")
        if method.startswith("notifications/"):
            return mcp_pb2.JsonRpc(body=b"")
        if method == "busy":
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "busy")
        if method == "slow":
            import time

            time.sleep(2)
        result = {
            "ping": {},
            "initialize": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            },
            "tools/list": {
                "tools": [
                    {
                        "name": "add",
                        "description": "a+b",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            "required": ["a", "b"],
                        },
                    }
                ]
            },
        }.get(method)
        if method == "tools/call":
            args = msg["params"]["arguments"]
            result = {"content": [{"type": "text", "text": str(args["a"] + args["b"])}], "isError": False}
        if result is None:
            body = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        else:
            body = {"jsonrpc": "2.0", "id": rid, "result": result}
        return mcp_pb2.JsonRpc(body=json.dumps(body).encode())


@pytest.fixture
def fake():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    svc = FakeMcp()
    mcp_pb2_grpc.add_McpServicer_to_server(svc, server)
    hs = health.HealthServicer()
    hs.set("", health_pb2.HealthCheckResponse.SERVING)
    hs.set("ramen.v1.Mcp", health_pb2.HealthCheckResponse.NOT_SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(hs, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    svc.target = f"127.0.0.1:{port}"
    yield svc
    server.stop(None)


def make_bridge(fake, key="good", timeout=5.0) -> bridge.Bridge:
    stub = mcp_pb2_grpc.McpStub(grpc.insecure_channel(fake.target))
    md = [("authorization", f"Bearer {key}"), ("ramen-group", "demo"), ("ramen-zone", "local")]
    return bridge.Bridge(stub, md, timeout)


def run(b: bridge.Bridge, data: bytes) -> bytes:
    out = io.BytesIO()
    assert bridge.serve(b, io.BytesIO(data), out) == 0
    return out.getvalue()


def test_parse_args_flags_and_env(monkeypatch):
    a = bridge.parse_args(["--target", "h:1", "--key", "k", "--group", "g", "--zone", "z"])
    assert (a.target, a.key, a.group, a.zone, a.use_tls, a.timeout) == ("h:1", "k", "g", "z", False, 120.0)
    assert bridge.parse_args(["--target", "h:1", "--key", "k", "--tls"]).use_tls
    assert bridge.parse_args(["--target", "h:1", "--key", "k", "--ca", "/x.pem"]).use_tls
    assert not bridge.parse_args(["--target", "h:1", "--key", "k", "--ca", "/x.pem", "--insecure"]).use_tls
    monkeypatch.setenv("RAMEN_BRIDGE_TARGET", "e:2")
    monkeypatch.setenv("RAMEN_BRIDGE_KEY", "ek")
    monkeypatch.setenv("RAMEN_BRIDGE_TLS", "1")
    monkeypatch.setenv("RAMEN_BRIDGE_TIMEOUT", "3")
    a = bridge.parse_args([])
    assert (a.target, a.key, a.use_tls, a.timeout, a.health) == ("e:2", "ek", True, 3.0, None)
    assert bridge.parse_args(["--health"]).health == ""
    assert bridge.parse_args(["--health", "ramen.v1.Admin"]).health == "ramen.v1.Admin"
    monkeypatch.delenv("RAMEN_BRIDGE_KEY")
    assert bridge.parse_args(["--health"]).key is None
    with pytest.raises(SystemExit):
        bridge.parse_args([])  # key missing for the bridge mode
    monkeypatch.delenv("RAMEN_BRIDGE_TARGET")
    with pytest.raises(SystemExit):
        bridge.parse_args(["--key", "k"])


def test_channel_tls_and_plain(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nMA==\n-----END CERTIFICATE-----\n")
    assert bridge.channel(bridge.parse_args(["--target", "h:1", "--key", "k"])) is not None
    assert bridge.channel(bridge.parse_args(["--target", "h:1", "--key", "k", "--tls"])) is not None
    assert bridge.channel(bridge.parse_args(["--target", "h:1", "--key", "k", "--ca", str(ca)])) is not None


def test_framing_newline_and_content_length():
    body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    msgs = list(bridge.read_messages(io.BytesIO(body + b"\n\n" + body + b"\r\n")))
    assert msgs == [(body, False), (body, False)]
    framed = b"Content-Length: %d\r\nContent-Type: application/json\r\n\r\n%s" % (len(body), body)
    assert list(bridge.read_messages(io.BytesIO(framed + framed))) == [(body, True), (body, True)]
    out = io.BytesIO()
    bridge.write_message(out, body, False)
    bridge.write_message(out, body, True)
    assert out.getvalue() == body + b"\n" + framed.replace(b"Content-Type: application/json\r\n", b"")


def test_forwards_metadata_and_relays_responses(fake):
    b = make_bridge(fake)
    out = run(
        b,
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"nope"}\n',
    )
    lines = [json.loads(x) for x in out.splitlines()]
    assert lines[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert lines[1]["error"]["code"] == -32601 and len(lines) == 2
    md = fake.seen[0]
    assert (md["authorization"], md["ramen-group"], md["ramen-zone"]) == ("Bearer good", "demo", "local")
    # Content-Length in → Content-Length out
    req = b'{"jsonrpc":"2.0","id":3,"method":"ping"}'
    out = run(b, b"Content-Length: %d\r\n\r\n%s" % (len(req), req))
    assert out.startswith(b"Content-Length: ") and out.endswith(b'"result": {}}')


def test_grpc_errors_become_jsonrpc_errors_for_requests_only(fake, capsys):
    bad = make_bridge(fake, key="wrong")
    out = run(bad, b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n{"jsonrpc":"2.0","method":"notifications/x"}\n')
    lines = [json.loads(x) for x in out.splitlines()]
    assert len(lines) == 1 and lines[0]["id"] == 7 and lines[0]["error"]["code"] == -32001
    assert "UNAUTHENTICATED" in lines[0]["error"]["message"]
    assert "grpc error" in capsys.readouterr().err
    busy = json.loads(run(make_bridge(fake), b'{"jsonrpc":"2.0","id":8,"method":"busy"}\n'))
    assert busy["error"]["code"] == -32000 and "RESOURCE_EXHAUSTED" in busy["error"]["message"]
    slow = json.loads(run(make_bridge(fake, timeout=0.2), b'{"jsonrpc":"2.0","id":9,"method":"slow"}\n'))
    assert "DEADLINE_EXCEEDED" in slow["error"]["message"]
    assert run(bad, b"[1,2]\n") == b""  # not an object: no id → nothing to answer
    down = bridge.Bridge(mcp_pb2_grpc.McpStub(grpc.insecure_channel("127.0.0.1:1")), [], 1.0)
    unavailable = json.loads(run(down, b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
    assert unavailable["error"]["code"] == -32000


def test_health_probe(fake, capsys):
    ch = grpc.insecure_channel(fake.target)
    assert bridge.health(ch, "", 5) == 0
    assert capsys.readouterr().out.strip() == "SERVING"
    assert bridge.health(ch, "ramen.v1.Mcp", 5) == 1
    assert bridge.health(ch, "unknown", 5) == 1
    assert "NOT_FOUND" in capsys.readouterr().err
    assert bridge.main(["--target", fake.target, "--health"]) == 0
    assert bridge.main(["--target", "127.0.0.1:1", "--health", "--timeout", "1"]) == 1


def test_main_end_to_end_over_stdio(fake, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    assert bridge.main(["--target", fake.target, "--key", "good", "--group", "g", "--zone", "z"]) == 0
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert (fake.seen[-1]["ramen-group"], fake.seen[-1]["ramen-zone"]) == ("g", "z")


def test_bridge_as_subprocess_omits_empty_routing_metadata(fake):
    env = os.environ | {"PYTHONPATH": SRC, "RAMEN_BRIDGE_KEY": "good"}
    p = subprocess.run(
        [sys.executable, "-m", "ramen_runtime.bridge", "--target", fake.target],
        input=b'{"jsonrpc":"2.0","id":5,"method":"ping"}\n',
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["result"] == {}
    assert "ramen-group" not in fake.seen[-1] and "ramen-zone" not in fake.seen[-1]


def test_mcp_sdk_stdio_client_through_the_bridge(fake):
    """The official `mcp` client spawns the bridge as a stdio server: initialize → tools/list → tools/call."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ramen_runtime.bridge", "--target", fake.target, "--key", "good", "--group", "demo"],
        env=os.environ | {"PYTHONPATH": SRC},
    )

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                res = await s.call_tool("add", {"a": 2, "b": 3})
                return [t.name for t in tools.tools], res.content[0].text, res.is_error

    assert asyncio.run(go()) == (["add"], "5", False)
    assert any(m.get("ramen-group") == "demo" for m in fake.seen)
