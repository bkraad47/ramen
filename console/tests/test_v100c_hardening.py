"""1.0.0c hardening after the security pass (CONTRACTS §33.4a, D79), console side: the Jobs' own identity, the
promptfoo provider allowlist at sync, provider keys scrubbed, the console's own read of an app, the per-zone egress
policy and the chart's Redis / NetworkPolicies, Azure's blob-prefix condition."""

import json
import logging
import subprocess

import pytest
import yaml

from ramen_console import evals, security
from ramen_console.cloud import aws_k8s, gcp_k8s
from ramen_console.cloud.azure import AzureCloud, blob_condition
from ramen_console.cloud.azure_api import ROLE_IDS
from ramen_console.cloud.azure_k8s import azure_manifests
from tests.fakes_azure import ACCOUNT, ISSUER, LOCATION, RG, SUB, VAULT, FakeAzureClients
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_azure_cloud import SPEC as AZ_SPEC
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixture
from tests.test_v100c_evals import (
    CHART,
    by_kind,
    console_env,
    deploy,
    needs_helm,
    runner,  # noqa: F401 - fixture
    template,
    write_evals,
)  # noqa: F401

KEY = "test-key-not-real-anthropic"  # low entropy on purpose: scanners flag realistic fakes


# allowlist -----------------------------------------------------------------------------------------------------------


def errs(text, configured=(), urls=()):
    return evals.validate(text, configured, urls)


@pytest.mark.parametrize(
    "providers",
    [
        "[mcp]",
        "[http://canary:8080/mcp]",
        "[https://x.example/api, websocket://x.example/ws, ws://x/ws]",
        "[{id: mcp, config: {server: {url: 'http://x'}}}]",
        "[{mcp: {server: {url: 'http://x'}}}]",
        "mcp",
    ],
)
def test_mcp_and_remote_providers_are_always_allowed(providers):
    assert errs(f"providers: {providers}\ntests: []\n") == []


@pytest.mark.parametrize(
    ("pid", "configured", "ok"),
    [
        ("anthropic:messages:claude-3-5-sonnet", ["anthropic"], True),
        ("anthropic:claude", [], False),
        ("openai:gpt-4o-mini", ["openai"], True),
        ("openai:gpt-4o-mini", ["openai_compatible"], True),
        ("openai:gpt-4o-mini", ["anthropic"], False),
        ("azure:chat:my-deployment", ["azure_openai"], True),
        ("azureopenai:chat:d", ["foundry"], True),
        ("azure:chat:d", ["openai"], False),
        ("vertex:gemini-pro", ["vertex"], True),
        ("bedrock:anthropic.claude", ["bedrock"], True),
        ("bedrock:anthropic.claude", ["vertex"], False),
    ],
)
def test_llm_families_need_their_configured_provider(pid, configured, ok):
    out = errs(f"providers: ['{pid}']\n", configured)
    assert (out == []) is ok, out
    if not ok:
        assert pid in out[0] and ("configured" in out[0] or "refused" in out[0])


def test_an_openai_base_url_must_be_a_configured_one():
    text = "providers:\n  - id: openai:gpt-4o\n    config: {apiBaseUrl: %s}\n"
    assert errs(text % "https://proxy.example/v1", ["openai_compatible"], ["https://proxy.example/v1/"]) == []
    out = errs(text % "https://evil.example/v1", ["openai_compatible"], ["https://proxy.example/v1"])
    assert len(out) == 1 and "apiBaseUrl" in out[0] and "openai:gpt-4o" in out[0]
    assert errs(text % "https://x", ["openai"], []) and "apiBaseUrl" in errs(text % "https://x", ["openai"], [])[0]


