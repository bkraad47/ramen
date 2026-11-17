"""D86 / CONTRACTS §39.1: CA rotation in two phases — the two-CA bundle reaches every peer before any leaf moves to
the new CA (GKE 1.0.0b33: rotate switched Redis/Phoenix at once and workers lost both), and retire touches only
leaves of the retired CA."""

import ssl
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509

from ramen_console import serve, tls
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.grpcclient import Client
from ramen_console.storage.memory import MemoryStore
from tests.fake_grpc import FakeWorker
from tests.fakes_gcp import FakeClients
from tests.test_api import app, client, cloud, demo, root  # noqa: F401 - fixtures

SPEC = {"region": "us-central1-a", "size": "s", "count": 1}
SYS = "ramen-system"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("RAMEN_TLS_CA_DIR", raising=False)
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    monkeypatch.setenv("RAMEN_TLS_SERVICES", "redis,phoenix,promptfoo")
    monkeypatch.setenv("RAMEN_NAMESPACE", SYS)
    monkeypatch.setenv("RAMEN_TLS_ROTATE_SETTLE_SECS", "0")
    tls.set_current(None)
    tls.set_console_leaf(None, None)
    yield
    tls.set_current(None)
    tls.set_console_leaf(None, None)


@pytest.fixture
def install(tmp_path):
    """A GKE install: store-backed CA, the console's own leaf (as the launcher writes it), chart services, zone a."""
    w = FakeWorker(admin_key="adm", mcp_keys=("rmk_1",)).start()
    fk = FakeClients()
    cloud = GcpCloud(
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
    fk.k8s.objs[("RoleBinding", SYS, "ramen-console")] = {}
    store = MemoryStore()
    yield cloud, fk, store, tmp_path
    w.stop()


async def _boot(cloud, store, out):
    ca = await tls.ensure_ca(store)
    serve.write_leaf(out, ca)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serve.register(ctx, out)
    await cloud.ensure_system_tls()
    await cloud.attach_zone("demo", "a", SPEC)
    return ca


def _issued_by(leaf_pem: str, bundle: str) -> bool:
    leaf = x509.load_pem_x509_certificate(leaf_pem.encode())
    for ca in x509.load_pem_x509_certificates(bundle.encode()):
        try:
            leaf.verify_directly_issued_by(ca)
            return True
        except Exception:  # noqa: BLE001, S112 - another CA of the bundle
            continue
    return False


def _peers(cloud, fk, out) -> tuple[dict[str, str], dict[str, str]]:
    """(leaves served, bundles trusted) by every peer: the console (its own leaf; in-process trust), each zone's
    workers (`ramen-tls`), each chart service (`ramen-tls-<svc>`), and every `ramen-ca` reader (Envoy, Jobs)."""
    leaves, trusts = {"console": (out / "tls.crt").read_text()}, {"console": tls.current().bundle}
    for (kind, ns, name), obj in list(fk.k8s.objs.items()):
        if kind == "Secret" and (name == "ramen-tls" or name.startswith("ramen-tls-")):
            data = cloud.kube.secret_values(ns, name)
            leaves[f"{ns}/{name}"], trusts[f"{ns}/{name}"] = data["tls.crt"], data["ca.crt"]
        elif kind == "ConfigMap" and name == "ramen-ca":
            trusts[f"{ns}/ramen-ca"] = obj["data"]["ca.crt"]
    return leaves, trusts


class Walk:
    """Checks after every write that each served leaf is trusted by every bundle a peer may still hold: the bundles
    of this phase and the last one's (a peer catches up only after the settle time, which a phase boundary is)."""

    def __init__(self, cloud, fk, out, monkeypatch):
        self.cloud, self.fk, self.out = cloud, fk, out
        self.writes = 0
        self.phase()
        for fn in ("_create", "_patch"):
            orig = getattr(fk.k8s, fn)

            def wrapped(*a, _orig=orig, **kw):
                r = _orig(*a, **kw)
                self.check()
                return r

            monkeypatch.setattr(fk.k8s, fn, wrapped)

    def phase(self):
        self.held = set(_peers(self.cloud, self.fk, self.out)[1].values())

    def check(self):
        self.writes += 1
        leaves, trusts = _peers(self.cloud, self.fk, self.out)
        self.held |= set(trusts.values())
        for name, leaf in leaves.items():
            for bundle in self.held:
                assert _issued_by(leaf, bundle), f"write {self.writes}: a peer does not trust {name}'s leaf"


async def test_rotation_never_serves_a_leaf_a_peer_does_not_trust(install, monkeypatch):
    cloud, fk, store, out = install
    old = await _boot(cloud, store, out)
    walk = Walk(cloud, fk, out, monkeypatch)
    restarted = []
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: restarted.append(name))

    # phase 1: the two-CA bundle everywhere; no leaf moves, nothing restarts
    first = await tls.start_rotation(store, cloud)
    walk.check()
    ca = tls.current()
    assert ca.cert == old.cert and ca.next and ca.bundle == old.cert + ca.next  # still issuing with the old CA
    assert first["stage"] == "bundle" and first["fingerprint"] == tls.fingerprint(ca.next) and first["previous"]
    assert sorted(first["zones"]) == ["ramen-demo-a"] and restarted == []
    leaves, trusts = _peers(cloud, fk, out)
    assert all(t == ca.bundle for t in trusts.values()), trusts.keys()
    assert all(_issued_by(pem, old.cert) for pem in leaves.values())
    await cloud.attach_zone("demo", "b", SPEC)  # a deploy in the window: an old-CA leaf, the two-CA bundle
    assert _issued_by(cloud.kube.secret_values("ramen-demo-b", "ramen-tls")["tls.crt"], old.cert)
    walk.phase()  # the settle time: kubelet refreshed every mount, every node re-read its bundle

    # phase 2+3: zone leaves and the console's own first, then the chart services (restarted)
    done = await tls.finish_rotation(store, cloud)
    walk.check()
    new = tls.current()
    assert new.cert == ca.next and new.previous == old.cert and not new.next
    assert done["console"] is True and sorted(done["zones"]) == ["ramen-demo-a", "ramen-demo-b"]
    assert sorted(done["system"]) == ["phoenix", "promptfoo", "redis"] and sorted(restarted) == sorted(done["system"])
    leaves, _ = _peers(cloud, fk, out)
    assert all(_issued_by(pem, new.cert) for pem in leaves.values()), leaves.keys()
    walk.phase()

    # retire: only bundles shrink; no leaf re-issued, nothing restarted
    restarted.clear()
    before = _peers(cloud, fk, out)[0]
    gone = await tls.retire_previous(store, cloud)
    walk.check()
    assert gone["previous"] is False and gone["system"] == [] and restarted == []
    leaves, trusts = _peers(cloud, fk, out)
    assert leaves == before and all(t == new.cert for t in trusts.values())
    assert walk.writes > 10


