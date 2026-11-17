"""§35 load scenarios: opt-in with `RAMEN_LOAD=1` on top of the cloud env (RAMEN_CONSOLE_URL, RAMEN_NODE_URL,
RAMEN_ADMIN_EMAIL/PASSWORD, RAMEN_E2E_GROUP/ENV/ZONE, RAMEN_NODE_TLS/CA); everything skips otherwise. Each scenario
appends a markdown row; the table is printed at the end and appended to `RAMEN_LOAD_REPORT` when set.

Other env: `RAMEN_LOAD_FAST=1` (short runs, no idle wait: a dry run), `RAMEN_LOAD_IDLE_SECS` (scale-down wait, default
300; 4→1 takes ≥ 360 s with the default 120 s cooldown), `RAMEN_LOAD_KEY` / `RAMEN_MCP_KEY` (a group key the
workers already know — else one is minted and the environment redeployed, §8), `RAMEN_PHOENIX_URL` (+
`RAMEN_PHOENIX_PROJECT`, default `demo`) for the span delta."""

import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest

from ramen_tests import env as E
from ramen_tests import load as L
from ramen_tests import state
from ramen_tests.console import Console, items
from ramen_tests.localconsole import REDIRECT
from ramen_tests.mcp_client import HttpNode, Node

GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
ZONE = E.env("RAMEN_E2E_ZONE", "local")
FAST = E.env("RAMEN_LOAD_FAST") == "1"
IDLE_SECS = float(E.env("RAMEN_LOAD_IDLE_SECS", "300"))
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
ROWS: list[str] = []


def ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), (
        f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    )
    return r


def poll(fn, timeout=90, every=3, what="condition"):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"{what} not met after {timeout}s (last={last!r})")


@pytest.fixture(scope="session")  # session: set up before `console_url`, so the skip names RAMEN_LOAD
def load_enabled():
    if E.env("RAMEN_LOAD") != "1":
        pytest.skip("RAMEN_LOAD=1 not set (the live load scenarios are opt-in)")
    E.require("RAMEN_CONSOLE_URL")
    E.require("RAMEN_NODE_URL")


@pytest.fixture(scope="package")
def world(load_enabled, admin, console_url, suffix):
    """The deployed `GROUP/ENV` on `ZONE`: a group key the workers know, a person's OAuth token (viewer of the group,
    telemetry consent given), the public policy. Throwaways (user, OAuth client, a minted key) are removed after."""
    envs = items(ok(admin.get("environments_all", params={"group": GROUP})))
    e = next((x for x in envs if x["name"] == ENV), None)
    assert e and ZONE in (e.get("zones") or []), f"{GROUP}/{ENV} is not deployed on zone {ZONE} here: {envs}"
    key, minted = E.env("RAMEN_LOAD_KEY") or E.env("RAMEN_MCP_KEY") or state.MCP_KEY, None
    if not key:
        minted = ok(admin.create_mcp_key(GROUP, f"load-{suffix}"), 201).json()
        key = minted["key"]
        canary = E.env("RAMEN_LOAD_CANARY", "1") == "1"
        job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=canary), 202).json()["id"], timeout=900)
        assert job["status"] == "ok", {k: job.get(k) for k in ("status", "error")}
    email, pw = f"load-{suffix}@ramen.local", f"Ld-{uuid.uuid4().hex[:12]}-Aa1!"
    user = ok(
        admin.http.post("/api/v1/users", json={"email": email, "password": pw, "memberships": {GROUP: "viewer"}}), 201
    ).json()
    client = ok(
        admin.http.post("/api/v1/oauth/clients", json={"name": f"load-{suffix}", "redirect_uris": [REDIRECT]}), 201
    ).json()
    person = Console(console_url)
    assert person.login(email, pw).status_code in (200, 303)
    tokens = L.mint_token(person, client["client_id"], f"mcp:{GROUP}:{ZONE}", form={"telemetry_consent": "1"})
    w = {
        "key": key,
        "tokens": tokens,
        "minted_at": time.monotonic(),
        "client_id": client["client_id"],
        "policy": admin.policy(GROUP, ZONE),
        "person": person,
    }
    yield w
    person.close()
    admin.http.delete(f"/api/v1/users/{user['id']}")
    admin.http.delete(f"/api/v1/oauth/clients/{client['client_id']}")
    if minted:
        admin.delete("mcp_key", group=GROUP, id=minted["id"])  # forgotten by the workers at the next deploy


def token(world) -> str:
    """The person's access token, refreshed through the rotation grant when the run outlives 40 min."""
    if time.monotonic() - world["minted_at"] > 2400:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": world["tokens"]["refresh_token"],
            "client_id": world["client_id"],
        }
        r = ok(world["person"].http.post("/oauth/token", data=form))
        world["tokens"], world["minted_at"] = r.json(), time.monotonic()
    return world["tokens"]["access_token"]


@asynccontextmanager
async def edge(world, n: int):
    """`n` callers through the edge: alternating Streamable HTTP / gRPC and group key / OAuth token (§35: both
    transports, one key plus one token), one pooled HTTP client and one gRPC channel. Yields `(callers, label)`
    once the edge routes and every caller has made one call (consent given where a pod asks)."""
    http_key, http_tok = HttpNode.from_env(world["key"], group=GROUP, zone=ZONE), None
    poll(lambda: http_key.status(PING) in (200, 401, 403), timeout=600, every=10, what="edge route")
    http_tok = HttpNode.from_env(token(world), group=GROUP, zone=ZONE, policy=http_key.policy)
    grpc_key = Node.from_env(world["key"], group=GROUP, zone=ZONE, policy=http_key.policy)
    grpc_tok = Node.from_env(token(world), group=GROUP, zone=ZONE, policy=http_key.policy)
    client, channel = L.http_client(http_key, n + 8), L.aio_channel(grpc_key)
    callers: list = []
    for i in range(n):
        if i % 2 == 0:
            node = http_key if (i // 2) % 2 == 0 else http_tok
            callers.append(L.HttpCaller(node, node.key, client))
        else:
            node = grpc_key if (i // 2) % 2 == 0 else grpc_tok
            callers.append(L.GrpcCaller(node, node.key, channel))
    n_http = sum(1 for c in callers if c.transport == "http")
    proto = "h2" if L.http2_available() else "h1"
    label = f"{n} ({n_http} http/{proto}, {n - n_http} grpc; key + token)"
    try:
        bodies = L.Bodies(http_key.policy)
        warm = L.Stats()
        for _ in range(2):  # the pods behind the LB learn each credential's consent before the clock starts
            await L.run(callers, L.calls(lambda i, k: bodies.body("tools/call", CALC)), 0.01, warm)
        yield callers, label
    finally:
        await client.aclose()
        await channel.close()
        for node in (http_key, http_tok, grpc_key, grpc_tok):
            node.close()


CALC = {"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": "add"}}


@pytest.fixture(scope="package")
def report():
    class Report:
        def add(self, row: str) -> None:
            ROWS.append(row)
            print("\n" + L.HEADER + "\n" + row, flush=True)

    return Report()


def pytest_terminal_summary(terminalreporter):
    if not ROWS:
        return
    terminalreporter.section("load tests (CONTRACTS §35)")
    terminalreporter.write_line(L.HEADER)
    for row in ROWS:
        terminalreporter.write_line(row)
    out = E.env("RAMEN_LOAD_REPORT")
    if out:
        new = not os.path.exists(out) or os.path.getsize(out) == 0
        with open(out, "a") as f:
            if new:
                f.write(L.HEADER + "\n")
            f.writelines(row + "\n" for row in ROWS)
