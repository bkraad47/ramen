"""D86 §39.2/§39.3: the console's TLS launcher and the TLS front for loopback-only servers."""

import asyncio
import ssl

import pytest
from cryptography import x509

from ramen_console import serve, tls, tlsproxy
from ramen_console.storage.memory import MemoryStore


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("RAMEN_TLS_CA_DIR", "RAMEN_NAMESPACE", "RAMEN_CONSOLE_INTERNAL_URL", "RAMEN_REQUIRE_TLS"):
        monkeypatch.delenv(k, raising=False)
    tls.set_current(None)
    yield
    tls.set_current(None)


def _client_ctx(ca: tls.CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=ca.bundle)
    return ctx


def test_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "SA_NAMESPACE", tmp_path / "missing")
    assert serve.namespace() == "ramen-system"
    (tmp_path / "ns").write_text("ops\n")
    monkeypatch.setattr(serve, "SA_NAMESPACE", tmp_path / "ns")
    assert serve.namespace() == "ops"
    monkeypatch.setenv("RAMEN_NAMESPACE", "ramen-x")
    assert serve.namespace() == "ramen-x"


async def test_prepare_issues_the_consoles_leaf_from_the_install_ca(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMEN_NAMESPACE", "ramen-system")
    store = MemoryStore()
    cert, key = await serve.prepare(store, tmp_path / "tls")
    ca = tls.current()
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    c.verify_directly_issued_by(x509.load_pem_x509_certificate(ca.cert.encode()))
    sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "console.ramen-system.svc.cluster.local" in sans
    assert key.stat().st_mode & 0o077 == 0
    assert (await store.get("config", "install_ca"))["cert"] == ca.cert


async def test_renew_once_reloads_the_live_context_when_due(tmp_path, monkeypatch):
    store = MemoryStore()
    out = tmp_path / "tls"
    cert, key = await serve.prepare(store, out)
    ctx = tlsproxy.context(str(cert), str(key))
    assert serve.renew_once(ctx, out) is False  # fresh
    assert serve.renew_once(None, out) is False
    before = cert.read_text()
    await tls.rotate(store)  # staged (§39.1 phase 1): trusted, not issuing — the leaf stays
    assert serve.renew_once(ctx, out) is False and cert.read_text() == before
    await tls.promote(store)  # the new CA issues: the console's leaf is due
    assert serve.renew_once(ctx, out) is True and cert.read_text() != before
    tls.set_current(None)
    assert serve.renew_once(ctx, out) is False  # no CA loaded: nothing to do


async def test_renew_forever_survives_errors(tmp_path):
    class Boom:
        async def get(self, *a):
            raise RuntimeError("store down")

        def transaction(self):
            raise RuntimeError("store down")

    task = asyncio.create_task(serve.renew_forever(object(), tmp_path, Boom(), 0.01))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()


def test_build_store_is_the_plain_store_locally(monkeypatch):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_SECRETS_BACKEND", "store")
    assert type(serve.build_store()).__name__ in ("MemoryStore", "EncryptedStore")


async def test_run_refuses_plaintext_before_it_binds(monkeypatch):
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    monkeypatch.setenv("RAMEN_REDIS_URL", "redis://redis:6379")
    with pytest.raises(RuntimeError, match="rediss"):
        await serve.run(0)


async def _echo_server():
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(echo, "127.0.0.1", 0)


async def test_tlsproxy_terminates_tls_and_pipes(tmp_path):
    ca = tls.new_ca()
    leaf = ca.issue(["promptfoo", "localhost", "127.0.0.1"])
    (tmp_path / "c").write_text(leaf.cert)
    (tmp_path / "k").write_text(leaf.key)
    up = await _echo_server()
    up_port = up.sockets[0].getsockname()[1]
    c, k = str(tmp_path / "c"), str(tmp_path / "k")
    server, reloader = await tlsproxy.serve("127.0.0.1:0", f"127.0.0.1:{up_port}", c, k, 0.01)
    port = server.sockets[0].getsockname()[1]
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ctx(ca), server_hostname="promptfoo")
        w.write(b"GET /health\r\n")
        await w.drain()
        assert await r.readexactly(13) == b"GET /health\r\n"
        w.close()
        # a plaintext client gets nothing through
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(b"GET / HTTP/1.1\r\n\r\n")
        await w2.drain()
        assert b"GET" not in await r2.read(100)
        w2.close()
        # a renewed pair is picked up without a restart
        new = tls.new_ca()
        leaf2 = new.issue(["promptfoo"])
        (tmp_path / "c").write_text(leaf2.cert)
        (tmp_path / "k").write_text(leaf2.key)
        await asyncio.sleep(0.1)
        r3, w3 = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ctx(new), server_hostname="promptfoo")
        w3.write(b"x")
        await w3.drain()
        assert await r3.readexactly(1) == b"x"
        w3.close()
    finally:
        reloader.cancel()
        server.close()
        up.close()


