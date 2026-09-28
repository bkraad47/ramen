import pytest

from ramen_console.errors import ApiError
from ramen_console.secrets import make_secrets_backend
from ramen_console.secrets.base import StoreBackend
from ramen_console.secrets.gcp import GcpSecrets, secret_id
from tests.fakes_gcp import FakeApiError, FakeClients, FakeSM


async def test_store_backend():
    b = StoreBackend()
    assert await b.put("g", None, None, "A", "v") == {"value": "v", "ref": None}
    assert await b.resolve("v") == "v"
    await b.delete({"ref": None})
    assert await b.resolve_config({"X": "a,b"}) == {"X": "a,b"}


def test_secret_id():
    assert secret_id("demo", "prod", None, "TOKEN") == "ramen-demo-prod-all-TOKEN"
    assert secret_id("demo", None, "a", "ci", kind="mcp_key") == "ramen-demo-all-a-mcp-ci"


async def test_gcp_backend_roundtrip():
    sm = FakeSM()
    b = GcpSecrets("p1", sm)
    doc = await b.put("demo", "prod", None, "TOKEN", "s3cret")
    assert doc == {"value": None, "ref": "sm://projects/p1/secrets/ramen-demo-prod-all-TOKEN"}
    assert sm.secrets[doc["ref"][5:]]["labels"] == {"ramen": "secret", "group": "demo", "env": "prod", "zone": "all"}
    doc2 = await b.put("demo", "prod", None, "TOKEN", "v2")  # idempotent create, new version
    assert doc2 == doc and sm.calls.count("create") == 2 and sm.calls.count("add_version") == 2
    bound = []
    b2 = GcpSecrets("p1", sm, on_create=lambda name, group: bound.append((name, group)))
    await b2.put("demo", "prod", None, "TOKEN", "v2")  # exists: no binding call
    await b2.put("demo", "dev", "a", "TOKEN", "v1")
    assert bound == [("projects/p1/secrets/ramen-demo-dev-a-TOKEN", "demo")]
    await b2.delete({"ref": "sm://projects/p1/secrets/ramen-demo-dev-a-TOKEN"})
    assert await b.resolve(doc["ref"]) == "v2"
    assert await b.resolve("plain") == "plain" and await b.resolve(None) is None
    assert await b.resolve_config({"RAMEN_MCP_KEYS": f"{doc['ref']},{doc['ref']}", "X": "1"}) == {
        "RAMEN_MCP_KEYS": "v2,v2",
        "X": "1",
    }
    await b.delete(doc)
    assert not sm.secrets
    await b.delete(doc)  # 404 ignored
    await b.delete({"ref": None})
    with pytest.raises(ApiError) as e:
        await b.resolve(doc["ref"])
    assert e.value.status_code == 502 and "v2" not in e.value.detail


async def test_gcp_backend_errors():
    class Boom(FakeSM):
        def create_secret(self, request):
            raise FakeApiError(500, "internal")

        def delete_secret(self, request):
            raise FakeApiError(500, "internal")

    b = GcpSecrets("p1", Boom())
    with pytest.raises(ApiError, match="secret manager"):
        await b.put("g", None, None, "A", "v")
    with pytest.raises(ApiError, match="secret manager"):
        await b.delete({"ref": "sm://projects/p1/secrets/x"})


def test_factory(monkeypatch):
    monkeypatch.delenv("RAMEN_SECRETS_BACKEND", raising=False)
    assert isinstance(make_secrets_backend(), StoreBackend)
    monkeypatch.setenv("RAMEN_SECRETS_BACKEND", "gcp")
    monkeypatch.delenv("RAMEN_GCP_PROJECT", raising=False)
    with pytest.raises(ValueError, match="RAMEN_GCP_PROJECT"):
        make_secrets_backend()
    monkeypatch.setenv("RAMEN_GCP_PROJECT", "p1")
    fk = FakeClients()
    b = make_secrets_backend(cloud=type("C", (), {"c": fk})())
    assert isinstance(b, GcpSecrets) and b.client is fk.secretmanager
    monkeypatch.setattr("ramen_console.cloud.gcp_clients.GcpClients.secretmanager", property(lambda self: "real"))
    assert make_secrets_backend().client == "real"
    monkeypatch.setenv("RAMEN_SECRETS_BACKEND", "vault")
    with pytest.raises(ValueError):
        make_secrets_backend()
