import asyncio
import json
import os

import asyncpg

from .base import Doc, Filters, Store, matches

DDL = """
CREATE TABLE IF NOT EXISTS ramen_store (
    collection TEXT NOT NULL,
    key TEXT NOT NULL,
    doc JSONB NOT NULL,
    PRIMARY KEY (collection, key)
)
"""


class PostgresStore(Store):
    """v0.5.5 I14: an optional store backend alongside firestore/dynamodb — one table, keyed like the others
    (collection, key) -> doc, so the same shallow Store contract (get/put/delete/list, no querying beyond
    exact-match filters) applies unchanged. Not the default (D2/G1: Firestore on GCP, DynamoDB on AWS); pick
    it with RAMEN_STORE=postgres for a self-hosted/on-prem deployment that has Postgres but not a cloud DB.

    The pool is created lazily on first use, not in __init__: make_store() is called synchronously at app
    startup (outside any event loop), but asyncpg.create_pool is a coroutine — connecting eagerly there is
    not possible without changing every other adapter's construction contract too."""

    def __init__(self, dsn: str = "", pool=None):
        self._dsn = dsn
        self._pool = pool  # tests inject a fake pool directly, skipping the DSN/create_pool path entirely
        self._pool_lock = asyncio.Lock()  # not `_lock`: Store._lock() is the base class's transaction lock

    @classmethod
    def from_env(cls) -> PostgresStore:
        dsn = os.environ.get("RAMEN_POSTGRES_DSN")
        if not dsn:
            raise ValueError("RAMEN_POSTGRES_DSN is required for RAMEN_STORE=postgres")
        return cls(dsn)

    async def _get_pool(self):
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(self._dsn)
                    async with self._pool.acquire() as conn:
                        await conn.execute(DDL)
        return self._pool

    async def get(self, collection, key):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT doc FROM ramen_store WHERE collection = $1 AND key = $2", collection, key)
        return json.loads(row["doc"]) if row else None

    async def put(self, collection, key, doc: Doc):
        stored = {**doc, "id": key}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ramen_store (collection, key, doc) VALUES ($1, $2, $3) "
                "ON CONFLICT (collection, key) DO UPDATE SET doc = EXCLUDED.doc",
                collection,
                key,
                json.dumps(stored),
            )
        return dict(stored)

    async def delete(self, collection, key):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM ramen_store WHERE collection = $1 AND key = $2", collection, key)

    async def list(self, collection, filters: Filters = None):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT doc FROM ramen_store WHERE collection = $1", collection)
        docs = [json.loads(r["doc"]) for r in rows]
        return [d for d in docs if matches(d, filters)]
