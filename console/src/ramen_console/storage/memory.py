from copy import deepcopy

from .base import Doc, Filters, Store, matches


class MemoryStore(Store):
    def __init__(self):
        self._data: dict[str, dict[str, Doc]] = {}

    async def get(self, collection, key):
        doc = self._data.get(collection, {}).get(key)
        return deepcopy(doc) if doc is not None else None

    async def put(self, collection, key, doc: Doc):
        stored = {**deepcopy(doc), "id": key}
        self._data.setdefault(collection, {})[key] = stored
        return deepcopy(stored)

    async def delete(self, collection, key):
        self._data.get(collection, {}).pop(key, None)

    async def list(self, collection, filters: Filters = None):
        return [deepcopy(d) for d in self._data.get(collection, {}).values() if matches(d, filters)]
