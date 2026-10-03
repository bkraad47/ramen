import hmac

from .auth import apikeys
from .auth.passwords import hash_password, verify_password
from .errors import conflict, forbidden, invalid, not_found
from .rbac import GROUP_ROLES, RANK, ROLES, Principal, can, memberships_of, principal_from, summarize
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
        return principal_from(u)

    async def authenticate(self, email, password) -> dict | None:
        users = await self.store.list("users", {"email": email})
        if not users or users[0].get("login_disabled"):  # §13.1: restored without a hash, until a reset
            return None
        return users[0] if verify_password(password, users[0].get("password_hash", "")) else None

    async def principal_for_user(self, user_id) -> Principal | None:
        u = await self.store.get("users", user_id)
        return principal_from(u) if u else None

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

    @staticmethod
    def out(u: dict) -> dict:
        return {**public(u), "memberships": memberships_of(u)}

    async def _check_memberships(self, p: Principal, memberships: dict, super_: bool) -> None:
        """D41: a super admin grants anything; a group admin grants viewer / mcp_user in groups they administer."""
        known = {g["id"] for g in await self.store.list("groups")}
        for g, r in memberships.items():
            if g not in known:
                raise invalid(f"Unknown group {g!r}")
            if r not in GROUP_ROLES:
                raise invalid(f"Role must be one of {', '.join(GROUP_ROLES)} (or super_admin for the whole console)")
        if p.role == "super_admin":
            return
        if super_:
            raise forbidden("Only a super admin can make a super admin")
        for g, r in memberships.items():
            if not can(p, "group_admin", g) or r == "group_admin":
                raise forbidden(f"You may grant viewer or mcp_user in groups you administer, not {r} in {g}")

    def _visible_to(self, p: Principal) -> set[str]:
        return {g for g, r in p.memberships.items() if RANK[r] >= RANK["viewer"]}

    async def can_manage(self, p: Principal, target: dict) -> bool:
        """May `p` act on this user's account (password, delete)? Super admins always, except on each other's
        passwords; group admins on members (never super admins) of a group they administer."""
        if p.role == "super_admin":
            return True
        if target.get("role") == "super_admin":
            return False
        return any(can(p, "group_admin", g) for g in memberships_of(target))

    async def migrate_memberships(self) -> int:
        """One-time (0.5.95): documents written before D41 get their `memberships` from `role` + `groups`."""
        n = 0
        for u in await self.store.list("users"):
            if "memberships" not in u:
                await self.store.put("users", u["id"], summarize(u))
                n += 1
        return n

    async def list_users(self, p: Principal) -> list[dict]:
        users = await self.store.list("users")
        if p.role != "super_admin":
            mine = self._visible_to(p)
            users = [u for u in users if set(memberships_of(u)) & mine]
        return [self.out(u) for u in sorted(users, key=lambda u: u["created"])]

    async def list_members(self, group: str) -> list[dict]:
        out = []
        for u in await self.store.list("users"):
            if (r := memberships_of(u).get(group)) is not None:
                out.append({"id": u["id"], "email": u["email"], "role": r, "provider": u.get("provider", "password")})
        return sorted(out, key=lambda x: x["email"])

    async def create_user(
        self, p: Principal, email, password, role="viewer", groups=(), provider="password", memberships=None
    ) -> dict:
        if await self.store.list("users", {"email": email}):
            raise conflict(f"User {email} exists")
        if role not in ROLES:
            raise invalid(f"Role must be one of {', '.join(ROLES)}")
        super_ = role == "super_admin" and memberships is None
        m = dict(memberships) if memberships is not None else ({} if super_ else {g: role for g in groups})
        await self._check_memberships(p, m, super_)
        if password:
            check_password(password)
        doc = summarize(
            {
                "email": email,
                "role": "super_admin" if super_ else role,
                "memberships": m,
                "created": now(),
                "provider": provider,
                "password_hash": hash_password(password) if password else "",
            }
        )
        return self.out(await self.store.put("users", uid(), doc))

    async def set_membership(self, p: Principal, group: str, uid_: str, role: str | None) -> dict:
        """Grant (or, with role None, take away) one person's role in one group — the group admin's lever (R4)."""
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        if u.get("role") == "super_admin":
            raise invalid("A super admin has every group already")
        if role is not None:
            await self._check_memberships(p, {group: role}, False)
        elif not can(p, "group_admin", group):
            raise forbidden(f"Requires Group Admin on group {group}")
        m = memberships_of(u)
        if role is None:
            m.pop(group, None)
        else:
            m[group] = role
        u["memberships"] = m
        await self.store.put("users", uid_, await self.bump_epoch(summarize(u)))
        return self.out(u)

    async def upsert_sso_user(self, email, memberships=None, super_=False, provider="oauth", authoritative=False):
        """Link by verified email or create with the mapped memberships. With `authoritative` (D38: the provider
        has a role mapping) the mapped memberships replace the stored ones on every login."""
        m = dict(memberships or {})
        found = await self.store.list("users", {"email": email})
        if found:
            u = found[0]
            same = (u.get("role") == "super_admin") == super_ and memberships_of(u) == m
            if not authoritative or same:
                return u
            u["role"] = "super_admin" if super_ else "viewer"
            u["memberships"] = m
            return await self.store.put("users", u["id"], summarize(u))
        doc = summarize(
            {
                "email": email,
                "role": "super_admin" if super_ else "viewer",
                "memberships": m,
                "created": now(),
                "provider": provider,
                "password_hash": "",
            }
        )
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

    async def update_user(self, uid_, role=None, groups=None, memberships=None) -> dict:
        """Super-admin edit: `memberships` replaces the per-group roles; `role` + `groups` is the old shorthand
        (that role in every listed group, or super_admin for the whole console)."""
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        before = (u.get("role"), memberships_of(u))
        if memberships is not None:
            known = {g["id"] for g in await self.store.list("groups")}
            for g, r in memberships.items():
                if g not in known:
                    raise invalid(f"Unknown group {g!r}")
                if r not in GROUP_ROLES:
                    raise invalid(f"Role must be one of {', '.join(GROUP_ROLES)}")
            u["memberships"], u["role"] = dict(memberships), "viewer"
        elif role or groups is not None:
            if role and role not in ROLES:
                raise invalid("Unknown role")
            role = role or (u.get("role") if u.get("role") != "super_admin" else "viewer")
            if role == "super_admin":
                u["role"], u["memberships"] = "super_admin", {}
            else:
                gs = list(groups) if groups is not None else list(memberships_of(u))
                u["role"], u["memberships"] = role, {g: role for g in gs}
        summarize(u)
        if (u.get("role"), memberships_of(u)) != before:  # V1.4: a scope change revokes the old sessions
            await self.bump_epoch(u)
        return self.out(await self.store.put("users", uid_, u))

    async def delete_user(self, p: Principal, uid_) -> None:
        u = await self.store.get("users", uid_)
        if not u:
            raise not_found("user")
        if p.role != "super_admin" and (
            u.get("role") == "super_admin"
            or any(r == "group_admin" or not can(p, "group_admin", g) for g, r in memberships_of(u).items())
        ):
            raise forbidden("Group admins may only delete viewers and MCP users of their own groups")
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

    async def list_requests(self, p: Principal | None = None) -> list[dict]:
        rows = await self.store.list("activity", {"kind": "permission_request"})
        if p is not None and p.role != "super_admin":  # group admins see their groups' requests only
            rows = [r for r in rows if r.get("group") and can(p, "group_admin", r["group"])]
        return rows

    @staticmethod
    def _may_decide(p: Principal | None, r: dict) -> None:
        """R4: a group admin decides requests of their groups, never their own — another admin must."""
        if p is None or p.role == "super_admin":
            return
        if not (r.get("group") and can(p, "group_admin", r["group"])):
            raise forbidden(f"Requires Group Admin on group {r.get('group')}")
        if r.get("user") == p.id:
            raise conflict("You cannot approve or deny your own request; another admin of the group must")

    async def approve_request(self, rid, by, apply=None, p: Principal | None = None) -> dict:
        r = await self.store.get("activity", rid)
        if not r or r.get("kind") != "permission_request":
            raise not_found("request")
        self._may_decide(p, r)
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
            # §13.2: what the approval changed is recorded, so a later revoke can put it back exactly (D41: per group).
            m, grp = memberships_of(u), r.get("group")
            r["prior_role"] = u.get("role")
            if r["role"] == "super_admin":
                u["role"] = "super_admin"
            elif grp:
                r["prior_membership"] = m.get(grp)
                if m.get(grp) is None or RANK[r["role"]] > RANK[m[grp]]:
                    m[grp] = r["role"]
                u["memberships"] = m
            await self.store.put("users", u["id"], await self.bump_epoch(summarize(u)))
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

    async def deny_request(self, rid, by, p: Principal | None = None) -> dict:
        r = await self._request(rid, "pending")
        self._may_decide(p, r)
        r.update(status="denied", denied_by=by, denied=now())
        return await self.store.put("activity", rid, r)

    async def revoke_request(self, rid, by, revoke=None, p: Principal | None = None) -> dict:
        """An approved request taken back. Permission requests go to the cloud adapter; role requests put the user's
        role and group back to what the approval recorded and end their sessions at once (U25)."""
        r = await self._request(rid, "approved")
        self._may_decide(p, r)
        if r.get("type") == "permission":
            r["revoked_result"] = await revoke(r["group"], r["zone"], r["permission"]) if revoke else None
        else:
            u = await self.store.get("users", r["user"])
            if u:
                if r.get("role") == "super_admin" and u.get("role") == "super_admin":
                    u["role"] = r.get("prior_role") if r.get("prior_role") in GROUP_ROLES else "viewer"
                elif r.get("group"):
                    m = memberships_of(u)
                    if r.get("prior_membership") is None:
                        m.pop(r["group"], None)
                    else:
                        m[r["group"]] = r["prior_membership"]
                    u["memberships"] = m
                    if not m:  # back to the label they had before the grant
                        u["role"] = r.get("prior_role") if r.get("prior_role") in GROUP_ROLES else "viewer"
                await self.store.put("users", u["id"], await self.bump_epoch(summarize(u)))
        r.update(status="revoked", revoked_by=by, revoked=now())
        return await self.store.put("activity", rid, r)
