"""MCP 2026-07-28 client (CONTRACTS §23) over both transports: per-request `_meta`, standard headers, no session.

`Modern(local.http)` / `Modern(local.node)` wrap the legacy clients so one test body runs on each transport. `raw()`
returns a `Reply` (HTTP status and headers, the JSON-RPC message, any SSE notifications before it) and never raises on
a JSON-RPC error; `request()` returns `result` or raises `JsonRpcError` (with `.status`). `listen()` holds a
`text/event-stream` POST open (subscriptions, §23.6)."""

from __future__ import annotations

import base64
import itertools
import json
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

from .mcp_client import CLIENT_INFO, UNSET, HttpNode, JsonRpcError, Node, sse_events, with_report

MODERN = "2026-07-28"
LEGACY = ["2025-06-18", "2025-03-26", "2024-11-05"]
NS = "io.modelcontextprotocol/"
PV, CI, CC = NS + "protocolVersion", NS + "clientInfo", NS + "clientCapabilities"
SERVER_INFO, SUB_ID, TASKS = NS + "serverInfo", NS + "subscriptionId", NS + "tasks"
ALL_INPUT = {"elicitation": {"form": {}, "url": {}}, "sampling": {}, "roots": {}}  # every MRTR input method declared
WITH_TASKS = {"extensions": {TASKS: {}}}
NAME_PARAM = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
    "tasks/get": "taskId",
    "tasks/update": "taskId",
    "tasks/cancel": "taskId",
}
_ids = itertools.count(5000)


def meta(capabilities: dict | None = None, version: str = MODERN, **extra) -> dict:
    """The per-request `_meta` of §23.1; `extra` adds keys such as `progressToken` or `traceparent`."""
    return {PV: version, CI: CLIENT_INFO, CC: {} if capabilities is None else capabilities, **extra}


def b64(value: str) -> str:
    """§23.2: the base64 sentinel for a header value that is not plain ASCII."""
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def standard_headers(method: str, params: dict | None, version: str = MODERN) -> dict[str, str]:
    h = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    key = NAME_PARAM.get(method)
    name = (params or {}).get(key) if key else None
    if isinstance(name, str):
        h["Mcp-Name"] = name if name.isascii() and name == name.strip() else b64(name)
    return h


@dataclass
class Reply:
    message: dict | None  # the JSON-RPC response (None: 202, or nothing parseable)
    status: int | None = None  # HTTP status; None on gRPC
    headers: dict = field(default_factory=dict)  # lower-cased
    notifications: list[dict] = field(default_factory=list)  # SSE events before the response
    sse: bool = False

    @property
    def result(self) -> dict | None:
        return (self.message or {}).get("result")

    @property
    def error(self) -> dict | None:
        return (self.message or {}).get("error")

    @property
    def code(self) -> int | None:
        return (self.error or {}).get("code")


