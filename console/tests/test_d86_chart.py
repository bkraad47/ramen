"""D86 / CONTRACTS §39: the chart runs every in-cluster service on TLS and refuses to render plaintext."""

import pytest

from tests.test_v100c_evals import by_kind, console_env, needs_helm, template

pytestmark = needs_helm


def _c(docs, name, container=0):
    return by_kind(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][container]


def _vols(docs, name):
    return {v["name"]: v for v in by_kind(docs, "Deployment", name)["spec"]["template"]["spec"].get("volumes", [])}


def test_plaintext_is_refused():
    rc, err, _ = template("tls.require=false")
    assert rc != 0 and "D86" in err and "plaintext is refused" in err


def test_the_console_serves_tls_and_is_told_to_require_it():
    rc, err, docs = template("redis.enabled=true", "otel.collector=phoenix", "evals.engine=promptfoo")
    assert rc == 0, err
    env = console_env(docs)
    assert env["RAMEN_REQUIRE_TLS"] == "1" and env["RAMEN_NAMESPACE"] == "ramen-system"
    assert env["RAMEN_CONSOLE_INTERNAL_URL"] == "https://console.ramen-system.svc.cluster.local:443"
    assert env["RAMEN_TLS_SERVICES"] == "redis,phoenix,promptfoo"
    assert env["RAMEN_EVALS_API_URL"] == "https://promptfoo.ramen-system:3000"
    assert env["RAMEN_OTEL_UI_URL"] == "https://phoenix.ramen-system:6006"
    c = _c(docs, "console")
    assert c["readinessProbe"]["httpGet"]["scheme"] == "HTTPS" and c["livenessProbe"]["httpGet"]["scheme"] == "HTTPS"
    svc = by_kind(docs, "Service", "console")["spec"]["ports"][0]
    assert svc["port"] == 443 and svc["targetPort"] == "https" and svc["appProtocol"] == "HTTPS"
    route = by_kind(docs, "HTTPRoute", "console")["spec"]["rules"][0]["backendRefs"][0]
    assert route["port"] == 443
    hcp = by_kind(docs, "HealthCheckPolicy", "console")["spec"]["default"]["config"]
    assert hcp["type"] == "HTTPS" and hcp["httpsHealthCheck"]["requestPath"] == "/healthz"


def test_the_console_may_write_only_the_tls_objects():
    rc, err, docs = template()
    assert rc == 0, err
    rules = by_kind(docs, "Role", "ramen-console-tls")["rules"]
    get_secrets = next(r for r in rules if r["resources"] == ["secrets"] and "get" in r["verbs"])
    assert set(get_secrets["resourceNames"]) == {f"ramen-tls-{s}" for s in ("redis", "phoenix", "jaeger", "promptfoo")}
    zone = by_kind(docs, "ClusterRole", "ramen-console-zone")["rules"][0]
    assert "configmaps" in zone["resources"]


def test_redis_is_tls_only():
    rc, err, docs = template("redis.enabled=true")
    assert rc == 0, err
    args = _c(docs, "redis")["args"]
    assert ["--port", "0"] == args[args.index("--port") : args.index("--port") + 2]
    assert "--tls-port" in args and "--tls-auth-clients" in args
    assert _vols(docs, "redis")["tls"]["secret"]["secretName"] == "ramen-tls-redis"
    url = by_kind(docs, "Secret", "ramen-redis")["stringData"]["url"]
    assert url.startswith("rediss://:") and url.endswith("@redis.ramen-system.svc.cluster.local:6379")


