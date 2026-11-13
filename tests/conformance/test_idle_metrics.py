"""CONTRACTS §24.1 (v1.0.0): what the console reads to decide a zone is idle — `Admin/Metrics` on a real worker.

`last_call_at` (epoch seconds, 0 = never) moves on an authenticated MCP call and on nothing else: health checks,
reflection, `Admin/*` and denied calls do not keep a zone awake. `inflight`, `tasks` and `subscriptions` are the other
three numbers of the idle rule: a running task or an open `subscriptions/listen` stream shows in them."""

import secrets
import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import ALL_INPUT, WITH_TASKS, Modern

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
KEY = "rmk_" + secrets.token_hex(12)
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture
def node():
    """A fresh worker per test: `last_call_at` starts at 0."""
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", key=KEY) as n:
        n.wait_serving()
        yield n


def last(node) -> float:
    m = node.node.admin_metrics()
    assert "last_call_at" in m, f"Admin/Metrics has no last_call_at: {sorted(m)}"
    value = m["last_call_at"]
    assert isinstance(value, (int, float)) and not isinstance(value, bool), value
    return value


def test_a_worker_that_was_never_called_reports_zero(node):
    assert last(node) == 0
    m = node.node.admin_metrics()
    assert (m["inflight"], m["tasks"], m["subscriptions"]) == (0, 0, 0), m
    assert "asleep" not in m or m["asleep"] is False, "only an activator says asleep"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_an_accepted_call_moves_it_to_now(node, transport):
    before = time.time()
    assert text_of(Modern(node.for_transport(transport)).call("plain_tool", {"text": "hi"})) == "hi"
    at = last(node)
    assert int(before) <= at <= time.time() + 1, f"epoch seconds of the call: {at} vs {before}"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_legacy_call_counts_too(node, transport):
    c = node.for_transport(transport)
    c.initialize()
    assert last(node) > 0, "initialize is an authenticated MCP call"


def test_it_moves_forward_with_each_call(node):
    m = Modern(node.http)
    m.call("plain_tool", {"text": "a"})
    first = last(node)
    time.sleep(1.2)  # whole seconds may be all the node keeps
    m.request("tools/list")
    assert last(node) > first, "a list is an authenticated MCP call"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_denied_call_does_not_move_it(node, transport):
    c = node.for_transport(transport)
    assert c.outcome(PING, key=None) == "UNAUTHENTICATED"
    assert c.outcome(PING, key="rmk_" + secrets.token_hex(12)) == "UNAUTHENTICATED"
    assert c.outcome(PING, key="not a key") == "UNAUTHENTICATED"
    assert last(node) == 0, "an unauthenticated call must never keep a zone awake"


def test_a_denied_call_does_not_refresh_an_older_stamp(node):
    Modern(node.http).call("plain_tool", {"text": "a"})
    stamp = last(node)
    time.sleep(1.2)
    assert node.http.outcome(PING, key=None) == "UNAUTHENTICATED"
    assert node.node.outcome(PING, key="wrong") == "UNAUTHENTICATED"
    assert last(node) == stamp


def test_health_reflection_and_admin_do_not_move_it(node):
    for _ in range(3):
        assert node.node.health() == "SERVING"
    try:
        node.node.reflect()
    except grpc.RpcError:
        pass  # reflection may be off: either way it is not an MCP call
    for _ in range(3):
        node.node.admin_metrics()
    assert node.node.admin_reload()["errors"] == []
    assert last(node) == 0


def test_a_refused_admin_call_does_not_move_it(node):
    with pytest.raises(grpc.RpcError):
        node.node.admin_metrics(admin_key="wrong")
    assert last(node) == 0


def test_an_origin_or_address_refusal_does_not_move_it():
    """The guards before the credential: a caller outside the allow-list is denied before it could count."""
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", key=KEY, env={"RAMEN_ALLOWED_CIDRS": "10.9.9.0/24"}) as n:
        n.wait_serving()
        assert n.node.outcome(PING) == "PERMISSION_DENIED"
        assert n.http.outcome(PING) == "PERMISSION_DENIED"
        assert last(n) == 0


def test_a_running_task_shows_in_tasks(node):
    m = Modern(node.http, {**ALL_INPUT, **WITH_TASKS})
    created = m.call("slow_job", {"seconds": 2, "label": "idle"})
    assert created["resultType"] == "task", created
    assert node.node.admin_metrics()["tasks"] == 1, "a zone with a running task is not idle"
    m.wait_task(created["taskId"])
    deadline = time.monotonic() + 5
    while node.node.admin_metrics()["tasks"] and time.monotonic() < deadline:
        time.sleep(0.2)
    assert node.node.admin_metrics()["tasks"] == 0


def test_an_open_subscription_shows_in_subscriptions(node):
    m = Modern(node.http)
    with m.listen({"toolsListChanged": True}) as s:
        assert s.next(5) is not None
        assert node.node.admin_metrics()["subscriptions"] == 1, "a zone with a listener is not idle"
    deadline = time.monotonic() + 5
    while node.node.admin_metrics()["subscriptions"] and time.monotonic() < deadline:
        time.sleep(0.2)
    assert node.node.admin_metrics()["subscriptions"] == 0


def test_a_call_in_flight_shows_in_inflight():
    import threading

    with LocalNode(bucket=E.FIXTURES / "slow_group", key=KEY) as n:
        n.wait_serving()
        t = threading.Thread(target=lambda: n.http_client().call_tool("slow_tool", {"ms": 1500}))
        t.start()
        try:
            deadline, seen = time.monotonic() + 3, 0
            while time.monotonic() < deadline and not seen:
                seen = n.node.admin_metrics()["inflight"]
                time.sleep(0.05)
            assert seen == 1
        finally:
            t.join()
        assert last(n) > 0
