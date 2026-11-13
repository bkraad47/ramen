"""§24.1 sleeping workers on every adapter: labels and selectors per provider, sleep/wake/zone_runtime against the
shared k8s fake."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ramen_console.cloud import aws_k8s, azure_k8s, gcp_k8s
from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.aws_k8s import parse_weights
from ramen_console.cloud.azure import AzureCloud
from ramen_console.cloud.base import Cloud
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.gcp_k8s import ACTIVATOR, DIRECT, ROLE, activator, selects
from ramen_console.cloud.local import LocalCloud
from ramen_console.errors import ApiError
from tests.fakes_aws import BUCKET, FakeAwsClients, aws_env
from tests.fakes_azure import ACCOUNT, ISSUER, LOCATION, RG, SUB, VAULT, FakeAzureClients
from tests.fakes_gcp import FakeClients

SPEC = {"region": "r-a", "size": "s", "count": 2}
NS = "ramen-demo-a"
URL = "http://console.ramen-system.svc.cluster.local:80"


def render(provider):
    if provider == "gcp":
        return gcp_k8s.manifests("demo", "a", SPEC, "img:1", "gs://b/demo", "gsa@x")
    if provider == "aws":
        return aws_k8s.manifests("demo", "a", SPEC, "img:1", "s3://b/demo", "arn:role")
    return azure_k8s.azure_manifests("demo", "a", SPEC, "img:1", "az://acct/groups/demo", "cid")


def named(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


@pytest.mark.parametrize("provider", ["gcp", "aws", "azure"])
def test_services_select_workers_and_the_activator_as_the_contract_says(provider):
    docs = render(provider)
    worker_svc, direct = named(docs, "Service", "worker"), named(docs, "Service", DIRECT)
    assert direct["spec"]["selector"] == {"app": "worker", ROLE: "worker"}
    assert direct["spec"]["ports"][0]["port"] == 8080 and "annotations" not in direct["metadata"]  # no NEG, no edge
    stable, canary = named(docs, "Deployment", "worker"), named(docs, "Deployment", "worker-canary")
    for dep in (stable, canary):
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert labels[ROLE] == "worker" and selects(direct["spec"]["selector"], labels)
        assert ROLE not in dep["spec"]["selector"]["matchLabels"]  # immutable on an existing Deployment: untouched
        assert dep["spec"]["selector"]["matchLabels"] == {
            "app": "worker",
            "ramen.io/track": labels["ramen.io/track"],
        }
    assert selects(worker_svc["spec"]["selector"], stable["spec"]["template"]["metadata"]["labels"])
    assert stable["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    assert canary["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 1, "maxSurge": 0}
    if provider == "aws":  # the canary rides its own target group; the activator stands in for the stable one
        assert selects(
            named(docs, "Service", "worker-canary")["spec"]["selector"],
            canary["spec"]["template"]["metadata"]["labels"],
        )
    else:
        assert selects(worker_svc["spec"]["selector"], canary["spec"]["template"]["metadata"]["labels"])

    act = activator(stable, worker_svc["spec"]["selector"], URL)
    labels = act["spec"]["template"]["metadata"]["labels"]
    assert selects(worker_svc["spec"]["selector"], labels) and not selects(direct["spec"]["selector"], labels)
    assert labels["app"] == "worker" and labels[ROLE] == "activator"
    assert act["spec"]["selector"]["matchLabels"] == {"app": "worker", ROLE: "activator"}
    assert not selects(act["spec"]["selector"]["matchLabels"], stable["spec"]["template"]["metadata"]["labels"])
    if provider == "aws":
        assert not selects(named(docs, "Service", "worker-canary")["spec"]["selector"], labels)
    # every edge object still points at Service/worker (and, on AWS, worker-canary) only
    edges = [d for d in docs if d["kind"] in ("HTTPRoute", "Ingress", "HealthCheckPolicy")]
    assert edges and DIRECT not in str(edges) and ACTIVATOR not in str(edges)
    # the NetworkPolicy covers the activator (app=worker) and lets it reach the workers
    np = named(docs, "NetworkPolicy", "worker")
    assert np["spec"]["podSelector"] == {"matchLabels": {"app": "worker"}}
    assert np["spec"]["ingress"][1] == {
        "from": [{"podSelector": {"matchLabels": {"app": "worker"}}}],
        "ports": [{"port": 8080, "protocol": "TCP"}],
    }


@pytest.mark.parametrize("provider", ["gcp", "aws", "azure"])
def test_activator_is_the_zones_worker_minus_what_it_does_not_need(provider):
    docs = render(provider)
    stable = named(docs, "Deployment", "worker")
    src = stable["spec"]["template"]["spec"]["containers"][0]
    act = activator(stable, named(docs, "Service", "worker")["spec"]["selector"], URL)
    assert act["metadata"]["name"] == ACTIVATOR and act["metadata"]["namespace"] == NS and act["spec"]["replicas"] == 1
    pod = act["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["name"] == "worker" and c["image"] == src["image"] == "img:1"
    env = {e["name"]: e["value"] for e in c["env"]}
    assert {k: v for k, v in env.items() if not k.startswith(("RAMEN_ACTIVATOR", "RAMEN_CONSOLE_"))} == {
        e["name"]: e["value"] for e in src["env"]
    }  # the zone's whole worker config, exactly as the worker gets it …
    assert c["envFrom"] == src["envFrom"] == [{"secretRef": {"name": "ramen-deploy", "optional": True}}]
    assert env["RAMEN_ACTIVATOR"] == "1" and env["RAMEN_CONSOLE_INTERNAL_URL"] == URL  # … plus these three
    assert env["RAMEN_ACTIVATOR_UPSTREAM"] == "worker-direct:8080"
    assert c["resources"]["requests"] == {"cpu": "10m", "memory": "32Mi"}
    assert c["readinessProbe"] == src["readinessProbe"] and "livenessProbe" not in c
    assert "volumes" not in pod and "volumeMounts" not in c  # no bucket
    assert pod["serviceAccountName"] == "worker" and pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False and "privileged" not in str(act)
    assert pod["nodeSelector"] == {"topology.kubernetes.io/zone": "r-a"}
    assert azure_k8s.WI_USE not in act["spec"]["template"]["metadata"]["labels"]  # no identity it does not need


def test_console_internal_url_default_and_override(monkeypatch):
    monkeypatch.delenv("RAMEN_CONSOLE_INTERNAL_URL", raising=False)
    assert gcp_k8s.console_internal_url() == URL
    monkeypatch.setenv("RAMEN_CONSOLE_INTERNAL_URL", "http://c.x:8000")
    stable = named(render("gcp"), "Deployment", "worker")
    env = {
        e["name"]: e["value"]
        for e in activator(stable, {"app": "worker"})["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["RAMEN_CONSOLE_INTERNAL_URL"] == "http://c.x:8000"


# adapters ----------------------------------------------------------------------
@pytest.fixture(params=["gcp", "aws", "azure"])
def zone(request, monkeypatch):
    """(provider, cloud, fake k8s) with zone demo/a attached at 2 workers."""
    monkeypatch.setenv("RAMEN_ALB_SPLIT_DRAIN_SECS", "0")
    monkeypatch.delenv("RAMEN_CONSOLE_INTERNAL_URL", raising=False)
    if request.param == "gcp":
        fk = FakeClients()
        cloud = GcpCloud(project="p1", bucket="b", image="img:1", admin_key="k", clients=fk, wait_secs=1, poll=0)
        yield request.param, cloud, fk.k8s
    elif request.param == "aws":
        with aws_env():
            fk = FakeAwsClients().seed()
            yield (
                request.param,
                AwsCloud(bucket=BUCKET, image="img:1", admin_key="k", clients=fk, wait_secs=1, poll=0),
                fk.k8s,
            )
    else:
        fk = FakeAzureClients()
        cloud = AzureCloud(SUB, RG, LOCATION, ACCOUNT, VAULT, ISSUER, "img:1", "k", clients=fk, wait_secs=1, poll=0)
        yield request.param, cloud, fk.k8s


def dep(k8s, name):
    return k8s.objs.get(("Deployment", NS, name))


def replicas(k8s):
    return {
        n: (dep(k8s, n) or {"spec": {"replicas": None}})["spec"]["replicas"]
        for n in ("worker", "worker-canary", ACTIVATOR)
    }


async def test_sleep_then_wake(zone):
    provider, cloud, k8s = zone
    assert await cloud.zone_runtime("demo", "a") == {"workers": 0, "canary": 0, "activator": 0}  # not deployed
    await cloud.attach_zone("demo", "a", SPEC)
    k8s.objs[("Deployment", NS, "worker-canary")]["spec"]["replicas"] = 1
    assert ("Service", NS, DIRECT) in k8s.objs
    assert await cloud.zone_runtime("demo", "a") == {"workers": 2, "canary": 1, "activator": 0}

    k8s.calls.clear()
    r = await cloud.sleep_zone("demo", "a")
    assert r == {
        "ok": True,
        "namespace": NS,
        "previous": {"workers": 2, "canary": 1},
        "workers": 0,
        "canary": 0,
        "activator": 1,
    }
    assert replicas(k8s) == {"worker": 0, "worker-canary": 0, ACTIVATOR: 1}
    # the activator exists before a worker is scaled down
    order = [c for c in k8s.calls if c[1] == "Deployment"]
    assert order[0] == ("create", "Deployment", NS, ACTIVATOR) and order[1][3] == "worker"
    act = dep(k8s, ACTIVATOR)
    svc = k8s.objs[("Service", NS, "worker")]["spec"]["selector"]
    labels = act["spec"]["template"]["metadata"]["labels"]
    assert selects(svc, labels) and not selects(k8s.objs[("Service", NS, DIRECT)]["spec"]["selector"], labels)
    c = act["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "img:1" and {"name": "RAMEN_CONSOLE_INTERNAL_URL", "value": URL} in c["env"]
    assert await cloud.workers("demo", "a") == []  # the activator is not a worker
    if provider == "aws":
        assert parse_weights(k8s.objs[("Ingress", NS, "worker")]) == {"worker": 100, "worker-canary": 0}

    again = await cloud.sleep_zone("demo", "a")  # idempotent
    assert again["activator"] == 1 and again["workers"] == 0 and replicas(k8s)[ACTIVATOR] == 1

    w = await cloud.wake_zone("demo", "a", 2, 1)
    assert w == {"ok": True, "namespace": NS, "workers": 2, "canary": 1, "activator": 0}
    assert replicas(k8s) == {"worker": 2, "worker-canary": 1, ACTIVATOR: 0}
    assert dep(k8s, "worker")["spec"]["template"]["metadata"]["labels"][ROLE] == "worker"
    if provider == "aws":
        assert parse_weights(k8s.objs[("Ingress", NS, "worker")]) == {"worker": 67, "worker-canary": 33}
        assert parse_weights(k8s.objs[("Ingress", NS, "worker-http")]) == {"worker": 67, "worker-canary": 33}
    assert (await cloud.wake_zone("demo", "a", 2, 1))["activator"] == 0  # idempotent
    assert (await cloud.wake_zone("demo", "a", 0, 0))["workers"] == 1  # never below one worker


async def test_wake_scales_the_activator_down_only_after_the_workers_are_ready(zone):
    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.sleep_zone("demo", "a")
    k8s.unready.add("worker")
    k8s.calls.clear()
    with pytest.raises(ApiError) as e:
        await cloud.wake_zone("demo", "a", 2, 0)
    assert e.value.status_code == 502 and "not ready" in e.value.detail and "0/2 ready" in e.value.detail
    assert replicas(k8s) == {"worker": 2, "worker-canary": 0, ACTIVATOR: 1}  # still forwarding / answering 503
    k8s.unready.clear()
    assert (await cloud.wake_zone("demo", "a", 2, 0))["activator"] == 0


async def test_sleep_waits_for_the_activator_before_touching_the_workers(zone):
    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    k8s.unready.add(ACTIVATOR)
    with pytest.raises(ApiError) as e:
        await cloud.sleep_zone("demo", "a")
    assert "worker-activator not ready" in e.value.detail
    assert replicas(k8s)["worker"] == 2


async def test_sleep_and_wake_refuse_a_zone_that_is_not_deployed(zone):
    _, cloud, _ = zone
    for call in (cloud.sleep_zone("demo", "a"), cloud.wake_zone("demo", "a", 1, 0)):
        with pytest.raises(ApiError) as e:
            await call
        assert e.value.status_code == 409


async def test_a_zone_attached_before_1_0_gets_what_sleeping_needs(zone):
    """No role label, no worker-direct, a NetworkPolicy without the same-zone peer: sleep adds all three without
    touching a Deployment selector."""
    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    del k8s.objs[("Service", NS, DIRECT)]
    for name in ("worker", "worker-canary"):
        del dep(k8s, name)["spec"]["template"]["metadata"]["labels"][ROLE]
    np = k8s.objs[("NetworkPolicy", NS, "worker")]
    np["spec"]["ingress"] = np["spec"]["ingress"][:1]
    first = dict(np["spec"]["ingress"][0])
    selector = dict(dep(k8s, "worker")["spec"]["selector"]["matchLabels"])
    await cloud.sleep_zone("demo", "a")
    assert ("Service", NS, DIRECT) in k8s.objs
    rules = k8s.objs[("NetworkPolicy", NS, "worker")]["spec"]["ingress"]
    assert rules[0] == first and rules[1]["from"] == [{"podSelector": {"matchLabels": {"app": "worker"}}}]
    for name in ("worker", "worker-canary"):
        assert dep(k8s, name)["spec"]["template"]["metadata"]["labels"][ROLE] == "worker"
    assert dep(k8s, "worker")["spec"]["selector"]["matchLabels"] == selector
    await cloud.sleep_zone("demo", "a")
    assert len(k8s.objs[("NetworkPolicy", NS, "worker")]["spec"]["ingress"]) == 2  # not twice


async def test_redeploy_keeps_labels_and_does_not_resurrect_the_activator(zone):
    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.sleep_zone("demo", "a")
    await cloud.wake_zone("demo", "a", 2, 0)
    selector = dict(dep(k8s, "worker")["spec"]["selector"]["matchLabels"])
    await cloud.attach_zone("demo", "a", {**SPEC, "image": "img:2"})  # what deploy applies first
    assert replicas(k8s)[ACTIVATOR] == 0
    assert dep(k8s, "worker")["spec"]["template"]["metadata"]["labels"][ROLE] == "worker"
    assert dep(k8s, "worker")["spec"]["selector"]["matchLabels"] == selector
    assert dep(k8s, "worker")["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    # the next sleep runs the image the zone runs now
    await cloud.sleep_zone("demo", "a")
    assert dep(k8s, ACTIVATOR)["spec"]["template"]["spec"]["containers"][0]["image"] == "img:2"


async def test_detach_removes_the_activator_with_everything_else(zone):
    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.sleep_zone("demo", "a")
    await cloud.detach_zone("demo", "a")
    assert not [k for k in k8s.objs if k[1] == NS]
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.sleep_zone("demo", "a")
    await cloud.detach_group("demo")
    assert not [k for k in k8s.objs if k[1] == NS]
    assert await cloud.zone_runtime("demo", "a") == {"workers": 0, "canary": 0, "activator": 0}


async def test_ip_rules_roll_a_sleeping_zones_activator(zone):
    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.sleep_zone("demo", "a")
    await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    ann = dep(k8s, ACTIVATOR)["spec"]["template"]["metadata"]["annotations"]
    assert "ramen.io/restartedAt" in ann
    assert "ramen.io/restartedAt" not in (dep(k8s, "worker")["spec"]["template"]["metadata"].get("annotations") or {})


def test_stuck_activator_pods_are_named(zone):
    _, cloud, k8s = zone
    k8s.ready = False
    k8s.objs[("Deployment", NS, ACTIVATOR)] = activator(
        named(render("gcp"), "Deployment", "worker"), {"app": "worker"}, URL
    )
    assert "worker-activator-0 Pending (Unschedulable" in cloud.kube._stuck_pods(NS, ACTIVATOR)


# base and local ------------------------------------------------------------------
async def test_local_adapter_only_reports(tmp_path):
    c = LocalCloud(tmp_path, workers={"demo/a": ["w1:8080", "w2:8080"]})
    shape = {"workers": 2, "canary": 0, "activator": 0}
    assert await c.zone_runtime("demo", "a") == shape
    assert (await c.sleep_zone("demo", "a")) == {
        "ok": True,
        "note": "local adapter: the compose worker keeps running",
        **shape,
    }
    assert (await c.wake_zone("demo", "a", 3, 1))["workers"] == 2
    assert await c.zone_runtime("demo", "other") == {"workers": 1, "canary": 0, "activator": 0}


async def test_base_defaults():
    class Bare(Cloud):
        sync_repo = deploy = rebalance = workers = logs = set_ip_rules = create_service_account = refresh = None

    c = Bare()
    assert (await c.sleep_zone("g", "z"))["ok"] and (await c.wake_zone("g", "z", 1, 0))["ok"]
    assert await c.zone_runtime("g", "z") == {"workers": 0, "canary": 0, "activator": 0}
    assert c.builder_available() is False
    with pytest.raises(ApiError) as e:
        await c.build_image("g", "t", "FROM x", {})
    assert e.value.status_code == 422 and "No image builder" in e.value.detail


# renderer vs chart ------------------------------------------------------------------
CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "ramen-worker"
CHART_SETS = {
    "gcp": "provider=gcp,project=p1,group=demo,zone=a",
    "aws": "provider=aws,aws.account=123456789012,group=demo,zone=a",
    "azure": "provider=azure,azure.acr=r.azurecr.io,azure.storageAccount=acct,group=demo,zone=a",
}


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize("provider", ["gcp", "aws", "azure"])
def test_worker_chart_stays_in_step_with_the_renderer(provider):
    """§24.1 parity: deploy/helm/ramen-worker renders the same labels, selectors, `worker-direct` and NetworkPolicy
    peer as the console's own renderer, for every provider."""
    out = subprocess.run(
        ["helm", "template", "ramen-worker", str(CHART), "--set", CHART_SETS[provider]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    chart = [d for d in yaml.safe_load_all(out) if d]
    ours = render(provider)

    def facts(docs):
        svc = {d["metadata"]["name"]: d["spec"]["selector"] for d in docs if d["kind"] == "Service"}
        deps = {
            d["metadata"]["name"]: (
                d["spec"]["selector"]["matchLabels"],
                {
                    k: v
                    for k, v in d["spec"]["template"]["metadata"]["labels"].items()
                    if k in ("app", ROLE, "ramen.io/track")
                },
            )
            for d in docs
            if d["kind"] == "Deployment"
        }
        np = named(docs, "NetworkPolicy", "worker")
        return svc, deps, np["spec"]["podSelector"], np["spec"]["ingress"][1]

    assert facts(chart) == facts(ours)
    act = activator(named(chart, "Deployment", "worker"), named(chart, "Service", "worker")["spec"]["selector"], URL)
    labels = act["spec"]["template"]["metadata"]["labels"]
    assert selects(named(chart, "Service", "worker")["spec"]["selector"], labels)
    assert not selects(named(chart, "Service", DIRECT)["spec"]["selector"], labels)


def test_a_chart_from_before_1_0_is_completed_by_the_console():
    docs = [d for d in render("gcp") if d["metadata"]["name"] != DIRECT]
    for d in docs:
        if d["kind"] == "Deployment":
            del d["spec"]["template"]["metadata"]["labels"][ROLE]
    gcp_k8s.ensure_direct(docs, "demo", "a")
    assert named(docs, "Service", DIRECT)["spec"]["selector"] == {"app": "worker", ROLE: "worker"}
    assert all(d["spec"]["template"]["metadata"]["labels"][ROLE] == "worker" for d in docs if d["kind"] == "Deployment")
    gcp_k8s.ensure_direct(docs, "demo", "a")
    assert len([d for d in docs if d["metadata"]["name"] == DIRECT]) == 1


# deploy config: what reaches the worker, the activator and the runtime (§24.3) ----------------------
OTEL = {
    "RAMEN_OTEL_ENDPOINT": "http://jaeger.ramen-system:4318",
    "RAMEN_OTEL_HEADERS": '{"authorization": "Bearer collector-credential"}',
    "RAMEN_OTEL_CLIENT": '{"otlp_endpoint": "https://otel.example", "headers": {"x-key": "client-credential"}}',
    "RAMEN_TELEMETRY_CONSENT": "1",
    "RAMEN_TELEMETRY_NOTICE": "jaeger.ramen-system",
    "RAMEN_SOMETHING_NEW": "passes through",
}


async def test_deploy_config_reaches_workers_and_the_activator_through_the_secret_only(zone, monkeypatch):
    """Every deploy key — the ones this adapter has never heard of included — lands in Secret `ramen-deploy`, which
    workers and the activator both load (`envFrom`). Nothing of it is written to the group's bucket, which is what
    the runtime (tenant code) reads."""
    provider, cloud, k8s = zone

    async def reload(target, key):
        return {"tools": [], "errors": []}

    async def health(target):
        return True

    monkeypatch.setattr(cloud.rpc, "reload", reload)
    monkeypatch.setattr(cloud.rpc, "health", health)
    r = await cloud.deploy("demo", "dev", "a", canary=False, config=dict(OTEL), spec=SPEC)
    assert r["ok"], r
    secret = k8s.objs[("Secret", NS, "ramen-deploy")]["stringData"]
    assert {k: secret[k] for k in OTEL} == OTEL
    await cloud.sleep_zone("demo", "a")
    for name in ("worker", ACTIVATOR):
        c = dep(k8s, name)["spec"]["template"]["spec"]["containers"][0]
        assert c["envFrom"] == [{"secretRef": {"name": "ramen-deploy", "optional": True}}]
        assert not {e["name"] for e in c["env"]} & set(OTEL)  # never inline: the Secret is the one source
    stored = str({k: v for k, v in k8s.objs.items() if k[0] != "Secret"})
    assert "collector-credential" not in stored and "client-credential" not in stored


async def test_local_adapter_hands_the_telemetry_settings_to_the_worker_in_its_deploy_file(tmp_path):
    """On the local stack the deploy file is the only channel for settings made on the Config page (the node keeps
    them out of the runtime's environment); the kube adapters use the Secret, tested above."""

    class Rpc:
        async def reload(self, target, key):
            return {"tools": []}

    c = LocalCloud(tmp_path, rpc=Rpc())
    r = await c.deploy("demo", "dev", "a", config=dict(OTEL))
    assert r["ok"]
    for name in ("env", "env-a"):
        text = (tmp_path / "demo" / ".ramen" / name).read_text()
        for k in OTEL:
            assert f"{k}={OTEL[k]}\n" in text


async def test_wake_is_safe_to_call_twice_at_once(zone):
    import asyncio

    _, cloud, k8s = zone
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.sleep_zone("demo", "a")
    a, b = await asyncio.gather(cloud.wake_zone("demo", "a", 2, 0), cloud.wake_zone("demo", "a", 2, 0))
    assert a == b == {"ok": True, "namespace": NS, "workers": 2, "canary": 0, "activator": 0}
    assert replicas(k8s) == {"worker": 2, "worker-canary": 0, ACTIVATOR: 0}


CONSOLE_CHART = CHART.parent / "ramen"
CONSOLE_SETS = {
    "gcp": ("project=p1,region=us-central1,builder.gsa=b@p1.iam.gserviceaccount.com", "gcp", "firestore"),
    "aws": (
        "provider=aws,region=us-east-1,aws.account=123456789012,builder.roleArn=arn:aws:iam::1:role/b",
        "aws",
        "dynamodb",
    ),
    "azure": (
        "provider=azure,region=westus2,azure.acr=r.azurecr.io,azure.storageAccount=s,azure.nodeResourceGroup=MC_x,"
        "azure.clientId=c,azure.publicHostname=h.sslip.io,builder.clientId=b",
        "azure",
        "azuretables",
    ),
}


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize("provider", sorted(CONSOLE_SETS))
def test_the_console_chart_keeps_the_providers_backends_when_the_builder_is_on(provider):
    """1.0.0 AKS run: the §24 env block was merged after the chart's default `console.env` (RAMEN_CLOUD gcp, RAMEN_STORE
    firestore), so an AWS or Azure console came up asking for Google credentials."""
    sets, cloud, store = CONSOLE_SETS[provider]
    out = subprocess.run(
        ["helm", "template", "ramen", str(CONSOLE_CHART), "--set", sets, "--set", "otel.collector=jaeger"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    env = dict(re.findall(r'- \{ name: (\w+), value: "([^"]*)" \}', out))
    assert (env["RAMEN_CLOUD"], env["RAMEN_STORE"]) == (cloud, store), {
        k: env[k] for k in ("RAMEN_CLOUD", "RAMEN_STORE")
    }
    assert env["RAMEN_BUILDER"] == "1" and env["RAMEN_BUILD_REGISTRY"].endswith("/ramen")
    assert env["RAMEN_CONSOLE_INTERNAL_URL"].startswith("http://console.")