@pytest.mark.parametrize(
    "pid",
    [
        "exec:python run.py",
        "python:provider.py",
        "javascript:p.js",
        "js:p.js",
        "file://p.js",
        "golang:p.go",
        "webhook:http://x",
        "sequence:x",
        "promptfoo:redteam",
        "echo",
        "ollama:llama3",
    ],
)
def test_code_and_unknown_providers_are_refused_naming_the_entry(pid):
    out = errs(f"providers: ['{pid}']\n", ["anthropic", "openai", "openai_compatible", "vertex", "bedrock"])
    assert len(out) == 1 and pid in out[0] and "refused" in out[0], out
    out = errs(f"providers: [mcp, {{id: '{pid}'}}]\n", ["anthropic"])
    assert len(out) == 1 and pid in out[0]


def test_scripts_outside_providers_are_refused_and_assertions_are_not():
    base = "providers: [mcp]\n"
    assert "extensions" in errs(base + "extensions: ['file://hook.js']\n")[0]
    assert "transform" in errs(base + "transform: 'output.x'\n")[0]
    assert "defaultTest.options.transform" in errs(base + "defaultTest: {options: {transform: 'file://t.js'}}\n")[0]
    assert "tests[2].options.transform" in errs(base + "tests: [{vars: {q: 1}}, {options: {transform: 'x'}}]\n")[0]
    out = errs("providers: [{id: mcp, config: {transform: 'file://t.py'}}]\n")
    assert len(out) == 1 and "config.transform" in out[0] and "mcp" in out[0]
    js = "      - type: javascript\n        value: output.length > 0\n"
    assert errs(base + "tests:\n  - assert:\n" + js + "      - type: python\n        value: 'True'\n") == []
    assert errs("providers: [mcp]\nextensions: []\ntransform: ''\n") == []  # empty = none
    assert errs("") == [] and errs("# nothing\n") == []
    assert "parse" in errs("providers: [mcp\n")[0] and "mapping" in errs("- a\n")[0]
    assert len(errs("providers: [1, {a: 1, b: 2}]\n")) == 2  # malformed entries are named, not skipped


def test_a_refused_provider_fails_the_deploy_at_sync_before_any_zone(demo, runner, tmp_path, ws):  # noqa: F811
    write_evals(tmp_path, "providers:\n  - exec:python steal.py\n  - mcp\ntests: []\n")
    job = deploy(demo)
    assert job["status"] == "error", job
    assert evals.PATH in job["error"] and "exec:python steal.py" in job["error"] and "refused" in job["error"]
    assert "evals" not in (job["result"] or {}).get("zone-a", {}) and runner.specs == []
    assert not any("deploying" in line for line in job["log"])  # stopped at the sync step
    write_evals(tmp_path, "providers: [anthropic:claude-3-5-haiku, mcp]\ntests: []\n")
    job = deploy(demo)
    assert job["status"] == "error" and "anthropic:claude-3-5-haiku" in job["error"] and "configured" in job["error"]
    demo.put("/api/v1/config/llm-providers", json={"anthropic": {"api_key": KEY}})
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert runner.specs and runner.specs[0][2]["secret"]["stringData"]["ANTHROPIC_API_KEY"] == KEY


def test_the_allowlist_runs_without_an_engine_too(demo, tmp_path, monkeypatch):
    monkeypatch.delenv("RAMEN_EVALS_IMAGE", raising=False)
    write_evals(tmp_path, "providers: [python:p.py]\n")
    job = deploy(demo)
    assert job["status"] == "error" and "python:p.py" in job["error"]


# scrubbing -----------------------------------------------------------------------------------------------------------


def test_a_provider_key_is_scrubbed_from_every_log_line_once_loaded(demo, caplog):
    r = demo.put(
        "/api/v1/config/llm-providers",
        json={"anthropic": {"api_key": KEY}, "openai_compatible": {"headers": '{"x-key": "test-header-not-real"}'}},
    )
    assert r.status_code == 200, r.text
    security.scrub_all_logs()  # the handler pytest attached for this test gets the filter the process handlers have
    logging.getLogger("ramen.evals").warning("promptfoo said: env ANTHROPIC_API_KEY=%s x-key=test-header-not-real", KEY)
    assert KEY not in caplog.text and "test-header-not-real" not in caplog.text
    assert "ANTHROPIC_API_KEY=***" in caplog.text
    assert security.scrub(f"reason: {KEY} leaked") == "reason: *** leaked"
    log = security.ScrubbedLog()
    log.append(f"evals case 1: FAILED {KEY}")
    assert log == ["evals case 1: FAILED ***"]


