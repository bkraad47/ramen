"""CONTRACTS §24.1 (v1.0.0): the activator — `ramen-node` started with `RAMEN_ACTIVATOR=1` in front of a sleeping zone.

Real processes: the activator, a stdlib stand-in for the console's internal wake endpoint, and a real worker (node +
Python runtime) as the upstream, which the test starts only when the wake request arrives — exactly what the console
does when it scales the zone's Deployment back up. An unauthenticated call never wakes a zone; an accepted call is
held, the zone is woken once however many calls wait, and the call is forwarded unchanged and answered by the worker.
A zone that does not come up within `RAMEN_WAKE_TIMEOUT_SECS` answers 503 + `Retry-After: 10` / `UNAVAILABLE`."""

import json
import os
import secrets
import shutil
import subprocess
import threading
import time

import grpc
import httpx
import pytest

from ramen_tests import env as E
from ramen_tests.fakes import FakeConsole
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, text_of
from ramen_tests.modern import Modern, assert_envelope
from ramen_tests.sidecar import runtime_python

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
IGNORE = shutil.ignore_patterns("__pycache__")
KEY, KEY_B = ("rmk_" + secrets.token_hex(12) for _ in range(2))
ADMIN = secrets.token_hex(16)
ZONE_SECRET = secrets.token_urlsafe(32)
WAKE_PATH = "/api/v1/internal/zones/demo/local/wake"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
ACK = "notifications/subscriptions/acknowledged"
TICK_JSON = {
    "type": "tool",
    "name": "tick_tool",
    "description": "Report `steps` progress updates `gap_ms` apart, then answer.",
    "callable": "run",
    "input": {
        "steps": {"type": "integer", "description": "how many updates"},
        "gap_ms": {"type": "integer", "description": "milliseconds between updates"},
    },
}
TICK_PY = """import time

from ramen_runtime import progress, text


def run(steps: int, gap_ms: int):
    for n in range(1, int(steps) + 1):
        progress(n, total=int(steps), message=f"tick {n}")
        time.sleep(int(gap_ms) / 1000)
    return text(f"ticked {steps}")
"""


def make_bucket(root):
    """`mrtr_group` plus a tool whose progress updates are far enough apart to tell a stream from a buffer."""
    root.mkdir(parents=True, exist_ok=True)
    bucket = root / "bucket"
    shutil.copytree(E.FIXTURES / "mrtr_group", bucket, ignore=IGNORE)
    tool = bucket / "mcp" / "tools" / "tick_tool"
    tool.mkdir()
    (tool / "tick_tool.json").write_text(json.dumps(TICK_JSON))
    (tool / "tick_tool.py").write_text(TICK_PY)
    return bucket


class Zone:
    """One sleeping zone: the fake console, the activator, and the worker that `wake` starts (or never does)."""

    def __init__(self, root, wake: bool = True, wake_timeout: int = 60, env: dict | None = None):
        self.bucket = make_bucket(root)
        shared = {"RAMEN_SESSION_SECRET": ZONE_SECRET, "RAMEN_MCP_KEYS": f"{KEY},{KEY_B}"}
        self.worker = LocalNode(bucket=self.bucket, key=KEY, admin_key=ADMIN, env=shared)
        self.console = FakeConsole(on_wake=self.start_worker if wake else None)
        self.marker = root / "python-started"
        wrapper = root / "python-wrapper.sh"
        wrapper.write_text(f'#!/bin/sh\ntouch "{self.marker}"\nexec "{runtime_python()}" "$@"\n')
        wrapper.chmod(0o755)
        self.activator = LocalNode(
            bucket=self.bucket,
            key=KEY,
            admin_key=ADMIN,
            env={
                **shared,
                "RAMEN_ACTIVATOR": "1",
                "RAMEN_CONSOLE_INTERNAL_URL": self.console.url,
                "RAMEN_ACTIVATOR_UPSTREAM": self.worker.target,
                "RAMEN_WAKE_TIMEOUT_SECS": str(wake_timeout),
                "RAMEN_PYTHON": str(wrapper),
                **(env or {}),
            },
        )
        self._started = threading.Lock()
        self.worker_up = False

    def start_worker(self):
        with self._started:
            if not self.worker_up:
                self.worker.__enter__()
                self.worker_up = True

    def __enter__(self):
        self.console.__enter__()
        self.activator.__enter__()
        self.activator.wait_serving(30)
        return self

    def __exit__(self, *a):
        self.activator.__exit__(*a)
        with self._started:
            if self.worker_up:
                self.worker.__exit__(*a)
        self.console.__exit__(*a)

    def wake_now(self):
        """Bring the worker up the way an earlier call would have, and wait for it."""
        self.start_worker()
        self.worker.wait_serving()

    def mcp_lines(self, node: LocalNode) -> list[dict]:
        return [x for x in node.log_lines() if x.get("msg") == "mcp"]


