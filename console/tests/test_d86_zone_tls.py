"""D86 / CONTRACTS §39.2–§39.3: zone manifests carry the leaf, HTTPS probes and TLS backends on every cloud."""

import pytest
from cryptography import x509

from ramen_console import evals, tls
from ramen_console.cloud import aws_k8s, azure_k8s, zone_tls
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.gcp_k8s import activator, manifests
from ramen_console.grpcclient import Client
from tests.fake_grpc import FakeWorker
from tests.fakes_gcp import FakeClients

SPEC = {"region": "us-central1-a", "size": "s", "count": 1}
NS = "ramen-demo-a"


@pytest.fixture(autouse=True)
def _ca(monkeypatch):
    monkeypatch.delenv("RAMEN_TLS_CA_DIR", raising=False)
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    ca = tls.new_ca()
    tls.set_current(ca)
    yield ca
    tls.set_current(None)


def _by(docs, kind, name=None):
    return [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]


def _container(dep):
    return dep["spec"]["template"]["spec"]["containers"][0]


def _env(c):
    return {e["name"]: e.get("value") for e in c["env"]}


def _assert_worker_tls(dep):
    c, pod = _container(dep), dep["spec"]["template"]["spec"]
    env = _env(c)
    assert env["RAMEN_TLS_CERT"] == "/etc/ramen/tls/tls.crt" and env["RAMEN_TLS_KEY"] == "/etc/ramen/tls/tls.key"
    assert env["RAMEN_TLS_CA"] == "/etc/ramen/tls/ca.crt" and env["RAMEN_REQUIRE_TLS"] == "1"
    assert {"name": "ramen-tls", "mountPath": "/etc/ramen/tls", "readOnly": True} in c["volumeMounts"]
    vol = next(v for v in pod["volumes"] if v["name"] == "ramen-tls")
    assert vol["secret"]["secretName"] == "ramen-tls" and "items" not in vol["secret"]  # a directory: renewals land
    assert c["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 8080, "scheme": "HTTPS"}
    assert c["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8080, "scheme": "HTTPS"}
    # an explicit null: the update is a strategic merge patch, which would keep a pre-D86 grpc handler next to the
    # httpGet one ("may not specify more than 1 handler type", GKE b30)
    assert c["readinessProbe"]["grpc"] is None and c["livenessProbe"]["grpc"] is None


def test_tls_objects_hold_the_leaf_and_the_bundle(_ca):
    secret, cm = zone_tls.objects("demo", "a")
    assert secret["type"] == "kubernetes.io/tls" and secret["metadata"]["namespace"] == NS
    data = secret["stringData"]
    assert data["ca.crt"] == _ca.bundle and cm["data"]["ca.crt"] == _ca.bundle and cm["metadata"]["name"] == "ramen-ca"
    c = x509.load_pem_x509_certificate(data["tls.crt"].encode())
    c.verify_directly_issued_by(x509.load_pem_x509_certificate(_ca.cert.encode()))
    sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert tls.WORKER_NAME in sans and f"worker.{NS}.svc.cluster.local" in sans


def test_objects_need_a_loaded_ca():
    tls.set_current(None)
    with pytest.raises(tls.CaError):
        zone_tls.objects("demo", "a")


def test_gke_backends_speak_http2_over_tls():
    docs = zone_tls.secure(manifests("demo", "a", SPEC, "img", "gs://b/demo"), "GCP")
    for d in _by(docs, "Deployment"):
        _assert_worker_tls(d)
    for s in _by(docs, "Service"):
        assert s["spec"]["ports"][0]["appProtocol"] == "HTTP2"
    hcp = _by(docs, "HealthCheckPolicy")[0]["spec"]["default"]["config"]
    # grpcHealthCheck null: the update is a merge patch, which kept the pre-D86 GRPC check (3 items, GKE b31)
    assert hcp == {
        "type": "HTTP2",
        "http2HealthCheck": {"port": 8080, "requestPath": "/readyz"},
        "grpcHealthCheck": None,
    }


def test_alb_target_groups_are_https():
    docs = zone_tls.secure(aws_k8s.manifests("demo", "a", SPEC, "img", "s3://b/demo"), "AWS")
    for d in _by(docs, "Deployment"):
        _assert_worker_tls(d)
    for ing in _by(docs, "Ingress"):
        ann = ing["metadata"]["annotations"]
        assert ann["alb.ingress.kubernetes.io/backend-protocol"] == "HTTPS"
        assert ann["alb.ingress.kubernetes.io/healthcheck-protocol"] == "HTTPS"
    grpc = _by(docs, "Ingress", "worker")[0]["metadata"]["annotations"]
    assert grpc["alb.ingress.kubernetes.io/backend-protocol-version"] == "GRPC"
    for s in _by(docs, "Service"):
        assert s["spec"]["ports"][0]["appProtocol"] == "HTTP2"


def test_envoy_verifies_the_worker_with_a_backend_tls_policy():
    docs = zone_tls.secure(azure_k8s.azure_manifests("demo", "a", SPEC, "img", "az://a/groups"), "Azure")
    for d in _by(docs, "Deployment"):
        _assert_worker_tls(d)
    for s in _by(docs, "Service"):  # Envoy's HTTP/2 signal; the policy adds the TLS
        assert s["spec"]["ports"][0]["appProtocol"] == "kubernetes.io/h2c"
    (p,) = _by(docs, "BackendTLSPolicy")
    assert p["apiVersion"] == "gateway.networking.k8s.io/v1" and p["metadata"]["namespace"] == NS
    assert p["spec"]["targetRefs"] == [{"group": "", "kind": "Service", "name": "worker"}]
    assert p["spec"]["validation"] == {
        "caCertificateRefs": [{"group": "", "kind": "ConfigMap", "name": "ramen-ca"}],
        "hostname": tls.WORKER_NAME,
    }


def test_secure_is_idempotent_and_leaves_other_docs_alone():
    once = zone_tls.secure(manifests("demo", "a", SPEC, "img", "gs://b/demo"), "GCP")
    twice = zone_tls.secure([dict(d) for d in once], "GCP")
    dep = _by(twice, "Deployment", "worker")[0]
    assert sum(1 for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "ramen-tls") == 1
    assert [e["name"] for e in _container(dep)["env"]].count("RAMEN_TLS_CERT") == 1
    assert _by(twice, "NetworkPolicy") == _by(once, "NetworkPolicy")


def test_the_activator_inherits_the_tls_volume_and_probe():
    docs = zone_tls.secure(manifests("demo", "a", SPEC, "img", "gs://b/demo"), "GCP")
    act = activator(_by(docs, "Deployment", "worker")[0], {"app": "worker"})
    c, pod = _container(act), act["spec"]["template"]["spec"]
    assert {"name": "ramen-tls", "mountPath": "/etc/ramen/tls", "readOnly": True} in c["volumeMounts"]
    assert any(v["name"] == "ramen-tls" for v in pod["volumes"])
    assert c["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    env = _env(c)
    assert env["RAMEN_CONSOLE_INTERNAL_URL"].startswith("https://") and env["RAMEN_TLS_CA"] == "/etc/ramen/tls/ca.crt"


def test_the_activator_of_a_plaintext_zone_stays_plain(monkeypatch):
    monkeypatch.delenv("RAMEN_REQUIRE_TLS")
    act = activator(_by(manifests("demo", "a", SPEC, "img", "gs://b/demo"), "Deployment", "worker")[0], {})
    assert "volumeMounts" not in _container(act)


def test_the_internal_url_defaults_to_https_under_the_flag(monkeypatch):
    from ramen_console.cloud.gcp_k8s import console_internal_url

    monkeypatch.delenv("RAMEN_CONSOLE_INTERNAL_URL", raising=False)
    assert console_internal_url() == "https://console.ramen-system.svc.cluster.local:443"
    monkeypatch.delenv("RAMEN_REQUIRE_TLS")
    assert console_internal_url() == "http://console.ramen-system.svc.cluster.local:80"


@pytest.fixture
def gcp():
    w = FakeWorker(admin_key="adm", mcp_keys=("rmk_1",)).start()
    fk = FakeClients()
    c = GcpCloud(
        project="p1",
        region="us-central1",
        bucket="p1-groups",
        image="img/worker:1.0.0",
        admin_key="adm",
        clients=fk,
        rpc=Client(resolve=w.resolve, deadline=5),
        wait_secs=5,
        poll=0.01,
    )
    yield c, fk
    w.stop()


async def test_attach_writes_the_leaf_before_the_workers(gcp, _ca):
    cloud, fk = gcp
    await cloud.attach_zone("demo", "a", SPEC)
    secret = fk.k8s.objs[("Secret", NS, "ramen-tls")]
    assert secret["stringData"]["ca.crt"] == _ca.bundle
    assert ("ConfigMap", NS, "ramen-ca") in fk.k8s.objs
    _assert_worker_tls(fk.k8s.objs[("Deployment", NS, "worker")])
    order = [c[1:] for c in fk.k8s.calls if c[0] == "create"]
    assert order.index(("Secret", NS, "ramen-tls")) < order.index(("Deployment", NS, "worker"))


async def test_renew_rewrites_only_due_leaves(gcp, _ca, monkeypatch):
    cloud, fk = gcp
    await cloud.attach_zone("demo", "a", SPEC)
    first = fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"]
    assert await cloud.renew_tls() == {"zones": [], "system": []}
    assert fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"] == first
    tls.set_current(tls.CA(*tls._fresh(), _ca.cert))  # rotated: every leaf is due
    out = await cloud.renew_tls()
    assert out["zones"] == [NS]
    assert fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"] != first


async def test_system_tls_writes_one_secret_per_chart_service_and_restarts_on_renewal(gcp, _ca, monkeypatch):
    cloud, fk = gcp
    monkeypatch.setenv("RAMEN_TLS_SERVICES", "redis,phoenix")
    monkeypatch.setenv("RAMEN_NAMESPACE", "ramen-system")
    fk.k8s.objs[("RoleBinding", "ramen-system", "ramen-console")] = {}  # the chart's Role ramen-console-tls
    assert await cloud.ensure_system_tls() == ["redis", "phoenix"]
    s = fk.k8s.objs[("Secret", "ramen-system", "ramen-tls-redis")]
    c = x509.load_pem_x509_certificate(s["stringData"]["tls.crt"].encode())
    sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "redis.ramen-system.svc.cluster.local" in sans
    assert fk.k8s.objs[("ConfigMap", "ramen-system", "ramen-ca")]["data"]["ca.crt"] == _ca.bundle
    assert await cloud.ensure_system_tls() == []  # fresh: nothing rewritten, nothing restarted
    tls.set_current(tls.CA(*tls._fresh(), _ca.cert))
    restarted = []
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: restarted.append((ns, name)))
    assert await cloud.ensure_system_tls() == ["redis", "phoenix"]
    assert restarted == [("ramen-system", "redis"), ("ramen-system", "phoenix")]


def test_evals_job_reaches_the_canary_by_the_worker_name(monkeypatch):
    monkeypatch.setenv("RAMEN_EVALS_SA", "ramen-evals")
    spec = evals.job_spec("demo", "a", "tests: []", {"RAMEN_MCP_URL": "x"}, image="pf", worker_ip="10.4.0.7")
    pod = spec["job"]["spec"]["template"]["spec"]
    assert pod["hostAliases"] == [{"ip": "10.4.0.7", "hostnames": [tls.WORKER_NAME]}]
    c = pod["containers"][0]
    assert {"name": "NODE_EXTRA_CA_CERTS", "value": "/etc/ramen/ca/ca.crt"} in c["env"]
    assert {"name": "ramen-ca", "mountPath": "/etc/ramen/ca", "readOnly": True} in c["volumeMounts"]
    assert {"name": "ramen-ca", "configMap": {"name": "ramen-ca"}} in pod["volumes"]
    plain = evals.job_spec("demo", "a", "tests: []", {}, image="pf")["job"]["spec"]["template"]["spec"]
    assert "hostAliases" not in plain
    assert evals.mcp_url("10.4.0.7:8080") == f"https://{tls.WORKER_NAME}:8080/mcp"
    monkeypatch.delenv("RAMEN_REQUIRE_TLS")
    assert evals.mcp_url("10.4.0.7:8080") == "http://10.4.0.7:8080/mcp"
