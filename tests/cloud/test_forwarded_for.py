"""The `x-forwarded-for` hop count, measured against a real load balancer instead of assumed.

`RAMEN_TRUST_PROXY_HOPS=n` makes the node treat the n-th `x-forwarded-for` entry **counted from the right** as the
client address (node-rs `auth::client_ip`). The worker chart and both renderers ship **2 for GCP** and **1 for AWS**,
on the belief that the external load balancer appends `<client>, <lb>`. If that is wrong the address allowlist denies
legitimate traffic — it fails closed, so it is an availability bug, not an exposure — and the number is asserted in
the chart helper, both renderers and the documentation, so it is worth measuring rather than believing.

Method, and every part of it was learned the hard way on a live Gateway:
  * scale `worker-canary` to 0 first. The LB's NEG contains **both** tracks, so a call can otherwise land on a pod
    this suite never reconfigured, and the answer looks random.
  * read the access log of the **one** pod that is serving. Reading `-l app=worker` during a rollout mixes the old
    pod's lines with the new pod's and produces contradictory results.
  * restore the hop count by setting it back explicitly. `kubectl set env … RAMEN_TRUST_PROXY_HOPS-` *deletes* the
    renderer's variable rather than restoring it, which silently turns proxy trust off.

Needs, all of them, or it skips:
  RAMEN_XFF_PROBE=1            deliberate opt-in: it reconfigures and rolls a deployed worker several times
  RAMEN_CONSOLE_URL            the console
  RAMEN_NODE_URL + node TLS    the load balancer (`<ip>:443`, RAMEN_NODE_TLS=1)
  RAMEN_MCP_KEY                or a key minted by e2e earlier in the same run
  RAMEN_KUBE_CONTEXT           kubectl context for the cluster (gcloud container clusters get-credentials)
  RAMEN_E2E_GROUP / _ZONE      which worker to reconfigure
"""

import json
import subprocess
import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.console import Console
from ramen_tests.mcp_client import Node

pytestmark = pytest.mark.cloud
S = grpc.StatusCode
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
CANDIDATES = (1, 2, 3)
SHIPPED = {"gcp": 2, "aws": 1}
SETTLE = 20.0  # the LB keeps sending to a removed endpoint for a few seconds after a rollout


@pytest.fixture(scope="module")
def probe():
    if E.env("RAMEN_XFF_PROBE") != "1":
        pytest.skip("RAMEN_XFF_PROBE=1 not set (this suite reconfigures and rolls a deployed worker)")
    group, zone = E.env("RAMEN_E2E_GROUP", "demo"), E.env("RAMEN_E2E_ZONE", "local")
    p = {
        "ctx": E.require("RAMEN_KUBE_CONTEXT"),
        "ns": f"ramen-{group}-{zone}",
        "group": group,
        "zone": zone,
        "shipped": SHIPPED.get(E.env("RAMEN_ZONE_PROVIDER", "gcp"), 2),
    }
    kubectl(p, "scale", "deployment/worker-canary", "--replicas=0")
    kubectl(p, "rollout", "status", "deployment/worker-canary", "--timeout=300s")
    time.sleep(SETTLE)
    yield p
    set_hops(p, p["shipped"])  # never `-`: that would delete the renderer's variable
    kubectl(p, "scale", "deployment/worker-canary", "--replicas=1")


