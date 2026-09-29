"""U22: each zone's language model sees only that zone's enabled tools, resources and prompts.

Storage per CONTRACTS §12.1: `environments[].blocked` stays environment-wide, `environments[].blocked_zones[zone]`
adds to it, and a deploy writes the union into that zone's `RAMEN_BLOCKED`. The proof is at the wire: after
disabling the demo tool in one zone only, that zone's `tools/list` no longer offers it and calling it answers
JSON-RPC -32601, while the other zone is unchanged.
"""

import pytest

from ramen_tests import kube
from ramen_tests.mcp_client import JsonRpcError

pytestmark = pytest.mark.kind
TOOL = "demo_calculator_tool"
RESOURCE = "demo_readme"
PROMPT = "get_calculation_prompt"


def _deploy(admin, group, env_name):
    job = admin.wait_job(admin.deploy(group, env_name).json()["id"], timeout=900)
    assert job["status"] == "ok", f"deploy failed: {job.get('error')}\n" + "\n".join(job.get("log", []))
    return job


def _set_zone_blocked(admin, group, env_name, zone, names):
    r = admin.put("env_zone_blocked", {"blocked": list(names)}, group=group, env=env_name, zone=zone)
    assert r.status_code == 200, f"{zone}: {r.status_code} {r.text[:300]}"
    return r.json()


@pytest.fixture
def blocked(admin, stack, group, env_name):
    """Set every zone's list explicitly, then deploy once. Each test states the whole picture, so nothing has to be
    restored between tests — only once at the end of the module (a deploy costs about a minute on kind)."""

    def apply(per_zone: dict[str, list[str]]):
        for zone in stack["zones"]:
            _set_zone_blocked(admin, group, env_name, zone, per_zone.get(zone, []))
        return _deploy(admin, group, env_name)

    return apply


@pytest.fixture(scope="module", autouse=True)
def _clean_slate(admin, stack, group, env_name):
    """Nothing blocked on either side of the module, whatever an earlier run left behind."""

    def clear():
        env = admin.get("environment", group=group, env=env_name).json()
        dirty = bool(env.get("blocked")) or any((env.get("blocked_zones") or {}).values())
        for zone in stack["zones"]:
            _set_zone_blocked(admin, group, env_name, zone, [])
        admin.put("env_blocked", {"blocked": []}, group=group, env=env_name)
        if dirty:
            _deploy(admin, group, env_name)

    clear()
    yield
    clear()


def test_both_zones_start_with_the_same_packages(nodes, stack):
    listings = {}
    for zone, n in nodes.items():
        n.initialize()
        listings[zone] = (
            sorted(t["name"] for t in n.list_tools()),
            sorted(r.get("name", r.get("uri", "")) for r in n.list_resources()),
            sorted(p["name"] for p in n.list_prompts()),
        )
    first = listings[stack["zones"][0]]
    assert TOOL in first[0], first
    for zone, got in listings.items():
        assert got == first, f"zone {zone} differs before anything is blocked: {got} vs {first}"


def test_disabling_a_tool_in_one_zone_hides_it_from_that_zone_only(admin, nodes, stack, group, env_name, blocked):
    hidden, kept = stack["zones"][0], stack["zones"][1]
    blocked({hidden: [TOOL]})
    body = admin.get("environment", group=group, env=env_name).json()
    assert (body.get("blocked_zones") or {}).get(hidden) == [TOOL], body
    assert body.get("blocked") == [], f"the environment-wide list must not change: {body}"

    nodes[hidden].initialize()
    nodes[kept].initialize()
    assert TOOL not in [t["name"] for t in nodes[hidden].list_tools()], f"{hidden} still offers {TOOL}"
    assert TOOL in [t["name"] for t in nodes[kept].list_tools()], f"{kept} lost {TOOL} too"

    with pytest.raises(JsonRpcError) as e:
        nodes[hidden].call_tool(TOOL, {"var1": 1, "var2": 1, "func": "add"})
    assert e.value.code == -32601, e.value
    assert nodes[kept].call_tool(TOOL, {"var1": 1, "var2": 1, "func": "add"})


def test_the_deploy_secret_carries_only_that_zones_blocked_list(admin, kube_ready, stack, group, env_name, blocked):
    hidden, kept = stack["zones"][0], stack["zones"][1]
    blocked({hidden: [TOOL]})
    blocked = {
        z: kube.kubectl(
            "get",
            "secret",
            "ramen-deploy",
            "-n",
            kube.ns_name(group, z),
            "-o",
            'go-template={{index .data "RAMEN_BLOCKED"}}',
        )
        for z in (hidden, kept)
    }
    import base64

    decoded = {z: base64.b64decode(v).decode() for z, v in blocked.items()}
    assert TOOL in decoded[hidden], decoded
    assert TOOL not in decoded[kept], decoded


def test_resources_and_prompts_are_per_zone_too(admin, nodes, stack, group, env_name, blocked):
    hidden, kept = stack["zones"][1], stack["zones"][0]
    blocked({hidden: [RESOURCE, PROMPT]})
    nodes[hidden].initialize()
    nodes[kept].initialize()
    hidden_res = [r.get("name", r.get("uri", "")) for r in nodes[hidden].list_resources()]
    hidden_prompts = [p["name"] for p in nodes[hidden].list_prompts()]
    assert RESOURCE not in hidden_res, hidden_res
    assert PROMPT not in hidden_prompts, hidden_prompts
    assert RESOURCE in [r.get("name", r.get("uri", "")) for r in nodes[kept].list_resources()]
    assert PROMPT in [p["name"] for p in nodes[kept].list_prompts()]


def test_the_environment_wide_list_still_covers_every_zone(admin, nodes, stack, group, env_name, blocked):
    """§9 behaviour must survive U5: a name on the environment list is gone from every zone."""
    r = admin.put("env_blocked", {"blocked": [TOOL]}, group=group, env=env_name)
    assert r.status_code == 200, r.text[:300]
    blocked({})
    for zone, n in nodes.items():
        n.initialize()
        assert TOOL not in [t["name"] for t in n.list_tools()], f"{zone} still offers an environment-blocked tool"
