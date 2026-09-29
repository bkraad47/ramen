from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

COLLECTIONS = (
    "users",
    "groups",
    "environments",
    "zones",
    "workers",
    "secrets",
    "api_keys",
    "audit",
    "activity",
    "config",
    "backups",
    "images",
)
Doc = dict[str, Any]
Filters = dict[str, Any] | None


class Store(ABC):
    @abstractmethod
    async def get(self, collection: str, key: str) -> Doc | None: ...

    @abstractmethod
    async def put(self, collection: str, key: str, doc: Doc) -> Doc: ...

    @abstractmethod
    async def delete(self, collection: str, key: str) -> None: ...

    @abstractmethod
    async def list(self, collection: str, filters: Filters = None) -> list[Doc]: ...

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Store]:
        async with self._lock():
            yield self

    def _lock(self):
        import asyncio

        if not hasattr(self, "_tx_lock"):
            self._tx_lock = asyncio.Lock()
        return self._tx_lock


def matches(doc: Doc, filters: Filters) -> bool:
    return all(doc.get(k) == v for k, v in (filters or {}).items())