def kubectl(probe, *args, timeout=420) -> str:
    r = subprocess.run(
        ["kubectl", "--context", probe["ctx"], "-n", probe["ns"], *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert r.returncode == 0, f"kubectl {' '.join(args)}: {r.stderr.strip()[:400]}"
    return r.stdout


def set_hops(probe, n: int) -> None:
    kubectl(probe, "set", "env", "deployment/worker", f"RAMEN_TRUST_PROXY_HOPS={n}")
    kubectl(probe, "rollout", "status", "deployment/worker", "--timeout=400s")
    time.sleep(SETTLE)


def serving_pod(probe) -> str:
    items = json.loads(kubectl(probe, "get", "pods", "-l", "app=worker,ramen.io/track=stable", "-o", "json"))["items"]
    live = [p for p in items if not p["metadata"].get("deletionTimestamp") and p["status"].get("phase") == "Running"]
    assert len(live) == 1, f"expected one serving stable pod, got {[p['metadata']['name'] for p in live]}"
    return live[0]["metadata"]["name"]


def logged_ips(probe, pod: str, want: int) -> list[str]:
    for _ in range(12):
        lines = [
            json.loads(line)
            for line in kubectl(probe, "logs", pod, "--tail=200").splitlines()
            if line.startswith("{") and '"msg":"mcp"' in line
        ]
        if len(lines) >= want:
            return [e["ip"] for e in lines[-want:]]
        time.sleep(5)
    return [e["ip"] for e in lines]


def call(zone: str, group: str, extra=None) -> grpc.StatusCode:
    target, tls = E.node_target(E.require("RAMEN_NODE_URL"))
    with Node(target, E.env("RAMEN_MCP_KEY"), group=group, zone=zone, tls=tls, ca=E.env("RAMEN_NODE_CA")) as n:
        return n.status(PING, extra=extra)


@pytest.fixture(scope="module")
def measured(probe, mcp_key) -> dict:
    """{hop count: the addresses the node checked}. Printed verbatim: it is the evidence the docs cite."""
    seen = {}
    for n in CANDIDATES:
        set_hops(probe, n)
        pod = serving_pod(probe)
        codes = [call(probe["zone"], probe["group"]).name for _ in range(3)]
        assert set(codes) == {"OK"}, f"hops={n}: {codes}"
        seen[n] = logged_ips(probe, pod, 3)
        print(f"\nx-forwarded-for probe: HOPS={n} -> node checked {seen[n]}")
    return seen


def test_the_shipped_hop_count_resolves_one_stable_client_address(measured, probe):
    """At the shipped count the node must resolve the *same* address for every call: that is the client.

    A hop count past the end of the header falls back to the peer, which on a cloud load balancer is a front end
    and therefore differs from call to call — that is what makes the two cases distinguishable without having to
    know this machine's public address, which behind a NAT pool is not knowable from the outside.
    """
    shipped = probe["shipped"]
    ips = measured[shipped]
    assert len(set(ips)) == 1, (
        f"at the shipped hop count {shipped} the node checked {ips} — three different addresses means it is reading "
        "past the end of the header and falling back to the peer. Measured positions: "
        f"{json.dumps(measured)}. Correct the chart helper, both renderers and the documentation."
    )
    stable = sorted(n for n, v in measured.items() if len(set(v)) == 1)
    want = sorted(n for n in CANDIDATES if n <= shipped)
    assert stable == want, (
        f"positions with a stable address were {stable}, expected {want} — "
        f"the header is not `<client>, <lb>`: {json.dumps(measured)}"
    )


def test_the_allowlist_admits_exactly_that_address(probe, measured):
    """The address from the shipped position is the one the allowlist has to work with, end to end."""
    client = measured[probe["shipped"]][0]
    set_hops(probe, probe["shipped"])
    c = Console(E.require("RAMEN_CONSOLE_URL"), timeout=600)  # ip-rules rolls the workers and attaches Cloud Armor
    r = c.login(E.env("RAMEN_ADMIN_EMAIL", "admin@ramen.local"), E.require("RAMEN_ADMIN_PASSWORD"))
    assert r.status_code in (200, 303), r.status_code
    try:
        r = c.put("ip_rules", {"cidrs": [f"{client}/32"]}, group=probe["group"], zone=probe["zone"])
        assert r.status_code == 200, r.text[:300]
        time.sleep(SETTLE)
        assert call(probe["zone"], probe["group"]) == S.OK, f"the allowlist denied {client}, the address it was set to"

        r = c.put("ip_rules", {"cidrs": ["198.51.100.0/24"]}, group=probe["group"], zone=probe["zone"])
        assert r.status_code == 200, r.text[:300]
        time.sleep(SETTLE)
        assert call(probe["zone"], probe["group"]) == S.PERMISSION_DENIED, "a range we are not in still allowed us"

        for spoof in ("198.51.100.5", f"198.51.100.5, {client}", "198.51.100.5, 203.0.113.1, 203.0.113.9"):
            code = call(probe["zone"], probe["group"], extra=[("x-forwarded-for", spoof)])
            assert code == S.PERMISSION_DENIED, f"x-forwarded-for {spoof!r} was trusted: {code.name}"
    finally:
        c.put("ip_rules", {"cidrs": []}, group=probe["group"], zone=probe["zone"])
        c.close()


def test_reflection_is_off_through_the_load_balancer(probe, mcp_key):
    """deploy/helm/ramen-worker and both renderers set RAMEN_REFLECTION=0; prove it on the deployed worker."""
    target, tls = E.node_target(E.require("RAMEN_NODE_URL"))
    with Node(target, mcp_key, group=probe["group"], zone=probe["zone"], tls=tls, ca=E.env("RAMEN_NODE_CA")) as n:
        with pytest.raises(grpc.RpcError) as e:
            n.reflect()
    assert e.value.code() in (S.UNIMPLEMENTED, S.NOT_FOUND), e.value.code()
