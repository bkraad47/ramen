"""Stdlib stand-ins for what a node talks to in CONTRACTS §24: the console's internal wake endpoint (§24.1) and an
OTLP/HTTP collector (§24.3). Both record every request so a test can assert what was sent — and what never was."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server:
    """A ThreadingHTTPServer on a free loopback port, served from a daemon thread."""

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _any(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = outer.handle(self.command, self.path, dict(self.headers.items()), body)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_POST = do_GET = do_PUT = _any  # noqa: N815 - http.server's naming

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.lock = threading.Lock()

    def handle(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        raise NotImplementedError

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


class FakeConsole(_Server):
    """Records every request; `POST /api/v1/internal/zones/{g}/{z}/wake` runs `on_wake()` (on its own thread, so the
    answer is not held) and answers `status`. `.wakes` = the wake requests as `{path, headers, body, at}`."""

    def __init__(self, on_wake=None, status: int = 200):
        super().__init__()
        self.on_wake, self.status = on_wake, status
        self.requests: list[dict] = []

    def handle(self, method, path, headers, body):
        seen = {"method": method, "path": path, "headers": {k.lower(): v for k, v in headers.items()}, "body": body}
        seen["at"] = time.time()
        with self.lock:
            self.requests.append(seen)
        if method == "POST" and path.startswith("/api/v1/internal/zones/") and path.endswith("/wake"):
            if self.on_wake:
                threading.Thread(target=self.on_wake, daemon=True).start()
            return self.status, {"ok": self.status < 300, "state": "waking"}
        return 404, {"detail": "not found"}

    @property
    def wakes(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "POST" and r["path"].endswith("/wake")]

    def wait_wake(self, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if w := self.wakes:
                return w[0]
            time.sleep(0.05)
        raise AssertionError(f"no wake request within {timeout}s; saw {[r['path'] for r in self.requests]}")


def attrs(obj: dict) -> dict:
    """OTLP/JSON `attributes: [{key, value: {stringValue|intValue|boolValue|…}}]` → a plain dict."""
    out = {}
    for a in obj.get("attributes") or []:
        v = a.get("value") or {}
        out[a["key"]] = next(iter(v.values()), None)
    return out


class Collector(_Server):
    """OTLP/HTTP receiver: keeps the raw body and headers of every `/v1/traces` and `/v1/metrics` POST."""

    def __init__(self, status: int = 200):
        super().__init__()
        self.status = status
        self.posts: list[dict] = []  # {path, headers, body: bytes}

    def handle(self, method, path, headers, body):
        with self.lock:
            self.posts.append({"path": path, "headers": {k.lower(): v for k, v in headers.items()}, "body": body})
        return self.status, {}

    def bodies(self, path: str | None = None) -> list[bytes]:
        with self.lock:
            return [p["body"] for p in self.posts if path is None or p["path"] == path]

    def raw(self) -> str:
        """Everything the node ever sent here, bodies and headers, as one text — for "X appears nowhere" checks."""
        with self.lock:
            return "\n".join(json.dumps(p["headers"]) + "\n" + p["body"].decode(errors="replace") for p in self.posts)

    def _json(self, path: str) -> list[dict]:
        out = []
        for raw in self.bodies(path):
            try:
                out.append(json.loads(raw))
            except ValueError as e:
                raise AssertionError(f"{path}: not OTLP/HTTP JSON ({e}): {raw[:200]!r}") from None
        return out

    def spans(self) -> list[dict]:
        """Every span received, each with `resource` (dict of resource attributes) and `attrs` (dict) added."""
        out = []
        for doc in self._json("/v1/traces"):
            for rs in doc.get("resourceSpans") or []:
                resource = attrs(rs.get("resource") or {})
                for ss in rs.get("scopeSpans") or []:
                    for s in ss.get("spans") or []:
                        out.append({**s, "resource": resource, "attrs": attrs(s)})
        return out

    def metric_points(self, name: str) -> list[dict]:
        """Data points of the sum metric `name`: `{attrs, value, resource}` (cumulative or delta, as sent)."""
        out = []
        for doc in self._json("/v1/metrics"):
            for rm in doc.get("resourceMetrics") or []:
                resource = attrs(rm.get("resource") or {})
                for sm in rm.get("scopeMetrics") or []:
                    for m in sm.get("metrics") or []:
                        if m.get("name") != name:
                            continue
                        for p in (m.get("sum") or {}).get("dataPoints") or []:
                            value = p.get("asInt", p.get("asDouble", 0))
                            out.append({"attrs": attrs(p), "value": float(value), "resource": resource})
        return out

    def wait_spans(self, pred, n: int = 1, timeout: float = 15) -> list[dict]:
        deadline, got = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            got = [s for s in self.spans() if pred(s)]
            if len(got) >= n:
                return got
            time.sleep(0.2)
        names = [(s.get("name"), s["attrs"].get("mcp.name")) for s in self.spans()]
        raise AssertionError(f"wanted {n} matching span(s) within {timeout}s, got {len(got)}; all spans: {names}")

    def wait_points(self, name: str, pred=lambda p: True, timeout: float = 20) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if got := [p for p in self.metric_points(name) if pred(p)]:
                return got
            time.sleep(0.3)
        raise AssertionError(
            f"no data point of {name} within {timeout}s; /v1/metrics posts: {len(self.bodies('/v1/metrics'))}"
        )
