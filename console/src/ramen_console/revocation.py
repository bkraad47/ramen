"""D92 (CONTRACTS §16.7): revocation that takes effect at once.

Workers check `rmk_` keys and OAuth access tokens offline, so the console tells them what is revoked: a deleted key's
SHA-256, a token id (`jti`) and a person's cut-off (`sub` → unix seconds: tokens issued at or before it are refused).
Entries live in the `revocations` collection, scoped to a group, kept only as long as something they cover can still
be presented (a token entry until the longest token it covers expires, ≤ `RAMEN_OAUTH_ACCESS_TTL`; a key entry a day,
long after every zone's Secret lost the key), and reach the group's workers as `RAMEN_REVOKED` in every deploy and in
the scoped config push (§26.3), whose `Admin/Reload` carries the list so a running pod applies it before its restart.
A zone the push cannot reach is recorded under `config/revocation_pending` and retried in the background until it
lands; a sleeping zone takes the rewritten Secret when it wakes.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time

from .util import now, uid

log = logging.getLogger("ramen.revocation")
COLL = "revocations"
PENDING = ("config", "revocation_pending")
ENV = "RAMEN_REVOKED"
KEY_KEEP = 24 * 3600


def key_hash(raw: str) -> str:
    """What the node compares a configured key with (`revoke::key_hash`): lowercase hex SHA-256."""
    return hashlib.sha256((raw or "").encode()).hexdigest()


def access_ttl() -> int:
    from .oauth_server import access_ttl as ttl

    return ttl()


async def _put(store, kind: str, group: str, value, exp: int, reason: str, before: int | None = None) -> None:
    doc = {"kind": kind, "group": group, "value": value, "exp": int(exp), "reason": reason, "created": now()}
    if before is not None:
        doc["before"] = int(before)
    await store.put(COLL, uid(), doc)


async def deny_key(store, group: str, raw: str, reason: str) -> None:
    await _put(store, "key", group, key_hash(raw), time.time() + KEY_KEEP, reason)


async def deny_jti(store, group: str, jti: str, exp: int, reason: str) -> bool:
    """One token id, kept until the token itself expires; nothing to do for an expired one."""
    if not jti or not group or int(exp or 0) <= time.time():
        return False
    await _put(store, "jti", group, jti, exp, reason)
    return True


async def deny_records(store, records: list[dict], reason: str) -> set[str]:
    """Every live access token minted with these refresh records (each record names the `jti` it came with)."""
    groups = set()
    for r in records:
        if await deny_jti(store, r.get("group") or "", r.get("jti") or "", r.get("access_exp") or 0, reason):
            groups.add(r["group"])
    return groups


async def subject_groups(store, user_id: str) -> set[str]:
    """Where a person can hold a live access token: every token comes with a refresh record naming its group."""
    t = time.time()
    return {
        r["group"]
        for r in await store.list("oauth_refresh", {"user": user_id})
        if r.get("group") and (r.get("access_exp") or r.get("exp") or 0) > t
    }


async def deny_subject(store, user_id: str, reason: str) -> set[str]:
    """Every token this person holds now, in every group they hold one for; tokens minted later pass."""
    if not user_id:
        return set()
    groups = await subject_groups(store, user_id)
    t = int(time.time())
    for g in sorted(groups):
        await _put(store, "sub", g, user_id, t + access_ttl(), reason, before=t)
    return groups


async def entries(store, group: str) -> dict:
    """The group's live list in the node's shape; expired entries are deleted on the way."""
    t = time.time()
    out: dict = {"keys": set(), "jti": set(), "sub": {}}
    for d in await store.list(COLL, {"group": group}):
        if int(d.get("exp") or 0) <= t:
            with contextlib.suppress(Exception):
                await store.delete(COLL, d["id"])
            continue
        if d["kind"] == "key":
            out["keys"].add(d["value"])
        elif d["kind"] == "jti":
            out["jti"].add(d["value"])
        elif d["kind"] == "sub":
            out["sub"][d["value"]] = max(out["sub"].get(d["value"], 0), int(d.get("before") or 0))
    return {"keys": sorted(out["keys"]), "jti": sorted(out["jti"]), "sub": dict(sorted(out["sub"].items()))}


async def worker_env(store, group: str) -> dict[str, str]:
    """`RAMEN_REVOKED` for every zone of the group — always present, so a push always carries the current list."""
    return {ENV: json.dumps(await entries(store, group), separators=(",", ":"))}


async def pending(store) -> dict:
    return (await store.get(*PENDING) or {}).get("groups") or {}


class Pusher:
    """The scoped push (§26.3) for revocations: one at a time per group, and a request that arrives while a push runs
    is answered by the next one (a burst of sign-outs rolls the workers once more, not once each)."""

    def __init__(self, svc):
        self.svc = svc
        self._locks: dict[str, asyncio.Lock] = {}
        self._asked: dict[str, int] = {}
        self._done: dict[str, int] = {}
        self._last: dict[str, tuple[dict, dict]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def push(self, group: str) -> tuple[dict, dict]:
        """`(pushed, warnings)` as `deploy.push_config` answers them; failed zones are recorded as pending."""
        from . import deploy

        self._asked[group] = n = self._asked.get(group, 0) + 1
        async with self._locks.setdefault(group, asyncio.Lock()):
            if self._done.get(group, 0) >= n:
                return self._last[group]
            start, warnings = self._asked[group], {}
            try:
                pushed = await deploy.push_config(self.svc, warnings, group)
            except Exception as e:  # noqa: BLE001 - the group stays pending, retried below
                pushed = {f"{group}/*": f"{type(e).__name__}: {e}"}
            self._done[group], self._last[group] = start, (pushed, warnings)
            await self._record(group, pushed)
        return pushed, warnings

    async def _record(self, group: str, pushed: dict) -> None:
        store = self.svc.store
        failed = {k.split("/", 1)[1]: v for k, v in pushed.items() if v != "ok"}
        async with store.transaction() as tx:
            doc = await tx.get(*PENDING) or {"groups": {}}
            groups = doc.get("groups") or {}
            if failed:
                prev = groups.get(group) or {}
                tries = prev.get("tries", 0) + 1
                groups[group] = {"since": prev.get("since") or now(), "zones": failed, "tries": tries}
                log.warning("revocation push %s: not on every worker yet: %s", group, failed)
            else:
                groups.pop(group, None)
            await tx.put(*PENDING, {**doc, "groups": groups})

    def schedule(self, groups) -> None:
        """Push in the background (a person's revocation never waits on the workers); failures stay pending."""
        for g in sorted(set(groups or ())):
            t = asyncio.get_running_loop().create_task(self._quiet(g))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _quiet(self, group: str) -> None:
        try:
            await self.push(group)
        except Exception:  # noqa: BLE001 - logged; the retry loop takes it
            log.exception("revocation push %s failed", group)

    async def drain(self) -> None:
        """Until every scheduled push ended (tests; the tasks may run on another client's loop)."""
        while any(not t.done() for t in list(self._tasks)):
            await asyncio.sleep(0.01)

    async def retry_pending(self) -> dict:
        out = {}
        for g in sorted(await pending(self.svc.store)):
            out[g] = (await self.push(g))[0]
        return out

    async def retry_forever(self, every: float | None = None) -> None:
        every = every or float(os.environ.get("RAMEN_REVOKE_RETRY_SECS", "30"))
        while True:
            await asyncio.sleep(every)
            try:
                await self.retry_pending()
            except Exception:  # noqa: BLE001 - next round
                log.exception("revocation retry failed")


def answer(pushed: dict, warnings: dict, group: str) -> dict:
    """The `pushed` part of a revoking answer (§26.3's shape) plus, for a zone the push could not reach, a `warning`
    that says the revocation is retried in the background (it is refused there once the push lands)."""
    from .deploy import pushed_answer

    out = pushed_answer(pushed, warnings, group)
    if failed := {k: v for k, v in out["pushed"].items() if v != "ok"}:
        w = out.setdefault("warning", {})
        for k, v in failed.items():
            w[k] = f"{v}; the revocation is not on this zone's workers yet and is retried in the background"
        out["pending"] = sorted(failed)
    return out