async def test_the_old_order_is_caught_by_the_walk(install, monkeypatch):
    """The b33 behaviour (chart leaves switched while zones held the old CA only) fails the same check."""
    cloud, fk, store, out = install
    old = await _boot(cloud, store, out)
    Walk(cloud, fk, out, monkeypatch)  # checks every write from here on
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: None)
    new = tls.new_ca()
    tls.set_current(tls.CA(new.cert, new.key, old.cert))
    with pytest.raises(AssertionError, match="does not trust"):
        await cloud.ensure_system_tls()


async def test_rotate_and_retire_are_refused_mid_way_and_retire_names_old_leaves(install, monkeypatch):
    cloud, fk, store, out = install
    await _boot(cloud, store, out)
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: None)
    await tls.start_rotation(store, cloud)
    with pytest.raises(tls.CaError, match="in progress"):
        await tls.start_rotation(store, cloud)
    with pytest.raises(tls.CaError, match="in progress"):
        await tls.retire_previous(store, cloud)
    await tls.finish_rotation(store, cloud)
    with pytest.raises(tls.CaError, match="retire"):
        await tls.start_rotation(store, cloud)  # the previous CA must go first: a bundle holds two CAs at most
    # a leaf the switch missed (a satellite that was unreachable, say) keeps retire refused
    previous = tls.current().previous
    data = cloud.kube.secret_values("ramen-demo-a", "ramen-tls")
    fk.k8s.objs[("Secret", "ramen-demo-a", "ramen-tls")]["stringData"] = {
        **data,
        "tls.crt": tls.new_ca().issue(["x"]).cert,
    }
    with pytest.raises(tls.CaError, match=r"ramen-demo-a.*redeploy"):
        await tls.retire_previous(store, cloud)
    assert tls.current().previous == previous  # nothing dropped
    assert "previous_key" not in await store.get(*tls.CA_DOC)  # the old CA's key is never kept


async def test_finish_without_a_staged_rotation_is_a_no_op(install):
    cloud, _, store, out = install
    await _boot(cloud, store, out)
    assert await tls.finish_rotation(store, cloud) is None
    assert await tls.advance(store, cloud) is None


async def test_advance_finishes_a_staged_rotation_after_the_settle_time(install, monkeypatch):
    """A console restarted mid-rotation: any replica's renewal round finishes it once the settle time has passed."""
    cloud, _, store, out = install
    await _boot(cloud, store, out)
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: None)
    monkeypatch.setenv("RAMEN_TLS_ROTATE_SETTLE_SECS", "120")
    await tls.start_rotation(store, cloud)
    assert await tls.advance(store, cloud) is None  # too early
    later = datetime.now(UTC) + timedelta(seconds=121)
    done = await tls.advance(store, cloud, now=later)
    assert done is not None and not tls.current().next


