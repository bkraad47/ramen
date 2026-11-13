"""§24.1: what only a zone's own pods call. `/api/v1/internal/*` is routed by no edge; a request that came through
one carries `X-Forwarded-For` and is answered 404 as if the path did not exist."""

import hmac
import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from .. import sleeping
from ..audit import note
from ..errors import not_found
from . import models as m
from .helpers import svc

log = logging.getLogger("ramen.sleep")
r = APIRouter(prefix="/api/v1/internal")


def worker_admin_key(request: Request) -> str:
    return getattr(request.app.state.cloud, "admin_key", "") or os.environ.get("RAMEN_ADMIN_KEY", "")


async def _finish(s, group, zone):
    try:
        await s.wake_finish(group, zone, "call")
    except Exception as e:  # noqa: BLE001 - already audited; the zone is asleep again and the next call retries
        log.warning("wake of %s/%s failed: %s: %s", group, zone, type(e).__name__, e)


@r.post("/zones/{group}/{zone}/wake")
async def wake(request: Request, group: str, zone: str, bg: BackgroundTasks, body: m.WakeIn | None = None):
    """The activator's call when an authenticated MCP call reaches a sleeping zone. Answers at once with the zone's
    state: the first call of a wake starts the scale in the background (`started: true`), every other call — while
    the zone is `waking`, or once it is `awake` — changes nothing."""
    want = worker_admin_key(request)
    if "x-forwarded-for" in request.headers or not want:
        raise HTTPException(404, "Not Found")
    got = request.headers.get("x-ramen-admin-key") or ""
    if not hmac.compare_digest(got.encode(), want.encode()):
        raise HTTPException(401, "admin key required")
    s = svc(request)
    if not await s.store.get("groups", group):
        raise not_found("group")
    w, started = await s.wake_begin(group, zone)
    if started:
        tags = [f"group:{group}", f"zone:{zone}", "trigger:call"]
        note(request, "workers.wake", f"{group}/{zone}", tags, user="activator")
        bg.add_task(_finish, s, group, zone)
    return {"group": group, "zone": zone, "state": sleeping.state_of(w), "started": started}
