"""Clients for a Ramen node on both transports (CONTRACTS §11 gRPC, §16 Streamable HTTP).

`Node`: gRPC — `ramen.v1.Mcp/Call` carrying one JSON-RPC 2.0 message per call, `ramen.v1.Admin` (Reload/Metrics,
metadata `x-ramen-admin-key`) and `grpc.health.v1.Health`. `HttpNode`: the same surface over `POST /mcp`. Both
answer `outcome()` with the transport-neutral denial names (`OK`, `UNAUTHENTICATED`, `PERMISSION_DENIED`, ...), which
is what lets one conformance module run against both. Plus `bridge_session()` (the `mcp` SDK stdio client through
`ramen-mcp-bridge`) and `http_session()` (the `mcp` SDK Streamable HTTP client straight at the node)."""

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
import httpx
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

    def outcome(self, body: bytes | dict, key=UNSET, extra=None) -> str:
        """Transport-neutral result name (§16.1 table): `OK` or the gRPC code name, e.g. `UNAUTHENTICATED`."""
        return self.status(body, key, extra).name

    transport = "grpc"

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


# -- Streamable HTTP (§16) --------------------------------------------------------------------------------------------
HTTP_OUTCOMES = {
    200: "OK",
    202: "OK",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    413: "OUT_OF_RANGE",
    429: "RESOURCE_EXHAUSTED",
    503: "UNAVAILABLE",
}


class HttpError(Exception):
    """A non-2xx answer from `POST /mcp`."""

    def __init__(self, response: httpx.Response):
        self.response, self.status_code = response, response.status_code
        super().__init__(f"HTTP {response.status_code}: {response.text[:200]}")


