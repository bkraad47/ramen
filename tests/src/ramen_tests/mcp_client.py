"""gRPC client for a Ramen node (CONTRACTS §11): `ramen.v1.Mcp/Call` carrying one JSON-RPC 2.0 message per call,
`ramen.v1.Admin` (Reload/Metrics, metadata `x-ramen-admin-key`) and `grpc.health.v1.Health`. Plus `bridge_session()`:
the official `mcp` SDK stdio client talking to `ramen-mcp-bridge`, the way Claude Desktop / Cursor connect."""

from __future__ import annotations

import itertools
import json
import os
import shlex
import shutil
import ssl
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ramen_proto import admin_pb2, admin_pb2_grpc, mcp_pb2, mcp_pb2_grpc

from . import env as E

PROTOCOL = "2025-06-18"
UNSET = object()
MAX_MESSAGE = 4 * 1024 * 1024
CLIENT_INFO = {"name": "ramen-tests", "version": "0.4.0"}
_ids = itertools.count(1)


class JsonRpcError(Exception):
    """A JSON-RPC error object returned inside a successful gRPC call."""

    def __init__(self, error: dict):
        self.code = error.get("code")
        self.message = error.get("message", "")
        self.data = error.get("data")
        super().__init__(f"{self.code}: {self.message}")


def code_of(fn, *a, **kw) -> grpc.StatusCode:
    """Run fn; → grpc.StatusCode.OK or the status of the RpcError it raised."""
    try:
        fn(*a, **kw)
    except grpc.RpcError as e:
        return e.code()
    return grpc.StatusCode.OK


def _insecure_root(host: str) -> tuple[bytes, str | None]:
    """grpc cannot skip certificate verification; for RAMEN_TLS_INSECURE=1 pin the server's own (self-signed) cert
    as the root and override the target name with its CN so hostname checks pass."""
    h, _, p = host.rpartition(":")
    pem = ssl.get_server_certificate((h.strip("[]"), int(p)))
    cn = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
        info = ssl._ssl._test_decode_cert(f.name)  # noqa: SLF001 - stdlib has no public cert parser
        os.unlink(f.name)
        for rdn in info.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    cn = v
        san = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"]
        cn = san[0] if san else cn
    except Exception:  # noqa: BLE001 - best effort
        pass
    return pem.encode(), cn


