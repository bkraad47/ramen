"""gRPC client for worker nodes (CONTRACTS §11): `Mcp/Call`, `Admin/Reload`, `Admin/Metrics`, `Health/Check`.

One channel per call (targets are pod IPs that come and go), 10 s deadline, 4 MiB messages. `tls` is None/False for
h2c, True for system roots, or `{"ca": pem, "cert": pem, "key": pem, "name": server name}` — D86: `ca` may be a callable
(the install CA's bundle, read at dial time) and `name` the name verified on a pod IP. D92 (§39.2a): `name` may be a
callable `(group, zone) -> name` — each call then names its `peer=(group, zone)` and the worker must hold that zone's
leaf; `legacy` (a callable returning a name or None) is the one second try with the pre-D92 generic name while the
upgrade's transition window is open (§39.5b).
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
    if callable(v):
        v = v()
    if v is None or isinstance(v, bytes):
        return v or None
    v = str(v)
    if not v.strip():
        return None
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
    def __init__(self, tls=None, deadline: float = DEADLINE, resolve=None, admin_key: str | None = None):
        self.tls, self.deadline, self.resolve = tls, deadline, resolve
        # §24.3: the console's own `Mcp/Call`s (golden cases) present the worker admin key next to the group key, so a
        # group that requires telemetry consent does not ask the console for it. `Services` sets it from the adapter.
        self.admin_key = admin_key

    def channel(self, target: str, tls=None, peer=None, name=None) -> grpc.aio.Channel:
        target = self.resolve(target) if self.resolve else target
        tls = self.tls if tls is None else tls
        creds = credentials(tls)
        if creds is None:
            return grpc.aio.insecure_channel(target, options=OPTIONS)
        name = name or _name(tls, peer)
        options = [*OPTIONS, ("grpc.ssl_target_name_override", name)] if name else OPTIONS
        return grpc.aio.secure_channel(target, creds, options=options)

    async def _once(self, target, tls, deadline, stub_cls, method, request, metadata, peer, name=None):
        async with self.channel(target, tls, peer, name) as ch:
            try:
                return await getattr(stub_cls(ch), method)(
                    request, metadata=tuple(metadata), timeout=deadline or self.deadline
                )
            except grpc.aio.AioRpcError as e:
                raise GrpcError(e.code(), e.details() or "", target) from None

    async def _unary(self, target, tls, deadline, stub_cls, method, request, metadata, peer=None):
        args = (target, tls, deadline, stub_cls, method, request, metadata, peer)
        try:
            return await self._once(*args)
        except GrpcError as e:
            # §39.5b: a pod still on its pre-D92 leaf fails the handshake (UNAVAILABLE, nothing was sent); inside the
            # transition window only, the generic name gets one more try
            legacy = _legacy(self.tls if tls is None else tls, peer)
            if e.code != grpc.StatusCode.UNAVAILABLE or not legacy:
                raise
            return await self._once(*args, name=legacy)

    async def call(self, target, key, group, zone, jsonrpc: dict, tls=None, deadline=None, admin_key=None) -> dict:
        """One JSON-RPC message in, its response out ({} for a notification)."""
        md = [("authorization", f"Bearer {key}"), ("ramen-group", group), ("ramen-zone", zone)]
        if admin := admin_key or self.admin_key:
            md.append(("x-ramen-admin-key", admin))
        req = mcp_pb2.JsonRpc(body=json.dumps(jsonrpc).encode())
        r = await self._unary(target, tls, deadline, mcp_pb2_grpc.McpStub, "Call", req, md, (group, zone))
        return json.loads(r.body) if r.body else {}

    async def reload(self, target, admin_key, tls=None, deadline=None, peer=None, revoked=None) -> dict:
        """`revoked` (D92, §16.7): the `RAMEN_REVOKED` JSON the node merges at once, before any restart."""
        md = [("x-ramen-admin-key", admin_key)]
        req = admin_pb2.ReloadRequest(revoked=(revoked or "").encode())
        r = await self._unary(target, tls, deadline, admin_pb2_grpc.AdminStub, "Reload", req, md, peer)
        return json.loads(r.json) if r.json else {}

    async def metrics(self, target, admin_key, tls=None, deadline=None, peer=None) -> dict:
        md = [("x-ramen-admin-key", admin_key)]
        req = admin_pb2.MetricsRequest()
        r = await self._unary(target, tls, deadline, admin_pb2_grpc.AdminStub, "Metrics", req, md, peer)
        return json.loads(r.json) if r.json else {}

    async def health(self, target, tls=None, deadline=None, peer=None) -> bool:
        """True only when the node reports SERVING (runtime.load done); False on NOT_SERVING or any error."""
        req = health_pb2.HealthCheckRequest(service="")
        try:
            r = await self._unary(target, tls, deadline, health_pb2_grpc.HealthStub, "Check", req, [], peer)
        except GrpcError:
            return False
        return r.status == health_pb2.HealthCheckResponse.SERVING


def _name(tls, peer) -> str | None:
    name = tls.get("name") if isinstance(tls, dict) else None
    if not callable(name):
        return name
    if not peer:  # fail closed: a zone-derived name without the zone would verify nothing in particular
        raise ValueError("D92: the worker's group and zone are needed to verify its TLS name (peer=(group, zone))")
    return name(*peer)


def _legacy(tls, peer) -> str | None:
    if not (isinstance(tls, dict) and peer and callable(tls.get("name")) and callable(tls.get("legacy"))):
        return None
    return tls["legacy"]()


_default = Client()
call, reload, metrics, health = _default.call, _default.reload, _default.metrics, _default.health
