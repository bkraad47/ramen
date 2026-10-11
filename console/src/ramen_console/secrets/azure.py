"""§21.6 (D54): Key Vault backend (`RAMEN_SECRETS_BACKEND=azure`, `RAMEN_AZURE_KEY_VAULT`). Values live in the vault
as `ramen--<group>--<env>--<zone>--<name>`; the store keeps the name and `sm://azure/<vault>/<secret>` only. Key Vault
names allow letters, digits and dashes, so every other character (and the dash itself) is escaped as `-XX` (hex):
`eastus-1` → `eastus-2d1`, `API_TOKEN` → `API-5fTOKEN`; a doubled dash only ever separates components. Deleted secrets
are soft-deleted by Key Vault: writing the same name again recovers it and adds a version."""

import asyncio
import re

from ..errors import ApiError
from .base import REF_PREFIX, SecretsBackend

_SAFE = re.compile(r"[A-Za-z0-9]")


def _esc(part: str) -> str:
    return "".join(c if _SAFE.match(c) else f"-{ord(c):02x}" for c in part)


def _unesc(part: str) -> str:
    return re.sub(r"-([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), part)


def secret_name(group, env, zone, name, kind="secret") -> str:
    n = name if kind == "secret" else f"mcp-{name}"
    return "--".join(["ramen", *(_esc(p) for p in (group, env or "all", zone or "all", n))])[:127]


def unsecret_name(full: str) -> tuple[str, str, str, str]:
    _, group, env, zone, name = full.split("--")
    return _unesc(group), _unesc(env), _unesc(zone), _unesc(name)


def _status(e) -> int | None:
    return getattr(e, "status_code", None)


class AzureSecrets(SecretsBackend):
    kind = "azure"

    def __init__(self, vault: str, client, on_create=None):
        self.vault, self.client, self.on_create = vault, client, on_create

    def _ref(self, name) -> str:
        return f"{REF_PREFIX}azure/{self.vault}/{name}"

    def _put(self, group, env, zone, name, value, kind):
        from azure.core.exceptions import HttpResponseError

        sid = secret_name(group, env, zone, name, kind)
        tags = {"ramen": "secret", "group": group, "env": env or "all", "zone": zone or "all"}
        existed = True
        try:
            self.client.get_secret(sid)
        except Exception:  # noqa: BLE001 - absent (or soft-deleted): created below
            existed = False
        try:
            self.client.set_secret(sid, value, tags=tags)
        except HttpResponseError as e:
            if _status(e) != 409:  # 409: deleted but recoverable — recover, then write the new version
                raise
            self.client.begin_recover_deleted_secret(sid).wait()
            self.client.set_secret(sid, value, tags=tags)
        if not existed and self.on_create:  # per-secret Key Vault Secrets User for the group's zone identities
            self.on_create(f"secrets/{sid}", group)
        return {"value": None, "ref": self._ref(sid)}

    async def put(self, group, env, zone, name, value, kind="secret"):
        try:
            return await asyncio.to_thread(self._put, group, env, zone, name, value, kind)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"Key Vault: {type(e).__name__}: {str(e)[:200]}") from e

    async def resolve(self, value):
        if not value or not value.startswith(REF_PREFIX):
            return value
        name = value.rsplit("/", 1)[1]
        try:
            return await asyncio.to_thread(lambda: self.client.get_secret(name).value)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"Key Vault: cannot read {value}: {type(e).__name__}") from e

    async def delete(self, doc):
        ref = doc.get("ref")
        if not ref:
            return
        from azure.core.exceptions import ResourceNotFoundError

        try:
            await asyncio.to_thread(lambda: self.client.begin_delete_secret(ref.rsplit("/", 1)[1]).wait())
        except ResourceNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"Key Vault: {type(e).__name__}: {str(e)[:200]}") from e


def from_env(cloud=None) -> AzureSecrets:
    import os

    vault = os.environ.get("RAMEN_AZURE_KEY_VAULT")
    if not vault:
        raise ValueError("RAMEN_SECRETS_BACKEND=azure needs RAMEN_AZURE_KEY_VAULT")
    client = getattr(getattr(cloud, "c", None), "vault", None)
    if client is None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        client = SecretClient(f"https://{vault}.vault.azure.net", credential=DefaultAzureCredential())
    return AzureSecrets(vault, client, on_create=getattr(cloud, "bind_new_secret", None))
