import os
import uuid

from ..storage.base import Store
from ..util import now
from .passwords import hash_password


async def ensure_super_admin(store: Store) -> dict | None:
    email, pw = os.environ.get("RAMEN_ADMIN_EMAIL"), os.environ.get("RAMEN_ADMIN_PASSWORD")
    if not email or not pw:
        return None
    existing = await store.list("users", {"email": email})
    user = existing[0] if existing else {"id": uuid.uuid4().hex, "email": email, "created": now(), "groups": []}
    user.update(role="super_admin", password_hash=hash_password(pw), provider="password")
    return await store.put("users", user["id"], user)
