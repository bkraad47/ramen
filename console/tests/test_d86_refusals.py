"""D86 / CONTRACTS §39.3–§39.4: the console dials workers over TLS with the install CA, and refuses plaintext."""

import pytest
from fastapi.testclient import TestClient

from ramen_console import grpcclient, llm_providers, otel, tls
from ramen_console.app import create_app
from ramen_console.cloud.local import worker_tls_from_env
from ramen_console.errors import ApiError
from ramen_console.storage import make_store
from tests.fake_grpc import FakeWorker
from tests.test_api import app, client, cloud, demo, root  # noqa: F401 - fixtures

CLEAN = (
    "RAMEN_REQUIRE_TLS",
    "RAMEN_WORKER_TLS",
    "RAMEN_WORKER_CA",
    "RAMEN_WORKER_TLS_NAME",
    "RAMEN_REDIS_URL",
    "RAMEN_CONSOLE_INTERNAL_URL",
    "RAMEN_EVALS_API_URL",
    "RAMEN_TLS_CA_DIR",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in CLEAN:
        monkeypatch.delenv(k, raising=False)
    tls.set_current(None)
    yield
    tls.set_current(None)


@pytest.fixture
def required(monkeypatch):
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")


def test_no_flag_no_problems(monkeypatch):
    monkeypatch.setenv("RAMEN_WORKER_TLS", "0")
    monkeypatch.setenv("RAMEN_REDIS_URL", "redis://:pw@redis:6379")
    assert tls.startup_problems() == []


def test_startup_problems_under_the_flag(monkeypatch, required):
    assert tls.startup_problems() == []
    monkeypatch.setenv("RAMEN_WORKER_TLS", "0")
    monkeypatch.setenv("RAMEN_REDIS_URL", "redis://:hunter@redis.ramen-system:6379")
    monkeypatch.setenv("RAMEN_CONSOLE_INTERNAL_URL", "http://console.ramen-system.svc.cluster.local:80")
    monkeypatch.setenv("RAMEN_EVALS_API_URL", "http://promptfoo.ramen-system:3000")
    problems = tls.startup_problems()
    assert len(problems) == 4
    text = " ".join(problems)
    assert "RAMEN_WORKER_TLS=0" in text and "rediss://" in text and "hunter" not in text  # never the password
    assert "RAMEN_CONSOLE_INTERNAL_URL" in text and "RAMEN_EVALS_API_URL" in text
    monkeypatch.setenv("RAMEN_REDIS_URL", "rediss://:hunter@redis.ramen-system.svc.cluster.local:6379")
    monkeypatch.setenv("RAMEN_WORKER_TLS", "1")
    monkeypatch.setenv("RAMEN_CONSOLE_INTERNAL_URL", "https://console.ramen-system.svc.cluster.local:443")
    monkeypatch.setenv("RAMEN_EVALS_API_URL", "https://promptfoo.ramen-system:3000")
    assert tls.startup_problems() == []


def test_the_console_refuses_to_start_with_plaintext(monkeypatch, required, cloud):  # noqa: F811
    monkeypatch.setenv("RAMEN_WORKER_TLS", "0")
    with pytest.raises(RuntimeError, match="D86"):
        create_app(store=make_store(), cloud=cloud)


def test_worker_tls_from_env(monkeypatch):
    assert worker_tls_from_env() is None
    monkeypatch.setenv("RAMEN_WORKER_TLS", "1")
    assert worker_tls_from_env() is True  # unchanged without the flag: system roots
    monkeypatch.setenv("RAMEN_WORKER_CA", "/certs/ca.crt")
    assert worker_tls_from_env() == {"ca": "/certs/ca.crt"}
    monkeypatch.setenv("RAMEN_WORKER_TLS_NAME", "worker")
    assert worker_tls_from_env() == {"ca": "/certs/ca.crt", "name": "worker"}


def test_worker_tls_defaults_on_with_the_install_ca(monkeypatch, required):
    t = worker_tls_from_env()
    assert t["name"] is tls.worker_name and t["ca"] is tls.bundle  # D92: the peer zone's own name
    monkeypatch.setenv("RAMEN_WORKER_CA", "/certs/ca.crt")
    t = worker_tls_from_env()
    assert t["ca"] == "/certs/ca.crt" and t["name"] is tls.worker_name and t["legacy"] is tls.legacy_worker_name


def test_credentials_resolve_a_callable_bundle_and_an_empty_one():
    ca = tls.new_ca()
    tls.set_current(ca)
    assert grpcclient._pem(tls.bundle) == ca.bundle.encode()
    tls.set_current(None)
    assert grpcclient._pem(tls.bundle) is None and grpcclient._pem("") is None


async def test_the_console_dials_a_tls_worker_by_pod_ip_with_the_worker_name(required):
    ca = tls.new_ca()
    tls.set_current(ca)
    leaf = ca.issue(tls.zone_names("ramen-demo-a", tls.worker_name("demo", "a")))
    w = FakeWorker(tls=(leaf.cert, leaf.key)).start()
    try:
        c = grpcclient.Client(tls=worker_tls_from_env(), deadline=5)
        peer = ("demo", "a")
        assert await c.health(w.target, peer=peer) is True  # 127.0.0.1:<port>, verified as demo.a.worker.ramen.internal
        assert (await c.reload(w.target, "adm", peer=peer))["tools"] == [{"name": "calc"}]
        tls.set_current(tls.new_ca())  # another install's CA: the handshake fails
        assert await c.health(w.target, peer=peer) is False
        # and plaintext to a TLS port never works
        assert await grpcclient.Client(deadline=2).health(w.target) is False
    finally:
        w.stop()


def test_internal_routes_are_no_longer_exempt_from_https(monkeypatch, tmp_path, cloud):  # noqa: F811
    """§39.3: the wake call and the judge proxy reach the console over TLS now; plain http is refused like any path."""
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.local")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setenv("RAMEN_BACKUP_ROOT", str(tmp_path / "b"))
    monkeypatch.setenv("RAMEN_BUCKET_ROOT", str(tmp_path / "k"))
    monkeypatch.setenv("RAMEN_COOKIE_SECURE", "1")
    monkeypatch.setenv("RAMEN_ADMIN_KEY", "k" * 32)
    key = {"x-ramen-admin-key": getattr(cloud, "admin_key", "") or "k" * 32}
    url = "/api/v1/internal/zones/nogroup/a/wake"
    with TestClient(create_app(store=make_store(), cloud=cloud), base_url="http://console.ramen-system.svc") as c:
        r = c.post(url, json={"trigger": "call"}, headers=key)
        assert r.status_code == 403 and "HTTPS" in r.text
        r = c.post("/llm/v1/chat/completions", json={})
        assert r.status_code == 403 and "HTTPS" in r.text
    with TestClient(create_app(store=make_store(), cloud=cloud), base_url="https://console.ramen-system.svc") as c:
        r = c.post(url, json={"trigger": "call"}, headers=key)
        assert r.status_code == 404 and "HTTPS" not in r.text  # reached the route over TLS: no such group


def test_otel_endpoints_must_be_https_under_the_flag(monkeypatch):
    assert otel.clean_url("http://jaeger.ramen-system:4318", "endpoint") == "http://jaeger.ramen-system:4318"
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    with pytest.raises(ApiError) as e:
        otel.clean_url("http://jaeger.ramen-system:4318", "endpoint")
    assert e.value.status_code == 422 and "https://" in str(e.value.detail)
    assert otel.clean_url("https://phoenix.ramen-system:4318/", "endpoint") == "https://phoenix.ramen-system:4318"


def test_llm_base_urls_must_be_https_under_the_flag(monkeypatch):
    assert llm_providers._clean("openai_compatible", "base_url", "http://vllm.models:8000/v1")
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    with pytest.raises(ValueError, match="https://"):
        llm_providers._clean("openai_compatible", "base_url", "http://vllm.models:8000/v1")
    assert llm_providers._clean("openai_compatible", "base_url", "https://vllm.models/v1") == "https://vllm.models/v1"


def test_throttle_redis_urls_must_be_rediss_under_the_flag(demo, monkeypatch):  # noqa: F811
    client = demo
    ok = client.put("/api/v1/groups/demo/throttle", json={"redis_url": "redis://redis:6379"})
    assert ok.status_code == 200, ok.text
    monkeypatch.setenv("RAMEN_REQUIRE_TLS", "1")
    r = client.put("/api/v1/groups/demo/throttle", json={"redis_url": "redis://:pw@redis:6379"})
    assert r.status_code == 422 and "rediss://" in r.text and ":pw@" not in r.text
    assert client.put("/api/v1/groups/demo/throttle", json={"redis_url": "rediss://redis:6379"}).status_code == 200
    assert client.put("/api/v1/groups/demo/throttle", json={"redis_url": ""}).status_code == 200
    item = "/api/v1/groups/demo/zones/zone-a/item-throttle"
    assert client.put(item, json={"redis_url": "redis://redis:6379"}).status_code == 422


class _Cloud:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def ensure_system_tls(self):
        self.calls.append("system")
        if self.fail:
            raise RuntimeError("api down")
        return ["redis"]

    async def renew_tls(self):
        self.calls.append("renew")
        if self.fail:
            raise RuntimeError("api down")
        return {"zones": ["ramen-demo-a"], "system": []}


async def test_startup_loads_the_ca_and_writes_the_chart_services(required):
    from ramen_console.storage.memory import MemoryStore

    store, cloud = MemoryStore(), _Cloud()
    await tls.startup(store, cloud)
    assert tls.current() is not None and cloud.calls == ["system"]
    await tls.startup(store, _Cloud(fail=True))  # logged, never fatal


async def test_startup_without_the_flag_does_nothing():
    cloud = _Cloud()
    await tls.startup(None, cloud)
    assert tls.current() is None and cloud.calls == []


async def test_renew_forever_rounds_and_survives_errors(required):
    import asyncio

    from ramen_console.storage.memory import MemoryStore

    store, ok, bad = MemoryStore(), _Cloud(), _Cloud(fail=True)
    tasks = [asyncio.create_task(tls.renew_forever(store, c, 0.01)) for c in (ok, bad)]
    await asyncio.sleep(0.05)
    assert "renew" in ok.calls and "renew" in bad.calls and not any(t.done() for t in tasks)
    for t in tasks:
        t.cancel()


def test_rotate_api_is_super_admin_and_audited(root, client, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("RAMEN_TLS_ROTATE_SETTLE_SECS", "0")  # both phases in the call (§39.1)
    r = root.post("/api/v1/config/tls/rotate")
    assert r.status_code == 200, r.text
    first = r.json()
    assert len(first["fingerprint"]) == 32 and first["previous"] is True and first["system"] == []
    assert first["stage"] == "rotated"
    r = root.post("/api/v1/config/tls/rotate?retire=1")
    assert r.status_code == 200 and r.json()["previous"] is False and r.json()["fingerprint"] == first["fingerprint"]
    tls.init_dir(tmp_path, {})
    monkeypatch.setenv("RAMEN_TLS_CA_DIR", str(tmp_path / "ca"))
    r = root.post("/api/v1/config/tls/rotate")
    assert r.status_code == 409 and "RAMEN_TLS_CA_DIR" in r.text
    audit = root.get("/api/v1/audit").json()
    rows = audit if isinstance(audit, list) else audit.get("items", audit.get("entries", []))
    assert any("config.tls.rotate" in str(a) for a in rows)
