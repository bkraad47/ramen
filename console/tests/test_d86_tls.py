"""D86 / CONTRACTS §39.1–§39.2: the per-install CA and the leaves it issues."""

import asyncio
import ipaddress
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from ramen_console import tls
from ramen_console.storage.encrypted import EncryptedStore, FieldCipher
from ramen_console.storage.memory import MemoryStore


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RAMEN_TLS_CA_DIR", raising=False)
    monkeypatch.delenv("RAMEN_REQUIRE_TLS", raising=False)
    monkeypatch.delenv("RAMEN_TLS_LEAF_DAYS", raising=False)
    tls.set_current(None)
    yield
    tls.set_current(None)


def _cert(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def _sans(pem: str) -> set[str]:
    ext = _cert(pem).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(v) for v in ext.get_values_for_type(x509.DNSName)} | {
        str(v) for v in ext.get_values_for_type(x509.IPAddress)
    }


def test_new_ca_is_a_constrained_ca():
    ca = tls.new_ca("install-1")
    c = _cert(ca.cert)
    bc = c.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca and bc.path_length == 0
    assert "Ramen install CA install-1" in c.subject.rfc4514_string()
    assert c.not_valid_after_utc - c.not_valid_before_utc > timedelta(days=3600)
    assert "PRIVATE KEY" in ca.key and ca.previous == "" and ca.bundle == ca.cert


def test_leaf_is_signed_by_the_ca_with_sans_eku_and_30_days():
    ca = tls.new_ca()
    leaf = ca.issue(["worker.ramen.internal", "worker.ramen-demo-a.svc", "127.0.0.1"])
    c = _cert(leaf.cert)
    c.verify_directly_issued_by(_cert(ca.cert))
    assert _sans(leaf.cert) == {"worker.ramen.internal", "worker.ramen-demo-a.svc", "127.0.0.1"}
    eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku and ExtendedKeyUsageOID.CLIENT_AUTH in eku
    assert not c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    days = (c.not_valid_after_utc - c.not_valid_before_utc).days
    assert 29 <= days <= 30
    assert leaf.ca == ca.bundle


def test_leaf_days_come_from_the_env(monkeypatch):
    monkeypatch.setenv("RAMEN_TLS_LEAF_DAYS", "7")
    leaf = tls.new_ca().issue(["a"])
    c = _cert(leaf.cert)
    assert (c.not_valid_after_utc - c.not_valid_before_utc).days in (6, 7)


def test_ip_sans_are_ip_addresses():
    leaf = tls.new_ca().issue(["127.0.0.1", "localhost"])
    ext = _cert(leaf.cert).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ext.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]


def test_renewal_after_two_thirds_or_another_ca_or_garbage():
    ca = tls.new_ca()
    leaf = ca.issue(["a"])
    assert not tls.needs_renewal(leaf.cert, ca)
    assert tls.needs_renewal(leaf.cert, ca, now=datetime.now(UTC) + timedelta(days=21))
    assert tls.needs_renewal(leaf.cert, tls.new_ca())  # rotated: issued by the previous CA
    assert tls.needs_renewal("", ca) and tls.needs_renewal("not a pem", ca)


def test_names():
    zone = tls.zone_names("ramen-demo-a")
    assert zone[0] == tls.WORKER_NAME == "worker.ramen.internal"
    for svc in ("worker", "worker-direct", "worker-canary"):
        assert {svc, f"{svc}.ramen-demo-a", f"{svc}.ramen-demo-a.svc", f"{svc}.ramen-demo-a.svc.cluster.local"} <= set(
            zone
        )
    assert {"localhost", "127.0.0.1"} <= set(zone)
    assert tls.service_names("redis", "ramen-system") == [
        "redis",
        "redis.ramen-system",
        "redis.ramen-system.svc",
        "redis.ramen-system.svc.cluster.local",
        "localhost",
        "127.0.0.1",
    ]


def test_console_names_carry_the_internal_url_host(monkeypatch):
    monkeypatch.setenv("RAMEN_CONSOLE_INTERNAL_URL", "https://ramen-console.ops.svc:443")
    names = tls.console_names("ramen-system")
    assert "console.ramen-system.svc.cluster.local" in names and "ramen-console.ops.svc" in names


def test_require_flag(monkeypatch):
    assert not tls.require_tls()
    for v in ("1", "true", "TRUE"):
        monkeypatch.setenv("RAMEN_REQUIRE_TLS", v)
        assert tls.require_tls()
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "0")
    assert not tls.require_tls()


async def test_ensure_ca_creates_once_and_keeps_it(fernet_key):
    store = EncryptedStore(MemoryStore(), FieldCipher(fernet_key))
    a = await tls.ensure_ca(store)
    assert tls.current() is a
    tls.set_current(None)
    b = await tls.ensure_ca(store)
    assert a.cert == b.cert and a.key == b.key
    raw = await store.inner.get("config", "install_ca")
    assert raw["cert"] == a.cert and raw["ca_key"].startswith("enc:")  # the key is a console secret (vaulted on clouds)


async def test_two_consoles_starting_together_agree():
    store = MemoryStore()
    a, b = await asyncio.gather(tls.ensure_ca(store, cache=False), tls.ensure_ca(store, cache=False))
    assert a.cert == b.cert


async def test_a_restored_doc_without_its_key_is_replaced():
    store = MemoryStore()
    await store.put("config", "install_ca", {"cert": tls.new_ca().cert})  # a backup never carries ca_key
    ca = await tls.ensure_ca(store)
    assert ca.key and (await store.get("config", "install_ca"))["ca_key"] == ca.key