async def test_status_lists_each_leaf_and_its_ca(install, monkeypatch):
    cloud, _, store, out = install
    await _boot(cloud, store, out)
    monkeypatch.setenv("RAMEN_TLS_ROTATE_SETTLE_SECS", "120")
    await tls.start_rotation(store, cloud)
    st = await tls.status(store, cloud)
    assert st["stage"] == "bundle" and st["next"] and st["switch_after"]
    assert {x["name"]: x["ca"] for x in st["leaves"]}["console"] == "current"
    assert {x["ca"] for x in st["leaves"]} == {"current"}
    assert any(x["name"] == "ramen-demo-a" for x in st["leaves"])


async def test_the_console_leaf_is_reissued_at_rotation_and_by_the_minute_poll(tmp_path):
    store = MemoryStore()
    ca = await tls.ensure_ca(store)
    serve.write_leaf(tmp_path, ca)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serve.register(ctx, tmp_path)
    assert tls.console_leaf() == (tmp_path / "tls.crt").read_text()
    assert tls.renew_console_leaf() is False  # fresh
    tls.set_current(tls.CA(*tls._fresh()))
    assert tls.renew_console_leaf() is True
    assert serve.poll_secs(3600) == 60 and serve.poll_secs(5) == 5


def test_rotate_settle_default(monkeypatch):
    monkeypatch.delenv("RAMEN_TLS_ROTATE_SETTLE_SECS")
    assert tls.rotate_settle() == 180
    monkeypatch.setenv("RAMEN_TLS_ROTATE_SETTLE_SECS", "nonsense")
    assert tls.rotate_settle() == 180


async def test_deploy_log_names_the_issued_leaf_and_its_ca(install, monkeypatch):
    cloud, fk, store, out = install
    ca = await _boot(cloud, store, out)
    lines = []
    res = await cloud.deploy("demo", "dev", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec=SPEC, log=lines.append)
    assert res["ok"], res
    assert any(f"tls: issued ramen-demo-a leaf (CA {tls.fingerprint(ca.cert)})" in x for x in lines), lines


def test_rotate_api_publishes_the_bundle_now_and_switches_leaves_later(root, client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_TLS_ROTATE_SETTLE_SECS", "3600")
    r = root.post("/api/v1/config/tls/rotate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "bundle" and body["previous"] is True and body["switch_after"] == 3600
    st = root.get("/api/v1/config/tls").json()
    assert st["stage"] == "bundle" and st["next"] == body["fingerprint"]
    r = root.post("/api/v1/config/tls/rotate?retire=1")
    assert r.status_code == 409 and "in progress" in r.json()["detail"]


async def test_the_rotate_job_finishes_after_the_settle_time_and_logs_a_failure(install, monkeypatch, caplog):
    cloud, _, store, out = install
    await _boot(cloud, store, out)
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: None)
    await tls.start_rotation(store, cloud)
    await tls.finish_later(store, cloud, 0)
    assert not tls.current().next and tls.current().previous
    await tls.retire_previous(store, cloud)
    await tls.start_rotation(store, cloud)

    async def boom():
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(cloud, "renew_zone_tls", boom)
    await tls.finish_later(store, cloud, 0)
    assert "rotation not finished" in caplog.text
    # a doc whose stage time cannot be read is finished at the next round
    doc = await store.get(*tls.CA_DOC)
    await store.put(*tls.CA_DOC, {**doc, "next": tls.new_ca().cert, "next_key": tls.new_ca().key, "staged": "?"})
    assert tls._switch_at(await store.get(*tls.CA_DOC)) == "shortly"
    monkeypatch.undo()
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    monkeypatch.setenv("RAMEN_TLS_SERVICES", "redis,phoenix,promptfoo")
    monkeypatch.setenv("RAMEN_NAMESPACE", SYS)
    monkeypatch.setattr(cloud.kube, "restart", lambda ns, name: None)
    assert await tls.advance(store, cloud) is not None


async def test_a_bundle_that_does_not_reach_every_peer_unstages_the_new_ca(install, monkeypatch):
    cloud, _, store, out = install
    old = await _boot(cloud, store, out)

    async def unreachable():
        raise RuntimeError("satellite unreachable")

    monkeypatch.setattr(cloud, "publish_tls_bundle", unreachable)
    with pytest.raises(tls.CaError, match="nothing was switched"):
        await tls.start_rotation(store, cloud)
    ca = tls.current()
    assert ca.cert == old.cert and not ca.next and await tls.finish_rotation(store, cloud) is None