class Node:
    """Blocking gRPC client. `key` = MCP bearer; `admin_key` = worker admin key; group/zone → routing metadata."""

    def __init__(
        self,
        target: str,
        key: str | None = None,
        *,
        group: str | None = None,
        zone: str | None = None,
        tls: bool = False,
        ca: str | bytes | None = None,
        admin_key: str | None = None,
        timeout: float = 30,
    ):
        self.target, self.key, self.admin_key, self.timeout, self.tls = target, key, admin_key, timeout, tls
        self.group, self.zone = group, zone
        self._ca = Path(ca).read_bytes() if isinstance(ca, str) else ca
        self._channel: grpc.Channel | None = None

    @classmethod
    def from_env(cls, key: str | None = None, **kw) -> Node:
        target, tls = E.node_target(E.require("RAMEN_NODE_URL"))
        meta = dict(E.routing_metadata())
        return cls(
            target,
            key,
            group=kw.pop("group", meta["ramen-group"]),
            zone=kw.pop("zone", meta["ramen-zone"]),
            tls=tls,
            ca=E.env("RAMEN_NODE_CA"),
            admin_key=kw.pop("admin_key", E.env("RAMEN_ADMIN_KEY")),
            **kw,
        )

    # -- channel -------------------------------------------------------------------------------------------------
    def channel(self) -> grpc.Channel:
        if self._channel is None:
            opts = [("grpc.max_send_message_length", -1), ("grpc.max_receive_message_length", MAX_MESSAGE)]
            if not self.tls:
                self._channel = grpc.insecure_channel(self.target, options=opts)
            else:
                root, override = self._ca, None
                if root is None and not E.tls_verify():
                    root, override = _insecure_root(self.target)
                if override:
                    opts.append(("grpc.ssl_target_name_override", override))
                self._channel = grpc.secure_channel(self.target, grpc.ssl_channel_credentials(root), options=opts)
        return self._channel

    def close(self):
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def metadata(self, key=UNSET, extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
        k = self.key if key is UNSET else key
        md = [("authorization", f"Bearer {k}")] if k else []
        if self.group:
            md.append(("ramen-group", self.group))
        if self.zone:
            md.append(("ramen-zone", self.zone))
        return md + list(extra or [])

    # -- Mcp/Call ------------------------------------------------------------------------------------------------
    def call_raw(self, body: bytes | dict, key=UNSET, extra=None, timeout: float | None = None) -> bytes:
        """One Mcp/Call → response bytes (b"" for a notification). Raises grpc.RpcError on a non-OK status."""
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        stub = mcp_pb2_grpc.McpStub(self.channel())
        md = self.metadata(key, extra)
        return stub.Call(mcp_pb2.JsonRpc(body=raw), metadata=md, timeout=timeout or self.timeout).body

    def status(self, body: bytes | dict, key=UNSET, extra=None) -> grpc.StatusCode:
        return code_of(self.call_raw, body, key, extra)

    def request(self, method: str, params: dict | None = None, key=UNSET, timeout: float | None = None) -> dict:
        """JSON-RPC request → `result`. Raises JsonRpcError for an error object, grpc.RpcError for transport errors."""
        rid = next(_ids)
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        out = json.loads(self.call_raw(body, key, timeout=timeout) or b"null")
        assert isinstance(out, dict) and out.get("jsonrpc") == "2.0" and out.get("id") == rid, out
        if "error" in out:
            raise JsonRpcError(out["error"])
        return out["result"]

    def notify(self, method: str, params: dict | None = None, key=UNSET) -> bytes:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        return self.call_raw(body, key)

    def initialize(self, key=UNSET) -> dict:
        r = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO},
            key,
        )
        self.notify("notifications/initialized", key=key)
        return r

    def ping(self, key=UNSET) -> dict:
        return self.request("ping", key=key)

    def list_tools(self, key=UNSET) -> list[dict]:
        return self.request("tools/list", key=key)["tools"]

    def list_resources(self, key=UNSET) -> list[dict]:
        return self.request("resources/list", key=key)["resources"]

    def list_prompts(self, key=UNSET) -> list[dict]:
        return self.request("prompts/list", key=key)["prompts"]

    def call_tool(self, name: str, arguments: dict | None = None, key=UNSET) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, key)

    def read_resource(self, uri: str, key=UNSET) -> dict:
        return self.request("resources/read", {"uri": uri}, key)

    def get_prompt(self, name: str, arguments: dict | None = None, key=UNSET) -> dict:
        return self.request("prompts/get", {"name": name, "arguments": arguments or {}}, key)

    def session_supported(self) -> bool:
        """Mcp/Session is reserved: a server may answer UNIMPLEMENTED (→ False); anything that echoes works (→ True)."""
        stub = mcp_pb2_grpc.McpStub(self.channel())
        body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "ping"}).encode()
        try:
            for reply in stub.Session(iter([mcp_pb2.JsonRpc(body=body)]), metadata=self.metadata(), timeout=10):
                return b"result" in reply.body
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                return False
            raise
        return True

    # -- Health / Admin -------------------------------------------------------------------------------------------
    def health(self, service: str = "") -> str:
        """grpc.health.v1 status name: SERVING / NOT_SERVING / UNKNOWN / SERVICE_UNKNOWN. Raises grpc.RpcError when the
        target is unreachable. Unauthenticated by contract; routing metadata is still sent for the LB."""
        stub = health_pb2_grpc.HealthStub(self.channel())
        md = [m for m in self.metadata(None) if m[0] != "authorization"]
        r = stub.Check(health_pb2.HealthCheckRequest(service=service), metadata=md, timeout=10)
        return health_pb2.HealthCheckResponse.ServingStatus.Name(r.status)

    def health_code(self) -> grpc.StatusCode:
        return code_of(self.health)

    def reflect(self) -> list[str]:
        """Service names via grpc.reflection.v1 (the node enables server reflection, so grpcurl needs no -proto)."""
        from grpc_reflection.v1alpha import reflection_pb2  # v1 has the same wire messages as v1alpha

        call = self.channel().stream_stream(
            "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
            request_serializer=reflection_pb2.ServerReflectionRequest.SerializeToString,
            response_deserializer=reflection_pb2.ServerReflectionResponse.FromString,
        )
        req = reflection_pb2.ServerReflectionRequest(list_services="")
        for resp in call(iter([req]), metadata=self.metadata(None), timeout=10):
            return [s.name for s in resp.list_services_response.service]
        return []

    def admin_metadata(self, admin_key=UNSET) -> list[tuple[str, str]]:
        k = self.admin_key if admin_key is UNSET else admin_key
        return ([("x-ramen-admin-key", k)] if k else []) + self.metadata(None)

    def admin_reload(self, admin_key=UNSET, timeout: float = 180) -> dict:
        stub = admin_pb2_grpc.AdminStub(self.channel())
        r = stub.Reload(admin_pb2.ReloadRequest(), metadata=self.admin_metadata(admin_key), timeout=timeout)
        return json.loads(r.json)

    def admin_metrics(self, admin_key=UNSET) -> dict:
        stub = admin_pb2_grpc.AdminStub(self.channel())
        r = stub.Metrics(admin_pb2.MetricsRequest(), metadata=self.admin_metadata(admin_key), timeout=self.timeout)
        return json.loads(r.json)