async def test_the_ca_dir_overrides_the_store(tmp_path, monkeypatch):
    tls.init_dir(tmp_path, {"console": ["console", "localhost"]})
    monkeypatch.setenv("RAMEN_TLS_CA_DIR", str(tmp_path / "ca"))
    store = MemoryStore()
    ca = await tls.ensure_ca(store)
    assert ca.cert == (tmp_path / "ca" / "ca.crt").read_text()
    assert await store.get("config", "install_ca") is None
    with pytest.raises(tls.CaError, match="RAMEN_TLS_CA_DIR"):
        await tls.rotate(store)


async def test_rotate_stages_then_promotes_then_retires(fernet_key):
    store = EncryptedStore(MemoryStore(), FieldCipher(fernet_key))
    old = await tls.ensure_ca(store)
    staged = await tls.rotate(store)  # phase 1: trusted, not issuing
    assert staged.cert == old.cert and staged.next != old.cert and staged.bundle == old.cert + staged.next
    assert tls.current() is staged and (await store.inner.get("config", "install_ca"))["next_key"].startswith("enc:")
    with pytest.raises(tls.CaError, match="in progress"):
        await tls.retire(store)
    assert tls.CA(old.cert, old.key).which(staged.issue(["x"]).cert) == "current"
    new = await tls.promote(store)  # phase 2: the staged CA issues, the old one is trusted until retired
    assert new.cert == staged.next and new.previous == old.cert and new.bundle == new.cert + old.cert and not new.next
    assert await tls.promote(store) is None
    assert new.which(old.issue(["x"]).cert) == "previous" and new.which("garbage") == "unknown"
    assert staged.which(tls.CA(staged.next, staged.next_key).issue(["x"]).cert) == "next"
    retired = await tls.retire(store)
    assert retired.cert == new.cert and retired.previous == "" and retired.bundle == new.cert
    await store.put("config", "install_ca", {})
    with pytest.raises(tls.CaError, match="no install CA"):
        await tls.retire(store)


async def test_rotate_on_an_empty_store_creates_then_stages():
    store = MemoryStore()
    ca = await tls.rotate(store)
    assert ca.cert and ca.next and (await store.get("config", "install_ca"))["created"]


def test_init_dir_writes_the_ca_and_leaves_and_is_idempotent(tmp_path):
    out = tls.init_dir(tmp_path, {"worker": tls.zone_names("local"), "redis": ["redis", "localhost"]})
    ca = (tmp_path / "ca" / "ca.crt").read_text()
    assert set(out) == {"worker", "redis"}
    for name in ("worker", "redis"):
        d = tmp_path / name
        assert (d / "ca.crt").read_text() == ca
        _cert((d / "tls.crt").read_text()).verify_directly_issued_by(_cert(ca))
    assert (tmp_path / "ca" / "ca.key").stat().st_mode & 0o077 == 0
    assert (tmp_path / "worker" / "tls.key").stat().st_mode & 0o004  # read by containers running as other users
    first = (tmp_path / "worker" / "tls.crt").read_text()
    tls.init_dir(tmp_path, {"worker": tls.zone_names("local")})
    assert (tmp_path / "worker" / "tls.crt").read_text() == first  # fresh leaves are kept
    assert (tmp_path / "ca" / "ca.crt").read_text() == ca


def test_cli_init(tmp_path):
    tls.main(["init", str(tmp_path), "--leaf", "console=console,localhost,127.0.0.1", "--zone", "worker=local"])
    assert _sans((tmp_path / "console" / "tls.crt").read_text()) == {"console", "localhost", "127.0.0.1"}
    assert tls.WORKER_NAME in _sans((tmp_path / "worker" / "tls.crt").read_text())


def test_cli_legacy_self_signed(tmp_path):
    tls.main([str(tmp_path / "x"), "h.local"])
    assert "h.local" in _cert((tmp_path / "x" / "cert.pem").read_text()).subject.rfc4514_string()


async def test_a_ca_dir_without_its_key_trusts_but_cannot_issue(tmp_path, monkeypatch):
    tls.init_dir(tmp_path, {})
    (tmp_path / "ca" / "ca.key").unlink()
    monkeypatch.setenv("RAMEN_TLS_CA_DIR", str(tmp_path / "ca"))
    ca = await tls.ensure_ca(MemoryStore())
    assert ca.bundle == (tmp_path / "ca" / "ca.crt").read_text()
    with pytest.raises(tls.CaError, match="cannot issue"):
        ca.issue(["x"])
    monkeypatch.setenv("RAMEN_TLS_CA_DIR", str(tmp_path / "nope"))
    with pytest.raises(tls.CaError, match="nope"):
        await tls.ensure_ca(MemoryStore())


def test_client_verify_trusts_the_system_roots_and_the_install_ca():
    # GKE b32: the console's httpx clients (OTLP forward, exporter, LLM proxy, model list) used only the system roots,
    # so every in-cluster HTTPS hop (Phoenix collector) failed with CERTIFICATE_VERIFY_FAILED
    tls.set_current(None)
    plain = tls.client_verify()
    n = len(plain.get_ca_certs())
    ca = tls.new_ca("t")
    tls.set_current(ca)
    try:
        ctx = tls.client_verify()
        subjects = [c["subject"] for c in ctx.get_ca_certs()]
        assert len(subjects) == n + 1 and any(("commonName", "Ramen install CA t") in s[0] for s in subjects if s)
        assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname
    finally:
        tls.set_current(None)


@pytest.mark.parametrize("mod", ["web.otlp_routes", "otel", "llm_proxy", "llm_models"])
def test_every_outbound_client_verifies_with_the_install_ca(mod):
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(f"ramen_console.{mod}"))
    assert "httpx.AsyncClient(" in src
    assert src.count("httpx.AsyncClient(") == src.count("verify=tls.client_verify()"), mod
