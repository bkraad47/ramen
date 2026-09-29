"""U20: multiple workers across multiple zones, each serving independently.

Every assertion here is about *independence*: separate namespaces, separate Deployments, separate HPAs, separate
node pools, separate gRPC endpoints, and routing metadata that names the zone the caller reached.
"""

import json

import grpc
import pytest

from ramen_tests import kube
from ramen_tests.mcp_client import code_of, text_of

pytestmark = pytest.mark.kind
TOOL = "demo_calculator_tool"


def test_every_zone_is_its_own_namespace(kube_ready, stack, group):
    for zone in stack["zones"]:
        ns = kube.ns_name(group, zone)
        d = kube.deployment(ns)
        assert d["metadata"]["labels"]["ramen.io/zone"] == zone
        assert d["metadata"]["labels"]["ramen.io/group"] == group
    # and the namespaces really are distinct objects, not one namespace seen twice
    assert len({kube.ns_name(group, z) for z in stack["zones"]}) == len(stack["zones"])


def test_every_zone_serves_health_and_tools_independently(nodes, stack):
    for zone, n in nodes.items():
        assert n.health() == "SERVING", zone
        assert n.health("ramen.v1.Mcp") == "SERVING", zone
        n.initialize()
        assert TOOL in [t["name"] for t in n.list_tools()], zone
        assert text_of(n.call_tool(TOOL, {"var1": 2, "var2": 3, "func": "add"})).strip().startswith("5"), zone


def test_each_zone_has_its_own_workers_and_deploy_secret(kube_ready, stack, group):
    seen: set[str] = set()
    for zone in stack["zones"]:
        ns = kube.ns_name(group, zone)
        spec, ready = kube.replicas(ns)
        assert spec >= 1 and ready >= 1, f"{ns}: {ready}/{spec} ready"
        pods = {p["metadata"]["name"] for p in kube.pods(ns)}
        assert pods and not (pods & seen), f"{ns} shares pods with another zone: {pods & seen}"
        seen |= pods
        assert "RAMEN_ZONE" in kube.secret_keys(ns), ns


def test_zone_pods_are_pinned_to_their_zone_node(kube_ready, stack, group, zone_regions):
    """CONTRACTS §7: the zone record's `region` becomes nodeSelector topology.kubernetes.io/zone."""
    if not zone_regions:
        pytest.skip("RAMEN_KIND_NODE_ZONES not set")
    for zone, region in zone_regions.items():
        ns = kube.ns_name(group, zone)
        sel = kube.deployment(ns)["spec"]["template"]["spec"].get("nodeSelector") or {}
        assert sel.get("topology.kubernetes.io/zone") == region, f"{ns}: {sel}"
        for node in kube.pod_nodes(ns):
            assert kube.node_zone(node) == region, f"{ns} pod on {node} (zone {kube.node_zone(node)}), want {region}"
    # the two zones landed on different nodes, so a node loss takes one zone, not both
    nodes_per_zone = {z: set(kube.pod_nodes(kube.ns_name(group, z))) for z in zone_regions}
    all_nodes = [n for s in nodes_per_zone.values() for n in s]
    assert len(all_nodes) == len(set(all_nodes)), f"zones share a node: {nodes_per_zone}"


def test_zone_reports_its_own_identity(nodes, stack, group):
    """Each worker's env names its own zone; the routing metadata the client sends matches it (§11)."""
    for zone, n in nodes.items():
        ns = kube.ns_name(group, zone)
        envs = {
            e["name"]: e.get("value") for e in kube.deployment(ns)["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert envs.get("RAMEN_ZONE") == zone and envs.get("RAMEN_GROUP") == group
        assert n.zone == zone and n.group == group


def test_an_unknown_key_is_refused_by_every_zone(nodes):
    for zone, n in nodes.items():
        assert n.status({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="rmk_not-a-real-key") == (
            grpc.StatusCode.UNAUTHENTICATED
        ), zone
        assert n.status({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key=None) == (
            grpc.StatusCode.UNAUTHENTICATED
        ), zone


def test_the_group_key_works_on_both_zones_it_covers(nodes, stack):
    """A group MCP key is a group credential, so it is valid in every zone of that group — and only there."""
    for zone, n in nodes.items():
        assert code_of(n.list_tools) == grpc.StatusCode.OK, zone


def test_console_reports_both_zones_live(admin, stack, group):
    for zone in stack["zones"]:
        r = admin.get("workers", group=group, zone=zone)
        assert r.status_code == 200, r.text[:300]
        live = r.json().get("live") or []
        assert live, f"zone {zone}: no live workers"
        assert all(w.get("load") != "down" for w in live), f"zone {zone}: {json.dumps(live)[:300]}"


def test_dashboard_shows_a_cell_per_zone(admin, stack, group):
    r = admin.get("dashboard")
    assert r.status_code == 200
    body = r.json()
    for zone in stack["zones"]:
        assert zone in body["zones"], body["zones"]
        assert group in body["cells"].get(zone, {}), f"no cell for {group}/{zone}"
