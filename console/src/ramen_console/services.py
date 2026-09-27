import secrets as pysecrets

from .cloud.base import Cloud
from .errors import conflict, forbidden, invalid, not_found
from .rbac import Principal, RuleClash, check_clash
from .storage.base import Store
from .util import CIDR_RE, KEYNAME_RE, NAME_RE, SECRET_RE, now, public, uid


class Services:
    def __init__(self, store: Store, cloud: Cloud):
        self.store, self.cloud = store, cloud

    async def visible_groups(self, p: Principal) -> list[dict]:
        groups = await self.store.list("groups")
        if p.role != "super_admin":
            groups = [g for g in groups if g["id"] in p.groups]
        return sorted((public(g) for g in groups), key=lambda g: g["name"])

    async def get_group(self, name: str) -> dict:
        g = await self.store.get("groups", name)
        if not g:
            raise not_found("group")
        return g

    async def create_group(self, name, repo_url="", ref="main", by="") -> dict:
        if not NAME_RE.match(name or ""):
            raise invalid("group name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("groups", name):
            raise conflict(f"group {name} exists")
        doc = {"name": name, "repo_url": repo_url, "ref": ref, "created": now(), "created_by": by,
               "mcp_auth": {"mode": "bearer"}, "sa_restrictions": []}
        return public(await self.store.put("groups", name, doc))

    async def update_group(self, name, **fields) -> dict:
        g = await self.get_group(name)
        g.update({k: v for k, v in fields.items() if v is not None})
        return public(await self.store.put("groups", name, g))

    async def delete_group(self, name) -> None:
        await self.get_group(name)
        for col in ("environments", "secrets", "workers"):
            for d in await self.store.list(col, {"group": name}):
                await self.store.delete(col, d["id"])
        await self.store.delete("groups", name)

    async def set_sa_restrictions(self, name, rules: list[dict]) -> dict:
        cfg = await self.store.get("config", "sa_rules") or {"rules": []}
        try:
            check_clash(cfg["rules"], rules)
        except RuleClash as e:
            raise conflict(str(e))
        return await self.update_group(name, sa_restrictions=rules)

    async def set_sa_rules(self, rules: list[dict]) -> dict:
        try:
            check_clash(rules, [])
        except RuleClash as e:
            raise invalid(str(e))
        return await self.store.put("config", "sa_rules", {"rules": rules})

    async def zones(self) -> list[dict]:
        return sorted(await self.store.list("zones"), key=lambda z: z["name"])

    async def create_zone(self, name, provider="local", region="") -> dict:
        if not NAME_RE.match(name or ""):
            raise invalid("zone name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("zones", name):
            raise conflict(f"zone {name} exists")
        return await self.store.put("zones", name, {"name": name, "provider": provider, "region": region, "created": now()})

    async def delete_zone(self, name) -> None:
        if not await self.store.get("zones", name):
            raise not_found("zone")
        for e in await self.store.list("environments"):
            if name in e.get("zones", []):
                e["zones"] = [z for z in e["zones"] if z != name]
                await self.store.put("environments", e["id"], e)
        await self.store.delete("zones", name)

    async def _check_zones(self, zones):
        known = {z["id"] for z in await self.store.list("zones")}
        bad = [z for z in zones if z not in known]
        if bad:
            raise invalid(f"unknown zones: {', '.join(bad)}")

    async def environments(self, group=None) -> list[dict]:
        envs = await self.store.list("environments", {"group": group} if group else None)
        return sorted(envs, key=lambda e: (e["group"], e["name"]))

    async def get_env(self, group, name) -> dict:
        e = await self.store.get("environments", f"{group}:{name}")
        if not e:
            raise not_found("environment")
        return e

    async def create_env(self, group, name, ref=None, zones=()) -> dict:
        g = await self.get_group(group)
        if not NAME_RE.match(name or ""):
            raise invalid("environment name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("environments", f"{group}:{name}"):
            raise conflict(f"environment {name} exists in {group}")
        await self._check_zones(zones)
        doc = {"group": group, "name": name, "ref": ref or g.get("ref", "main"), "zones": list(zones),
               "verbose": False, "created": now(), "last_deploy": None}
        return await self.store.put("environments", f"{group}:{name}", doc)

    async def update_env(self, group, name, **fields) -> dict:
        e = await self.get_env(group, name)
        if fields.get("zones") is not None:
            await self._check_zones(fields["zones"])
        e.update({k: v for k, v in fields.items() if v is not None})
        return await self.store.put("environments", e["id"], e)

    async def delete_env(self, group, name) -> None:
        e = await self.get_env(group, name)
        await self.store.delete("environments", e["id"])

    async def worker_config(self, group, zone) -> dict:
        if not await self.store.get("zones", zone):
            raise not_found("zone")
        return await self.store.get("workers", f"{group}:{zone}") or {
            "id": f"{group}:{zone}", "group": group, "zone": zone, "count": 1, "size": "small"}

    async def set_workers(self, group, zone, p: Principal, count=None, size=None) -> dict:
        w = await self.worker_config(group, zone)
        if size is not None:
            if p.role != "super_admin":
                raise forbidden("only super admins change worker sizes")
            w["size"] = size
        if count is not None:
            if count < 1:
                raise invalid("count must be >= 1")
            w["count"] = count
        return await self.store.put("workers", w["id"], w)

    async def set_ip_rules(self, group, zone, cidrs: list[str]) -> dict:
        bad = [c for c in cidrs if not CIDR_RE.match(c)]
        if bad:
            raise invalid(f"invalid CIDRs: {', '.join(bad)}")
        w = await self.worker_config(group, zone)
        w["cidrs"] = cidrs
        await self.store.put("workers", w["id"], w)
        return await self.cloud.set_ip_rules(group, zone, cidrs)

    async def secrets(self, group, env=None, zone=None, kind="secret") -> list[dict]:
        f = {"group": group, "kind": kind}
        if env:
            f["env"] = env
        if zone:
            f["zone"] = zone
        return sorted((public(s) for s in await self.store.list("secrets", f)), key=lambda s: s["name"])

    async def add_secret(self, group, name, value, env=None, zone=None, by="", kind="secret") -> dict:
        await self.get_group(group)
        if kind == "secret" and not SECRET_RE.match(name or ""):
            raise invalid("secret name must match ^[A-Z][A-Z0-9_]{0,63}$")
        if kind != "secret" and not KEYNAME_RE.match(name or ""):
            raise invalid("key name must match ^[A-Za-z0-9_-]{1,64}$")
        env, zone = env or None, zone or None
        for s in await self.store.list("secrets", {"group": group, "name": name, "kind": kind}):
            if s.get("env") == env and s.get("zone") == zone:
                raise conflict(f"secret {name} exists for that scope")
        doc = {"group": group, "name": name, "value": value, "env": env, "zone": zone, "kind": kind,
               "created": now(), "created_by": by}
        return public(await self.store.put("secrets", uid(), doc))

    async def delete_secret(self, group, sid, kind="secret") -> None:
        s = await self.store.get("secrets", sid)
        if not s or s["group"] != group or s.get("kind") != kind:
            raise not_found("secret")
        await self.store.delete("secrets", sid)

    async def mint_mcp_key(self, group, name, by="") -> dict:
        key = "rmk_" + pysecrets.token_urlsafe(24)
        doc = await self.add_secret(group, name, key, by=by, kind="mcp_key")
        return {**doc, "key": key}

    async def secrets_for(self, group, env, zone) -> tuple[dict[str, str], str | None, list[str]]:
        vars_, token, mcp = {}, None, []
        for s in await self.store.list("secrets", {"group": group}):
            if s.get("env") not in (None, env) or s.get("zone") not in (None, zone):
                continue
            if s.get("kind") == "mcp_key":
                mcp.append(s["value"])
            else:
                vars_[f"RAMEN_SECRET_{group.upper().replace('-', '_')}__{s['name']}"] = s["value"]
                if s["name"] == "GITHUB_TOKEN":
                    token = s["value"]
        return vars_, token, mcp
