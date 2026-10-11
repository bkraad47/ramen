"""§21.6 (D54): `RAMEN_SECRETS_BACKEND=azure` — Key Vault, names `ramen--<group>--<env>--<zone>--<name>`."""

import asyncio
import secrets

import pytest

from ramen_console.errors import ApiError
from ramen_console.secrets import make_secrets_backend
from ramen_console.secrets.azure import AzureSecrets, secret_name, unsecret_name
from tests.fakes_azure import VAULT, FakeVault


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def vault():
    return FakeVault()


def test_names_are_key_vault_safe_and_reversible():
    n = secret_name("demo", "dev", "eastus-1", "API_TOKEN")
    assert n.startswith("ramen--demo--dev--eastus-2d1--") and set(n) <= set(
        "abcdefghijklmnopqrstuvwxyz0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    assert unsecret_name(n) == ("demo", "dev", "eastus-1", "API_TOKEN")
    assert unsecret_name(secret_name("demo", None, None, "X", "mcp")) == ("demo", "all", "all", "mcp-X")
    assert secret_name("a-b", "", "", "c") != secret_name("a", "b", "", "c")  # a dash never collides with a separator
    assert len(secret_name("g" * 30, "e" * 30, "z" * 30, "N" * 30)) <= 127


def test_put_stores_a_ref_resolve_reads_it_and_the_value_never_lands_in_the_doc(vault):
    bound: list[tuple] = []
    be = AzureSecrets(VAULT, vault, on_create=lambda scope, group: bound.append((scope, group)))
    value = "v-" + secrets.token_hex(8)
    doc = run(be.put("demo", "dev", "eastus-1", "API_TOKEN", value))
    assert doc["value"] is None and doc["ref"].startswith("sm://azure/ramen-kv/ramen--demo--")
    assert value not in str(doc)
    assert run(be.resolve(doc["ref"])) == value
    assert run(be.resolve("plain")) == "plain" and run(be.resolve(None)) is None
    name = doc["ref"].rsplit("/", 1)[1]
    assert bound == [(f"secrets/{name}", "demo")]  # the zone identities of the group get read on this secret
    run(be.put("demo", "dev", "eastus-1", "API_TOKEN", "second"))  # a new version, no second binding
    assert run(be.resolve(doc["ref"])) == "second" and len(bound) == 1


def test_delete_then_recreate_recovers_the_soft_deleted_secret(vault):
    be = AzureSecrets(VAULT, vault)
    doc = run(be.put("demo", None, None, "K", "one"))
    run(be.delete(doc))
    with pytest.raises(ApiError) as e:
        run(be.resolve(doc["ref"]))
    assert e.value.status_code == 502 and "one" not in e.value.detail
    doc = run(be.put("demo", None, None, "K", "two"))  # Key Vault soft-delete: recover, then a new version
    assert run(be.resolve(doc["ref"])) == "two"
    run(be.delete({"ref": None}))
    run(be.delete({"ref": "sm://azure/ramen-kv/ramen--gone--all--all--x"}))  # already gone is fine


def test_resolve_config_expands_refs_inside_values(vault):
    be = AzureSecrets(VAULT, vault)
    a = run(be.put("demo", None, None, "A", "aa"))["ref"]
    b = run(be.put("demo", None, None, "B", "bb"))["ref"]
    assert run(be.resolve_config({"RAMEN_MCP_KEYS": f"{a},{b}", "X": "plain"})) == {
        "RAMEN_MCP_KEYS": "aa,bb",
        "X": "plain",
    }


def test_factory_needs_the_vault_and_uses_the_clouds_binding(monkeypatch, vault):
    monkeypatch.setenv("RAMEN_SECRETS_BACKEND", "azure")
    monkeypatch.delenv("RAMEN_AZURE_KEY_VAULT", raising=False)
    with pytest.raises(ValueError, match="RAMEN_AZURE_KEY_VAULT"):
        make_secrets_backend()
    monkeypatch.setenv("RAMEN_AZURE_KEY_VAULT", VAULT)

    class Cloud:
        c = type("C", (), {"vault": vault})()

        def bind_new_secret(self, scope, group):
            return True

    be = make_secrets_backend(Cloud())
    assert isinstance(be, AzureSecrets) and be.client is vault and be.on_create is not None
