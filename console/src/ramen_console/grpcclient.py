"""gRPC client for worker nodes (CONTRACTS §11): `Mcp/Call`, `Admin/Reload`, `Admin/Metrics`, `Health/Check`.

One channel per call (targets are pod IPs that come and go), 10 s deadline, 4 MiB messages. `tls` is None/False for
h2c (the default inside the cluster), True for system roots, or `{"ca": pem, "cert": pem, "key": pem}`.
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from .proto.ramen_proto.ramen.v1 import admin_pb2, admin_pb2_grpc, mcp_pb2, mcp_pb2_grpc

DEADLINE = 10.0
PORT = 8080
MAX_MESSAGE = 4 * 1024 * 1024
OPTIONS = [("grpc.max_receive_message_length", MAX_MESSAGE), ("grpc.max_send_message_length", MAX_MESSAGE)]
TLS_SCHEMES = {"https", "grpcs"}


class GrpcError(RuntimeError):
    def __init__(self, code: grpc.StatusCode, details: str, target: str):
        super().__init__(f"{code.name}: {details}")
        self.code, self.details, self.target = code, details, target


def target_of(url: str) -> tuple[str, bool]:
    """`host:port`, `http://host:port[/…]` or `https://…` → (`host:port`, tls). A bare host gets port 8080."""
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host, tls = parts.netloc, parts.scheme in TLS_SCHEMES
    else:
        host, tls = url, False
    if ":" not in host.rsplit("]", 1)[-1]:
        host = f"{host}:{PORT}"
    return host, tls


def _pem(v):
    if v is None or isinstance(v, bytes):
        return v
    v = str(v)
    return Path(v).read_bytes() if not v.lstrip().startswith("-----") else v.encode()


def credentials(tls) -> grpc.ChannelCredentials | None:
    if not tls:
        return None
    if tls is True:
        return grpc.ssl_channel_credentials()
    return grpc.ssl_channel_credentials(
        root_certificates=_pem(tls.get("ca")), private_key=_pem(tls.get("key")), certificate_chain=_pem(tls.get("cert"))
    )


class Client:
    def __init__(self, tls=None, deadline: float = DEADLINE, resolve=None):
        self.tls, self.deadline, self.resolve = tls, deadline, resolve

    def channel(self, target: str, tls=None) -> grpc.aio.Channel:
        target = self.resolve(target) if self.resolve else target
        creds = credentials(self.tls if tls is None else tls)
        if creds is None:
            return grpc.aio.insecure_channel(target, options=OPTIONS)
        return grpc.aio.secure_channel(target, creds, options=OPTIONS)

    async def _unary(self, target, tls, deadline, stub_cls, method, request, metadata):
        async with self.channel(target, tls) as ch:
            try:
                return await getattr(stub_cls(ch), method)(
                    request, metadata=tuple(metadata), timeout=deadline or self.deadline
                )
            except grpc.aio.AioRpcError as e:
                raise GrpcError(e.code(), e.details() or "", target) from None

    async def call(self, target, key, group, zone, jsonrpc: dict, tls=None, deadline=None) -> dict:
        """One JSON-RPC message in, its response out ({} for a notification)."""
        md = [("authorization", f"Bearer {key}"), ("ramen-group", group), ("ramen-zone", zone)]
        req = mcp_pb2.JsonRpc(body=json.dumps(jsonrpc).encode())
        r = await self._unary(target, tls, deadline, mcp_pb2_grpc.McpStub, "Call", req, md)
        return json.loads(r.body) if r.body else {}

    async def reload(self, target, admin_key, tls=None, deadline=None) -> dict:
        md = [("x-ramen-admin-key", admin_key)]
        req = admin_pb2.ReloadRequest()
        r = await self._unary(target, tls, deadline, admin_pb2_grpc.AdminStub, "Reload", req, md)
        return json.loads(r.json) if r.json else {}

    async def metrics(self, target, admin_key, tls=None, deadline=None) -> dict:
        md = [("x-ramen-admin-key", admin_key)]
        req = admin_pb2.MetricsRequest()
        r = await self._unary(target, tls, deadline, admin_pb2_grpc.AdminStub, "Metrics", req, md)
        return json.loads(r.json) if r.json else {}

    async def health(self, target, tls=None, deadline=None) -> bool:
        """True only when the node reports SERVING (runtime.load done); False on NOT_SERVING or any error."""
        req = health_pb2.HealthCheckRequest(service="")
        try:
            r = await self._unary(target, tls, deadline, health_pb2_grpc.HealthStub, "Check", req, [])
        except GrpcError:
            return False
        return r.status == health_pb2.HealthCheckResponse.SERVING


_default = Client()
call, reload, metrics, health = _default.call, _default.reload, _default.metrics, _default.health
