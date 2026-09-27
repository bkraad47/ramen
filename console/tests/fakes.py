class _Snap:
    def __init__(self, data):
        self._d = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._d)


class _Doc:
    def __init__(self, col, key):
        self._col, self._key = col, key

    async def get(self):
        return _Snap(self._col._docs.get(self._key))

    async def set(self, data):
        self._col._docs[self._key] = dict(data)

    async def delete(self):
        self._col._docs.pop(self._key, None)


class _Query:
    def __init__(self, col, conds):
        self._col, self._conds = col, conds

    def where(self, filter):
        return _Query(self._col, self._conds + [(filter.field_path, filter.value)])

    async def stream(self):
        for d in self._col._docs.values():
            if all(d.get(k) == v for k, v in self._conds):
                yield _Snap(d)


class _Col(_Query):
    def __init__(self):
        super().__init__(self, [])
        self._docs = {}

    def document(self, key):
        return _Doc(self, key)


class FakeFirestoreClient:
    def __init__(self):
        self._cols = {}

    def collection(self, name):
        return self._cols.setdefault(name, _Col())
