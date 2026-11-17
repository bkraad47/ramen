"""Load-test helpers (CONTRACTS §35): latency statistics, the thresholds, the markdown row, and asyncio callers over
Streamable HTTP (httpx; HTTP/2 when `h2` is installed, else HTTP/1.1 pooled) and gRPC (`grpc.aio`) that carry the
policy `_meta` and a two-event telemetry report (§26.3) on every gated call. The pure parts are unit-tested in
tests/load/test_load_stats.py against `FakeCaller`; the live scenarios are tests/load/test_load_scenarios.py."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import grpc
import httpx

from ramen_proto import mcp_pb2, mcp_pb2_grpc

from . import env as E
from .mcp_client import (
    GATED,
    MAX_MESSAGE,
    TELEMETRY_META,
    HttpNode,
    Node,
    _insecure_root,
    sse_events,
    telemetry_event,
    telemetry_report,
    with_policy,
)
from .modern import ALL_INPUT, meta, standard_headers

CONSENT = "ramen_telemetry_consent"
ACCEPT = {CONSENT: {"action": "accept", "content": {"accept": True}}}
THROTTLED = {"429", "grpc:RESOURCE_EXHAUSTED"}  # expected under `burst`, never counted as an error
SERVER_FAILURES = {"grpc:UNAVAILABLE", "grpc:INTERNAL", "grpc:UNKNOWN"}  # the gRPC face of a 5xx
P95_BOUND_MS = 1500.0  # §35: p95 over the public edge on steady / mixed / scale
ERROR_RATE_BOUND = 0.005  # §35: < 0.5 % excluding expected 429s
HEADER = (
    "| scenario | clients | duration | requests | rps | errors (by code) | p50 / p95 / p99 ms | workers before→after "
    "| spans Δ | verdict |\n|---|---|---|---|---|---|---|---|---|---|"
)
_ids = itertools.count(100_000)


# -- statistics ------------------------------------------------------------------------------------------------------
def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile of `values` (any order); 0.0 when empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[k]


def is_5xx(code: str) -> bool:
    return (len(code) == 3 and code.startswith("5") and code.isdigit()) or code in SERVER_FAILURES


@dataclass
class Stats:
    """What one scenario records: a latency per message and an outcome code (`ok`, an HTTP status, `rpc<code>` for a
    JSON-RPC error inside a 200, `grpc:<STATUS>` for a transport status, `exc:<Error>` for a client-side failure)."""

    by_tag: dict[str, list[float]] = field(default_factory=dict)  # latencies per tag (the method, or a caller's label)
    codes: Counter = field(default_factory=Counter)
    started: float = 0.0
    finished: float = 0.0
    timings: dict[str, list[float]] = field(default_factory=dict)  # whole round trips (a task), not requests

    def record(self, ms: float, code: str, tag: str = "") -> None:
        self.by_tag.setdefault(tag, []).append(ms)
        self.codes[code] += 1

    def time(self, name: str, ms: float) -> None:
        self.timings.setdefault(name, []).append(ms)

    @property
    def latencies_ms(self) -> list[float]:
        return [ms for values in self.by_tag.values() for ms in values]

    def latencies(self, exclude: set[str] = frozenset()) -> list[float]:
        return [ms for tag, values in self.by_tag.items() if tag not in exclude for ms in values]

    def timings_text(self) -> str:
        """`<name> p50/p95 <a>/<b> ms (n)` per recorded round trip; "" when none."""
        return "; ".join(
            f"{name} p50/p95 {percentile(v, 50):.0f}/{percentile(v, 95):.0f} ms ({len(v)})"
            for name, v in sorted(self.timings.items())
        )

    @property
    def requests(self) -> int:
        return sum(self.codes.values())

    def errors(self, expected: set[str] = frozenset(THROTTLED)) -> int:
        return sum(n for c, n in self.codes.items() if c != "ok" and c not in expected)

    def error_rate(self) -> float:
        return self.errors() / self.requests if self.requests else 0.0

    def throttled(self) -> int:
        return sum(n for c, n in self.codes.items() if c in THROTTLED)

    def fives(self) -> int:
        return sum(n for c, n in self.codes.items() if is_5xx(c))

    def p(self, q: float, exclude: set[str] = frozenset()) -> float:
        """Latency percentile over the requests whose tag is not in `exclude` (a slow-by-design tool's tag)."""
        return percentile(self.latencies(exclude), q)

    def duration(self) -> float:
        return max(0.0, self.finished - self.started)

    def rps(self) -> float:
        d = self.duration()
        return self.requests / d if d else 0.0

    def errors_text(self) -> str:
        parts = [f"{c}:{n}" for c, n in sorted(self.codes.items()) if c != "ok"]
        return ", ".join(parts) or "none"


def verdict(
    scenario: str,
    stats: Stats,
    workers_before: int | None = None,
    workers_peak: int | None = None,
    workers_after: int | None = None,
    back_to_min: bool | None = None,
    exclude: set[str] = frozenset(),
    dry_run: bool = False,
) -> tuple[str, list[str]]:
    """`("PASS" | "FAIL", [what missed])` per §35: steady / mixed / scale — error rate < 0.5 % (429s excluded) and
    p95 < 1.5 s over the requests not tagged in `exclude` (a slow-by-design tool such as the demo's `slow_report` task
    is reported on its own, not judged); burst — 429s seen and no 5xx; scale — the count grew (≥ 2, above where it
    started) and, when the idle wait ran (`back_to_min` not None), it is back at 1. `dry_run` (RAMEN_LOAD_FAST on
    `scale`: too short for an autoscaler decision) judges errors only and answers `DRY RUN` instead of `PASS`."""
    misses: list[str] = []
    if stats.requests == 0:
        misses.append("no requests completed")
    if dry_run:
        if stats.errors():
            misses.append(f"{stats.errors()} errors ({stats.errors_text()})")
        return ("DRY RUN" if not misses else "FAIL"), misses
    if scenario in ("steady", "mixed", "scale"):
        if stats.error_rate() >= ERROR_RATE_BOUND:
            misses.append(f"error rate {stats.error_rate():.2%} ≥ 0.5 %")
        if stats.p(95, exclude) >= P95_BOUND_MS:
            what = f" (excluding {', '.join(sorted(exclude))})" if exclude else ""
            misses.append(f"p95 {stats.p(95, exclude):.0f} ms ≥ {P95_BOUND_MS:.0f}{what}")
    if scenario == "burst":
        if stats.fives():
            misses.append(f"{stats.fives()} 5xx under burst")
        if not stats.throttled():
            misses.append("no 429 / RESOURCE_EXHAUSTED seen (is the scope throttle live?)")
    if scenario == "scale":
        before = workers_before or 1
        if workers_peak is None or workers_peak < max(2, before + 1):
            misses.append(f"workers {before}→{workers_peak}: no scale-up")
        if back_to_min is False and (workers_after is None or workers_after > 1):  # the final read wins over the poll
            misses.append(f"not back to 1 after idle (count {workers_after})")
    return ("PASS" if not misses else "FAIL"), misses


def markdown_row(
    scenario: str,
    clients: str,
    stats: Stats,
    workers: str,
    spans: int | None,
    verdict_: str,
    notes: str = "",
    exclude: set[str] = frozenset(),
) -> str:
    """One table row; the latency cell is over the requests not tagged in `exclude`, and the round trips recorded with
    `Stats.time` (the task) are appended to the verdict cell."""
    extra = [n for n in (notes, stats.timings_text()) if n]
    cell = verdict_ if not extra else f"{verdict_} — {'; '.join(extra)}"
    lat = " / ".join(f"{stats.p(q, exclude):.0f}" for q in (50, 95, 99))
    return (
        f"| {scenario} | {clients} | {stats.duration():.0f} s | {stats.requests} | {stats.rps():.1f} "
        f"| {stats.errors_text()} | {lat} | {workers} | {'n/a' if spans is None else f'{spans:+d}'} | {cell} |"
    )


def burst_limit(expected_per_token: float, fast: bool = False, full_limit: int = 1000) -> int:
    """The scope throttle (per token per minute) a burst runs against: §35's 1000/min when the run will spend more than
    that per token (full mode), else half of what it will spend — never under 60 — so a short or slow run still trips
    the limit and proves 429s rather than nothing."""
    half = max(60, int(expected_per_token * 0.5))
    if not fast and expected_per_token > full_limit:
        return full_limit
    return half


def ramp_offsets(n0: int, n1: int, seconds: float) -> list[float]:
    """Start delays for `n1` clients: `n0` at once, the rest spread evenly over `seconds` (the §35 scale ramp)."""
    extra = max(0, n1 - n0)
    return [0.0] * min(n0, n1) + [k * seconds / extra for k in range(extra)]


def reasons_of(log_text: str) -> Counter:
    """`reason` counts of the access-log lines (`GET /api/v1/logs`, JSON per line; other lines ignored)."""
    out: Counter = Counter()
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("reason"):
            out[str(d["reason"])] += 1
    return out


def classify(status: int | None, message: dict | None) -> str:
    """The outcome code of one answer: HTTP `status` (None on gRPC) and the JSON-RPC message, if any."""
    if status is not None and status >= 300:
        return str(status)
    if isinstance(message, dict) and isinstance(message.get("error"), dict):
        return f"rpc{message['error'].get('code')}"
    return "ok"


# -- bodies ----------------------------------------------------------------------------------------------------------
def two_events() -> list[dict]:
    return [
        telemetry_event(kind="llm", name="load-model", ms=12, ok=True, reasoning="load test"),
        telemetry_event(kind="tool", name="local_grep", ms=3, ok=False),
    ]


class Bodies:
    """2026-07-28 request bodies: `_meta` with the protocol version and capabilities, every policy version the public
    policy marks required (§31.4a) and, on gated methods, a fresh two-event telemetry report (§26.3)."""

    def __init__(self, policy: dict | None, telemetry_version: str | None = None, capabilities: dict | None = None):
        self.policy = policy
        tel = (policy or {}).get("telemetry") if telemetry_version is None else None
        self.telemetry = telemetry_version if telemetry_version is not None else (tel or {}).get("version")
        self.capabilities = ALL_INPUT if capabilities is None else capabilities

    def body(self, method: str, params: dict | None = None, capabilities: dict | None = None) -> dict:
        p = dict(params or {})
        if method in GATED and self.telemetry is not None:
            m = dict(p.get("_meta") or {})
            m[TELEMETRY_META] = telemetry_report(self.telemetry, two_events())
            p["_meta"] = m
        p = dict(with_policy(method, p, self.policy) or p)
        caps = self.capabilities if capabilities is None else capabilities
        p["_meta"] = {**meta(caps), **p.get("_meta", {})}
        return {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": p}


# -- callers ---------------------------------------------------------------------------------------------------------
def http2_available() -> bool:
    return importlib.util.find_spec("h2") is not None


def http_client(node: HttpNode, connections: int) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    return httpx.AsyncClient(verify=node.verify, timeout=node.timeout, http2=http2_available(), limits=limits)


def aio_channel(node: Node) -> grpc.aio.Channel:
    """The `grpc.aio` twin of `Node.channel()` (same TLS anchor rules)."""
    opts = [("grpc.max_send_message_length", -1), ("grpc.max_receive_message_length", MAX_MESSAGE)]
    if not node.tls:
        return grpc.aio.insecure_channel(node.target, options=opts)
    root, override = node._ca, None  # noqa: SLF001 - the blocking client's anchor, reused
    if root is None and not E.tls_verify():
        root, override = _insecure_root(node.target)
    if override:
        opts.append(("grpc.ssl_target_name_override", override))
    return grpc.aio.secure_channel(node.target, grpc.ssl_channel_credentials(root), options=opts)


class HttpCaller:
    transport = "http"

    def __init__(self, node: HttpNode, key: str, client: httpx.AsyncClient):
        self.node, self.key, self.client = node, key, client

    async def send(self, body: dict) -> tuple[str, dict | None]:
        h = self.node.headers(self.key, session=False)
        h.pop("MCP-Protocol-Version", None)
        h.update(standard_headers(body["method"], body.get("params")))
        try:
            r = await self.client.post(self.node.url, content=json.dumps(body).encode(), headers=h)
        except httpx.HTTPError as e:
            return f"exc:{type(e).__name__}", None
        message = None
        if r.content:
            if r.headers.get("content-type", "").startswith("text/event-stream"):
                events = sse_events(r.text)
                message = next((e for e in events if e.get("id") == body["id"]), None)
            else:
                try:
                    message = r.json()
                except ValueError:
                    message = None
        return classify(r.status_code, message), message


class GrpcCaller:
    transport = "grpc"

    def __init__(self, node: Node, key: str, channel: grpc.aio.Channel):
        self.node, self.key, self.stub = node, key, mcp_pb2_grpc.McpStub(channel)

    async def send(self, body: dict) -> tuple[str, dict | None]:
        md = self.node.metadata(self.key)
        try:
            r = await self.stub.Call(
                mcp_pb2.JsonRpc(body=json.dumps(body).encode()), metadata=md, timeout=self.node.timeout
            )
        except grpc.aio.AioRpcError as e:
            return f"grpc:{e.code().name}", None
        message = json.loads(r.body) if r.body else None
        return classify(None, message if isinstance(message, dict) else None), message


class FakeCaller:
    """A scripted caller for the unit tests: `script` is a list of `(code, message)` answered in turn (cycled)."""

    transport = "fake"

    def __init__(self, script: list[tuple[str, dict | None]], delay: float = 0.0):
        self.script, self.delay, self.sent = list(script), delay, []

    async def send(self, body: dict) -> tuple[str, dict | None]:
        self.sent.append(body)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.script[(len(self.sent) - 1) % len(self.script)]


Caller = HttpCaller | GrpcCaller | FakeCaller
Step = Callable[[Caller, int, int, Stats], Awaitable[None]]


async def call(
    caller: Caller, body: dict, stats: Stats, consent: bool = True, tag: str | None = None
) -> tuple[str, dict | None]:
    """One message, its latency (under `tag`, default the method) and code recorded; the §24.3 consent prompt is
    accepted at once (recorded too)."""
    tag = body.get("method", "") if tag is None else tag
    t = time.perf_counter()
    code, msg = await caller.send(body)
    stats.record((time.perf_counter() - t) * 1000, code, tag)
    result = ((msg or {}).get("result") or {}) if isinstance(msg, dict) else {}
    if (
        consent
        and code == "ok"
        and result.get("resultType") == "input_required"
        and CONSENT in (result.get("inputRequests") or {})
    ):
        params = {**body["params"], "inputResponses": ACCEPT}
        if "requestState" in result:
            params["requestState"] = result["requestState"]
        again = {**body, "id": next(_ids), "params": params}
        t = time.perf_counter()
        code, msg = await caller.send(again)
        stats.record((time.perf_counter() - t) * 1000, code, tag)
    return code, msg


def calls(bodies: Callable[[int, int], dict]) -> Step:
    """A step that sends `bodies(client, iteration)` once."""

    async def step(caller: Caller, i: int, k: int, stats: Stats) -> None:
        await call(caller, bodies(i, k), stats)

    return step


async def run(
    callers: list[Caller], step: Step, seconds: float, stats: Stats, starts: list[float] | None = None
) -> Stats:
    """Every caller repeats `step` back to back until `seconds` elapse; `starts[i]` delays client `i` (the ramp)."""
    stats.started = time.monotonic()
    deadline = stats.started + seconds

    async def one(i: int, caller: Caller) -> None:
        if starts and i < len(starts) and starts[i]:
            await asyncio.sleep(starts[i])
        k = 0
        while time.monotonic() < deadline:
            await step(caller, i, k, stats)
            k += 1
            await asyncio.sleep(0)  # fairness: a caller that answers without suspending must not starve the rest

    await asyncio.gather(*(one(i, c) for i, c in enumerate(callers)))
    stats.finished = time.monotonic()
    return stats


# -- the console side ------------------------------------------------------------------------------------------------
def workers(console, group: str, zone: str) -> dict:
    """`GET /groups/{g}/zones/{z}/workers` → `{count, max_count, ready, decision}` (None fields on a console blip)."""
    try:
        r = console.get("workers", group=group, zone=zone)
        doc = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        doc = {}
    live = [w for w in (doc.get("live") or []) if w.get("load") != "down"]
    d = doc.get("last_autoscale") or {}
    decision = f"{d.get('action')} {d.get('from')}->{d.get('to')} {d.get('reason', '')}".strip() if d else ""
    return {"count": doc.get("count"), "max_count": doc.get("max_count"), "ready": len(live), "decision": decision}


def log_reasons(console, group: str, zone: str, tail: int = 2000) -> Counter | None:
    try:
        r = console.get("logs", params={"group": group, "zone": zone, "tail": tail})
    except httpx.HTTPError:
        return None
    return reasons_of(r.text) if r.status_code == 200 else None


def phoenix_span_count(url: str | None, project: str = "demo") -> int | None:
    """Spans in Phoenix project `project` (REST `/v1/projects` + paged `/v1/projects/{id}/spans`; GraphQL
    `recordCount` first when the server has it). None when `url` is unset or Phoenix does not answer."""
    if not url:
        return None
    base = url.rstrip("/")
    try:
        with httpx.Client(timeout=30, headers=E.phoenix_headers()) as c:  # D92: Phoenix's auth is on
            q = "{ projects { edges { node { id name recordCount } } } }"
            r = c.post(f"{base}/graphql", json={"query": q})
            if r.status_code == 200 and not (r.json().get("errors")):
                for e in r.json()["data"]["projects"]["edges"]:
                    if e["node"]["name"] == project:
                        return int(e["node"]["recordCount"])
            projects = c.get(f"{base}/v1/projects").json().get("data") or []
            pid = next((p["id"] for p in projects if p.get("name") == project), None)
            if pid is None:
                return 0
            total, cursor = 0, None
            for _ in range(200):  # 200 × 1000 spans: more than a test cluster's project holds
                params = {"limit": 1000, **({"cursor": cursor} if cursor else {})}
                page = c.get(f"{base}/v1/projects/{pid}/spans", params=params).json()
                total += len(page.get("data") or [])
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            return total
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def mint_token(console, client_id: str, scope: str, form: dict | None = None) -> dict:
    """Authorization code + PKCE as the signed-in `console` user → the `/oauth/token` answer (§16.3; configs accepted
    first, §31.1). The same flow as cloud/test_release_features.py::mint_as, kept here so `tests/load` needs no import
    from another suite."""
    from .localconsole import REDIRECT, pkce

    console.accept_configs(*scope.split(":")[1:3])
    verifier, challenge = pkce()
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": "load",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": scope,
    }
    page = console.http.get("/oauth/authorize", params=q, follow_redirects=False)
    assert page.status_code == 200 and "Allow" in page.text, f"consent page: {page.status_code} {page.text[:200]}"
    r = console.http.post(
        "/oauth/authorize",
        data={**q, "decision": "allow", "csrf_token": console.http.cookies.get("ramen_csrf", ""), **(form or {})},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:300]
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = console.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, f"/oauth/token {r.status_code}: {r.text[:300]}"
    return r.json()