@pytest.fixture
def zone(tmp_path):
    with Zone(tmp_path) as z:
        yield z


@pytest.fixture(scope="module")
def awake(tmp_path_factory):
    """A zone already woken by one call: the pass-through cases share it."""
    with Zone(tmp_path_factory.mktemp("awake")) as z:
        assert text_of(Modern(z.activator.http).call("plain_tool", {"text": "wake"}, timeout=90)) == "wake"
        yield z


@pytest.fixture(scope="module")
def stuck(tmp_path_factory):
    """A zone whose workers never come up: the console takes the wake request and nothing happens."""
    with Zone(tmp_path_factory.mktemp("stuck"), wake=False, wake_timeout=3) as z:
        yield z


# --- the activator itself ---------------------------------------------------------------------------------------------
def test_health_is_serving_with_no_worker_behind_it(zone):
    assert zone.activator.node.health() == "SERVING", "every edge keeps its health check green while the zone sleeps"
    assert zone.console.wakes == [], "a health check does not wake a zone"


def test_admin_metrics_say_asleep_and_reload_is_refused(zone):
    m = zone.activator.node.admin_metrics()
    assert m["asleep"] is True and m["last_call_at"] == 0 and m["woken"] == 0, m
    with pytest.raises(grpc.RpcError) as e:
        zone.activator.node.admin_reload(timeout=10)
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    with pytest.raises(grpc.RpcError) as e:
        zone.activator.node.admin_metrics(admin_key="wrong")
    assert e.value.code() in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED)
    assert zone.console.wakes == [], "Admin/* does not wake a zone"


@pytest.mark.skipif(os.name == "nt", reason="process tree read with pgrep")
def test_it_starts_no_python_runtime(zone):
    """Requests 10m / 32Mi: the activator is the node alone. Neither at start nor while it wakes and forwards."""
    assert text_of(Modern(zone.activator.http).call("plain_tool", {"text": "x"}, timeout=90)) == "x"
    children = subprocess.run(["pgrep", "-P", str(zone.activator.proc.pid)], capture_output=True, text=True)
    assert children.stdout.split() == [], f"the activator has child processes: {children.stdout}"
    assert not zone.marker.exists(), "RAMEN_PYTHON was started by the activator"
    assert subprocess.run(["pgrep", "-P", str(zone.worker.proc.pid)], capture_output=True, text=True).stdout.split(), (
        "(control) the real worker does run a runtime"
    )


# --- an unauthenticated call never wakes a zone -----------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_call_without_a_valid_credential_is_refused_and_does_not_wake(zone, transport):
    c = zone.activator.for_transport(transport)
    began = time.monotonic()
    assert c.outcome(PING, key=None) == "UNAUTHENTICATED"
    assert c.outcome(PING, key="rmk_" + secrets.token_hex(12)) == "UNAUTHENTICATED"
    assert c.outcome(PING, key="garbage") == "UNAUTHENTICATED"
    assert time.monotonic() - began < 5, "refused at once, not held"
    time.sleep(0.5)
    assert zone.console.requests == [], "the console was never asked"
    assert zone.activator.node.admin_metrics()["last_call_at"] == 0


def test_the_http_refusal_is_the_workers_own_401_challenge(zone):
    r = zone.activator.http.post(PING, key=None)
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers, (r.status_code, dict(r.headers))
    assert zone.console.wakes == []


