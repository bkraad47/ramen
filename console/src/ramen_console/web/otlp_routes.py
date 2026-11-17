"""§31.4 OTLP ingest: `POST /otlp/v1/{traces,metrics,logs}` (OTLP/HTTP, protobuf or JSON, gzip, ≤ 4 MiB) for every
signed-in LLM tool. The bearer is resolved to a group and a person (or group key), the resource attributes are
stamped, the body is forwarded once to `config/otel.endpoint` + `/v1/<signal>` with Ramen's headers. Nothing is
stored; bodies and tokens never reach a log. Per-token rate limit (in memory) and one audit line per AUDIT_EVERY
requests per token."""

import base64
import hashlib
import hmac
import json
import threading
import time
import zlib

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from .. import otel, tls
from ..audit import note
from ..errors import ApiError
from ..keyid import key_id
from ..oauth_server import token_key
from ..rbac import can_connect
from .helpers import svc

r = APIRouter(prefix="/otlp/v1")
MAX_BODY = 4 * 1024 * 1024
RATE, WINDOW = 60, 60.0
AUDIT_EVERY = 1000
TIMEOUT = 10.0
PROTO = "application/x-protobuf"
SIGNALS = {
    "traces": (ExportTraceServiceRequest, "resource_spans", "resourceSpans"),
    "metrics": (ExportMetricsServiceRequest, "resource_metrics", "resourceMetrics"),
    "logs": (ExportLogsServiceRequest, "resource_logs", "resourceLogs"),
}
_lock = threading.Lock()
_hits: dict[str, tuple[float, int]] = {}
_totals: dict[str, int] = {}


def reset() -> None:
    with _lock:
        _hits.clear()
        _totals.clear()


def _count(key: str) -> tuple[int, int]:
    """(requests in the current window, requests ever) for one token hash; memory bounded under a flood."""
    now = time.monotonic()
    with _lock:
        start, n = _hits.get(key, (now, 0))
        if now - start > WINDOW:
            start, n = now, 0
        _hits[key] = (start, n + 1)
        _totals[key] = _totals.get(key, 0) + 1
        if len(_hits) > 10000:
            for k in [k for k, v in _hits.items() if now - v[0] > WINDOW]:
                _hits.pop(k, None)
                _totals.pop(k, None)
        return n + 1, _totals[key]


def _jwt(token: str) -> dict | None:
    """The claims of a console-issued access token (HS256 with the group's derived key), None otherwise."""
    try:
        h, p, s = token.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
        claims = json.loads(base64.urlsafe_b64decode(pad(p)))
        return {"claims": claims, "signed": f"{h}.{p}".encode(), "sig": base64.urlsafe_b64decode(pad(s))}
    except (ValueError, TypeError):
        return None


async def resolve(request: Request) -> dict:
    """Who sends: `{"group", "user", "zone", "key"}` — `user` is the email or `key:<name>`, `key` the token hash the
    limits are kept under. Order: `Authorization: Bearer` (OAuth access token → `rmk_` group key → console session
    token), else the cookie session / API-key header the middleware resolved; a session names its group in
    `ramen-group` (header or query). 401 unknown, 403 a group the person cannot connect to."""
    st, s = request.app.state, svc(request)
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    key = hashlib.sha256(bearer.encode()).hexdigest()[:32] if bearer else None
    if bearer and (j := _jwt(bearer)) is not None:
        c = j["claims"]
        group = str(c.get("group") or "")
        if not group or not await s.store.get("groups", group) or int(c.get("exp") or 0) < time.time():
            raise ApiError(401, "token is unknown or expired")
        want = hmac.new(token_key(await s.session_secret(group)), j["signed"], hashlib.sha256).digest()
        if not hmac.compare_digest(want, j["sig"]):
            raise ApiError(401, "token signature does not verify")
        return {"group": group, "user": str(c.get("email") or c.get("sub") or "-"), "zone": c.get("zone"), "key": key}
    if bearer.startswith("rmk_"):
        kid = key_id(bearer)
        for doc in await s.store.list("secrets", {"kind": "mcp_key", "key_id": kid}):
            val = await s.secrets_backend.resolve(doc.get("ref") or doc.get("value"))
            if val and hmac.compare_digest(val, bearer):
                return {"group": doc["group"], "user": f"key:{doc['name']}", "zone": doc.get("zone"), "key": key}
        raise ApiError(401, "unknown MCP key")
    p = request.state.principal
    if bearer:
        p = await st.accounts.principal_for_session(st.signer.load(bearer))
    if p is None or p.kind != "user":
        raise ApiError(401, "a user token, console session or MCP key is required")
    group = request.headers.get("ramen-group") or request.query_params.get("group") or ""
    if not group:
        raise ApiError(401, "name the group in the ramen-group header")
    if not await s.store.get("groups", group) or not can_connect(p, group):
        raise ApiError(403, f"no access to group {group}")
    sess = key or hashlib.sha256(f"session:{p.id}".encode()).hexdigest()[:32]
    return {"group": group, "user": p.name, "zone": None, "key": sess}


