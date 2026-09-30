"""Fixtures for the kind multi-zone proofs (CONTRACTS §12.3, U20–U22).

The suite seeds its own stack through the console API — two zones, one group, one environment covering both zones —
and deploys it, so `make kind-up && make kind-test` is the whole story. Everything skips when
RAMEN_KIND_ZONES is unset, which is why the suite is inert in CI and against a cloud deployment.
"""

import json
import threading
import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests import kube, state
from ramen_tests.mcp_client import HttpError, HttpNode, Node

pytestmark = pytest.mark.kind


def _pairs(value: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        k, _, v = item.partition("=")
        out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="session")
def zone_targets() -> dict[str, str]:
    """`RAMEN_KIND_ZONES=a=localhost:18081,b=localhost:18082` → {zone: grpc target}. Two zones minimum (U20)."""
    zones = _pairs(E.require("RAMEN_KIND_ZONES"))
    if len(zones) < 2:
        pytest.skip("RAMEN_KIND_ZONES needs at least two zones")
    return zones


@pytest.fixture(scope="session")
def zone_regions() -> dict[str, str]:
    """`RAMEN_KIND_NODE_ZONES=a=kind-a,b=kind-b` → the zone record's `region`, i.e. the node label each zone pins to."""
    return _pairs(E.env("RAMEN_KIND_NODE_ZONES", ""))


@pytest.fixture(scope="session")
def stable_targets(zone_targets) -> dict[str, str]:
    """`RAMEN_KIND_STABLE_ZONES=a=localhost:18084,...` → the same zones, stable track only.

    A NodePort is an L4 hop: one long-lived HTTP/2 connection pins to one pod for its whole life, and the zone
    Service selects both tracks (CONTRACTS §7), so a generator aimed at the zone port can end up driving only
    `worker-canary` — which no HPA manages. Load is therefore aimed at the stable track. Falls back to the zone
    ports when the ports are not published (then the autoscale proof is best-effort)."""
    return _pairs(E.env("RAMEN_KIND_STABLE_ZONES", "")) or dict(zone_targets)


@pytest.fixture(scope="session")
def kube_ready():
    kube.require()
    return kube


@pytest.fixture(scope="session")
def group() -> str:
    return E.env("RAMEN_E2E_GROUP", "demo")


@pytest.fixture(scope="session")
def env_name() -> str:
    return E.env("RAMEN_E2E_ENV", "dev")


@pytest.fixture(scope="session")
def stack(admin, zone_targets, zone_regions, group, env_name, demo_repo, suffix) -> dict:
    """Zones + group + environment over every zone, deployed. Idempotent: 409s from an earlier run are fine."""
    for zone in zone_targets:
        r = admin.create_zone(zone, provider=E.env("RAMEN_ZONE_PROVIDER", "gcp"), region=zone_regions.get(zone, ""))
        assert r.status_code in (201, 409), f"zone {zone}: {r.status_code} {r.text[:300]}"
    r = admin.create_group(group, demo_repo)
    assert r.status_code in (201, 409), f"group {group}: {r.status_code} {r.text[:300]}"
    zones = sorted(zone_targets)
    r = admin.create_environment(group, env_name, zones)
    if r.status_code == 409:
        r = admin.put("environment", {"zones": zones}, group=group, env=env_name)
    assert r.status_code in (200, 201), f"environment: {r.status_code} {r.text[:300]}"

    # a fresh key per run: the raw value of an existing one cannot be read back, so a reused name would 409
    r = admin.create_mcp_key(group, f"kind-proofs-{suffix}")
    assert r.status_code == 201, f"mcp key: {r.status_code} {r.text[:300]}"
    key = r.json()["key"]
    state.MCP_KEY = key  # the e2e/conformance suites in the same run reuse it

    job = admin.wait_job(admin.deploy(group, env_name).json()["id"], timeout=900)
    assert job["status"] == "ok", f"deploy failed: {job.get('error')}\n" + "\n".join(job.get("log", []))
    return {"key": key, "group": group, "env": env_name, "zones": zones, "deploy_job": job}


@pytest.fixture(scope="session")
def nodes(stack, zone_targets, group) -> dict[str, Node]:
    """One authenticated gRPC client per zone, each carrying its own ramen-group/ramen-zone metadata (§11)."""
    admin_key = E.env("RAMEN_ADMIN_KEY")
    clients = {z: Node(t, stack["key"], group=group, zone=z, admin_key=admin_key) for z, t in zone_targets.items()}
    yield clients
    for c in clients.values():
        c.close()


# -- load generation ---------------------------------------------------------------------------------------------
class Load:
    """Concurrent `tools/call` load against one zone, over either transport (v0.5.5 I16 — HTTP and gRPC share
    the same port and guard functions, D32, so the same autoscale/rebalance proofs must hold for both, not
    just gRPC). RESOURCE_EXHAUSTED / HTTP 429 is a legitimate outcome (the node caps concurrency at
    RAMEN_MAX_INFLIGHT, §11) and is counted, not raised."""

    def __init__(
        self, target: str, key: str, group: str, zone: str, threads: int = 8, recycle: int = 10, transport: str = "grpc"
    ):
        self.target, self.key, self.group, self.zone, self.threads = target, key, group, zone, threads
        self.recycle = (
            recycle  # new channel/client every N calls, so new pods get their share of a round-robin NodePort
        )
        self.transport = transport
        self.ok = self.busy = self.failed = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []

    BODY = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "demo_calculator_tool", "arguments": {"var1": 7, "var2": 6, "func": "multiply"}},
    }

    def _client(self):
        if self.transport == "http":
            return HttpNode(E.node_http_url(self.target), self.key, group=self.group, zone=self.zone, timeout=20)
        return Node(self.target, self.key, group=self.group, zone=self.zone, timeout=20)

    def _run(self):
        while not self._stop.is_set():
            with self._client() as n:
                for _ in range(self.recycle):
                    if self._stop.is_set():
                        return
                    try:
                        got = "result" in json.loads(n.call_raw(dict(self.BODY)) or b"{}")
                        with self._lock:
                            self.ok += 1 if got else 0
                            self.failed += 0 if got else 1
                    except grpc.RpcError as e:
                        with self._lock:
                            if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                                self.busy += 1
                            else:
                                self.failed += 1
                        time.sleep(0.05)
                    except HttpError as e:
                        with self._lock:
                            if e.status_code == 429:
                                self.busy += 1
                            else:
                                self.failed += 1
                        time.sleep(0.05)

    def __enter__(self):
        self._workers = [threading.Thread(target=self._run, daemon=True) for _ in range(self.threads)]
        for t in self._workers:
            t.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        for t in self._workers:
            t.join(timeout=30)

    @property
    def total(self) -> int:
        return self.ok + self.busy + self.failed


@pytest.fixture
def load(stack, stable_targets, group):
    def make(zone: str, threads: int = 8, transport: str = "grpc") -> Load:
        return Load(stable_targets[zone], stack["key"], group, zone, threads, transport=transport)

    return make