def test_the_evals_key_and_the_redis_password_are_scrubbed(demo, runner, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_REDIS_URL", "redis://:test-redis-pass-not-real@redis.ramen-system.svc.cluster.local:6379")
    write_evals(tmp_path)
    job = deploy(demo)
    assert job["status"] == "ok", job
    key = runner.specs[0][2]["secret"]["stringData"]["RAMEN_MCP_KEY"]
    assert key.startswith("rek_") and security.scrub(f"k={key} p=test-redis-pass-not-real") == "k=*** p=***"
    env_file = open(job["result"]["zone-a"]["env_file"]).read()  # noqa: SIM115
    assert "RAMEN_REDIS_SCOPE_URL=redis://:test-redis-pass-not-real@redis.ramen-system" in env_file  # the default store
    assert "test-redis-pass-not-real" not in json.dumps(job) and key not in json.dumps(job)
    demo.put("/api/v1/groups/demo/throttle", json={"redis_url": "redis://mine:6379", "ip_per_min": 5})
    env_file = open(deploy(demo)["result"]["zone-a"]["env_file"]).read()  # noqa: SIM115
    assert "RAMEN_REDIS_SCOPE_URL=redis://mine:6379\n" in env_file  # the group's own Redis wins


# the deploy's own key ------------------------------------------------------------------------------------------------


def test_the_evals_key_is_derived_per_deploy_and_kept_by_a_config_push(demo, runner, tmp_path):  # noqa: F811
    write_evals(tmp_path)
    job = deploy(demo)
    env_file = job["result"]["zone-a"]["env_file"]
    line = next(ln for ln in open(env_file).read().splitlines() if ln.startswith("RAMEN_EVALS_KEY="))  # noqa: SIM115
    assert line == f"RAMEN_EVALS_KEY={runner.specs[0][2]['secret']['stringData']['RAMEN_MCP_KEY']}"
    assert evals.evals_key("s", job["id"]) != evals.evals_key("s", "other") and len(evals.evals_key("s", "j")) == 36
    r = demo.put("/api/v1/groups/demo/evals", json={"required": True})  # §26.3 push rewrites the zone's config
    assert r.json()["pushed"] == {"demo/zone-a": "ok"}
    assert line in open(env_file).read().splitlines()  # noqa: SIM115 - the same key, not dropped, not rotated
    assert deploy(demo)["status"] == "ok" and line not in open(env_file).read()  # noqa: SIM115 - a deploy rotates it


# the per-zone egress policy ------------------------------------------------------------------------------------------


def egress_of(docs):
    return next((d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "worker-egress"), None)


def test_worker_egress_is_rendered_only_with_the_switch_and_the_service_cidr(monkeypatch):
    args = ("demo", "a", {"region": "us-central1-a", "size": "s", "count": 1}, "img", "gs://b/demo")
    monkeypatch.delenv("RAMEN_WORKER_EGRESS", raising=False)
    assert egress_of(gcp_k8s.manifests(*args)) is None
    monkeypatch.setenv("RAMEN_WORKER_EGRESS", "1")
    monkeypatch.delenv("RAMEN_CLUSTER_SERVICE_CIDR", raising=False)
    assert egress_of(gcp_k8s.manifests(*args)) is None  # no Service range = no policy (the chart's NOTES warn)
    monkeypatch.setenv("RAMEN_CLUSTER_SERVICE_CIDR", "10.96.0.0/16, 10.97.0.0/16")
    monkeypatch.setenv("RAMEN_CONSOLE_PORT", "8000")
    docs = gcp_k8s.manifests(*args)
    np = egress_of(docs)
    assert np["metadata"]["namespace"] == "ramen-demo-a" and np["spec"]["policyTypes"] == ["Egress"]
    assert np["spec"]["podSelector"] == {"matchLabels": {"app": "worker"}}
    rules = np["spec"]["egress"]
    system = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ramen-system"}}}
    assert rules[0]["to"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "kube-system"}
    assert {p["port"] for p in rules[0]["ports"]} == {53} and {p["protocol"] for p in rules[0]["ports"]} == {
        "UDP",
        "TCP",
    }
    console = {**system, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ramen-console"}}}
    assert rules[1] == {"to": [console], "ports": [{"port": 8000, "protocol": "TCP"}]}
    redis = {**system, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "redis"}}}
    legacy = {**system, "podSelector": {"matchLabels": {"app": "redis"}}}  # §38.3: a hand-applied Redis too
    assert rules[2] == {"to": [redis, legacy], "ports": [{"port": 6379, "protocol": "TCP"}]}
    assert rules[3]["ports"] == [{"port": 4318, "protocol": "TCP"}] and "phoenix" in json.dumps(rules[3]["to"])
    assert rules[4] == {
        "to": [{"podSelector": {"matchLabels": {"app": "worker"}}}],
        "ports": [{"port": 8080, "protocol": "TCP"}],
    }
    assert rules[5] == {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.96.0.0/16", "10.97.0.0/16"]}}]}
    assert sum(d["kind"] == "NetworkPolicy" for d in docs) == 2  # the SEC-04 ingress policy is untouched
    assert egress_of(aws_k8s.manifests("demo", "a", args[2], "img", "s3://b/demo")) == np
    az = azure_manifests("demo", "a", AZ_SPEC, "img", "az://a/groups/demo", "cid")
    assert egress_of(az) == np and next(d for d in az if d["metadata"]["name"] == "worker")["kind"] == "NetworkPolicy"


# Azure ABAC ----------------------------------------------------------------------------------------------------------


async def test_the_azure_blob_grant_carries_the_group_prefix_condition():
    fk = FakeAzureClients()
    az = AzureCloud(
        subscription=SUB,
        resource_group=RG,
        location=LOCATION,
        storage_account=ACCOUNT,
        key_vault=VAULT,
        oidc_issuer=ISSUER,
        image="acr.example/worker:1",
        admin_key="adm",
        clients=fk,
        wait_secs=1,
        node_wait_secs=2,
        poll=0,
    )
    await az.attach_zone("demo", "eus1", AZ_SPEC)
    puts = [c for c in fk.arm.calls if c[0] == "PUT" and "roleAssignments" in c[1]]
    blob = [c for c in puts if c[2]["properties"]["roleDefinitionId"].endswith(ROLE_IDS["Storage Blob Data Reader"])]
    assert len(blob) == 1
    props = blob[0][2]["properties"]
    assert props["conditionVersion"] == "2.0" and props["condition"] == blob_condition("demo")
    assert blob_condition("demo") == (
        "(!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'} AND NOT "
        "SubOperationMatches{'Blob.List'}) OR @Resource[Microsoft.Storage/storageAccounts/blobServices/containers/"
        "blobs:path] StringStartsWith 'demo/') AND (!(ActionMatches{'Microsoft.Storage/storageAccounts/blobServices/"
        "containers/blobs/read'} AND SubOperationMatches{'Blob.List'}) OR @Request[Microsoft.Storage/storageAccounts/"
        "blobServices/containers/blobs:prefix] StringStartsWith 'demo/')"
    )
    assert all("condition" not in c[2]["properties"] for c in puts if c is not blob[0])  # Key Vault grants: plain


# chart ---------------------------------------------------------------------------------------------------------------

ALL = ("evals.engine=promptfoo", "otel.collector=phoenix", "skillspector.engine=skillspector", "redis.enabled=true")


def netpol(docs, name):
    return by_kind(docs, "NetworkPolicy", name)


def peers(np, i=0):
    return json.dumps(np["spec"]["ingress"][i]["from"], sort_keys=True)


@needs_helm
def test_system_network_policies_by_default_and_who_they_admit():
    rc, err, docs = template(*ALL, "cluster.serviceCidr=10.96.0.0/16", "console.sourceCidrs={10.7.128.0/17}")
    assert rc == 0, err
    console = netpol(docs, "console")
    assert console["spec"]["ingress"][0]["ports"] == [{"port": 8000, "protocol": "TCP"}]
    p = peers(console)
    assert "35.191.0.0/16" in p and "130.211.0.0/22" in p and "10.7.128.0/17" in p  # the edge (+ console.sourceCidrs)
    assert '"key": "ramen.io/group", "operator": "Exists"' in p  # the worker namespaces: /otlp, wake, Admin callbacks
    assert '"values": ["evals", "scan"]' in p
    phoenix = netpol(docs, "phoenix")
    assert phoenix["spec"]["ingress"][0]["ports"] == [{"port": 4318, "protocol": "TCP"}]
    assert "ramen-console" in peers(phoenix) and "ramen.io/group" in peers(phoenix) and "ramen.io/job" in peers(phoenix)
    assert phoenix["spec"]["ingress"][1]["ports"] == [{"port": 6006, "protocol": "TCP"}]  # D92: the UI port only
    assert "35.191.0.0/16" in peers(phoenix, 1)  # the UI: edge only
    promptfoo = netpol(docs, "promptfoo")
    assert promptfoo["spec"]["ingress"][0]["ports"] == [{"port": 3443, "protocol": "TCP"}]  # D86: the TLS front
    assert "ramen.io/job" in peers(promptfoo) and "ramen.io/group" not in peers(promptfoo)  # never the workers
    redis = netpol(docs, "redis")
    assert redis["spec"]["ingress"][0]["ports"] == [{"port": 6379, "protocol": "TCP"}]
    assert "ramen.io/group" in peers(redis) and "ramen-console" in peers(redis) and "ipBlock" not in peers(redis)
    env = console_env(docs)
    assert env["RAMEN_WORKER_EGRESS"] == "1" and env["RAMEN_CLUSTER_SERVICE_CIDR"] == "10.96.0.0/16"
    rc, _, docs = template(*ALL)  # no Service range: the console writes no egress policy, the NOTES warn
    assert rc == 0 and "RAMEN_WORKER_EGRESS" not in console_env(docs) and netpol(docs, "console")
    rc, _, docs = template(*ALL, "networkPolicy.system=false")
    assert rc == 0 and not any(d["kind"] == "NetworkPolicy" for d in docs)
    rc, _, docs = template("otel.collector=jaeger")
    assert rc == 0 and netpol(docs, "jaeger") and netpol(docs, "promptfoo") is None and netpol(docs, "redis") is None
    rc, _, docs = template(*ALL, "provider=aws", "aws.account=1", "networkPolicy.awsVpcCidr=10.9.0.0/16")
    assert (
        rc == 0 and "10.9.0.0/16" in peers(netpol(docs, "console")) and "35.191" not in peers(netpol(docs, "console"))
    )
    rc, _, docs = template(*ALL, *AZURE)
    assert (
        rc == 0
        and "envoy-gateway-system" in peers(netpol(docs, "console"))
        and "ipBlock" not in peers(netpol(docs, "console"))
    )
    rc, _, docs = template(
        *ALL, "networkPolicy.edgeCidrs={192.0.2.0/24}", "networkPolicy.edgeNamespaces={ingress-nginx}"
    )
    p = peers(netpol(docs, "console"))
    assert "192.0.2.0/24" in p and "ingress-nginx" in p and "35.191" not in p


AZURE = ("provider=azure", "azure.nodeResourceGroup=MC_x", "azure.clientId=x", "azure.publicHostname=h.sslip.io")


@needs_helm
def test_redis_runs_with_a_generated_password_the_console_reads_from_the_secret():
    rc, err, docs = template("redis.enabled=true")
    assert rc == 0, err
    secret = by_kind(docs, "Secret", "ramen-redis")
    pw = secret["stringData"]["password"]
    assert len(pw) == 32 and secret["stringData"]["url"] == f"rediss://:{pw}@redis.ramen-system.svc.cluster.local:6379"
    dep = by_kind(docs, "Deployment", "redis")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "redis:7.4-alpine" and c["args"][:2] == ["--requirepass", "$(REDIS_PASSWORD)"]
    assert c["env"] == [
        {"name": "REDIS_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "ramen-redis", "key": "password"}}}
    ]
    assert pw not in json.dumps(dep) and by_kind(docs, "Service", "redis")["spec"]["ports"][0]["port"] == 6379
    console = by_kind(docs, "Deployment", "console")["spec"]["template"]["spec"]["containers"][0]
    url = next(e for e in console["env"] if e["name"] == "RAMEN_REDIS_URL")
    assert url == {"name": "RAMEN_REDIS_URL", "valueFrom": {"secretKeyRef": {"name": "ramen-redis", "key": "url"}}}
    assert pw not in json.dumps(console)
    rc, _, docs = template("redis.enabled=true", "redis.auth.existingSecret=my-redis")
    assert rc == 0 and by_kind(docs, "Secret", "ramen-redis") is None
    assert "my-redis" in json.dumps(by_kind(docs, "Deployment", "redis")) and "my-redis" in json.dumps(
        console_env_raw(docs)
    )
    rc, err, _ = template("redis.enabled=true", "redis.auth.generate=false")
    assert rc != 0 and "password" in err
    rc, _, docs = template()
    assert rc == 0 and by_kind(docs, "Deployment", "redis") is None and "RAMEN_REDIS_URL" not in json.dumps(docs)


@needs_helm
def test_a_managed_redis_url_goes_into_the_secret_and_no_redis_runs():
    """1.0.0c: `redis.externalUrl` (Azure Managed Redis, TLS-only `rediss://`): Secret `ramen-redis` carries the URL
    and its password, the console reads RAMEN_REDIS_URL from it, no Deployment/Service/NetworkPolicy for a Redis."""
    cred = "-".join(["fake", "redis", "cred"])  # built, not a literal: scanners flag realistic fakes
    url = f"rediss://:{cred}@ramen.westus2.redis.azure.net:10000/0"
    rc, err, docs = template(f"redis.externalUrl={url}")
    assert rc == 0, err
    secret = by_kind(docs, "Secret", "ramen-redis")
    assert secret["stringData"] == {"password": cred, "url": url}
    assert by_kind(docs, "Deployment", "redis") is None and by_kind(docs, "Service", "redis") is None
    assert netpol(docs, "redis") is None
    console = by_kind(docs, "Deployment", "console")["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "RAMEN_REDIS_URL", "valueFrom": {"secretKeyRef": {"name": "ramen-redis", "key": "url"}}} in console[
        "env"
    ]
    assert cred not in json.dumps(console)
    rc, err, _ = template(f"redis.externalUrl={url}", "redis.enabled=true")
    assert rc != 0 and "not both" in err
    rc, _, docs = template(f"redis.externalUrl={url}", "redis.auth.existingSecret=mine")  # the operator's own Secret
    assert rc == 0 and by_kind(docs, "Secret", "ramen-redis") is None and "mine" in json.dumps(console_env_raw(docs))


def console_env_raw(docs):
    return by_kind(docs, "Deployment", "console")["spec"]["template"]["spec"]["containers"][0]["env"]


@needs_helm
def test_the_evals_service_account_carries_the_cloud_identity_and_nothing_else():
    rc, _, docs = template("evals.gsa=ramen-evals@p.iam.gserviceaccount.com")
    sa = by_kind(docs, "ServiceAccount", "ramen-evals")
    assert rc == 0 and sa["metadata"]["annotations"] == {
        "iam.gke.io/gcp-service-account": "ramen-evals@p.iam.gserviceaccount.com"
    }
    assert sa["automountServiceAccountToken"] is False and by_kind(docs, "ServiceAccount", "ramen-builder") is None
    rc, _, docs = template("provider=aws", "aws.account=1", "evals.roleArn=arn:aws:iam::1:role/ramen/ramen-evals")
    assert rc == 0 and by_kind(docs, "ServiceAccount", "ramen-evals")["metadata"]["annotations"] == {
        "eks.amazonaws.com/role-arn": "arn:aws:iam::1:role/ramen/ramen-evals"
    }
    rc, _, docs = template(*AZURE, "azure.tenantId=t", "evals.clientId=c-evals")
    assert rc == 0 and by_kind(docs, "ServiceAccount", "ramen-evals")["metadata"]["annotations"] == {
        "azure.workload.identity/client-id": "c-evals",
        "azure.workload.identity/tenant-id": "t",
    }
    rc, _, docs = template("evals.serviceAccount=evals-x", "builder.gsa=b@p.iam.gserviceaccount.com")
    assert rc == 0 and by_kind(docs, "ServiceAccount", "evals-x") and console_env(docs)["RAMEN_EVALS_SA"] == "evals-x"
    # §38.2: a legacy builder value still turns builds on, but no shared builder SA exists any more (per group instead)
    assert by_kind(docs, "ServiceAccount", "ramen-builder") is None and console_env(docs)["RAMEN_BUILDER"] == "1"
    bound = {s["name"] for d in docs if d["kind"] in ("RoleBinding", "ClusterRoleBinding") for s in d["subjects"]}
    assert "ramen-evals" not in bound and "evals-x" not in bound
    rc, _, docs = template("evals.image=")
    assert (
        rc == 0 and by_kind(docs, "ServiceAccount", "ramen-evals") is None and "RAMEN_EVALS_SA" not in console_env(docs)
    )


@needs_helm
@pytest.mark.parametrize("flags", [(), ("provider=aws", "aws.account=1"), AZURE])
def test_helm_lint_with_everything_on(flags):
    r = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "--set",
            "project=p",
            *[x for s in (*ALL, "cluster.serviceCidr=10.96.0.0/16", *flags) for x in ("--set", s)],
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_kind_values_turn_the_hardening_on():
    values = yaml.safe_load((CHART.parents[1] / "kind" / "values.yaml").read_text())
    assert values["redis"]["enabled"] is True and values["cluster"]["serviceCidr"] == "10.96.0.0/16"
    up = (CHART.parents[1] / "kind" / "up.sh").read_text()
    assert "networkPolicy.edgeCidrs=$EDGE_JSON" in up


# S6b: the AWS edge CIDR reaches the console ------------------------------------------------------------------------


ALB = "10.42.0.0/24,10.42.1.0/24"  # D89: the home public subnets (nodes + ALB ENIs); pods live in 10.42.64.0/20 …
POD_IP = "10.42.64.10"  # … so a pod in any namespace has an address like this one


def admits(peer_cidrs, ip) -> bool:
    import ipaddress

    return any(ipaddress.ip_address(ip) in ipaddress.ip_network(c) for c in peer_cidrs)


def ip_blocks(policy, i=0) -> list[str]:
    return [p["ipBlock"]["cidr"] for p in policy["spec"]["ingress"][i]["from"] if "ipBlock" in p]


@needs_helm
def test_chart_hands_the_alb_subnets_to_the_console():
    """D89: `networkPolicy.awsAlbCidrs` (Terraform `network_set`) drives the ramen-system edge and, as
    RAMEN_AWS_ALB_CIDRS, the zone policies; a pod-subnet source is admitted by neither. The deprecated
    `networkPolicy.awsVpcCidr` and `console.env.RAMEN_AWS_VPC_CIDR` still render the same ranges as before."""
    rc, err, docs = template("provider=aws", "aws.account=1", "networkPolicy.awsAlbCidrs={10.42.0.0/24,10.42.1.0/24}")
    assert rc == 0, err
    assert console_env(docs)["RAMEN_AWS_ALB_CIDRS"] == ALB and "RAMEN_AWS_VPC_CIDR" not in console_env(docs)
    edge = ip_blocks(netpol(docs, "console"))
    assert set(edge) == set(ALB.split(",")) and not admits(edge, POD_IP)
    rc, _, docs = template("provider=aws", "aws.account=1", "networkPolicy.awsVpcCidr=10.42.0.0/16")  # deprecated
    assert console_env(docs)["RAMEN_AWS_ALB_CIDRS"] == "10.42.0.0/16" and ip_blocks(netpol(docs, "console")) == [
        "10.42.0.0/16"
    ]
    rc, _, docs = template("provider=aws", "aws.account=1")  # chart default = the console's default
    assert console_env(docs)["RAMEN_AWS_ALB_CIDRS"] == "10.0.0.0/16"
    old = r"console.env.RAMEN_AWS_VPC_CIDR=10.42.0.0/16\,10.43.0.0/16"  # a pre-D89 satellite override
    rc, _, docs = template("provider=aws", "aws.account=1", old)
    assert console_env(docs)["RAMEN_AWS_ALB_CIDRS"] == "10.42.0.0/16,10.43.0.0/16"
    both = r"console.env.RAMEN_AWS_ALB_CIDRS=10.42.0.0/24\,10.42.1.0/24\,10.43.0.0/24\,10.43.1.0/24"
    rc, _, docs = template(
        "provider=aws", "aws.account=1", "networkPolicy.awsAlbCidrs={10.42.0.0/24,10.42.1.0/24}", both
    )
    assert console_env(docs)["RAMEN_AWS_ALB_CIDRS"].endswith("10.43.1.0/24")  # console.env still wins
    assert set(ip_blocks(netpol(docs, "console"))) == set(ALB.split(","))  # the satellites' stay out of ramen-system
    rc, _, docs = template()
    assert "RAMEN_AWS_ALB_CIDRS" not in console_env(docs) and "RAMEN_AWS_VPC_CIDR" not in console_env(docs)


def test_aws_zone_policy_admits_the_alb_subnets_only(monkeypatch):
    """D89: RAMEN_AWS_ALB_CIDRS (home + satellite public subnets) becomes one ipBlock each and a pod-subnet source
    matches none; the deprecated RAMEN_AWS_VPC_CIDR is read when it is absent; 10.0.0.0/16 without either."""
    spec = {"region": "us-east-1a", "size": "s", "count": 1}
    monkeypatch.delenv("RAMEN_AWS_ALB_CIDRS", raising=False)
    monkeypatch.delenv("RAMEN_AWS_VPC_CIDR", raising=False)
    assert aws_k8s.aws_alb_cidrs() == ["10.0.0.0/16"]
    monkeypatch.setenv("RAMEN_AWS_VPC_CIDR", "10.42.0.0/16, 10.43.0.0/16,")
    assert aws_k8s.aws_alb_cidrs() == ["10.42.0.0/16", "10.43.0.0/16"]
    monkeypatch.setenv("RAMEN_AWS_ALB_CIDRS", ALB + ",10.43.0.0/24, 10.43.1.0/24")
    assert aws_k8s.aws_alb_cidrs() == [*ALB.split(","), "10.43.0.0/24", "10.43.1.0/24"]
    np = next(d for d in aws_k8s.manifests("demo", "a", spec, "img", "s3://b/demo") if d["kind"] == "NetworkPolicy")
    cidrs = ip_blocks(np)
    assert cidrs == aws_k8s.aws_alb_cidrs()
    assert admits(cidrs, "10.42.0.37") and not admits(cidrs, POD_IP) and not admits(cidrs, "10.43.80.5")


@needs_helm
def test_aws_worker_chart_path_gets_the_same_alb_cidrs(monkeypatch):
    monkeypatch.setenv("RAMEN_AWS_ALB_CIDRS", ALB)
    chart = CHART.parent / "ramen-worker"
    spec = {"region": "us-east-1a", "size": "s", "count": 1}
    docs = aws_k8s.helm_manifests(chart, "demo", "a", spec, "img", "s3://b/demo")
    np = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "worker")
    cidrs = ip_blocks(np)
    assert set(cidrs) == set(ALB.split(",")) and not admits(cidrs, POD_IP)