def stamps(who: dict) -> dict[str, str]:
    out = {
        "ramen.group": who["group"],
        "ramen.user": who["user"],
        "openinference.project.name": who["group"],
        "service.namespace": f"ramen/{who['group']}",
    }
    if who.get("zone"):
        out["ramen.zone"] = who["zone"]
    return out


def stamp_json(body: bytes, signal: str, attrs: dict[str, str]) -> bytes:
    try:
        doc = json.loads(body)
    except ValueError:
        raise ValueError("body is not JSON") from None
    if not isinstance(doc, dict):
        raise ValueError("body is not an OTLP/JSON export request")
    _, snake, camel = SIGNALS[signal]
    items = doc.get(camel, doc.get(snake)) or []
    if not isinstance(items, list):
        raise ValueError(f"{camel} must be a list")
    for it in items:
        if isinstance(it, dict):
            if not isinstance(it.get("resource"), dict):
                it["resource"] = {}
            res = it["resource"]
            kept = [a for a in (res.get("attributes") or []) if not (isinstance(a, dict) and a.get("key") in attrs)]
            res["attributes"] = kept + [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]
    return json.dumps(doc, separators=(",", ":")).encode()


def stamp_proto(body: bytes, signal: str, attrs: dict[str, str]) -> bytes:
    cls, field, _ = SIGNALS[signal]
    msg = cls()
    try:
        msg.ParseFromString(body)
    except Exception:  # noqa: BLE001 - DecodeError and friends: the body is not this message
        raise ValueError("body is not an OTLP protobuf export request") from None
    for it in getattr(msg, field):
        keep = [kv for kv in it.resource.attributes if kv.key not in attrs]
        del it.resource.attributes[:]
        it.resource.attributes.extend(keep)
        for k, v in attrs.items():
            kv = it.resource.attributes.add()
            kv.key, kv.value.string_value = k, v
    return msg.SerializeToString()


async def _body(request: Request) -> bytes:
    """The raw body, read in chunks and refused the moment it passes MAX_BODY — a chunked upload without a
    Content-Length is never buffered whole (§33.4)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise ApiError(400, "bad Content-Length") from None
    if declared > MAX_BODY:
        raise ApiError(413, "body over 4 MiB")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise ApiError(413, "body over 4 MiB")
    raw = bytes(buf)
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = d.decompress(raw, MAX_BODY + 1)
        except zlib.error:
            raise ApiError(400, "body is not gzip") from None
        if len(raw) > MAX_BODY or d.unconsumed_tail:
            raise ApiError(413, "body over 4 MiB once decompressed")
    return raw


@r.post("/{signal}")
async def ingest(request: Request, signal: str):
    if signal not in SIGNALS:
        raise ApiError(404, "signal must be traces, metrics or logs")
    who = await resolve(request)
    n, total = _count(who["key"])
    if n > RATE:
        return JSONResponse({"detail": "Too many requests for this token"}, 429, headers={"Retry-After": "60"})
    if total % AUDIT_EVERY == 1 or AUDIT_EVERY == 1:
        tags = [f"group:{who['group']}", f"signal:{signal}", f"count:{total}"]
        if who.get("zone"):
            tags.append(f"zone:{who['zone']}")
        note(request, "telemetry.ingest", who["group"], tags, user=who["user"])
    cfg = await otel.get_config(svc(request).store)
    if not cfg["enabled"] or not cfg["endpoint"]:
        return Response(status_code=204)
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in (PROTO, "application/protobuf", "application/json"):
        raise ApiError(415, "send application/x-protobuf or application/json")
    body = await _body(request)
    try:
        out = (stamp_json if ctype == "application/json" else stamp_proto)(body, signal, stamps(who))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    ctype = "application/json" if ctype == "application/json" else PROTO
    try:
        async with httpx.AsyncClient(verify=tls.client_verify(), transport=otel.TRANSPORT, timeout=TIMEOUT) as http:
            up = await http.post(
                f"{cfg['endpoint']}/v1/{signal}", content=out, headers={**cfg["headers"], "Content-Type": ctype}
            )
    except httpx.HTTPError as e:
        return JSONResponse({"detail": f"collector unreachable: {type(e).__name__}", "upstream_status": None}, 502)
    if up.status_code >= 400:
        return JSONResponse(
            {"detail": f"collector answered HTTP {up.status_code}", "upstream_status": up.status_code}, 502
        )
    return Response(up.content, up.status_code, media_type=up.headers.get("content-type", ctype))
