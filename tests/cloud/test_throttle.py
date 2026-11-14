"""Scope throttling through Redis on a live deployment (wiki/throttling.md; brief v1.0.0b item 7 "redis and
throttling on gcp"): `PUT /groups/{g}/throttle {redis_url, token_per_min}` reaches the workers at the next deploy, a
burst on one key is answered 429 over HTTP and RESOURCE_EXHAUSTED over gRPC, every zone of the group counts against
the same Redis, and the access log names `throttled`. The limits are put back and the environment redeployed.

Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL and RAMEN_REDIS_URL (a Redis the workers can reach, e.g.
`redis://redis.ramen-system.svc.cluster.local:6379` from the wiki's manifest; skipped otherwise)."""

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.console import items
from ramen_tests.mcp_client import HttpNode, Node

from .conftest import ENV, GROUP, ZONE, ok, poll

pytestmark = pytest.mark.cloud
LIMIT = 10  # per token per minute: the deploy's own smoke runs tools/list on every pod with the group key (3+ a minute)
CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": "add"}},
}


def redeploy(admin):
    job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=True), 202).json()["id"], timeout=900)
    assert job["status"] == "ok", {k: job.get(k) for k in ("status", "error")}


def fresh_key(admin, suffix, tag) -> str:
    return ok(admin.create_mcp_key(GROUP, f"thr-{tag}-{suffix}"), 201).json()["key"]


def burst_http(key: str, n: int = LIMIT + 4) -> list[int]:
    with HttpNode.from_env(key, group=GROUP, zone=ZONE) as h:
        poll(lambda: h.status(CALL) in (200, 429), timeout=600, every=10, what="edge route")
        return [h.status(CALL) for _ in range(n)]


@pytest.fixture(scope="module")
def throttled(admin, world, node_grpc, suffix):
    redis_url = E.env("RAMEN_REDIS_URL")
    if not redis_url:
        pytest.skip("RAMEN_REDIS_URL not set (a Redis reachable from the workers)")
    before = ok(admin.get("group", group=GROUP)).json()
    limits = {k: before.get(f"scope_{k}") or 0 for k in ("ip_per_min", "token_per_min")}
    ok(
        admin.http.put(
            f"/api/v1/groups/{GROUP}/throttle", json={"redis_url": redis_url, "ip_per_min": 0, "token_per_min": LIMIT}
        )
    )
    redeploy(admin)
    yield {"redis_url": redis_url}
    ok(admin.http.put(f"/api/v1/groups/{GROUP}/throttle", json=limits))  # the URL stays: write-only, kept as it was
    redeploy(admin)
    assert 429 not in burst_http(fresh_key(admin, suffix, "after")), "the limit is gone after the redeploy"


def test_the_throttle_is_audited_and_never_shows_the_url(admin, throttled):
    entries = items(ok(admin.audit()))
    assert any(e.get("action") == "throttle.scope" and f"group:{GROUP}" in e.get("tags", []) for e in entries)
    g = ok(admin.get("group", group=GROUP)).json()
    assert g.get("scope_token_per_min") == LIMIT and "redis_scope_url" not in g, sorted(g)
    assert throttled["redis_url"] not in ok(admin.audit()).text


def test_a_burst_on_one_key_is_throttled_over_http(admin, throttled, suffix):
    codes = burst_http(fresh_key(admin, suffix, "http"))
    assert codes.count(200) <= LIMIT and 429 in codes, codes
    assert all(c in (200, 429) for c in codes), codes


def test_the_same_limit_answers_resource_exhausted_over_grpc(admin, throttled, suffix):
    key = fresh_key(admin, suffix, "grpc")
    with Node.from_env(key, group=GROUP, zone=ZONE) as n:
        codes = [n.status(CALL) for _ in range(LIMIT + 4)]
    assert codes.count(grpc.StatusCode.OK) <= LIMIT and grpc.StatusCode.RESOURCE_EXHAUSTED in codes, codes


def test_the_throttle_counts_across_the_groups_zones(admin, throttled, suffix):
    """One Redis for the group: a key spent on `ZONE` is throttled on every other zone of the environment too."""
    envs = items(ok(admin.get("environments_all", params={"group": GROUP})))
    others = [z for z in next(e for e in envs if e["name"] == ENV)["zones"] if z != ZONE]
    if not others:
        pytest.skip(f"{GROUP}/{ENV} has one zone; attach another to prove the shared counter")
    key = fresh_key(admin, suffix, "zones")
    assert 429 in burst_http(key)
    with HttpNode.from_env(key, group=GROUP, zone=others[0]) as h:
        assert h.status(CALL) == 429, f"zone {others[0]} does not share the counter"


def test_the_access_log_names_the_reason(admin, throttled, suffix):
    key = fresh_key(admin, suffix, "log")
    assert 429 in burst_http(key)
    r = poll(
        lambda: (
            (x := admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 400})).status_code == 200
            and "throttled" in x.text
            and x
        ),
        timeout=120,
        every=5,
        what="a `throttled` access-log line",
    )
    assert key not in r.text