def text_of(result: dict) -> str:
    """Concatenated text of a tools/call result (raw JSON dict form)."""
    return "".join(c.get("text", "") for c in result.get("content", []))


# -- bridge (stdio) ---------------------------------------------------------------------------------------------------
def bridge_command() -> list[str] | None:
    """RAMEN_BRIDGE_CMD (shell words) > `ramen-mcp-bridge` on PATH > ../runtime-py/.venv/bin/ramen-mcp-bridge."""
    if E.env("RAMEN_BRIDGE_CMD"):
        return shlex.split(E.env("RAMEN_BRIDGE_CMD"))
    exe = shutil.which("ramen-mcp-bridge")
    if exe:
        return [exe]
    venv = E.RAMEN_DIR / "runtime-py" / ".venv" / "bin" / "ramen-mcp-bridge"
    if venv.exists():
        return [str(venv)]
    py = E.RAMEN_DIR / "runtime-py" / ".venv" / "bin" / "python"
    if py.exists():
        return [str(py), "-m", "ramen_runtime.bridge"]
    return None


def bridge_args(target: str, key: str, group: str, zone: str, tls: bool = False, ca: str | None = None) -> list[str]:
    """§11: ramen-mcp-bridge --target <host:port> --key <rmk_…> --group <g> --zone <z> [--tls|--insecure] [--ca pem]."""
    args = ["--target", target, "--key", key, "--group", group, "--zone", zone, "--tls" if tls else "--insecure"]
    if ca:
        args += ["--ca", ca]
    return args


@asynccontextmanager
async def bridge_session(node: Node, key: str | None = None, timeout: float = 60, errlog=sys.stderr):
    """Official mcp SDK stdio ClientSession through `ramen-mcp-bridge` pointed at `node`. Initialised on entry."""
    cmd = bridge_command()
    assert cmd, "ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH / ../runtime-py/.venv)"
    k = node.key if key is None else key
    ca_pem = node._ca
    if ca_pem is None and node.tls and not E.tls_verify():
        ca_pem = _insecure_root(node.target)[0]  # RAMEN_TLS_INSECURE=1: pin the server's own (self-signed) cert
    ca = None if ca_pem is None else _ca_file(ca_pem)
    params = StdioServerParameters(
        command=cmd[0],
        args=cmd[1:] + bridge_args(node.target, k or "", node.group or "demo", node.zone or "local", node.tls, ca),
        env={**os.environ},
    )
    async with stdio_client(params, errlog=errlog) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timeout) as s:
            await s.initialize()
            yield s


def _ca_file(pem: bytes) -> str:
    import tempfile

    f = tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False)
    f.write(pem)
    f.close()
    return f.name


def sdk_text(result) -> str:
    """Text of an mcp SDK CallToolResult."""
    return "".join(getattr(c, "text", "") for c in result.content)
