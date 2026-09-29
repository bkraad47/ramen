"""D21 / U9 end to end on a real worker: an `agent` key reaches the workers of the groups it names and nothing
else, and a `devops` key reaches the console and nothing else.

The console half of the matrix is in `conformance/test_security_matrix.py`; this file is the part that needs a
deployed worker, because an agent key only reaches a node once a deploy has written it into the zone's key set.
"""

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.mcp_client import Node

pytestmark = pytest.mark.kind


@pytest.fixture(scope="module")
def agent_key(admin, stack, group, env_name, suffix) -> str:
    r = admin.create_api_key(f"kind-agent-{suffix}", groups=[group], role="viewer", client_type="agent")
    assert r.status_code == 201, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["client_type"] == "agent" and body["key"].startswith("rmk_"), body
    job = admin.wait_job(admin.deploy(group, env_name).json()["id"], timeout=900)
    assert job["status"] == "ok", job.get("error")
    return body["key"]


@pytest.fixture(scope="module")
def devops_key(admin, group, suffix) -> str:
    r = admin.create_api_key(f"kind-devops-{suffix}", groups=[group], role="group_admin", client_type="devops")
    assert r.status_code == 201, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["client_type"] == "devops" and body["key"].startswith("rmn_"), body
    return body["key"]


def test_an_agent_key_works_on_every_zone_of_its_group(agent_key, zone_targets, group):
    for zone, target in zone_targets.items():
        with Node(target, agent_key, group=group, zone=zone) as n:
            assert [t["name"] for t in n.list_tools()], zone


def test_a_devops_key_is_refused_by_every_zone(devops_key, zone_targets, group):
    for zone, target in zone_targets.items():
        with Node(target, devops_key, group=group, zone=zone) as n:
            code = n.status({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert code == grpc.StatusCode.UNAUTHENTICATED, f"{zone} accepted a devops key: {code.name}"


def test_another_groups_mcp_key_is_refused_by_this_groups_workers(admin, stack, zone_targets, group, suffix, demo_repo):
    """Cross-group isolation at the worker: a key minted for another group is simply not in this zone's key set."""
    other = f"kindother{suffix}"
    r = admin.create_group(other, demo_repo)
    assert r.status_code in (201, 409), r.text[:300]
    r = admin.create_mcp_key(other, "cross-group")
    assert r.status_code == 201, r.text[:300]
    foreign = r.json()["key"]
    for zone, target in zone_targets.items():
        with Node(target, foreign, group=group, zone=zone) as n:
            code = n.status({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert code == grpc.StatusCode.UNAUTHENTICATED, f"{zone} accepted another group's key: {code.name}"
    admin.delete("group", group=other)


def test_deleting_an_agent_key_stops_it_reaching_the_workers(admin, stack, zone_targets, group, env_name, suffix):
    """Revocation reaches the data plane on the next deploy (the node's key set comes from the deploy Secret)."""
    r = admin.create_api_key(f"kind-agent-rev-{suffix}", groups=[group], role="viewer", client_type="agent")
    assert r.status_code == 201, r.text[:300]
    doc = r.json()
    job = admin.wait_job(admin.deploy(group, env_name).json()["id"], timeout=900)
    assert job["status"] == "ok", job.get("error")
    zone, target = next(iter(zone_targets.items()))
    with Node(target, doc["key"], group=group, zone=zone) as n:
        assert [t["name"] for t in n.list_tools()], "the new agent key never reached the worker"

    assert admin.delete("api_key", id=doc["id"]).status_code == 200
    job = admin.wait_job(admin.deploy(group, env_name).json()["id"], timeout=900)
    assert job["status"] == "ok", job.get("error")
    with Node(target, doc["key"], group=group, zone=zone) as n:
        code = n.status({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert code == grpc.StatusCode.UNAUTHENTICATED, f"a deleted agent key still works: {code.name}"


def test_the_console_still_refuses_the_agent_key_that_a_worker_accepts(console_url, agent_key):
    from ramen_tests.console import Console

    with Console(console_url, api_key=agent_key) as c:
        r = c.me()
    assert r.status_code == 403 and "agent key" in r.text.lower(), f"{r.status_code} {r.text[:200]}"


def test_zone_metadata_is_required_by_the_load_balancer_path(agent_key, zone_targets, group):
    """On kind each zone has its own port, so a missing zone header still reaches a worker; the assertion is that
    the worker answers for its own zone regardless, which is what makes the LB the only router (§11)."""
    zone, target = next(iter(zone_targets.items()))
    with Node(target, agent_key, group=group, zone=None) as n:
        assert [t["name"] for t in n.list_tools()], "a worker refused a call without routing metadata"
    assert E.env("RAMEN_KIND_ZONES")
