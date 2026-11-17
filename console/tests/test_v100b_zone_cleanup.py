"""D69: deleting a zone finishes GKE's own cleanup. The Gateway controller holds the namespace `Terminating` on
`networking.gke.io/neg-finalizer` and keeps the zone's network endpoint groups "in use" for minutes; Ramen clears the
finalizer so the namespace goes, reaps the NEGs in the background once the controller lets go, and a re-create of
the same zone waits out a namespace that is still terminating instead of answering 403."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ramen_console.errors import ApiError
from tests.test_gcp_cloud import SPEC, cloud, fk, http_state  # noqa: F401 - fixtures

NS = "ramen-demo-a"
SNEG = ("ServiceNetworkEndpointGroup", NS, NS)


def hold(fk):  # noqa: F811
    """GKE's shape after `kubectl delete ns`: Terminating, one ServiceNetworkEndpointGroup with the finalizer."""
    fk.k8s.ns_hold.add(NS)
    fk.k8s.objs[SNEG] = {
        "kind": "ServiceNetworkEndpointGroup",
        "metadata": {"name": NS, "namespace": NS, "finalizers": ["networking.gke.io/neg-finalizer"]},
    }


async def test_detach_zone_frees_the_namespace_and_reaps_the_negs_once_the_gateway_lets_go(cloud, fk):  # noqa: F811
    await cloud.attach_zone("demo", "a", SPEC)
    hold(fk)
    fk.compute_state["negs"] = {NS: {"us-central1-b", "us-central1-c", "us-central1-f"}}
    fk.compute_state["neg_in_use"] = 2  # "already being used by backendServices/gkegw1-…" twice, then free
    cloud.neg_delay = 0
    r = await cloud.detach_zone("demo", "a")
    assert r["namespaces"] == [NS] and r["network_endpoint_groups"] == NS
    assert ("Namespace", None, NS) not in fk.k8s.objs, "the finalizer was cleared and the namespace went"
    assert ("patch", *SNEG) in fk.k8s.calls
    await asyncio.wait_for(cloud.drain_reapers(), 5)
    assert fk.compute_state["negs"][NS] == set(), "every zonal NEG deleted after the in-use retries"


async def test_attach_zone_waits_out_a_terminating_namespace_instead_of_403(cloud, fk):  # noqa: F811
    await cloud.attach_zone("demo", "a", SPEC)
    hold(fk)
    fk.k8s.core.delete_namespace(NS)  # the user's delete, before the controller let go
    assert fk.k8s.objs[("Namespace", None, NS)]["status"]["phase"] == "Terminating"
    await cloud.attach_zone("demo", "a", SPEC)
    assert fk.k8s.objs[("Namespace", None, NS)].get("status", {}).get("phase") != "Terminating"
    assert ("Deployment", NS, "worker") in fk.k8s.objs


async def test_a_namespace_that_never_clears_is_a_409_not_a_502(cloud, fk):  # noqa: F811
    await cloud.attach_zone("demo", "a", SPEC)
    hold(fk)
    fk.k8s.ns_stuck.add(NS)  # something other than the NEG finalizer holds it
    fk.k8s.core.delete_namespace(NS)
    with pytest.raises(ApiError) as e:
        await cloud.attach_zone("demo", "a", SPEC)
    assert e.value.status_code == 409 and "still terminating" in e.value.detail


async def test_the_orphaned_backend_service_is_deleted_once_no_url_map_uses_it(cloud, fk):  # noqa: F811
    """Live on GKE: the Gateway dropped the route from its URL map at once but never collected the backend
    service, so the NEGs stayed 'in use' for good. GCE refuses the delete while a URL map references it."""
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["negs"] = {NS: {"us-central1-b", "us-central1-c"}}
    neg = f"https://www.googleapis.com/compute/v1/projects/p1/zones/us-central1-b/networkEndpointGroups/{NS}"
    fk.compute_state["backend"] = {"name": "gkegw1-h1il-ramen-demo-a-worker-8080-x", "backends": [{"group": neg}]}
    fk.compute_state["bs_in_use"] = 2  # the URL map still names it twice, then the controller lets go
    fk.compute_state["bs_not_ready_delete"] = 2  # live on GKE: "is not ready" for minutes after a change
    cloud.neg_delay = 0
    await cloud.detach_zone("demo", "a")
    await asyncio.wait_for(cloud.drain_reapers(), 5)
    assert fk.compute_state["backend"] is None and fk.compute_state["negs"][NS] == set()


async def test_reaping_survives_a_compute_failure(cloud, fk):  # noqa: F811
    await cloud.attach_zone("demo", "a", SPEC)
    fk.compute_state["negs"] = {NS: {"us-central1-b"}}
    fk.compute_state["neg_in_use"] = 10**6  # never let go
    cloud.neg_delay, cloud.neg_attempts = 0, 3
    r = await cloud.detach_zone("demo", "a")
    assert r["namespaces"] == [NS]
    await asyncio.wait_for(cloud.drain_reapers(), 5)  # ends; the failure is logged, never raised
    assert fk.compute_state["negs"][NS] == {"us-central1-b"}


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_chart_lets_the_console_clear_the_neg_finalizer_cluster_wide():
    """Live on GKE: inside a Terminating namespace the zone RoleBinding is already collected, so the right to list
    and patch ServiceNetworkEndpointGroups must come from the console's ClusterRole."""
    chart = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "ramen"
    out = subprocess.run(
        ["helm", "template", "ramen", str(chart), "--set", "project=p"], capture_output=True, text=True, check=True
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    role = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "ramen-console")
    rule = next(r for r in role["rules"] if "servicenetworkendpointgroups" in r.get("resources", []))
    assert "networking.gke.io" in rule["apiGroups"] and {"list", "patch"} <= set(rule["verbs"])


async def test_attach_seeds_the_deploy_secret_so_the_first_pod_has_the_admin_key(cloud, fk):  # noqa: F811
    """Live on a new zone: the stable pod started before the first deploy wrote `ramen-deploy` (optional envFrom)
    and answered UNAUTHENTICATED to the console until the rollout replaced it."""
    from tests.test_gcp_cloud import ADMIN

    await cloud.attach_zone("demo", "a", SPEC)
    sec = fk.k8s.objs[("Secret", NS, "ramen-deploy")]["stringData"]
    assert sec["RAMEN_ADMIN_KEY"] == ADMIN and sec["RAMEN_GROUP"] == "demo" and sec["RAMEN_ZONE"] == "a"