def test_phoenix_and_its_collector_speak_tls():
    rc, err, docs = template("otel.collector=phoenix")
    assert rc == 0, err
    cfg = by_kind(docs, "ConfigMap", "phoenix-collector")["data"]["config.yaml"]
    assert "cert_file: /tls/tls.crt" in cfg and "https://127.0.0.1:6006" in cfg and "ca_file: /tls/ca.crt" in cfg
    env = {e["name"]: e.get("value") for e in _c(docs, "phoenix")["env"]}
    assert env["PHOENIX_TLS_ENABLED"] == "true" and env["PHOENIX_TLS_CERT_FILE"] == "/tls/tls.crt"
    assert _c(docs, "phoenix")["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert _c(docs, "phoenix", 1)["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert _vols(docs, "phoenix")["tls"]["secret"]["secretName"] == "ramen-tls-phoenix"


def test_jaeger_speaks_tls():
    rc, err, docs = template("otel.collector=jaeger")
    assert rc == 0, err
    args = _c(docs, "jaeger")["args"]
    for flag in ("collector.otlp.http.tls", "collector.otlp.grpc.tls", "admin.http.tls"):  # D92: query → its TLS front
        assert f"--{flag}.enabled=true" in args and f"--{flag}.cert=/tls/tls.crt" in args
    assert console_env(docs)["RAMEN_TLS_SERVICES"] == "jaeger"


def test_promptfoo_sits_behind_the_tls_proxy():
    rc, err, docs = template("evals.engine=promptfoo")
    assert rc == 0, err
    proxy = _c(docs, "promptfoo", 1)
    assert proxy["command"][:3] == ["python", "-m", "ramen_console.tlsproxy"]
    assert "--upstream" in proxy["command"] and "127.0.0.1:3000" in proxy["command"]
    assert proxy["readinessProbe"]["tcpSocket"]["port"] == "https"  # D92: every HTTP path wants the credential
    svc = by_kind(docs, "Service", "promptfoo")["spec"]["ports"][0]
    assert svc["port"] == 3000 and svc["targetPort"] == "https"
    np = by_kind(docs, "NetworkPolicy", "promptfoo")["spec"]["ingress"][0]["ports"]
    assert np == [{"port": 3443, "protocol": "TCP"}]  # the loopback-only plaintext port is closed to other pods


@pytest.mark.parametrize("provider", ["aws", "azure"])
def test_every_edge_reaches_the_console_over_tls(provider):
    sets = {
        "aws": ("provider=aws", "aws.account=123456789012"),
        "azure": (
            "provider=azure",
            "azure.publicHostname=1-2-3-4.sslip.io",
            "azure.nodeResourceGroup=mc",
            "azure.acr=r.azurecr.io",
            "azure.clientId=c",
        ),
    }[provider]
    rc, err, docs = template(*sets)
    assert rc == 0, err
    if provider == "aws":
        ing = by_kind(docs, "Ingress", "console")
        ann = ing["metadata"]["annotations"]
        assert ann["alb.ingress.kubernetes.io/backend-protocol"] == "HTTPS"
        assert ann["alb.ingress.kubernetes.io/healthcheck-protocol"] == "HTTPS"
        assert ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"]["number"] == 443
    else:
        p = by_kind(docs, "BackendTLSPolicy", "console")
        assert p["spec"]["validation"]["hostname"] == "console.ramen-system.svc.cluster.local"
        assert p["spec"]["validation"]["caCertificateRefs"] == [{"group": "", "kind": "ConfigMap", "name": "ramen-ca"}]
        assert by_kind(docs, "HTTPRoute", "console")["spec"]["rules"][0]["backendRefs"][0]["port"] == 443
        rules = by_kind(docs, "ClusterRole", "ramen-console")["rules"]
        assert any("backendtlspolicies" in r["resources"] for r in rules)


def test_extra_tls_services_get_a_leaf_too():
    """kind's fake LLM judge (1.0.0): an in-cluster service outside the chart gets an install-CA leaf
    (`Secret ramen-tls-<name>`) by naming it in `tls.extraServices`."""
    rc, err, docs = template("redis.enabled=true", "tls.extraServices={fake-llm}")
    assert rc == 0, err
    assert console_env(docs)["RAMEN_TLS_SERVICES"] == "redis,fake-llm"
    role = next(d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == "ramen-console-tls")
    names = [n for r in role["rules"] for n in r.get("resourceNames", [])]
    assert "ramen-tls-fake-llm" in names and "fake-llm" in names, "the console may write its leaf and restart it"
