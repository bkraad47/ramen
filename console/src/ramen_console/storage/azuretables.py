"""§21.6 (D54): `RAMEN_STORE=azuretables` — one Azure Table Storage table (`ramen`), partition = collection,
row = id, the document as JSON. A string property holds at most 32K characters, so a document is split over
`doc0..docN`; Table Storage forbids `/ \\ # ?` (and control characters) in keys, which are percent-escaped.
Credentials: `DefaultAzureCredential` (workload identity on AKS). Env: `RAMEN_AZURE_STORAGE_ACCOUNT`,
`RAMEN_AZURE_TABLE` (default `ramen`)."""

import asyncio
import json
import os

from .base import Doc, Filters, Store, matches

CHUNK = 30000  # characters per property, under the 32K limit
_FORBIDDEN = "%/\\#?"


def row_key(key: str) -> str:
    return "".join(f"%{ord(c):02X}" if c in _FORBIDDEN or ord(c) < 32 else c for c in key)


def unrow_key(key: str) -> str:
    from urllib.parse import unquote

    return unquote(key)


def _join(entity) -> Doc:
    parts = sorted((k for k in entity if k.startswith("doc") and k[3:].isdigit()), key=lambda k: int(k[3:]))
    return json.loads("".join(entity[k] for k in parts))


class TableStore(Store):
    def __init__(self, table):
        self._t = table

    @classmethod
    def _service(cls, account):
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        return TableServiceClient(f"https://{account}.table.core.windows.net", credential=DefaultAzureCredential())

    @classmethod
    def from_env(cls):
        account = os.environ.get("RAMEN_AZURE_STORAGE_ACCOUNT")
        if not account:
            raise ValueError("RAMEN_STORE=azuretables needs RAMEN_AZURE_STORAGE_ACCOUNT")
        return cls(cls._service(account).create_table_if_not_exists(os.environ.get("RAMEN_AZURE_TABLE", "ramen")))

    def _get(self, collection, key):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return _join(self._t.get_entity(partition_key=collection, row_key=row_key(key)))
        except ResourceNotFoundError:
            return None

    async def get(self, collection, key):
        return await asyncio.to_thread(self._get, collection, key)

    async def put(self, collection, key, doc: Doc):
        stored = {**doc, "id": key}
        raw = json.dumps(stored)
        entity = {"PartitionKey": collection, "RowKey": row_key(key)}
        entity |= {f"doc{i}": raw[n : n + CHUNK] for i, n in enumerate(range(0, max(len(raw), 1), CHUNK))}
        from azure.data.tables import UpdateMode

        await asyncio.to_thread(self._t.upsert_entity, entity, mode=UpdateMode.REPLACE)  # replace drops old chunks
        return dict(stored)

    async def delete(self, collection, key):
        await asyncio.to_thread(self._t.delete_entity, partition_key=collection, row_key=row_key(key))

    async def list(self, collection, filters: Filters = None):
        rows = await asyncio.to_thread(
            lambda: list(self._t.query_entities("PartitionKey eq @pk", parameters={"pk": collection}))
        )
        return [d for d in (_join(r) for r in rows) if matches(d, filters)]
