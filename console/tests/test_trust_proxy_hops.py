"""Rendered worker env for the proxy-trust and reflection knobs (CONTRACTS §11).

Load balancers *append* to `x-forwarded-for`, so the node counts the client address from the right-hand end
and the hop count is what tells it where to stop. It is per provider and the deployment is what knows it:
the GCP external load balancer appends `<client>, <lb>` (2), an AWS ALB appends `<client>` (1). A wrong
value must not be silently equivalent to trusting the left-most, caller-supplied entry, which is what the
v0.4.0 claims review found (rows 14/36).
"""

import pytest

from ramen_console.cloud import aws_k8s, gcp_k8s

SPEC = {"count": 1, "size": "s"}


def _worker_env(objs) -> dict:
    deps = [o for o in objs if o["kind"] == "Deployment"]
    assert deps, "no worker Deployment rendered"
    envs = [{e["name"]: e["value"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]} for d in deps]
    keys = ("RAMEN_TRUST_PROXY", "RAMEN_TRUST_PROXY_HOPS", "RAMEN_REFLECTION")
    trust = [{k: v for k, v in e.items() if k in keys} for e in envs]
    assert trust[0] == trust[-1], "stable and canary must agree on the trust settings"
    return envs[0]


@pytest.mark.parametrize(
    ("render", "hops"),
    [
        (lambda: gcp_k8s.manifests("demo", "a", SPEC, "img", "gs://b/demo"), "2"),
        (lambda: aws_k8s.manifests("demo", "a", SPEC, "img", "s3://b/demo"), "1"),
    ],
    ids=["gcp", "aws"],
)
def test_hop_count_is_per_provider(render, hops):
    env = _worker_env(render())
    assert env["RAMEN_TRUST_PROXY_HOPS"] == hops
    # the old boolean is gone: it meant "trust the left-most hop", which any caller could choose
    assert "RAMEN_TRUST_PROXY" not in env


@pytest.mark.parametrize(
    "render",
    [
        lambda: gcp_k8s.manifests("demo", "a", SPEC, "img", "gs://b/demo"),
        lambda: aws_k8s.manifests("demo", "a", SPEC, "img", "s3://b/demo"),
    ],
    ids=["gcp", "aws"],
)
def test_reflection_is_off_on_deployed_workers(render):
    # unauthenticated, not CIDR-gated, and routed through the load balancer, so not on a deployed worker
    assert _worker_env(render())["RAMEN_REFLECTION"] == "0"