def test_the_other_guards_hold_too(tmp_path):
    """Address and Origin are checked with the zone's own config before anything is woken."""
    with Zone(tmp_path / "cidr", env={"RAMEN_ALLOWED_CIDRS": "10.9.9.0/24"}) as z:
        assert z.activator.node.outcome(PING) == "PERMISSION_DENIED"
        assert z.activator.http.outcome(PING) == "PERMISSION_DENIED"
        assert z.console.wakes == []
    with Zone(tmp_path / "origin") as z:
        assert z.activator.http.status(PING, extra=[("Origin", "https://evil.example")]) == 403
        time.sleep(0.3)
        assert z.console.wakes == []


# --- an accepted call wakes, is held, forwarded and answered ---------------------------------------------------------
def test_an_http_call_is_held_until_the_worker_serves_and_is_answered_by_it(zone):
    began = time.time()
    result = Modern(zone.activator.http).call("plain_tool", {"text": "good morning"}, timeout=90)
    assert text_of(result) == "good morning"
    assert_envelope(result)
    wakes = zone.console.wakes
    assert len(wakes) == 1, [w["path"] for w in wakes]
    assert (wakes[0]["method"], wakes[0]["path"]) == ("POST", WAKE_PATH)
    assert wakes[0]["headers"].get("x-ramen-admin-key") == ADMIN, "the wake is authenticated with the zone admin key"
    assert KEY not in json.dumps(wakes[0]["headers"]) + wakes[0]["body"].decode(errors="replace"), (
        "the caller's credential is not sent to the console"
    )
    assert began <= wakes[0]["at"], "the wake follows the call"
    served = [x for x in zone.mcp_lines(zone.worker) if x.get("name") == "plain_tool"]
    assert len(served) == 1, "the worker served it, once"
    m = zone.activator.node.admin_metrics()
    assert m["asleep"] is True and m["woken"] == 1 and m["last_call_at"] >= int(began), m


def test_a_grpc_call_is_held_until_the_worker_serves_and_is_answered_by_it(zone):
    result = Modern(zone.activator.node).call("plain_tool", {"text": "good morning"}, timeout=90)
    assert text_of(result) == "good morning"
    wakes = zone.console.wakes
    assert len(wakes) == 1 and wakes[0]["path"] == WAKE_PATH, [w["path"] for w in wakes]
    assert wakes[0]["headers"].get("x-ramen-admin-key") == ADMIN
    assert [x.get("name") for x in zone.mcp_lines(zone.worker) if x.get("method") == "tools/call"] == ["plain_tool"]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_legacy_session_starts_through_the_activator(zone, transport):
    """`initialize` is an accepted call like any other: it wakes the zone, the worker mints the session."""
    c = zone.activator.for_transport(transport)
    c.timeout = 90
    assert c.initialize()["serverInfo"]["name"] == "ramen-node"
    assert text_of(c.call_tool("plain_tool", {"text": "legacy"})) == "legacy"
    if transport == "http":
        assert c.session_id, "the worker's Mcp-Session-Id header came back through the activator"
    assert len(zone.console.wakes) == 1


def test_concurrent_calls_share_one_wake_and_all_are_answered(zone):
    n = 8
    barrier, results, errors = threading.Barrier(n), {}, []

    def one(i: int):
        m = Modern(zone.activator.for_transport(TRANSPORTS[i % 2]))
        barrier.wait()
        try:
            results[i] = text_of(m.call("plain_tool", {"text": f"caller-{i}"}, timeout=90))
        except Exception as e:  # noqa: BLE001 - reported below with the caller's number
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == [], errors
    assert results == {i: f"caller-{i}" for i in range(n)}, "each caller got its own answer"
    assert len(zone.console.wakes) == 1, f"exactly one wake request, saw {len(zone.console.wakes)}"
    assert zone.activator.node.admin_metrics()["woken"] == 1


def test_calls_after_the_wake_are_forwarded_without_waking_again(awake):
    for transport in TRANSPORTS:
        m = Modern(awake.activator.for_transport(transport))
        assert text_of(m.call("plain_tool", {"text": transport})) == transport
    assert len(awake.console.wakes) == 1, "while both exist the activator forwards: the zone was woken once"


