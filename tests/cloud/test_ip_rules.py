"""IP rules (CONTRACTS §11/§7): PUT cidrs → next deploy locks the node to those CIDRs (PERMISSION_DENIED outside),
restore → open again; on GCP the Cloud Armor policy `ramen-<group>` exists. Needs RAMEN_CONSOLE_URL, RAMEN_NODE_URL."""

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests import gcp

from .conftest import ENV, GROUP, ZONE, S, mcp_status, ok, poll

pytestmark = pytest.mark.cloud
LOCKED = ["192.0.2.0/24"]  # TEST-NET-1: never a real client
OPEN = ["0.0.0.0/0"]  # finding: console CIDR_RE rejects IPv6 (::/0) although the node accepts it


def _mcp(node, key) -> S:
    return mcp_status(node, key or "no-key")


def _apply(admin, cidrs):
    ok(admin.put("ip_rules", {"cidrs": cidrs}, group=GROUP, zone=ZONE))
    job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=False), 202).json()["id"])
    assert job["status"] == "ok", job


def test_invalid_cidr_rejected(admin, world):
    r = admin.put("ip_rules", {"cidrs": ["10.0.0.0/33", "not-a-cidr"]}, group=GROUP, zone=ZONE)
    assert r.status_code in (400, 422), r.text[:200]


def test_ip_rules_persist_in_worker_config(admin, world):
    ok(admin.put("ip_rules", {"cidrs": OPEN}, group=GROUP, zone=ZONE))
    cfg = ok(admin.get("workers", group=GROUP, zone=ZONE)).json()
    assert cfg.get("cidrs") == OPEN, cfg


def test_out_of_range_client_is_rejected_then_restored(admin, world, node_grpc, mcp_key_opt):
    """The node gates Admin/* by RAMEN_ADMIN_CIDRS, which the console derives from the same rules (finding carried from
    0.2.0), so the console's own address must stay allowed: RAMEN_TRUSTED_CIDRS = the console as the worker sees it
    (compose: `docker inspect ramen-console-1` → 172.18.0.4/32; GKE: the cluster pod range)."""
    trusted = [c.strip() for c in (E.env("RAMEN_TRUSTED_CIDRS") or "").split(",") if c.strip()]
    if not trusted:
        pytest.skip("RAMEN_TRUSTED_CIDRS not set: locking would also lock out the console's Admin/Reload (finding)")
    _apply(admin, OPEN)  # self-heal: a previous aborted run may have left the node locked
    before = poll(
        lambda: _mcp(node_grpc, mcp_key_opt) in (S.OK, S.UNAUTHENTICATED) and _mcp(node_grpc, mcp_key_opt),
        timeout=120,
        what="node reachable before locking",
    )
    try:
        _apply(admin, LOCKED + trusted)
        poll(lambda: _mcp(node_grpc, mcp_key_opt) == S.PERMISSION_DENIED, timeout=420, what="denied outside CIDRs")
        try:
            assert node_grpc.health() in ("SERVING", "NOT_SERVING"), "health probes must stay reachable"
        except grpc.RpcError as e:
            raise AssertionError(f"health must not be CIDR-gated: {e.code().name}") from e
    finally:
        _apply(admin, OPEN)
    poll(lambda: _mcp(node_grpc, mcp_key_opt) == before, timeout=420, what="node open again")


def test_ip_rules_audited(admin, world, admin_creds):
    from ramen_tests.console import items

    entries = items(ok(admin.audit()))
    assert any(e.get("action") == "ip_rules" and e.get("target") == f"{GROUP}/{ZONE}" and e.get("ok") for e in entries)


def test_cloud_armor_policy_exists_on_gcp(admin, world, gcp_project):
    ok(admin.put("ip_rules", {"cidrs": OPEN}, group=GROUP, zone=ZONE))
    pol = poll(
        lambda: gcp.get("compute", "security-policies", "describe", f"ramen-{GROUP}", project=gcp_project),
        timeout=60,
        what=f"security policy ramen-{GROUP}",
    )
    pol = pol[0] if isinstance(pol, list) else pol
    rules = pol.get("rules", [])
    allows = [r for r in rules if r.get("action") == "allow"]
    denies = [r for r in rules if str(r.get("action", "")).startswith("deny")]
    assert allows and denies, rules
    allowed = {ip for r in allows for ip in r.get("match", {}).get("config", {}).get("srcIpRanges", [])}
    assert allowed & {"0.0.0.0/0", "*", "::/0"}, allowed
