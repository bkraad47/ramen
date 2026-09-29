import json
import os
from pathlib import Path

from .errors import conflict, invalid, not_found
from .services import Services
from .storage.encrypted import SENSITIVE
from .util import now, uid

COLLECTIONS = ("groups", "zones", "environments", "workers", "users", "config")


def newer(a: str, b: str) -> bool:
    """True when release `a` is newer than `b` (CONTRACTS §13.1 version gate); unparsable parts sort as 0."""
    part = lambda v: tuple(int(x) if x.isdigit() else 0 for x in str(v).split(".")[:3])  # noqa: E731
    return part(a) > part(b)


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
        raise invalid("Target must be local or bucket")

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
        doc = {
            "created": data["created"],
            "release_version": data["release_version"],
            "target": target,
            "path": str(file),
            "by": by,
            "counts": {c: len(data[c]) for c in COLLECTIONS},
        }
        return await self.store.put("backups", bid, doc)

    async def list(self) -> list[dict]:
        return sorted(await self.store.list("backups"), key=lambda b: b["created"], reverse=True)

    async def read(self, bid: str) -> dict:
        b = await self.store.get("backups", bid)
        if not b or not Path(b["path"]).exists():
            raise not_found("backup")
        return json.loads(Path(b["path"]).read_text())

    async def restore(
        self,
        bid: str,
        by: str = "",
        actor_id: str | None = None,
        dry_run: bool = False,
        prune: bool = False,
        reconcile: bool = False,
        force: bool = False,
    ) -> dict:
        """Restore a backup over the live store (CONTRACTS §13.1). `dry_run` only reports the plan.

        Docs are merged field-wise, so what the snapshot strips (hashes, secret values, tokens) survives."""
        data = await self.read(bid)
        version = data.get("release_version") or "0.0.0"
        if newer(version, release_version()) and not force:
            raise conflict(
                f"Backup was taken on release {version}, this console runs {release_version()}: "
                "restore it on that release, or repeat with force"
            )
        plan, extra = {}, {}
        for col in COLLECTIONS:
            docs = [d for d in data.get(col, []) if isinstance(d, dict) and d.get("id")]
            have = {d["id"]: d for d in await self.store.list(col)}
            created = [d["id"] for d in docs if d["id"] not in have]
            updated = [d["id"] for d in docs if d["id"] in have and {**have[d["id"]], **d} != have[d["id"]]]
            plan[col] = {"created": created, "updated": updated, "unchanged": len(docs) - len(created) - len(updated)}
            extra[col] = sorted(k for k in have if k not in {d["id"] for d in docs})
        out = {
            "release_version": version,
            "dry_run": dry_run,
            "restored": {c: {k: (len(v) if isinstance(v, list) else v) for k, v in p.items()} for c, p in plan.items()},
            "extra": extra,
            "pruned": {},
            "reconciled": [],
            "orphans": [],
            "warnings": [],
        }
        if dry_run:
            return out
        out["warnings"] += await self._write(data, plan)
        if prune:
            out["pruned"], pruned_warnings = await self._prune(extra, by, actor_id)
            out["warnings"] += pruned_warnings
        if reconcile:
            out["reconciled"], out["orphans"] = await self._reconcile(data)
        return out

    async def _write(self, data: dict, plan: dict) -> list[str]:
        """Merge every backed-up doc over what the store holds; new users come back unable to log in."""
        warnings = []
        for col in COLLECTIONS:
            new_ids = set(plan[col]["created"])
            for d in data.get(col, []):
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                existing = await self.store.get(col, d["id"]) or {}
                doc = {**existing, **d}
                if col == "users":
                    if d["id"] in new_ids and not existing.get("password_hash"):
                        doc["login_disabled"] = True
                        warnings.append(
                            f"User {d.get('email')} was restored without a password (backups carry no hashes): "
                            "log in is disabled until a password reset or an SSO sign-in"
                        )
                    doc["session_epoch"] = int(existing.get("session_epoch") or 0) + 1  # §13.1: old sessions end here
                await self.store.put(col, d["id"], doc)
        return warnings

    async def _prune(self, extra: dict, by: str, actor_id: str | None) -> tuple[dict, list[str]]:
        """Delete what the backup does not have. `config` is never pruned and the caller is never locked out."""
        pruned, warnings = {}, []
        for col in ("groups", "zones", "environments", "workers", "users"):
            gone = []
            for key in extra.get(col, []):
                if col == "users":
                    u = await self.store.get("users", key) or {}
                    if key == actor_id or (by and u.get("email") == by):
                        warnings.append(
                            f"Kept {u.get('email') or key}: pruning the account running the restore "
                            "would lock this console out"
                        )
                        continue
                await self.store.delete(col, key)
                gone.append(key)
            if gone:
                pruned[col] = gone
        return pruned, warnings

    async def _reconcile(self, data: dict) -> tuple[list[str], list[str]]:
        """Re-apply the restored zones so the cloud matches the state that just came back (§13.1)."""
        done, pairs = [], set()
        for w in data.get("workers", []):
            group, zone = w.get("group"), w.get("zone")
            if not group or not zone or not await self.store.get("zones", zone):
                continue
            pairs.add((group, zone))
            try:
                await self.svc.cloud.attach_zone(group, zone, await self.svc.zone_spec(group, zone))
                done.append(f"{group}/{zone}")
            except Exception as e:  # noqa: BLE001 - one unreachable zone must not abandon the rest
                done.append(f"{group}/{zone}: {type(e).__name__}: {str(e)[:120]}")
        orphans = []
        try:  # what the cloud still runs that the backup never knew about; reported, never deleted
            live = await self.svc.cloud.refresh()
            orphans = [
                f"{z['group']}/{z['zone']}"
                for z in live.get("zones", [])
                if z.get("group") and z.get("zone") and (z["group"], z["zone"]) not in pairs
            ]
        except Exception:  # noqa: BLE001 - a survey that fails leaves the restore intact
            orphans = []
        return done, sorted(orphans)
