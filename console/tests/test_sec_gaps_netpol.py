"""S6 (sec-gaps, 2026-11-17): the console's home networks (`console.sourceCidrs` → `RAMEN_CONSOLE_SOURCE_CIDRS`) are
admitted only where the console needs them — a satellite zone's worker policy, as their own rule on the worker port —
never in a home zone (the console arrives there from `ramen-system`, a namespace peer) and never in `ramen-system`."""

import asyncio
import contextlib
import json

import pytest

from ramen_console.cloud.gcp_k8s import PORT, networkpolicy, with_console_sources
from tests.fakes_aws import aws_env
from tests.test_v100b_satellites import Lookup, aws_pair, azure_pair, gcp_pair, grpc_worker  # noqa: F401 - fixture
from tests.test_v100c_evals import needs_helm, template

CIDRS = "10.7.128.0/17, 10.42.0.0/16"
BLOCKS = [{"ipBlock": {"cidr": "10.7.128.0/17"}}, {"ipBlock": {"cidr": "10.42.0.0/16"}}]


def peers(policy):
    return [p for rule in policy["spec"]["ingress"] for p in rule["from"]]


def test_the_worker_policy_itself_never_carries_the_home_networks(monkeypatch):
    monkeypatch.setenv("RAMEN_CONSOLE_SOURCE_CIDRS", CIDRS)
    pol = networkpolicy("ramen-demo-a", "demo", "a")
    assert not [p for p in peers(pol) if p in BLOCKS]


def test_only_a_satellite_zone_admits_the_home_networks_in_a_rule_of_their_own(monkeypatch):
    monkeypatch.setenv("RAMEN_CONSOLE_SOURCE_CIDRS", CIDRS)
    home = [networkpolicy("ramen-demo-a", "demo", "a"), {"kind": "Service", "metadata": {"name": "worker"}}]
    assert with_console_sources(home, satellite=False) == home
    sat = with_console_sources([networkpolicy("ramen-demo-a", "demo", "a")], satellite=True)
    rules = sat[0]["spec"]["ingress"]
    assert rules[-1] == {"from": BLOCKS, "ports": [{"port": PORT, "protocol": "TCP"}]}
    assert not [p for p in rules[0]["from"] if p in BLOCKS]  # the console + load balancer rule is unchanged
    again = with_console_sources(sat, satellite=True)  # idempotent (re-attach, push)
    assert again[0]["spec"]["ingress"].count(rules[-1]) == 1
    monkeypatch.delenv("RAMEN_CONSOLE_SOURCE_CIDRS")
    plain = [networkpolicy("ramen-demo-a", "demo", "a")]
    assert with_console_sources(plain, satellite=True) == plain  # nothing configured, nothing added
    egress = {"kind": "NetworkPolicy", "metadata": {"name": "worker-egress"}, "spec": {"egress": []}}
    assert with_console_sources([egress], satellite=True) == [egress]


@pytest.mark.parametrize("make", [gcp_pair, aws_pair, azure_pair], ids=["gcp", "aws", "azure"])
def test_attach_admits_the_home_networks_on_the_satellite_only(make, grpc_worker, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_CONSOLE_SOURCE_CIDRS", CIDRS)

    async def go():
        await cloud.attach_zone("demo", "far", {"region": zones["far"], "size": "s", "count": 1})
        await cloud.attach_zone("demo", "near", {"region": zones["near"], "size": "s", "count": 1})

    with aws_env() if make is aws_pair else contextlib.nullcontext():
        cloud, fk, sat, zones = make(grpc_worker)
        cloud.set_cluster_lookup(Lookup(sat, zones))
        asyncio.run(go())
    far = fk.satellites[sat["name"]].k8s.objs[("NetworkPolicy", "ramen-demo-far", "worker")]
    near = fk.k8s.objs[("NetworkPolicy", "ramen-demo-near", "worker")]
    assert all(b in peers(far) for b in BLOCKS)
    assert not [p for p in peers(near) if p in BLOCKS]


@needs_helm
@pytest.mark.parametrize("collector", ["phoenix", "jaeger"])
def test_the_chart_admits_console_source_cidrs_at_the_console_only(collector):
    rc, err, docs = template(
        f"otel.collector={collector}",
        "evals.engine=promptfoo",
        "redis.enabled=true",
        "console.sourceCidrs={10.7.128.0/17}",
    )
    assert rc == 0, err
    pols = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    assert set(pols) == {"console", collector, "promptfoo", "redis"}
    has = {name: "10.7.128.0/17" in json.dumps(p["spec"]) for name, p in pols.items()}
    assert has == {"console": True, collector: False, "promptfoo": False, "redis": False}
    console = pols["console"]["spec"]["ingress"]
    assert len(console) == 1 and console[0]["ports"] == [{"port": 8000, "protocol": "TCP"}]
    assert "35.191.0.0/16" in json.dumps(pols[collector]["spec"]) and "35.191.0.0/16" in json.dumps(
        pols["promptfoo"]["spec"]
    )


def test_the_chart_notes_warn_against_a_pod_range_in_source_cidrs_where_ipblock_matches_pods():
    from pathlib import Path

    notes = (Path(__file__).resolve().parents[2] / "deploy/helm/ramen/templates/NOTES.txt").read_text()
    assert '{{- if and .Values.console.sourceCidrs (ne .Values.provider "gcp") }}' in notes
    assert "never a pod range" in notes and "every worker of a" in notes