# --- forwarded unchanged ---------------------------------------------------------------------------------------------
def direct_and_through(awake, transport, method, params=None):
    return (
        Modern(awake.worker.for_transport(transport)).raw(method, params),
        Modern(awake.activator.for_transport(transport)).raw(method, params),
    )


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_answers_are_the_workers_answers(awake, transport):
    for method, params in (
        ("tools/list", None),
        ("server/discover", None),
        ("resources/list", None),
        ("tools/call", {"name": "no_such_tool", "arguments": {}}),
        ("tools/call", {"name": "ask_name", "arguments": {}}),
        ("nope/method", None),
    ):
        direct, through = direct_and_through(awake, transport, method, params)
        strip = lambda r: {k: v for k, v in (r.message or {}).items() if k != "id"}  # noqa: E731
        assert strip(through) == strip(direct), f"{method}: the activator changed the answer"
        assert through.status == direct.status, f"{method}: HTTP status {through.status} != {direct.status}"


def test_http_headers_reach_the_worker_and_its_headers_come_back(awake):
    trace_id = secrets.token_hex(16)
    parent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    r = Modern(awake.activator.http).raw("tools/list", headers={"traceparent": parent, "tracestate": "v=1"})
    assert r.status == 200 and r.headers.get("content-type", "").startswith("application/json")
    line = next((x for x in awake.mcp_lines(awake.worker) if trace_id in json.dumps(x.get("trace", {}))), None)
    assert line is not None, "the worker never saw the traceparent header"
    assert line["trace"]["traceparent"] == parent and line["trace"].get("tracestate") == "v=1"
    assert line.get("key_id") and line["key_id"] != "", "the credential went with it"


def test_grpc_metadata_and_body_reach_the_worker(awake):
    """Over gRPC the credential is metadata and trace context rides in `_meta` (§23.2): the worker sees both — it
    authenticates the caller itself, as the second key of the zone, and logs the trace of the forwarded bytes."""
    trace_id = secrets.token_hex(16)
    parent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    m = Modern(awake.activator.node, key=KEY_B)
    assert "tools" in m.request("tools/list", meta_extra={"traceparent": parent})
    line = next((x for x in awake.mcp_lines(awake.worker) if trace_id in json.dumps(x.get("trace", {}))), None)
    assert line is not None, "the body did not reach the worker as sent"
    mine = {x["key_id"] for x in awake.mcp_lines(awake.worker) if x.get("name") == "plain_tool"}
    assert line["transport"] == "grpc" and line["key_id"] and line["key_id"] not in mine, (line, mine)


def test_a_grpc_status_from_the_worker_is_relayed(awake):
    """A body that is not JSON is INVALID_ARGUMENT from a worker; through the activator it is the same code."""
    direct = awake.worker.node.status(b"{not json")
    assert awake.activator.node.status(b"{not json") == direct


def test_the_workers_http_errors_are_relayed(awake):
    r = awake.activator.http.get()
    assert r.status_code == awake.worker.http.get().status_code == 405


# --- streams pass through unbuffered ---------------------------------------------------------------------------------
def timed_events(m: Modern, body: dict) -> tuple[list[tuple[float, dict]], dict]:
    """POST `body`, read the SSE answer as it arrives → [(seconds since send, event)], response headers."""
    out, node = [], m.node
    began = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(30, read=30), verify=node.verify) as client:
        with client.stream("POST", node.url, content=json.dumps(body), headers=m.http_headers(body)) as r:
            headers = {k.lower(): v for k, v in r.headers.items()}
            data: list[str] = []
            for line in r.iter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and data:
                    out.append((time.monotonic() - began, json.loads("\n".join(data))))
                    data = []
    return out, headers


