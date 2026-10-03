import hmac

from .auth import apikeys
from .auth.passwords import hash_password, verify_password
from .errors import conflict, forbidden, invalid, not_found
from .rbac import RANK, ROLES, Principal
from .security import check_password
from .storage.base import Store
from .util import now, public, uid


class Accounts:
    def __init__(self, store: Store):
        self.store = store

    # --- session epoch (V1.4) ------------------------------------------------
    @staticmethod
    def epoch_of(user: dict) -> int:
        return int(user.get("session_epoch") or 0)

    async def bump_epoch(self, user: dict) -> dict:
        """Invalidate every session this user already holds. Callers pass the doc they are about to write."""
        user["session_epoch"] = self.epoch_of(user) + 1
        return user

    async def bump_all_epochs(self) -> int:
        """Used when the auth configuration changes: nobody keeps a session minted under the old rules."""
        users = await self.store.list("users")
        for u in users:
            await self.store.put("users", u["id"], await self.bump_epoch(u))
        return len(users)

    async def principal_for_session(self, session: dict | None) -> Principal | None:
        if not session:
            return None
        u = await self.store.get("users", session.get("uid"))
        if not u or self.epoch_of(u) != int(session.get("ep") or 0):
            return None
        return Principal(u["id"], u["email"], u["role"], list(u.get("groups", [])))

    async def authenticate(self, email, password) -> dict | None:
        users = await self.store.list("users", {"email": email})
        if not users or users[0].get("login_disabled"):  # §13.1: restored without a hash, until a reset
            return None
        return users[0] if verify_password(password, users[0].get("password_hash", "")) else None

    async def principal_for_user(self, user_id) -> Principal | None:
        u = await self.store.get("users", user_id)
        return Principal(u["id"], u["email"], u["role"], list(u.get("groups", []))) if u else None

    async def principal_for_key(self, raw) -> Principal | None:
        parsed = apikeys.parse(raw or "")
        if not parsed:
            return None
        kid, secret, prefix = parsed
        k = await self.store.get("api_keys", kid)
        if not k or not apikeys.verify(secret, k["secret_hash"]):
            return None
        if k.get("prefix") and k["prefix"] != prefix:  # a key may not be replayed under the other client type
            return None
        return Principal(
            k["id"],
            k["name"],
            k["role"],
            list(k["groups"]),
            kind="apikey",
            client_type=apikeys.client_type_of(k, prefix),
        )

    async def _check_scope(self, p: Principal, role, groups):
        if role not in ROLES:
            raise invalid(f"Role must be one of {', '.join(ROLES)}")
        known = {g["id"] for g in await self.store.list("groups")}
        if any(g not in known for g in groups):
            raise invalid("Unknown group")
        if p.role != "super_admin" and (RANK[role] >= RANK[p.role] or any(g not in p.groups for g in groups)):
            raise forbidden("Cannot grant beyond your own role and groups")

    async def list_users(self, p: Principal) -> list[dict]:
        users = await self.store.list("users")
        if p.role != "super_admin":
            users = [u for u in users if set(u.get("groups", [])) & set(p.groups)]
        return [public(u) for u in sorted(users, key=lambda u: u["created"])]

    async def create_user(self, p: Principal, email, password, role="viewer", groups=(), provider="password") -> dict:
        if await self.store.list("users", {"email": email}):
            raise conflict(f"User {email} exists")
        await self._check_scope(p, role, groups)
        if password:
            check_password(password)
        doc = {
            "email": email,
            "role": role,
            "groups": list(groups),
            "created": now(),
            "provider": provider,
            "password_hash": hash_password(password) if password else "",
        }
        return public(await self.store.put("users", uid(), doc))

    async def upsert_sso_user(self, email, role="viewer", groups=(), provider="oauth", authoritative=False) -> dict:
        """Link by verified email or create with the mapped role/groups. With `authoritative` (D38: the provider
        has a role mapping) the mapped role/groups replace the stored ones on every login."""
        found = await self.store.list("users", {"email": email})
        if found:
            u = found[0]
            if not authoritative or (u["role"], list(u.get("groups") or [])) == (role, list(groups)):
                return u
            u.update({"role": role, "groups": list(groups)})
            return await self.store.put("users", u["id"], u)
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
            check_password(password)
            u["password_hash"] = hash_password(password)
            await self.bump_epoch(u)
        return await self.store.put("users", uid_, u)

    async def update_user(self, uid_, role=None, groups=None) -> dict:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        changed = False
        if role and role != u.get("role"):
            if role not in ROLES:
                raise invalid("Unknown role")
            u["role"], changed = role, True
        if groups is not None and list(groups) != list(u.get("groups", [])):
            u["groups"], changed = list(groups), True
        if changed:  # V1.4: a role or group change revokes the sessions minted under the old scope
            await self.bump_epoch(u)
        return public(await self.store.put("users", uid_, u))

    async def delete_user(self, p: Principal, uid_) -> None:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        if p.role != "super_admin" and (u["role"] != "viewer" or any(g not in p.groups for g in u.get("groups", []))):
            raise forbidden("Group admins may only delete viewers in their groups")
        if u["id"] == p.id:
            raise conflict("Cannot delete yourself")
        await self.store.put("users", uid_, await self.bump_epoch(u))  # V1.4: kill live sessions before the doc goes
        await self.store.delete("users", uid_)

    async def set_password(self, uid_, password) -> None:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        if (provider := u.get("provider") or "password") != "password":
            raise invalid(f"{u['email']} signs in through {provider} and has no password to reset")
        check_password(password)
        u["password_hash"] = hash_password(password)
        u.pop("login_disabled", None)  # §13.1: a reset is how a restored account gets back in
        await self.store.put("users", uid_, await self.bump_epoch(u))

    async def list_keys(self, p: Principal) -> list[dict]:
        keys = await self.store.list("api_keys")
        if p.role != "super_admin":
            keys = [k for k in keys if k.get("owner") == p.id]
        return [
            {**public(k), "client_type": apikeys.client_type_of(k)} for k in sorted(keys, key=lambda k: k["created"])
        ]

    async def mint_key(self, p: Principal, name, role=None, groups=None, client_type="devops") -> dict:
        role, groups = role or p.role, list(p.groups if groups is None else groups)
        if client_type not in apikeys.CLIENT_TYPES:
            raise invalid(f"Client type must be one of {', '.join(apikeys.CLIENT_TYPES)}")
        if client_type == "agent" and not groups:
            raise invalid("An agent key must name at least one group")
        if p.role != "super_admin" and (RANK[role] > RANK[p.role] or any(g not in p.groups for g in groups)):
            raise forbidden("API key scope cannot exceed your own")
        if role not in ROLES:
            raise invalid("Unknown role")
        raw, kid, h = apikeys.mint(client_type)
        doc = {
            "name": name,
            "role": role,
            "groups": groups,
            "client_type": client_type,
            "prefix": apikeys.prefix_for(client_type),
            "secret_hash": h,
            "owner": p.id,
            "created": now(),
        }
        return {**public(await self.store.put("api_keys", kid, doc)), "key": raw}

    async def get_key(self, kid) -> dict:
        k = await self.store.get("api_keys", kid)
        if not k:
            raise not_found("API key")
        return k

    async def delete_key(self, p: Principal, kid) -> None:
        k = await self.get_key(kid)
        if p.role != "super_admin" and k.get("owner") != p.id:
            raise forbidden("Not your key")
        await self.store.delete("api_keys", kid)

    async def request_permission(self, p: Principal, role=None, group=None, zone=None, permission=None, scope=None):
        """Role request (viewer → admin) or SA permission request (group+zone+permission[+scope], CONTRACTS §9)."""
        if permission:
            doc = {
                "kind": "permission_request",
                "type": "permission",
                "user": p.id,
                "email": p.name,
                "group": group,
                "zone": zone,
                "permission": permission,
                "scope": list(scope or ["*"]),
                "status": "pending",
                "created": now(),
            }
        else:
            if role not in ROLES:
                raise invalid("Unknown role")
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
            raise conflict("Request already handled")
        if r.get("type") == "permission":
            r["applied"] = (
                await apply(r["group"], r["zone"], r["permission"], r.get("scope") or ["*"]) if apply else None
            )
            r.update(status="approved", approved_by=by, approved=now())
            return await self.store.put("activity", rid, r)
        u = await self.store.get("users", r["user"])
        if u:
            # §13.2: what the approval changed is recorded, so a later revoke can put it back exactly.
            r["prior_role"] = u["role"]
            if RANK[r["role"]] > RANK[u["role"]]:
                u["role"] = r["role"]
            if r.get("group") and r["group"] not in u["groups"]:
                u["groups"].append(r["group"])
                r["granted_group"] = r["group"]
            await self.store.put("users", u["id"], await self.bump_epoch(u))
        r.update(status="approved", approved_by=by, approved=now())
        return await self.store.put("activity", rid, r)

    # --- the other half of the flow: deny a request, revoke a grant (§13.2, V5.2) --------------------------------
    async def _request(self, rid, expected) -> dict:
        r = await self.store.get("activity", rid)
        if not r or r.get("kind") != "permission_request":
            raise not_found("request")
        if r.get("status") != expected:
            raise conflict(f"Request is {r.get('status')}, not {expected}")
        return r

    async def deny_request(self, rid, by) -> dict:
        r = await self._request(rid, "pending")
        r.update(status="denied", denied_by=by, denied=now())
        return await self.store.put("activity", rid, r)

    async def revoke_request(self, rid, by, revoke=None) -> dict:
        """An approved request taken back. Permission requests go to the cloud adapter; role requests put the user's
        role and group back to what the approval recorded and end their sessions at once (U25)."""
        r = await self._request(rid, "approved")
        if r.get("type") == "permission":
            r["revoked_result"] = await revoke(r["group"], r["zone"], r["permission"]) if revoke else None
        else:
            u = await self.store.get("users", r["user"])
            if u:
                prior = r.get("prior_role")
                if prior in RANK and RANK[prior] < RANK[u["role"]]:
                    u["role"] = prior
                if r.get("granted_group") in u.get("groups", []):
                    u["groups"] = [g for g in u["groups"] if g != r["granted_group"]]
                await self.store.put("users", u["id"], await self.bump_epoch(u))
        r.update(status="revoked", revoked_by=by, revoked=now())
        return await self.store.put("activity", rid, r)
