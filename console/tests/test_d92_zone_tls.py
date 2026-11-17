"""D92 / CONTRACTS §39.2a: every worker leaf names its zone (`<group>.<zone>.worker.ramen.internal`) and every verifier
checks that name, so a verified peer is "a worker of this zone", not "a worker of this install"."""

import time

import pytest
from cryptography import x509

from ramen_console import evals, grpcclient, tls
from ramen_console.cloud import azure_k8s, zone_tls
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.gcp_k8s import activator, manifests
from ramen_console.cloud.local import worker_tls_from_env
from ramen_console.grpcclient import Client
from ramen_console.storage.memory import MemoryStore
from tests.fake_grpc import FakeWorker
from tests.fakes_gcp import FakeClients

SPEC = {"region": "us-central1-a", "size": "s", "count": 1}
NS = "ramen-demo-a"


@pytest.fixture(autouse=True)
def _ca(monkeypatch):
    for k in ("RAMEN_TLS_CA_DIR", "RAMEN_WORKER_TLS", "RAMEN_WORKER_CA", "RAMEN_WORKER_TLS_NAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    ca = tls.new_ca()
    tls.set_current(ca)
    tls.set_generic_until(0)
    yield ca
    tls.set_current(None)
    tls.set_generic_until(0)


def _sans(pem: str) -> set[str]:
    c = x509.load_pem_x509_certificate(pem.encode())
    return set(
        c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    )


def _by(docs, kind, name=None):
    return [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]


def _env(dep):
    return {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_the_zone_name():
    assert tls.worker_name("demo", "a") == "demo.a.worker.ramen.internal"
    assert tls.WORKER_NAME == "worker.ramen.internal"  # the pre-D92 name, kept only for the transition


def test_a_zone_leaf_names_its_zone_and_no_longer_the_install(_ca):
    secret, _ = zone_tls.objects("demo", "a")
    sans = _sans(secret["stringData"]["tls.crt"])
    assert "demo.a.worker.ramen.internal" in sans and f"worker.{NS}.svc.cluster.local" in sans
    assert tls.WORKER_NAME not in sans
    assert tls.has_name(secret["stringData"]["tls.crt"], tls.worker_name("demo", "a"))
    assert not tls.has_name(secret["stringData"]["tls.crt"], tls.worker_name("demo", "b"))
    assert not tls.has_name("not a pem", tls.WORKER_NAME)


def test_a_bridge_leaf_keeps_the_generic_name_for_verifiers_not_yet_redeployed(_ca):
    secret, _ = zone_tls.objects("demo", "a", legacy=True)
    sans = _sans(secret["stringData"]["tls.crt"])
    assert {"demo.a.worker.ramen.internal", tls.WORKER_NAME} <= sans


def test_envoy_and_the_activator_verify_the_zone_name():
    docs = zone_tls.secure(azure_k8s.azure_manifests("demo", "a", SPEC, "img", "az://a/groups"), "Azure")
    (p,) = _by(docs, "BackendTLSPolicy")
    assert p["spec"]["validation"]["hostname"] == "demo.a.worker.ramen.internal"
    for d in _by(docs, "Deployment"):
        assert _env(d)["RAMEN_TLS_SERVER_NAME"] == "demo.a.worker.ramen.internal"
    act = activator(_by(docs, "Deployment", "worker")[0], {"app": "worker"})
    assert _env(act)["RAMEN_TLS_SERVER_NAME"] == "demo.a.worker.ramen.internal"  # its own zone's worker-direct
    gke = zone_tls.secure(manifests("demo", "b", SPEC, "img", "gs://b/demo"), "GCP")
    assert _env(_by(gke, "Deployment", "worker")[0])["RAMEN_TLS_SERVER_NAME"] == "demo.b.worker.ramen.internal"


def test_the_evals_job_dials_the_canary_by_its_zone_name(monkeypatch):
    monkeypatch.setenv("RAMEN_EVALS_SA", "ramen-evals")
    spec = evals.job_spec("demo", "a", "tests: []", {}, image="pf", worker_ip="10.4.0.7")
    pod = spec["job"]["spec"]["template"]["spec"]
    assert pod["hostAliases"] == [{"ip": "10.4.0.7", "hostnames": ["demo.a.worker.ramen.internal"]}]
    assert evals.mcp_url("10.4.0.7:8080", "demo", "a") == "https://demo.a.worker.ramen.internal:8080/mcp"


def test_worker_tls_names_the_peer_zone(monkeypatch):
    t = worker_tls_from_env()
    assert t["name"]("demo", "a") == "demo.a.worker.ramen.internal"
    monkeypatch.setenv("RAMEN_WORKER_TLS_NAME", "w.example")  # an explicit override stays one fixed name
    assert worker_tls_from_env()["name"] == "w.example"


def test_a_zone_derived_name_needs_the_peer(_ca):
    c = Client(tls={"ca": _ca.bundle, "name": tls.worker_name})
    with pytest.raises(ValueError, match="zone"):
        c.channel("127.0.0.1:1")


async def test_a_leaf_of_zone_a_is_refused_when_dialled_as_zone_b(_ca):
    leaf = _ca.issue(tls.zone_names(NS, tls.worker_name("demo", "a")))
    w = FakeWorker(tls=(leaf.cert, leaf.key)).start()
    try:
        c = Client(tls=worker_tls_from_env(), deadline=3)
        assert await c.health(w.target, peer=("demo", "a")) is True
        assert (await c.reload(w.target, "adm", peer=("demo", "a")))["tools"] == [{"name": "calc"}]
        assert await c.health(w.target, peer=("demo", "b")) is False
        assert await c.health(w.target, peer=("other", "a")) is False
        with pytest.raises(grpcclient.GrpcError) as e:
            await c.metrics(w.target, "adm", peer=("demo", "b"))
        assert e.value.code.name == "UNAVAILABLE"
        r = await c.call(w.target, "rmk_1", "demo", "a", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "result" in r or "error" in r
        with pytest.raises(grpcclient.GrpcError):
            await c.call(w.target, "rmk_1", "demo", "b", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        w.stop()


async def test_a_pre_d92_leaf_is_accepted_only_inside_the_transition_window(_ca):
    """§39.5b: pods that still serve the generic-name leaf until kubelet and the node pick up the re-issued one."""
    leaf = _ca.issue(tls.zone_names(NS, tls.WORKER_NAME))  # what a 1.0.0c deploy wrote
    w = FakeWorker(tls=(leaf.cert, leaf.key)).start()
    try:
        c = Client(tls=worker_tls_from_env(), deadline=3)
        assert await c.health(w.target, peer=("demo", "a")) is False  # no window: refused
        tls.set_generic_until(time.time() + 60)
        assert await c.health(w.target, peer=("demo", "a")) is True
        assert (await c.reload(w.target, "adm", peer=("demo", "a")))["tools"] == [{"name": "calc"}]
        tls.set_generic_until(time.time() - 1)
        assert await c.health(w.target, peer=("demo", "a")) is False
    finally:
        w.stop()


async def test_the_window_opens_once_per_install_and_is_shared(monkeypatch):
    store = MemoryStore()
    monkeypatch.setenv("RAMEN_TLS_ZONE_NAME_GRACE_SECS", "600")
    first = await tls.open_zone_names(store)
    assert tls.generic_accepted()
    again = await tls.open_zone_names(store)  # another replica, or a restart: the same switch time
    assert again == first
    doc = await store.get(*tls.ZONE_NAMES_DOC)
    assert doc["since"] == first
    await store.put(*tls.ZONE_NAMES_DOC, {"since": "2026-01-01T00:00:00+00:00"})  # switched long ago
    await tls.open_zone_names(store)
    assert not tls.generic_accepted()


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


async def test_the_upgrade_reissues_every_generic_leaf_with_its_zone_name(gcp, _ca):
    cloud, fk = gcp
    await cloud.attach_zone("demo", "a", SPEC)
    fresh = fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"]
    assert await cloud.renew_zone_tls() == []  # a D92 leaf: nothing to do
    old = _ca.issue(tls.zone_names(NS, tls.WORKER_NAME))  # a 1.0.0c leaf, not due
    fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"].update({"tls.crt": old.cert, "tls.key": old.key})
    assert await cloud.renew_zone_tls() == [NS]
    bridge = fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"]
    assert {"demo.a.worker.ramen.internal", tls.WORKER_NAME} <= _sans(bridge)  # old activator/Envoy keep working
    assert await cloud.renew_zone_tls() == []  # has its zone name now
    tls.set_current(tls.CA(*tls._fresh(), _ca.cert))  # due (rotated): the bridge leaf keeps both names
    assert await cloud.renew_zone_tls() == [NS]
    assert tls.WORKER_NAME in _sans(fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"])
    await cloud.attach_zone("demo", "a", SPEC)  # the next deploy: zone name only
    assert tls.WORKER_NAME not in _sans(fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"])
    assert fresh


async def test_status_flags_leaves_that_still_carry_the_generic_name(gcp, _ca):
    cloud, fk = gcp
    await cloud.attach_zone("demo", "a", SPEC)
    store = MemoryStore()
    await store.put(*tls.CA_DOC, {"cert": _ca.cert, "ca_key": _ca.key, "previous": ""})
    out = await tls.status(store, cloud)
    assert out["leaves"] == [{"name": NS, "ca": "current"}]
    bridge = _ca.issue(tls.zone_names(NS, tls.worker_name("demo", "a"), legacy=True))
    fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"] = bridge.cert
    out = await tls.status(store, cloud)
    assert out["leaves"] == [{"name": NS, "ca": "current", "generic_name": True}]


async def test_startup_opens_the_window_and_upgrades_the_leaves(gcp, _ca, monkeypatch):
    cloud, fk = gcp
    await cloud.attach_zone("demo", "a", SPEC)
    old = _ca.issue(tls.zone_names(NS, tls.WORKER_NAME))
    fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"].update({"tls.crt": old.cert, "tls.key": old.key})
    store = MemoryStore()
    await tls.startup(store, cloud)
    assert tls.generic_accepted()
    assert "demo.a.worker.ramen.internal" in _sans(fk.k8s.objs[("Secret", NS, "ramen-tls")]["stringData"]["tls.crt"])


def test_the_local_cli_issues_a_wildcard_for_one_compose_worker(tmp_path):
    """Compose runs one worker for every group of zone `local`: its leaf names `*.local.worker.ramen.internal`."""
    tls.main(["init", str(tmp_path), "--zone", "worker=local:*.local"])
    sans = _sans((tmp_path / "worker" / "tls.crt").read_text())
    assert "*.local.worker.ramen.internal" in sans and tls.WORKER_NAME not in sans
    tls.main(["init", str(tmp_path / "x"), "--zone", "worker=ramen-demo-a"])  # no zone given: the namespace's name
    assert tls.worker_name("demo", "a") not in _sans((tmp_path / "x" / "worker" / "tls.crt").read_text())


async def test_the_console_dials_a_wildcard_compose_leaf_for_any_group(_ca):
    leaf = _ca.issue(tls.zone_names("local", "*.local.worker.ramen.internal"))
    w = FakeWorker(tls=(leaf.cert, leaf.key)).start()
    try:
        c = Client(tls=worker_tls_from_env(), deadline=3)
        assert await c.health(w.target, peer=("demo", "local")) is True
        assert await c.health(w.target, peer=("t-x1", "local")) is True
        assert await c.health(w.target, peer=("demo", "a")) is False
    finally:
        w.stop()