def test_an_sse_answer_arrives_event_by_event(awake):
    m = Modern(awake.activator.http)
    body = m.body("tools/call", {"name": "tick_tool", "arguments": {"steps": 4, "gap_ms": 500}})
    body["params"]["_meta"]["progressToken"] = "tick-1"
    events, headers = timed_events(m, body)
    assert headers.get("content-type", "").startswith("text/event-stream"), headers
    progress = [(t, e) for t, e in events if e.get("method") == "notifications/progress"]
    final = [(t, e) for t, e in events if "result" in e]
    assert [e["params"]["progress"] for _, e in progress] == [1, 2, 3, 4], events
    assert len(final) == 1 and text_of(final[0][1]["result"]) == "ticked 4"
    assert final[0][0] - progress[0][0] >= 1.2, (
        f"the first update came {final[0][0] - progress[0][0]:.2f}s before the answer — the tool spreads them over "
        "2 s, so the activator buffered the stream"
    )
    assert progress[1][0] - progress[0][0] >= 0.25, "updates arrive apart, as the worker sent them"


def test_a_subscription_stream_stays_open_through_the_activator(awake):
    m = Modern(awake.activator.http)
    with m.listen({"toolsListChanged": True}, rid=909) as s:
        assert s.status == 200 and s.headers["content-type"].startswith("text/event-stream"), (s.status, s.body)
        first = s.next(5)
        assert first is not None and first.get("method") == ACK, f"the ack must not wait for the stream to end: {first}"
        assert awake.worker.node.admin_metrics()["subscriptions"] == 1, "the stream is the worker's"
        shutil.copytree(
            E.FIXTURES / "slow_group" / "mcp" / "tools" / "slow_tool",
            awake.bucket / "mcp" / "tools" / "slow_tool",
            ignore=IGNORE,
        )
        assert awake.worker.node.admin_reload()["errors"] == []
        event = s.next(10)
        assert event is not None and event.get("method") == "notifications/tools/list_changed", event
    assert len(awake.console.wakes) == 1


# --- a zone that does not come up -------------------------------------------------------------------------------------
def test_http_gets_503_with_retry_after_when_the_zone_does_not_wake(stuck):
    began = time.monotonic()
    r = Modern(stuck.activator.http).raw("tools/list", timeout=30)
    took = time.monotonic() - began
    assert r.status == 503, (r.status, r.message)
    assert r.headers.get("retry-after") == "10", r.headers
    assert 2 <= took < 12, f"held for RAMEN_WAKE_TIMEOUT_SECS=3, took {took:.1f}s"
    assert KEY not in json.dumps(r.message)
    assert len(stuck.console.wakes) >= 1


def test_grpc_gets_unavailable_when_the_zone_does_not_wake(stuck):
    began = time.monotonic()
    with pytest.raises(grpc.RpcError) as e:
        stuck.activator.node.call_raw(Modern(stuck.activator.node).body("tools/list"), timeout=30)
    assert e.value.code() == grpc.StatusCode.UNAVAILABLE, e.value
    assert 2 <= time.monotonic() - began < 12


def test_the_log_says_waking_and_every_line_says_activator(stuck):
    Modern(stuck.activator.http).raw("tools/list", timeout=30)
    stuck.activator.http.post(PING, key=None)
    lines = stuck.mcp_lines(stuck.activator)
    assert lines, stuck.activator.output()
    assert all(x.get("activator") is True for x in lines), [x for x in lines if x.get("activator") is not True][:2]
    waking = [x for x in lines if x.get("reason") == "waking"]
    assert waking, f"no access-log line with reason waking: {lines[-3:]}"
    assert waking[-1].get("method") == "tools/list" and waking[-1].get("key_id"), waking[-1]
    text = stuck.activator.log.read_text() + stuck.activator.output()
    assert KEY not in text and ADMIN not in text, "no credential in the activator's log"


def test_a_later_call_can_still_wake_the_zone(tmp_path):
    """The failed wake is not remembered: when the workers do come up, the next call is forwarded."""
    with Zone(tmp_path, wake=False, wake_timeout=2) as z:
        assert Modern(z.activator.http).raw("tools/list", timeout=30).status == 503
        z.wake_now()
        assert text_of(Modern(z.activator.http).call("plain_tool", {"text": "now"})) == "now"


def test_an_unknown_error_from_the_worker_is_not_mistaken_for_waking(awake):
    with pytest.raises(JsonRpcError) as e:
        Modern(awake.activator.http).call("no_such_tool")
    assert getattr(e.value, "status", None) != 503
