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


class _FakeAsyncpgConn:
    """Matches PostgresStore's exact, fixed SQL strings (storage/postgres.py) against an in-memory
    dict — a real asyncpg driver isn't needed to exercise PostgresStore's own logic, same as
    FakeFirestoreClient above stands in for google-cloud-firestore."""

    def __init__(self, table):
        self._t = table

    async def execute(self, sql, *args):
        if sql.startswith("CREATE TABLE"):
            return
        if sql.startswith("INSERT INTO"):
            collection, key, doc = args
            self._t[(collection, key)] = doc
        elif sql.startswith("DELETE FROM"):
            collection, key = args
            self._t.pop((collection, key), None)

    async def fetchrow(self, sql, *args):
        collection, key = args
        doc = self._t.get((collection, key))
        return {"doc": doc} if doc is not None else None

    async def fetch(self, sql, *args):
        (collection,) = args
        return [{"doc": doc} for (c, _k), doc in self._t.items() if c == collection]


class _FakeAsyncpgAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakeAsyncpgPool:
    def __init__(self):
        self._table: dict[tuple[str, str], str] = {}
        self._conn = _FakeAsyncpgConn(self._table)

    def acquire(self):
        return _FakeAsyncpgAcquire(self._conn)
