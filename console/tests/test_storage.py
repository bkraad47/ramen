import os

import boto3
import pytest
from moto import mock_aws

from ramen_console.storage import make_store
from ramen_console.storage.base import Store
from ramen_console.storage.dynamodb import DynamoStore
from ramen_console.storage.encrypted import EncryptedStore, FieldCipher, SENSITIVE
from ramen_console.storage.firestore import FirestoreStore
from ramen_console.storage.memory import MemoryStore
from tests.fakes import FakeFirestoreClient


@pytest.fixture
def ddb_store():
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        os.environ["AWS_ACCESS_KEY_ID"] = "x"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "x"
        s = DynamoStore(table="ramen-test", resource=boto3.resource("dynamodb"))
        s.ensure_table()
        s.ensure_table()
        yield s


@pytest.fixture(params=["memory", "firestore", "dynamodb"])
def store(request, ddb_store) -> Store:
    if request.param == "memory":
        return MemoryStore()
    if request.param == "firestore":
        return FirestoreStore(client=FakeFirestoreClient())
    return ddb_store


async def test_put_get_delete(store):
    assert await store.get("users", "u1") is None
    doc = await store.put("users", "u1", {"email": "a@b", "n": 1, "tags": ["x"]})
    assert doc["id"] == "u1"
    got = await store.get("users", "u1")
    assert got == {"id": "u1", "email": "a@b", "n": 1, "tags": ["x"]}
    await store.delete("users", "u1")
    await store.delete("users", "u1")
    assert await store.get("users", "u1") is None


async def test_list_filters(store):
    await store.put("secrets", "s1", {"group": "g1", "zone": "z1", "name": "A"})
    await store.put("secrets", "s2", {"group": "g1", "zone": "z2", "name": "B"})
    await store.put("secrets", "s3", {"group": "g2", "zone": "z1", "name": "C"})
    assert {d["id"] for d in await store.list("secrets")} == {"s1", "s2", "s3"}
    assert [d["id"] for d in await store.list("secrets", {"group": "g1", "zone": "z2"})] == ["s2"]
    assert await store.list("nothing") == []


async def test_transaction(store):
    async with store.transaction() as tx:
        await tx.put("groups", "g", {"n": 1})
        cur = await tx.get("groups", "g")
        await tx.put("groups", "g", {"n": cur["n"] + 1})
    assert (await store.get("groups", "g"))["n"] == 2


async def test_put_returns_copy(store):
    src = {"a": 1}
    doc = await store.put("config", "c", src)
    doc["a"] = 2
    assert (await store.get("config", "c"))["a"] == 1


def test_cipher_roundtrip(fernet_key):
    c = FieldCipher(fernet_key)
    token = c.encrypt("hello")
    assert token != "hello" and token.startswith("enc:")
    assert c.decrypt(token) == "hello"
    assert c.decrypt("plain") == "plain"


async def test_encrypted_store_hides_sensitive(fernet_key):
    inner = MemoryStore()
    es = EncryptedStore(inner, FieldCipher(fernet_key))
    await es.put("secrets", "s", {"name": "TOKEN", "value": "hunter2"})
    raw = await inner.get("secrets", "s")
    assert raw["value"] != "hunter2" and raw["value"].startswith("enc:")
    assert (await es.get("secrets", "s"))["value"] == "hunter2"
    assert (await es.list("secrets"))[0]["value"] == "hunter2"
    assert await es.get("secrets", "missing") is None
    async with es.transaction() as tx:
        await tx.put("users", "u", {"password_hash": "h"})
        assert (await tx.get("users", "u"))["password_hash"] == "h"
    assert (await inner.get("users", "u"))["password_hash"].startswith("enc:")
    await es.delete("users", "u")
    assert await inner.get("users", "u") is None
    assert "value" in SENSITIVE["secrets"]


def test_factory(monkeypatch, fernet_key):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    s = make_store()
    assert isinstance(s, EncryptedStore) and isinstance(s.inner, MemoryStore)
    monkeypatch.setenv("RAMEN_STORE", "firestore")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8081")
    s = make_store(firestore_client=FakeFirestoreClient())
    assert isinstance(s.inner, FirestoreStore)
    monkeypatch.setenv("RAMEN_STORE", "bogus")
    with pytest.raises(ValueError):
        make_store()
    monkeypatch.delenv("RAMEN_FERNET_KEY")
    monkeypatch.setenv("RAMEN_STORE", "memory")
    assert isinstance(make_store(), MemoryStore)


def test_factory_dynamodb(monkeypatch):
    monkeypatch.setenv("RAMEN_STORE", "dynamodb")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        s = make_store()
        assert isinstance(s.inner, DynamoStore)


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("FIRESTORE_EMULATOR_HOST"), reason="no emulator")
async def test_firestore_real_client():
    s = FirestoreStore.from_env()
    await s.put("users", "it", {"x": 1})
    assert (await s.get("users", "it"))["x"] == 1


def test_firestore_from_env_constructs(monkeypatch):
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8081")
    monkeypatch.delenv("RAMEN_GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    s = FirestoreStore.from_env()
    assert s._p == "ramen_" and s._c.project == "ramen-local"
