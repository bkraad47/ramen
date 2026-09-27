import os

from .base import Doc, Filters, Store


class FirestoreStore(Store):
    def __init__(self, client, prefix: str = "ramen_"):
        self._c, self._p = client, prefix

    @classmethod
    def from_env(cls):
        from google.cloud import firestore
        project = os.environ.get("RAMEN_GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if os.environ.get("FIRESTORE_EMULATOR_HOST") and not project:
            project = "ramen-local"
        return cls(firestore.AsyncClient(project=project), os.environ.get("RAMEN_FIRESTORE_PREFIX", "ramen_"))

    def _col(self, collection):
        return self._c.collection(self._p + collection)

    async def get(self, collection, key):
        snap = await self._col(collection).document(key).get()
        return snap.to_dict() if snap.exists else None

    async def put(self, collection, key, doc: Doc):
        stored = {**doc, "id": key}
        await self._col(collection).document(key).set(stored)
        return dict(stored)

    async def delete(self, collection, key):
        await self._col(collection).document(key).delete()

    async def list(self, collection, filters: Filters = None):
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self._col(collection)
        for k, v in (filters or {}).items():
            q = q.where(filter=FieldFilter(k, "==", v))
        return [s.to_dict() async for s in q.stream()]
