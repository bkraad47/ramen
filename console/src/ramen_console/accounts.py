import hmac

from .auth import apikeys
from .auth.passwords import hash_password, verify_password
from .errors import conflict, forbidden, invalid, not_found
from .rbac import RANK, ROLES, Principal
from .storage.base import Store
from .util import now, public, uid


class Accounts:
    def __init__(self, store: Store):
        self.store = store

    async def authenticate(self, email, password) -> dict | None:
        users = await self.store.list("users", {"email": email})
        if users and verify_password(password, users[0].get("password_hash", "")):
            return users[0]
        return None

    async def principal_for_user(self, user_id) -> Principal | None:
        u = await self.store.get("users", user_id)
        return Principal(u["id"], u["email"], u["role"], list(u.get("groups", []))) if u else None

    async def principal_for_key(self, raw) -> Principal | None:
        parsed = apikeys.parse(raw or "")
        if not parsed:
            return None
        k = await self.store.get("api_keys", parsed[0])
        if not k or not apikeys.verify(parsed[1], k["secret_hash"]):
            return None
        return Principal(k["id"], k["name"], k["role"], list(k["groups"]), kind="apikey")

    async def _check_scope(self, p: Principal, role, groups):
        if role not in ROLES:
            raise invalid(f"role must be one of {', '.join(ROLES)}")
        known = {g["id"] for g in await self.store.list("groups")}
        if any(g not in known for g in groups):
            raise invalid("unknown group")
        if p.role != "super_admin" and (RANK[role] >= RANK[p.role] or any(g not in p.groups for g in groups)):
            raise forbidden("cannot grant beyond your own role and groups")

    async def list_users(self, p: Principal) -> list[dict]:
        users = await self.store.list("users")
        if p.role != "super_admin":
            users = [u for u in users if set(u.get("groups", [])) & set(p.groups)]
        return [public(u) for u in sorted(users, key=lambda u: u["created"])]

    async def create_user(self, p: Principal, email, password, role="viewer", groups=(), provider="password") -> dict:
        if await self.store.list("users", {"email": email}):
            raise conflict(f"user {email} exists")
        await self._check_scope(p, role, groups)
        doc = {
            "email": email,
            "role": role,
            "groups": list(groups),
            "created": now(),
            "provider": provider,
            "password_hash": hash_password(password) if password else "",
        }
        return public(await self.store.put("users", uid(), doc))

    async def upsert_sso_user(self, email, role="viewer", groups=(), provider="oauth") -> dict:
        """Link by verified email (existing role kept) or create with the mapped role/groups."""
        found = await self.store.list("users", {"email": email})
        if found:
            return found[0]
        doc = {
            "email": email,
            "role": role,
            "groups": list(groups),
            "created": now(),
            "provider": provider,
            "password_hash": "",
        }
        return await self.store.put("users", uid(), doc)

    async def start_token(self, email, kind) -> tuple[dict, str] | None:
        """Issue a single-use nonce for a reset/magic token; None when the email is unknown (never revealed)."""
        found = await self.store.list("users", {"email": email})
        if not found:
            return None
        u, nonce = found[0], uid()
        u[f"{kind}_nonce"] = nonce
        return await self.store.put("users", u["id"], u), nonce

    async def redeem_token(self, uid_, kind, nonce, password=None) -> dict | None:
        u = await self.store.get("users", uid_)
        if not u or not nonce or not hmac.compare_digest(str(u.get(f"{kind}_nonce") or ""), str(nonce)):
            return None
        u.pop(f"{kind}_nonce", None)
        if password is not None:
            u["password_hash"] = hash_password(password)
        return await self.store.put("users", uid_, u)

    async def update_user(self, uid_, role=None, groups=None) -> dict:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        if role:
            if role not in ROLES:
                raise invalid("bad role")
            u["role"] = role
        if groups is not None:
            u["groups"] = list(groups)
        return public(await self.store.put("users", uid_, u))

    async def delete_user(self, p: Principal, uid_) -> None:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        if p.role != "super_admin" and (u["role"] != "viewer" or any(g not in p.groups for g in u.get("groups", []))):
            raise forbidden("group admins may only delete viewers in their groups")
        if u["id"] == p.id:
            raise conflict("cannot delete yourself")
        await self.store.delete("users", uid_)

    async def set_password(self, uid_, password) -> None:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        u["password_hash"] = hash_password(password)
        await self.store.put("users", uid_, u)

    async def list_keys(self, p: Principal) -> list[dict]:
        keys = await self.store.list("api_keys")
        if p.role != "super_admin":
            keys = [k for k in keys if k.get("owner") == p.id]
        return [public(k) for k in sorted(keys, key=lambda k: k["created"])]

    async def mint_key(self, p: Principal, name, role=None, groups=None) -> dict:
        role, groups = role or p.role, list(p.groups if groups is None else groups)
        if p.role != "super_admin" and (RANK[role] > RANK[p.role] or any(g not in p.groups for g in groups)):
            raise forbidden("API key scope cannot exceed your own")
        if role not in ROLES:
            raise invalid("bad role")
        raw, kid, h = apikeys.mint()
        doc = {"name": name, "role": role, "groups": groups, "secret_hash": h, "owner": p.id, "created": now()}
        return {**public(await self.store.put("api_keys", kid, doc)), "key": raw}

    async def delete_key(self, p: Principal, kid) -> None:
        k = await self.store.get("api_keys", kid)
        if not k:
            raise not_found("api key")
        if p.role != "super_admin" and k.get("owner") != p.id:
            raise forbidden("not your key")
        await self.store.delete("api_keys", kid)

    async def request_permission(self, p: Principal, role=None, group=None, zone=None, permission=None) -> dict:
        """Role request (viewer → admin) or SA permission request (group+zone+permission, CONTRACTS §9)."""
        if permission:
            doc = {
                "kind": "permission_request",
                "type": "permission",
                "user": p.id,
                "email": p.name,
                "group": group,
                "zone": zone,
                "permission": permission,
                "status": "pending",
                "created": now(),
            }
        else:
            if role not in ROLES:
                raise invalid("bad role")
            doc = {
                "kind": "permission_request",
                "type": "role",
                "user": p.id,
                "email": p.name,
                "role": role,
                "group": group,
                "status": "pending",
                "created": now(),
            }
        return await self.store.put("activity", uid(), doc)

    async def list_requests(self) -> list[dict]:
        return await self.store.list("activity", {"kind": "permission_request"})

    async def approve_request(self, rid, by, apply=None) -> dict:
        r = await self.store.get("activity", rid)
        if not r or r.get("kind") != "permission_request":
            raise not_found("request")
        if r["status"] != "pending":
            raise conflict("request already handled")
        if r.get("type") == "permission":
            r["applied"] = await apply(r["group"], r["zone"], r["permission"]) if apply else None
            r.update(status="approved", approved_by=by, approved=now())
            return await self.store.put("activity", rid, r)
        u = await self.store.get("users", r["user"])
        if u:
            if RANK[r["role"]] > RANK[u["role"]]:
                u["role"] = r["role"]
            if r.get("group") and r["group"] not in u["groups"]:
                u["groups"].append(r["group"])
            await self.store.put("users", u["id"], u)
        r.update(status="approved", approved_by=by, approved=now())
        return await self.store.put("activity", rid, r)