async def test_tlsproxy_upstream_down_closes_the_client(tmp_path):
    ca = tls.new_ca()
    leaf = ca.issue(["localhost"])
    (tmp_path / "c").write_text(leaf.cert)
    (tmp_path / "k").write_text(leaf.key)
    server, reloader = await tlsproxy.serve("127.0.0.1:0", "127.0.0.1:1", str(tmp_path / "c"), str(tmp_path / "k"))
    port = server.sockets[0].getsockname()[1]
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ctx(ca), server_hostname="localhost")
        assert await r.read(10) == b""
        w.close()
    finally:
        reloader.cancel()
        server.close()


async def test_tlsproxy_reload_keeps_the_old_pair_on_garbage(tmp_path):
    ca = tls.new_ca()
    leaf = ca.issue(["localhost"])
    (tmp_path / "c").write_text(leaf.cert)
    (tmp_path / "k").write_text(leaf.key)
    ctx = tlsproxy.context(str(tmp_path / "c"), str(tmp_path / "k"))
    task = asyncio.create_task(tlsproxy.reload_forever(ctx, str(tmp_path / "c"), str(tmp_path / "k"), 0.01))
    (tmp_path / "k").write_text("garbage")
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()


async def test_run_serves_a_given_leaf(tmp_path, monkeypatch):
    """Local compose: the leaf comes from `tls init`, the console does not issue (and holds no CA key)."""
    import uvicorn

    tls.init_dir(tmp_path, {"console": ["localhost", "127.0.0.1"]})
    (tmp_path / "ca" / "ca.key").unlink()
    monkeypatch.setenv("RAMEN_TLS_CA_DIR", str(tmp_path / "ca"))
    monkeypatch.setenv("RAMEN_TLS_CERT", str(tmp_path / "console" / "tls.crt"))
    monkeypatch.setenv("RAMEN_TLS_KEY", str(tmp_path / "console" / "tls.key"))
    seen = {}

    async def fake_serve(self):
        seen["cert"] = self.config.ssl_certfile

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(serve, "build_store", MemoryStore)
    await serve.run(0)
    assert seen["cert"] == str(tmp_path / "console" / "tls.crt") and tls.current() is not None


async def test_run_issues_its_own_leaf_on_a_cluster(monkeypatch):
    import uvicorn

    monkeypatch.delenv("RAMEN_TLS_CERT", raising=False)
    monkeypatch.delenv("RAMEN_TLS_KEY", raising=False)
    seen = {}

    async def fake_serve(self):
        seen["cert"] = self.config.ssl_certfile
        seen["hook"] = tls.console_leaf()  # §39.1: registered while it serves (rotation and retire use it)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(serve, "build_store", MemoryStore)
    await serve.run(0)
    assert seen["cert"].endswith("tls.crt") and "ramen-tls-" in seen["cert"]
    assert seen["hook"].startswith("-----BEGIN CERTIFICATE")
    assert tls.console_leaf() is None  # the launcher's hook goes with its server