class HttpNode:
    """Blocking Streamable HTTP client with the `Node` surface. `url` is the `/mcp` endpoint; `key` the bearer."""

    transport = "http"

    def __init__(
        self,
        url: str,
        key: str | None = None,
        *,
        group: str | None = None,
        zone: str | None = None,
        ca: str | bytes | None = None,
        verify: bool | None = None,
        timeout: float = 30,
    ):
        self.url, self.key, self.group, self.zone, self.timeout = url, key, group, zone, timeout
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.notifications: list[dict] = []  # server notifications carried on SSE answers (C11), in order
        self.last_response: httpx.Response | None = None
        v: bool | str = E.tls_verify() if verify is None else verify
        if isinstance(ca, (str, bytes)):
            v = ca if isinstance(ca, str) else _ca_file(ca)
        self._client = httpx.Client(timeout=timeout, verify=v)

    @classmethod
    def from_env(cls, key: str | None = None, **kw) -> HttpNode:
        meta = dict(E.routing_metadata())
        return cls(
            E.node_http_url(E.require("RAMEN_NODE_URL")),
            key,
            group=kw.pop("group", meta["ramen-group"]),
            zone=kw.pop("zone", meta["ramen-zone"]),
            ca=E.env("RAMEN_NODE_CA"),
            **kw,
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def headers(self, key=UNSET, extra: list[tuple[str, str]] | None = None, session: bool = True) -> dict:
        k = self.key if key is UNSET else key
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if k:
            h["Authorization"] = f"Bearer {k}"
        if self.group:
            h["ramen-group"] = self.group
        if self.zone:
            h["ramen-zone"] = self.zone
        if session and self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        for name, value in extra or []:
            h[name] = value
        return h

    def post(
        self, body: bytes | dict, key=UNSET, extra=None, timeout: float | None = None, session=True
    ) -> httpx.Response:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self._client.post(
            self.url, content=raw, headers=self.headers(key, extra, session), timeout=timeout or self.timeout
        )

    def call_raw(self, body: bytes | dict, key=UNSET, extra=None, timeout: float | None = None) -> bytes:
        """One POST → response bytes (b"" for a notification). Raises HttpError on a non-2xx status."""
        r = self.post(body, key, extra, timeout)
        if r.status_code >= 300:
            raise HttpError(r)
        return r.content

    def status(self, body: bytes | dict, key=UNSET, extra=None) -> int:
        return self.post(body, key, extra).status_code

    def outcome(self, body: bytes | dict, key=UNSET, extra=None) -> str:
        return HTTP_OUTCOMES.get(self.status(body, key, extra), "UNKNOWN")

    def request(self, method: str, params: dict | None = None, key=UNSET, timeout: float | None = None) -> dict:
        rid = next(_ids)
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        r = self.post(body, key, timeout=timeout)
        if r.status_code >= 300:
            raise HttpError(r)
        self.last_response = r
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            # 0.7.2 C11: after a rollout the node may answer a POST as SSE — notifications first, the response last.
            events = sse_events(r.text)
            self.notifications += [e for e in events if "id" not in e]
            out = next((e for e in events if e.get("id") == rid), None)
            assert out is not None, events
        else:
            out = r.json()
        assert isinstance(out, dict) and out.get("jsonrpc") == "2.0" and out.get("id") == rid, out
        if "error" in out:
            raise JsonRpcError(out["error"])
        if method == "initialize":  # §16.2: the id the server minted for this credential
            self.session_id = r.headers.get("Mcp-Session-Id")
            self.protocol_version = r.headers.get("MCP-Protocol-Version") or out["result"].get("protocolVersion")
        return out["result"]

    def notify(self, method: str, params: dict | None = None, key=UNSET) -> bytes:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        return self.call_raw(body, key)

    def initialize(self, key=UNSET) -> dict:
        r = self.request(
            "initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO}, key
        )
        self.notify("notifications/initialized", key=key)
        return r

    def end_session(self, key=UNSET) -> int:
        """`DELETE /mcp` with the current session → HTTP status (204 when it was ours)."""
        h = self.headers(key)
        h.pop("Content-Type", None)
        return self._client.delete(self.url, headers=h, timeout=self.timeout).status_code

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

    def get(self, path: str | None = None, headers: dict | None = None) -> httpx.Response:
        """A GET on `/mcp` (405 by contract) or on a sibling path such as the RFC 9728 metadata."""
        url = self.url if path is None else self.url.rsplit("/mcp", 1)[0] + path
        return self._client.get(url, headers=headers or {}, timeout=self.timeout)


def sse_events(text: str) -> list[dict]:
    """JSON messages of a `text/event-stream` body, in order (`data:` lines joined per event; other fields ignored)."""
    out, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            out.append(json.loads("\n".join(data)))
            data = []
    return out


def sdk_http_client(node: HttpNode, key: str | None = None, timeout: float = 60):
    """The httpx2.AsyncClient `http_session` drives the SDK with: bearer + routing headers. Handed out so a test can
    change `client.headers["Authorization"]` on the live session (what a client does after a token refresh)."""
    import httpx2

    k = node.key if key is None else key
    headers = {
        name: value for name, value in node.headers(k, session=False).items() if name not in ("Content-Type", "Accept")
    }
    return httpx2.AsyncClient(headers=headers, verify=E.tls_verify(), timeout=timeout)


@asynccontextmanager
async def http_session(node: HttpNode, key: str | None = None, timeout: float = 60, http_client=None, **session_kw):
    """Official mcp SDK Streamable HTTP ClientSession straight at the node's `/mcp` (§16.4: no bridge to install).
    `http_client`: a caller-owned `sdk_http_client()` (the caller closes it); else one is made for the session.
    `session_kw` go to `ClientSession` (e.g. `message_handler=` to see server notifications, C11)."""
    from mcp.client.streamable_http import streamable_http_client

    own = http_client is None
    client = sdk_http_client(node, key, timeout) if own else http_client
    try:
        async with streamable_http_client(node.url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=timeout, **session_kw) as s:
                await s.initialize()
                yield s
    finally:
        if own:
            await client.aclose()


# -- bridge (stdio) ---------------------------------------------------------------------------------------------------
def bridge_command() -> list[str] | None:
    """RAMEN_BRIDGE_CMD (shell words) > `ramen-mcp-bridge` on PATH > ../../ramen-mcp-bridge/.venv (v0.5.7: the
    bridge moved to its own repo/package, submoduled alongside `ramen` at the ramen-master root)."""
    if E.env("RAMEN_BRIDGE_CMD"):
        return shlex.split(E.env("RAMEN_BRIDGE_CMD"))
    exe = shutil.which("ramen-mcp-bridge")
    if exe:
        return [exe]
    from .sidecar import venv_bin  # noqa: PLC0415 - avoids an import cycle at module load

    venv = E.RAMEN_DIR.parent / "ramen-mcp-bridge" / ".venv"
    exe = venv_bin(venv, "ramen-mcp-bridge")
    if exe:
        return [str(exe)]
    py = venv_bin(venv, "python")
    if py:
        return [str(py), "-m", "ramen_mcp_bridge.bridge"]
    return None


def bridge_args(target: str, key: str, group: str, zone: str, tls: bool = False, ca: str | None = None) -> list[str]:
    """§11: ramen-mcp-bridge --target <host:port> --key <rmk_…> --group <g> --zone <z> [--tls|--insecure] [--ca pem]."""
    args = ["--target", target, "--key", key, "--group", group, "--zone", zone, "--tls" if tls else "--insecure"]
    if ca:
        args += ["--ca", ca]
    return args


@asynccontextmanager
async def bridge_session(node: Node, key: str | None = None, timeout: float = 60, errlog=sys.stderr, args=None):
    """Official mcp SDK stdio ClientSession through `ramen-mcp-bridge` pointed at `node`. Initialised on entry.
    `args`: the bridge's own flags instead of the `--key` set (e.g. `--oauth <console> --client-id ...`)."""
    cmd = bridge_command()
    assert cmd, "ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH / ../runtime-py/.venv)"
    k = node.key if key is None else key
    ca_pem = node._ca
    if ca_pem is None and node.tls and not E.tls_verify():
        ca_pem = _insecure_root(node.target)[0]  # RAMEN_TLS_INSECURE=1: pin the server's own (self-signed) cert
    ca = None if ca_pem is None else _ca_file(ca_pem)
    if args is None:
        args = bridge_args(node.target, k or "", node.group or "demo", node.zone or "local", node.tls, ca)
    params = StdioServerParameters(command=cmd[0], args=cmd[1:] + list(args), env={**os.environ})
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
