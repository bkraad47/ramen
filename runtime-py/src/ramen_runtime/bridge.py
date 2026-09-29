"""ramen-mcp-bridge: a stdio MCP server that forwards every JSON-RPC message to `ramen.v1.Mcp/Call` (CONTRACTS §11).

Standard MCP clients (Claude Desktop, Cursor, the `mcp` SDK) speak newline-delimited JSON-RPC on stdin/stdout; the
bridge also accepts `Content-Length:` framing and answers in the framing it was asked in. Metadata sent per call:
`authorization: Bearer <key>`, `ramen-group`, `ramen-zone` (the LB routes on the last two). Notifications produce no
output. A gRPC status becomes a JSON-RPC error for requests (never for notifications). `--health [SERVICE]` checks
`grpc.health.v1` instead and exits 0 when SERVING.
"""

import argparse
import json
import os
import sys
from collections.abc import Iterator
from typing import BinaryIO

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from ramen_proto.ramen.v1 import mcp_pb2, mcp_pb2_grpc

CODES = {
    grpc.StatusCode.UNAUTHENTICATED: -32001,
    grpc.StatusCode.PERMISSION_DENIED: -32000,
    grpc.StatusCode.RESOURCE_EXHAUSTED: -32000,
    grpc.StatusCode.OUT_OF_RANGE: -32000,
    grpc.StatusCode.UNAVAILABLE: -32000,
    grpc.StatusCode.DEADLINE_EXCEEDED: -32000,
}


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"RAMEN_BRIDGE_{name}", default)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ramen-mcp-bridge", description=__doc__.split("\n")[0])
    ap.add_argument("--target", default=_env("TARGET"), help="host:port of a worker or its LB (RAMEN_BRIDGE_TARGET)")
    # §16.4: the key may live in the environment so it never has to sit in a client's config file.
    ap.add_argument(
        "--key",
        default=_env("KEY") or os.environ.get("RAMEN_MCP_KEY"),
        help="rmk_ MCP key (RAMEN_BRIDGE_KEY, else RAMEN_MCP_KEY)",
    )
    ap.add_argument("--group", default=_env("GROUP", ""), help="ramen-group metadata (RAMEN_BRIDGE_GROUP)")
    ap.add_argument("--zone", default=_env("ZONE", ""), help="ramen-zone metadata (RAMEN_BRIDGE_ZONE)")
    ap.add_argument("--tls", action="store_true", default=_env("TLS") in ("1", "true"), help="TLS (RAMEN_BRIDGE_TLS=1)")
    ap.add_argument("--insecure", action="store_true", help="plaintext h2c (default unless --tls/--ca)")
    ap.add_argument("--ca", default=_env("CA"), help="PEM CA bundle for TLS (RAMEN_BRIDGE_CA); implies --tls")
    ap.add_argument("--timeout", type=float, default=float(_env("TIMEOUT", "120")), help="per-call deadline, seconds")
    ap.add_argument("--health", nargs="?", const="", metavar="SERVICE", help="check grpc.health.v1 and exit")
    a = ap.parse_args(argv)
    if not a.target:
        ap.error("--target (or RAMEN_BRIDGE_TARGET) is required")
    if a.health is None and not a.key:
        ap.error("--key (or RAMEN_BRIDGE_KEY / RAMEN_MCP_KEY) is required")
    a.use_tls = bool(a.tls or a.ca) and not a.insecure
    return a


def channel(a: argparse.Namespace) -> grpc.Channel:
    if not a.use_tls:
        return grpc.insecure_channel(a.target)
    roots = open(a.ca, "rb").read() if a.ca else None
    return grpc.secure_channel(a.target, grpc.ssl_channel_credentials(root_certificates=roots))


def read_messages(inp: BinaryIO) -> Iterator[tuple[bytes, bool]]:
    """Yield `(raw_json, content_length_framed)` for newline-delimited or `Content-Length:` framed input."""
    while line := inp.readline():
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":", 1)[1])
            while (h := inp.readline()) and h not in (b"\r\n", b"\n"):
                pass
            yield inp.read(n), True
        elif line.strip():
            yield line.strip(), False


def write_message(out: BinaryIO, raw: bytes, framed: bool) -> None:
    out.write(b"Content-Length: %d\r\n\r\n%s" % (len(raw), raw) if framed else raw + b"\n")
    out.flush()


class Bridge:
    def __init__(self, stub: mcp_pb2_grpc.McpStub, metadata: list[tuple[str, str]], timeout: float):
        self.stub, self.metadata, self.timeout = stub, metadata, timeout

    def forward(self, raw: bytes) -> bytes | None:
        try:
            return self.stub.Call(mcp_pb2.JsonRpc(body=raw), metadata=self.metadata, timeout=self.timeout).body or None
        except grpc.RpcError as e:
            return self.error_for(raw, e)

    @staticmethod
    def error_for(raw: bytes, e: grpc.RpcError) -> bytes | None:
        code, details = e.code(), e.details()
        line = {"level": "warn", "msg": "grpc error", "code": code.name, "details": details}
        print(json.dumps(line), file=sys.stderr)
        try:
            rid = json.loads(raw).get("id")
        except ValueError, AttributeError:
            rid = None
        if rid is None:
            return None
        err = {"code": CODES.get(code, -32603), "message": f"{code.name}: {details}"}
        return json.dumps({"jsonrpc": "2.0", "id": rid, "error": err}).encode()


def serve(bridge: Bridge, inp: BinaryIO, out: BinaryIO) -> int:
    for raw, framed in read_messages(inp):
        if (reply := bridge.forward(raw)) is not None:
            write_message(out, reply, framed)
    return 0


def health(ch: grpc.Channel, service: str, timeout: float) -> int:
    try:
        req = health_pb2.HealthCheckRequest(service=service)
        st = health_pb2_grpc.HealthStub(ch).Check(req, timeout=timeout).status
    except grpc.RpcError as e:
        print(f"health {service!r}: {e.code().name}", file=sys.stderr)
        return 1
    print(health_pb2.HealthCheckResponse.ServingStatus.Name(st))
    return 0 if st == health_pb2.HealthCheckResponse.SERVING else 1


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    ch = channel(a)
    if a.health is not None:
        return health(ch, a.health, a.timeout)
    md = [("authorization", f"Bearer {a.key}")]
    md += [(k, v) for k, v in (("ramen-group", a.group), ("ramen-zone", a.zone)) if v]
    return serve(Bridge(mcp_pb2_grpc.McpStub(ch), md, a.timeout), sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