class Modern:
    def __init__(self, node: Node | HttpNode, capabilities: dict | None = None, key=UNSET, telemetry=UNSET):
        """`telemetry`: the config version every gated call reports (§26.3; "" = whatever the worker was told,
        None = no report); defaults to the node's."""
        self.node, self.capabilities, self.key = node, capabilities, key
        self.telemetry = node.telemetry if telemetry is UNSET else telemetry
        self.transport = node.transport

    def with_caps(self, capabilities: dict | None) -> Modern:
        return Modern(self.node, capabilities, self.key, self.telemetry)

    def with_key(self, key: str) -> Modern:
        return Modern(self.node, self.capabilities, key, self.telemetry)

    def with_telemetry(self, version: str | None) -> Modern:
        return Modern(self.node, self.capabilities, self.key, version)

    def body(self, method, params=None, *, capabilities=UNSET, meta_extra=None, rid=UNSET, version=MODERN) -> dict:
        caps = self.capabilities if capabilities is UNSET else capabilities
        p = dict(with_report(method, params or {}, self.telemetry))
        p["_meta"] = {**meta(caps, version), **(meta_extra or {}), **p.get("_meta", {})}
        return {"jsonrpc": "2.0", "id": next(_ids) if rid is UNSET else rid, "method": method, "params": p}

    def http_headers(self, body: dict, headers: dict | None = None, accept: str | None = None) -> dict:
        """Transport + standard headers for `body`; `headers` overrides, a `None` value removes one."""
        node = self.node
        h = node.headers(self.key, session=False)
        h.pop("MCP-Protocol-Version", None)
        version = (body.get("params") or {}).get("_meta", {}).get(PV, MODERN)
        h.update(standard_headers(body.get("method", ""), body.get("params"), version))
        if accept:
            h["Accept"] = accept
        for k, v in (headers or {}).items():
            for existing in [x for x in h if x.lower() == k.lower()]:
                del h[existing]
            if v is not None:
                h[k] = v
        return h

    def send(self, body: dict | bytes, *, headers=None, accept=None, timeout: float | None = None) -> Reply:
        """One message exactly as given (`body` may lack `_meta`, or be raw bytes)."""
        node = self.node
        if self.transport == "grpc":
            raw = node.call_raw(body, self.key, timeout=timeout)
            return Reply(json.loads(raw) if raw else None)
        shaped = body if isinstance(body, dict) else {}
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        r = node._client.post(  # noqa: SLF001 - the modern era shares the legacy client's connection pool
            node.url, content=raw, headers=self.http_headers(shaped, headers, accept), timeout=timeout or node.timeout
        )
        hs = {k.lower(): v for k, v in r.headers.items()}
        if hs.get("content-type", "").startswith("text/event-stream"):
            events = sse_events(r.text)
            final = next((e for e in events if "id" in e and ("result" in e or "error" in e)), None)
            return Reply(final, r.status_code, hs, [e for e in events if "method" in e], sse=True)
        try:
            message = r.json() if r.content else None
        except ValueError:
            message = None
        return Reply(message, r.status_code, hs)

    def raw(self, method, params=None, *, headers=None, accept=None, timeout=None, **body_kw) -> Reply:
        return self.send(self.body(method, params, **body_kw), headers=headers, accept=accept, timeout=timeout)

    def request(self, method, params=None, **kw) -> dict:
        reply = self.raw(method, params, **kw)
        assert reply.message is not None, f"{method}: no JSON-RPC message (HTTP {reply.status})"
        if reply.error is not None:
            e = JsonRpcError(reply.error)
            e.status = reply.status
            raise e
        return reply.result

    # -- the methods tests spell most often ----------------------------------------------------------------------
    def call(self, name: str, arguments: dict | None = None, **extra_params) -> dict:
        kw = {k: extra_params.pop(k) for k in ("capabilities", "meta_extra", "headers", "accept", "timeout")
              if k in extra_params}  # fmt: skip
        return self.request("tools/call", {"name": name, "arguments": arguments or {}, **extra_params}, **kw)

    def call_raw(self, name: str, arguments: dict | None = None, **extra_params) -> Reply:
        kw = {k: extra_params.pop(k) for k in ("capabilities", "meta_extra", "headers", "accept", "timeout", "rid")
              if k in extra_params}  # fmt: skip
        return self.raw("tools/call", {"name": name, "arguments": arguments or {}, **extra_params}, **kw)

    def discover(self) -> dict:
        return self.request("server/discover")

    def task_get(self, task_id: str, **kw) -> dict:
        return self.request("tasks/get", {"taskId": task_id}, **kw)

    def wait_task(self, task_id: str, until=("completed", "failed", "cancelled"), timeout: float = 30, **kw) -> dict:
        import time

        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            last = self.task_get(task_id, **kw)
            if last.get("status") in until:
                return last
            time.sleep(0.2)
        raise AssertionError(f"task {task_id} never reached {until}: {last}")

    @contextmanager
    def listen(self, notifications: dict, *, capabilities=UNSET, rid=UNSET, timeout: float = 30):
        """`subscriptions/listen` held open → a `Stream` (`.status`, `.headers`, `.next(timeout)`), HTTP only."""
        assert self.transport == "http", "subscriptions are HTTP only (§23.6)"
        body = self.body("subscriptions/listen", {"notifications": notifications}, capabilities=capabilities, rid=rid)
        node = self.node
        client = httpx.Client(timeout=httpx.Timeout(timeout, read=None), verify=node.verify)
        with client.stream("POST", node.url, content=json.dumps(body), headers=self.http_headers(body)) as r:
            stream = Stream(r, body["id"])
            try:
                yield stream
            finally:
                stream.closed = True
                r.close()
        client.close()


class Stream:
    """An open SSE answer read on a thread, so a test can wait for the next event with a timeout."""

    def __init__(self, response: httpx.Response, rid):
        self.response, self.id, self.closed = response, rid, False
        self.status = response.status_code
        self.headers = {k.lower(): v for k, v in response.headers.items()}
        self.events: queue.Queue = queue.Queue()
        self.comments = 0
        self.body = b""
        if self.headers.get("content-type", "").startswith("text/event-stream"):
            threading.Thread(target=self._pump, daemon=True).start()
        else:
            self.body = response.read()

    def _pump(self):
        data: list[str] = []
        try:
            for line in self.response.iter_lines():
                if line.startswith(":"):
                    self.comments += 1
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and data:
                    self.events.put(json.loads("\n".join(data)))
                    data = []
        except (httpx.HTTPError, ValueError, RuntimeError):
            pass
        self.events.put(None)  # end of stream

    def next(self, timeout: float = 5) -> dict | None:
        """The next JSON event, `None` when the stream ended; raises `TimeoutError` when nothing came."""
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no SSE event within {timeout}s") from None

    def quiet(self, seconds: float) -> bool:
        """True when no event arrives for `seconds`."""
        try:
            self.events.get(timeout=seconds)
        except queue.Empty:
            return True
        return False

    def json(self) -> dict:
        return json.loads(self.body)


def envelope_of(result: dict) -> dict:
    """The modern-only members of a result (§23.1), for `assert_envelope`."""
    return {k: result[k] for k in ("resultType", "ttlMs", "cacheScope") if k in result}


def assert_envelope(result: dict, kind: str = "complete") -> None:
    assert result.get("resultType") == kind, result
    info = (result.get("_meta") or {}).get(SERVER_INFO)
    assert isinstance(info, dict) and info.get("name") == "ramen-node", result.get("_meta")
    assert isinstance(info.get("version"), str) and info["version"], info


def strip_envelope(result: dict) -> dict:
    """A modern result without what §23.1 adds: `resultType`, `ttlMs`, `cacheScope`, serverInfo `_meta`."""
    out = {k: v for k, v in result.items() if k not in ("resultType", "ttlMs", "cacheScope")}
    m = {k: v for k, v in (out.get("_meta") or {}).items() if k != SERVER_INFO}
    if m:
        out["_meta"] = m
    else:
        out.pop("_meta", None)
    return out
