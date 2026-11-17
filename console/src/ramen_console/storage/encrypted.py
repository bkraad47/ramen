from contextlib import asynccontextmanager

from cryptography.fernet import Fernet

from ..security import remember_secret
from .base import Doc, Filters, Store

SENSITIVE: dict[str, set[str]] = {
    "users": {"password_hash"},
    "secrets": {"value"},
    "api_keys": {"secret_hash"},
    "groups": {"github_token", "session_secret", "redis_scope_url"},
    "environments": {"github_token"},
    "config": {
        "smtp_password",
        "github_app_private_key",
        "token_salt",
        "otel_headers",
        "otel_client_headers",
        "ca_key",
        "next_key",  # §39.1: a staged CA's key
    },
    "workers": {"redis_item_url"},
}
# D50: a doc whose SECRET lives one level down, per entry — (collection, key) → the nested field name
NESTED: dict[tuple[str, str], str] = {
    ("config", "oauth_providers"): "client_secret",
    ("config", "llm_providers"): "api_key",  # §29.1
}
PREFIX = "enc:"


def nested_field(collection: str, key) -> str | None:
    """The NESTED rule of a doc; `config/groups/<g>/<doc>` (§36.2 group-scoped LLM providers) follows `config/<doc>`."""
    if not isinstance(key, str):
        return None
    if key.startswith("groups/") and key.count("/") == 2:
        key = key.rsplit("/", 1)[1]
    return NESTED.get((collection, key))


class FieldCipher:
    def __init__(self, key: str):
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, value: str) -> str:
        remember_secret(value)
        return PREFIX + self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        out = self._f.decrypt(value[len(PREFIX) :].encode()).decode()
        remember_secret(out)
        return out


class EncryptedStore(Store):
    def __init__(self, inner: Store, cipher: FieldCipher):
        self.inner, self._cipher = inner, cipher

    def _wrap(self, collection, doc: Doc | None, fn, key=None):
        if doc is None:
            return None
        fields = SENSITIVE.get(collection, set())
        out = {k: fn(v) if k in fields and isinstance(v, str) else v for k, v in doc.items()}
        if nested := nested_field(collection, key or doc.get("id")):
            for k, v in out.items():
                if isinstance(v, dict) and isinstance(v.get(nested), str):
                    out[k] = {**v, nested: fn(v[nested])}
        return out

    async def get(self, collection, key):
        return self._wrap(collection, await self.inner.get(collection, key), self._cipher.decrypt, key)

    async def put(self, collection, key, doc: Doc):
        stored = await self.inner.put(collection, key, self._wrap(collection, doc, self._cipher.encrypt, key))
        return self._wrap(collection, stored, self._cipher.decrypt, key)

    async def delete(self, collection, key):
        await self.inner.delete(collection, key)

    async def list(self, collection, filters: Filters = None):
        return [self._wrap(collection, d, self._cipher.decrypt) for d in await self.inner.list(collection, filters)]

    @asynccontextmanager
    async def transaction(self):
        async with self.inner.transaction():
            yield self
