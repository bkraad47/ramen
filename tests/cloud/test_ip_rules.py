"""IP rules (CONTRACTS §3/§7): PUT cidrs → next deploy locks the node to those CIDRs (403 outside), restore →
open again; on GCP the Cloud Armor policy `ramen-<group>` exists. Needs RAMEN_CONSOLE_URL (+RAMEN_NODE_URL)."""
import pytest

from ramen_tests import env as E
from ramen_tests import gcp
from ramen_tests.mcp_client import INIT_BODY

from .conftest import ENV, GROUP, ZONE, mcp_post, ok, poll

pytestmark = pytest.mark.cloud
LOCKED = ["192.0.2.0/24"]                  # TEST-NET-1: never a real client
OPEN = ["0.0.0.0/0"]                    # finding: console CIDR_RE rejects IPv6 (::/0) although the node accepts it


def _mcp(node_http, key):
    return mcp_post(node_http, INIT_BODY, key or "no-key").status_code


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


def test_out_of_range_client_is_rejected_then_restored(admin, world, node_http, mcp_key_opt):
    """The node gates /admin/reload by the same CIDRs (finding), so the console's own address must stay allowed:
    RAMEN_TRUSTED_CIDRS = the console as the worker sees it (compose: `docker inspect ramen-console-1` → 172.18.0.4/32;
    GKE: the cluster pod range)."""
    trusted = [c.strip() for c in (E.env("RAMEN_TRUSTED_CIDRS") or "").split(",") if c.strip()]
    if not trusted:
        pytest.skip("RAMEN_TRUSTED_CIDRS not set: locking would also lock out the console's /admin/reload (finding)")
    _apply(admin, OPEN)                    # self-heal: a previous aborted run may have left the node locked
    before = poll(lambda: _mcp(node_http, mcp_key_opt) in (200, 401) and _mcp(node_http, mcp_key_opt),
                  timeout=120, what="node reachable before locking")
    try:
        _apply(admin, LOCKED + trusted)
        poll(lambda: _mcp(node_http, mcp_key_opt) == 403, timeout=120, what="node returns 403 outside CIDRs")
        if E.node_admin(str(node_http.base_url)):
            assert node_http.get("/healthz").status_code == 200, "health probes must stay reachable"
    finally:
        _apply(admin, OPEN)
    poll(lambda: _mcp(node_http, mcp_key_opt) == before, timeout=120, what="node open again")


def test_ip_rules_audited(admin, world, admin_creds):
    from ramen_tests.console import items
    entries = items(ok(admin.audit()))
    assert any(e.get("action") == "ip_rules" and e.get("target") == f"{GROUP}/{ZONE}" and e.get("ok") for e in entries)


def test_cloud_armor_policy_exists_on_gcp(admin, world, gcp_project):
    ok(admin.put("ip_rules", {"cidrs": OPEN}, group=GROUP, zone=ZONE))
    pol = poll(lambda: gcp.get("compute", "security-policies", "describe", f"ramen-{GROUP}", project=gcp_project),
               timeout=60, what=f"security policy ramen-{GROUP}")
    rules = pol.get("rules", [])
    allows = [r for r in rules if r.get("action") == "allow"]
    denies = [r for r in rules if str(r.get("action", "")).startswith("deny")]
    assert allows and denies, rules
    allowed = {ip for r in allows for ip in r.get("match", {}).get("config", {}).get("srcIpRanges", [])}
    assert allowed & {"0.0.0.0/0", "*", "::/0"}, allowed
