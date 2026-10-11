"""§21.6 (D54): `RAMEN_STORE=azuretables` — one Table Storage table, partition = collection, row = id."""

import asyncio
import secrets

import pytest

from ramen_console.storage import make_store
from ramen_console.storage.azuretables import CHUNK, TableStore, row_key, unrow_key
from tests.fakes_azure import FakeTableService


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store():
    return TableStore(FakeTableService().create_table_if_not_exists("ramen"))


def test_put_get_list_delete_round_trip(store):
    doc = run(store.put("users", "u1", {"email": "a@x", "memberships": {"demo": "viewer"}}))
    assert doc == {"email": "a@x", "memberships": {"demo": "viewer"}, "id": "u1"}
    assert run(store.get("users", "u1")) == doc
    run(store.put("users", "u2", {"email": "b@x"}))
    run(store.put("groups", "demo", {"name": "demo"}))
    assert {d["id"] for d in run(store.list("users"))} == {"u1", "u2"}
    assert [d["id"] for d in run(store.list("users", {"email": "b@x"}))] == ["u2"]
    run(store.delete("users", "u1"))
    assert run(store.get("users", "u1")) is None
    run(store.delete("users", "missing"))  # deleting what is not there is not an error


def test_keys_with_characters_table_storage_forbids_round_trip(store):
    for key in ("a/b", "c#d", "e?f", "g\\h", "50%", "plain"):
        assert unrow_key(row_key(key)) == key and not set(row_key(key)) & set("/\\#?")
        run(store.put("config", key, {"v": key}))
        assert run(store.get("config", key))["v"] == key
    assert sorted(d["id"] for d in run(store.list("config"))) == sorted(["a/b", "c#d", "e?f", "g\\h", "50%", "plain"])


def test_a_document_larger_than_one_property_is_split_and_joined(store):
    big = {"blob": secrets.token_hex(CHUNK)}  # twice the chunk size in characters
    run(store.put("backups", "b1", big))
    ent = store._t.entities[("backups", "b1")]
    assert len([k for k in ent if k.startswith("doc")]) >= 2
    assert run(store.get("backups", "b1"))["blob"] == big["blob"]
    run(store.put("backups", "b1", {"blob": "small"}))  # shrinking drops the old chunks
    assert run(store.get("backups", "b1")) == {"blob": "small", "id": "b1"}
    assert len([k for k in store._t.entities[("backups", "b1")] if k.startswith("doc")]) == 1


def test_factory_builds_the_table_store_wrapped_by_encryption(monkeypatch):
    from cryptography.fernet import Fernet

    from ramen_console.storage.encrypted import EncryptedStore

    svc = FakeTableService()
    monkeypatch.setattr(TableStore, "_service", classmethod(lambda cls, account: svc))
    monkeypatch.setenv("RAMEN_STORE", "azuretables")
    monkeypatch.setenv("RAMEN_AZURE_STORAGE_ACCOUNT", "ramenacct")
    monkeypatch.setenv("RAMEN_FERNET_KEY", Fernet.generate_key().decode())
    s = make_store()
    assert isinstance(s, EncryptedStore) and isinstance(s.inner, TableStore)
    assert "ramen" in svc.tables
    monkeypatch.delenv("RAMEN_AZURE_STORAGE_ACCOUNT")
    with pytest.raises(ValueError, match="RAMEN_AZURE_STORAGE_ACCOUNT"):
        TableStore.from_env()
