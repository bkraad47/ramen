from contextlib import asynccontextmanager

from cryptography.fernet import Fernet

from .base import Doc, Filters, Store

SENSITIVE: dict[str, set[str]] = {
    "users": {"password_hash"},
    "secrets": {"value"},
    "api_keys": {"secret_hash"},
    "groups": {"github_token", "session_secret", "redis_scope_url"},
    "environments": {"github_token"},
    "config": {"smtp_password", "github_app_private_key"},
    "workers": {"redis_item_url"},
}
# D50: a doc whose SECRET lives one level down, per entry — (collection, key) → the nested field name
NESTED: dict[tuple[str, str], str] = {("config", "oauth_providers"): "client_secret"}
PREFIX = "enc:"


class FieldCipher:
    def __init__(self, key: str):
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, value: str) -> str:
        return PREFIX + self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        return self._f.decrypt(value[len(PREFIX) :].encode()).decode()


class EncryptedStore(Store):
    def __init__(self, inner: Store, cipher: FieldCipher):
        self.inner, self._cipher = inner, cipher

    def _wrap(self, collection, doc: Doc | None, fn, key=None):
        if doc is None:
            return None
        fields = SENSITIVE.get(collection, set())
        out = {k: fn(v) if k in fields and isinstance(v, str) else v for k, v in doc.items()}
        if nested := NESTED.get((collection, key or doc.get("id"))):
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
