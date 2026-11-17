"""1.0.0: on a cloud the console's own reversible secrets live in that cloud's secret manager, not the database.

Each value is written to a secret of its own (`console-<collection>-<key>-<field>-<random>`), the doc keeps
`vault:<ref>`; a changed value gets a new secret and the old one is deleted, so a ref never changes meaning and is
cached for good. Hashes (passwords, API keys) and group secrets (already refs) are not vaulted. Locally
(RAMEN_SECRETS_BACKEND=store) nothing changes: the fields stay Fernet-encrypted in the store. Values are never logged.
"""

import logging
import secrets
from contextlib import asynccontextmanager

from ..security import remember_secret
from .base import Doc, Filters, Store
from .encrypted import NESTED, SENSITIVE

log = logging.getLogger("ramen.vault")
HASHES = {("users", "password_hash"), ("api_keys", "secret_hash"), ("secrets", "value")}
VAULTED = {c: {f for f in fs if (c, f) not in HASHES} for c, fs in SENSITIVE.items()}
MARK = "vault:"


def _paths(collection: str, key: str, doc: Doc):
    """(field, nested field or None) for every vaulted value present in `doc`."""
    for f in VAULTED.get(collection, ()):
        if f in doc:
            yield f, None
    if nested := NESTED.get((collection, key)):
        for k, v in doc.items():
            if isinstance(v, dict) and nested in v:
                yield k, nested


def _get(doc, f, n):
    return doc.get(f) if n is None else (doc.get(f) or {}).get(n)


def _set(doc, f, n, v):
    if n is None:
        doc[f] = v
    else:
        doc[f] = {**doc[f], n: v}


class VaultedStore(Store):
    def __init__(self, inner: Store, backend):
        self.inner, self.backend, self._cache = inner, backend, {}

    async def _resolve(self, raw):
        if not isinstance(raw, str) or not raw.startswith(MARK):
            return raw
        ref = raw[len(MARK) :]
        if ref not in self._cache:
            self._cache[ref] = await self.backend.resolve(ref)
            remember_secret(self._cache[ref])
        return self._cache[ref]

    async def _open(self, collection, key, doc):
        if doc is None:
            return None
        out = dict(doc)
        for f, n in list(_paths(collection, key or doc.get("id", ""), doc)):
            _set(out, f, n, await self._resolve(_get(doc, f, n)))
        return out

    async def _seal(self, collection, key, doc, old):
        """Vault every plaintext value in `doc`; reuse the old ref when the value did not change. Returns the doc to
        store and the refs that are no longer used."""
        out, keep = dict(doc), set()
        for f, n in list(_paths(collection, key, doc)):
            v = _get(doc, f, n)
            if not isinstance(v, str) or not v or v.startswith(MARK):
                continue
            prev = _get(old, f, n) if old else None
            if isinstance(prev, str) and prev.startswith(MARK) and await self._resolve(prev) == v:
                _set(out, f, n, prev)
                keep.add(prev)
                continue
            name = "-".join(p for p in ("console", collection, key, f, n, secrets.token_hex(4)) if p)
            remember_secret(v)
            ref = await self.backend.put_value(name, v)
            self._cache[ref] = v
            _set(out, f, n, MARK + ref)
            keep.add(MARK + ref)
        return out, self._refs(collection, key, old) - keep - self._refs(collection, key, out)

    def _refs(self, collection, key, doc):
        if not doc:
            return set()
        return {
            v for f, n in _paths(collection, key, doc) if isinstance(v := _get(doc, f, n), str) and v.startswith(MARK)
        }

    async def _drop(self, refs):
        for r in refs:
            ref = r[len(MARK) :]
            self._cache.pop(ref, None)
            try:
                await self.backend.drop_value(ref)
            except Exception as e:  # noqa: BLE001 - a leftover secret is not worth failing the write
                log.warning("vault: could not delete an old secret (%s)", type(e).__name__)

    async def get(self, collection, key):
        return await self._open(collection, key, await self.inner.get(collection, key))

    async def put(self, collection, key, doc: Doc):
        if not _vaulted(collection, key):
            return await self.inner.put(collection, key, doc)
        old = await self.inner.get(collection, key)
        sealed, stale = await self._seal(collection, key, doc, old)
        stored = await self.inner.put(collection, key, sealed)
        await self._drop(stale)
        return await self._open(collection, key, stored)

    async def delete(self, collection, key):
        old = await self.inner.get(collection, key) if _vaulted(collection, key) else None
        await self.inner.delete(collection, key)
        await self._drop(self._refs(collection, key, old))

    async def list(self, collection, filters: Filters = None):
        return [await self._open(collection, None, d) for d in await self.inner.list(collection, filters)]

    @asynccontextmanager
    async def transaction(self):
        async with self.inner.transaction():
            yield self

    async def migrate(self) -> int:
        """Move plaintext values written before 1.0.0 (or while the backend was down) into the vault."""
        moved = 0
        for collection in VAULTED.keys() | {c for c, _ in NESTED}:
            for d in await self.inner.list(collection):
                key = d.get("id")
                if key and any(
                    isinstance(v := _get(d, f, n), str) and v and not v.startswith(MARK)
                    for f, n in _paths(collection, key, d)
                ):
                    await self.put(collection, key, {k: v for k, v in d.items()})
                    moved += 1
        if moved:
            log.info("vault: moved the secrets of %d documents into the secret manager", moved)
        return moved


def _vaulted(collection, key) -> bool:
    return bool(VAULTED.get(collection)) or (collection, key) in NESTED
