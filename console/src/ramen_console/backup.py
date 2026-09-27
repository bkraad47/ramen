import json
import os
from pathlib import Path

from .errors import invalid, not_found
from .services import Services
from .storage.encrypted import SENSITIVE
from .util import now, uid

COLLECTIONS = ("groups", "zones", "environments", "workers", "users", "config")


def release_version() -> str:
    for p in (Path(__file__).resolve().parents[3] / "VERSION", Path("/app/VERSION")):
        if p.exists():
            return p.read_text().strip()
    from . import __version__
    return __version__


class Backups:
    def __init__(self, svc: Services):
        self.svc, self.store = svc, svc.store

    def _root(self, target: str) -> Path:
        if target == "local":
            return Path(os.environ.get("RAMEN_BACKUP_ROOT", "./backups"))
        if target == "bucket":
            return Path(os.environ.get("RAMEN_BUCKET_ROOT", "./buckets")) / "_backups"
        raise invalid("target must be local or bucket")

    async def snapshot(self) -> dict:
        data = {"release_version": release_version(), "created": now()}
        for col in COLLECTIONS:
            hidden = SENSITIVE.get(col, set())
            data[col] = [{k: v for k, v in d.items() if k not in hidden} for d in await self.store.list(col)]
        return data

    async def create(self, target: str, by: str, path: str | None = None) -> dict:
        root = Path(path) if path else self._root(target)
        data = await self.snapshot()
        bid = f"{data['created'].replace(':', '').replace('-', '')}-{uid()[:6]}"
        root.mkdir(parents=True, exist_ok=True)
        file = root / f"ramen-backup-{bid}.json"
        file.write_text(json.dumps(data, indent=1))
        doc = {"created": data["created"], "release_version": data["release_version"], "target": target,
               "path": str(file), "by": by, "counts": {c: len(data[c]) for c in COLLECTIONS}}
        return await self.store.put("backups", bid, doc)

    async def list(self) -> list[dict]:
        return sorted(await self.store.list("backups"), key=lambda b: b["created"], reverse=True)

    async def read(self, bid: str) -> dict:
        b = await self.store.get("backups", bid)
        if not b or not Path(b["path"]).exists():
            raise not_found("backup")
        return json.loads(Path(b["path"]).read_text())

    async def restore(self, bid: str) -> dict:
        data = await self.read(bid)
        counts = {}
        for col in COLLECTIONS:
            for d in data.get(col, []):
                existing = await self.store.get(col, d["id"]) or {}
                await self.store.put(col, d["id"], {**existing, **d})
            counts[col] = len(data.get(col, []))
        return {"restored": counts, "release_version": data.get("release_version")}
