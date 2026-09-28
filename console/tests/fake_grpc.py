"""In-process gRPC worker double (CONTRACTS §11): ramen.v1.Mcp, ramen.v1.Admin, grpc.health.v1.Health on 127.0.0.1."""

import json
from concurrent import futures

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ramen_console.proto.ramen_proto.ramen.v1 import admin_pb2, admin_pb2_grpc, mcp_pb2, mcp_pb2_grpc

DOWN = "127.0.0.1:1"  # nothing listens here: UNAVAILABLE fast


class FakeWorker:
    """Records every call as (rpc, metadata dict, body); behaviour is driven by the public attributes."""

    def __init__(self, admin_key="adm", mcp_keys=("rmk_1",), serving=True):
        self.admin_key = admin_key
        self.mcp_keys = set(mcp_keys) if mcp_keys is not None else None  # None = any bearer key is accepted
        self.calls: list[tuple[str, dict, dict]] = []
        self.smoke_ok, self.reload_ok = True, True
        self.forbid: list[str] = []  # strings that must never show up in metadata (secret values)
        self.load_result = {"tools": [{"name": "calc"}], "errors": []}
        self.metrics = {"inflight": 2, "total": 9, "errors": 0, "load": "even"}
        self.load_fn = None  # (n-th metrics call) -> load string
        self._n = 0
        self._health = health.HealthServicer()
        self.set_serving(serving)
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        mcp_pb2_grpc.add_McpServicer_to_server(_Mcp(self), self._server)
        admin_pb2_grpc.add_AdminServicer_to_server(_Admin(self), self._server)
        health_pb2_grpc.add_HealthServicer_to_server(self._health, self._server)
        port = self._server.add_insecure_port("127.0.0.1:0")
        self.target = f"127.0.0.1:{port}"

    def set_serving(self, serving: bool):
        st = health_pb2.HealthCheckResponse.SERVING if serving else health_pb2.HealthCheckResponse.NOT_SERVING
        self._health.set("", st)

    def start(self):
        self._server.start()
        return self

    def stop(self):
        self._server.stop(None)

    def paths(self):
        return [c[0] for c in self.calls]

    def resolve(self, target):  # every pod IP the adapters compute lands on this server
        return DOWN if target.startswith("down") else self.target


def _md(ctx):
    return {k: v for k, v in ctx.invocation_metadata()}


class _Mcp(mcp_pb2_grpc.McpServicer):
    def __init__(self, w):
        self.w = w

    def Call(self, request, context):
        md, body = _md(context), json.loads(request.body or b"{}")
        self.w.calls.append(("Mcp/Call", md, body))
        auth = md.get("authorization", "")
        assert not any(f in v for f in self.w.forbid for v in md.values()), "secret value leaked into metadata"
        if not auth.startswith("Bearer ") or (self.w.mcp_keys is not None and auth[7:] not in self.w.mcp_keys):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or unknown key")
        if not self.w.smoke_ok:
            context.abort(grpc.StatusCode.INTERNAL, "boom")
        if "id" not in body:
            return mcp_pb2.JsonRpc(body=b"")
        result = {"tools": []} if body.get("method") == "tools/list" else {"ok": True}
        return mcp_pb2.JsonRpc(body=json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode())


class _Admin(admin_pb2_grpc.AdminServicer):
    def __init__(self, w):
        self.w = w

    def _auth(self, context, rpc):
        md = _md(context)
        self.w.calls.append((rpc, md, {}))
        if md.get("x-ramen-admin-key") != self.w.admin_key:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad admin key")

    def Reload(self, request, context):
        self._auth(context, "Admin/Reload")
        if not self.w.reload_ok:
            context.abort(grpc.StatusCode.INTERNAL, "pip failed")
        return admin_pb2.LoadResult(json=json.dumps(self.w.load_result).encode())

    def Metrics(self, request, context):
        self._auth(context, "Admin/Metrics")
        m = dict(self.w.metrics)
        if self.w.load_fn:
            m["load"] = self.w.load_fn(self.w._n)
        self.w._n += 1
        return admin_pb2.MetricsReply(json=json.dumps(m).encode())
