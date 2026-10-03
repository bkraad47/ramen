"""N2: WARNING+ log records queue here; a lifespan-hosted loop digests them to the selected users by email."""

import asyncio
import contextlib
import logging

from .mail import Mailer
from .services import Services

log = logging.getLogger("ramen.alerts")

MAX_PENDING = 200
FLUSH_INTERVAL_SECONDS = 30
PENDING: list[dict] = []


class MailAlertHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("ramen.alerts"):
            return  # a flush failure must not feed back into the thing it is trying to report on
        PENDING.append({"level": record.levelname, "logger": record.name, "message": record.getMessage()})
        del PENDING[:-MAX_PENDING]


HANDLER = MailAlertHandler(level=logging.WARNING)


async def get_notify_config(store) -> dict:
    return {"user_ids": list((await store.get("config", "notify") or {}).get("user_ids") or [])}


async def set_notify_config(store, user_ids: list[str]) -> dict:
    doc = {"user_ids": list(user_ids)}
    await store.put("config", "notify", doc)
    return doc


async def flush_alerts(svc: Services, mailer: Mailer) -> list[str]:
    """Drain PENDING into one digest email per selected recipient. Left undrained when there is nowhere to
    send it, so a log storm before SMTP/recipients are configured is capped (MAX_PENDING), not lost."""
    if not PENDING or not mailer.enabled:
        return []
    ids = (await get_notify_config(svc.store))["user_ids"]
    if not ids:
        return []
    batch, PENDING[:] = list(PENDING), []
    body = "\n".join(f"[{a['level']}] {a['logger']}: {a['message']}" for a in batch)
    sent = []
    for uid in ids:
        user = await svc.store.get("users", uid)
        if not user:
            continue
        await mailer.send(user["email"], f"Ramen console: {len(batch)} server warning(s)", body)
        sent.append(user["email"])
    return sent


async def run_forever(svc: Services, state) -> None:
    """Thin loop: re-reads `state.mailer` every pass so a super admin's SMTP save takes effect without restart."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        with contextlib.suppress(Exception):
            await flush_alerts(svc, state.mailer)
