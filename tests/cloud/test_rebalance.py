"""Rebalance (CONTRACTS §4a/§7): POST → 200 {ok} + audit record; on GCP the zone's NEG backend has a capacity
scaler of 0.5 (high) or 1.0. Needs RAMEN_CONSOLE_URL; RAMEN_GCP_PROJECT for the compute check."""

import pytest

from ramen_tests import gcp
from ramen_tests.console import items

from .conftest import GROUP, ZONE, ok

pytestmark = pytest.mark.cloud


@pytest.fixture(scope="module")
def rebalanced(admin, world):
    r = ok(admin.post("rebalance", group=GROUP, zone=ZONE))
    return r.json()


def test_rebalance_returns_ok(rebalanced):
    assert rebalanced.get("ok") is True, rebalanced


def test_rebalance_is_audited(admin, rebalanced, admin_creds):
    entries = items(ok(admin.audit()))
    mine = [e for e in entries if e.get("action") == "rebalance" and e.get("target") == f"{GROUP}/{ZONE}"]
    assert mine, [e.get("action") for e in entries[:10]]
    assert mine[0]["user"] == admin_creds[0] and mine[0]["ok"] is True
    assert f"group:{GROUP}" in mine[0].get("tags", [])


def test_rebalance_denied_for_unknown_zone(admin, world):
    r = admin.post("rebalance", group=GROUP, zone="no-such-zone-xyz")
    assert r.status_code in (400, 404, 422), r.text[:200]


def test_backend_capacity_scaler_on_gcp(rebalanced, gcp_project):
    if "backend_service" in rebalanced and not rebalanced["backend_service"]:
        pytest.skip(f"adapter reports no worker backend service: {rebalanced.get('note')}")
    ns = gcp.namespace(GROUP, ZONE)
    services = gcp.get("compute", "backend-services", "list", project=gcp_project) or []
    mine = [b for s in services for b in s.get("backends", []) if ns in b.get("group", "")]
    if not mine:
        mine = [b for s in services if GROUP in s.get("name", "") for b in s.get("backends", [])]
    assert mine, f"no backend for namespace {ns} among {[s.get('name') for s in services]}"
    scalers = {float(b.get("capacityScaler", 1.0)) for b in mine}
    assert scalers <= {0.5, 1.0}, scalers
    if "capacity_scaler" in rebalanced:
        assert float(rebalanced["capacity_scaler"]) in scalers
