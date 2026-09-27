import secrets as pysecrets

from .cloud.base import Cloud
from .cloud.gcp_k8s import normalize_size
from .errors import conflict, forbidden, invalid, not_found
from .rbac import Principal, RuleClash, check_clash
from .secrets.base import SecretsBackend, StoreBackend
from .storage.base import Store
from .util import KEYNAME_RE, NAME_RE, SECRET_RE, is_cidr, now, public, uid


class Services:
    def __init__(self, store: Store, cloud: Cloud, secrets: SecretsBackend | None = None):
        self.store, self.cloud = store, cloud
        self.secrets_backend = secrets or StoreBackend()

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
        await self.cloud.detach_group(name)  # destroys the group's infra first (F4.1); errors abort the delete
        for d in await self.store.list("secrets", {"group": name}):
            await self.secrets_backend.delete(d)  # removes Secret Manager secrets too, never just the store doc
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
        await self._attach_zones(group, zones)
        return await self.store.put("environments", f"{group}:{name}", doc)

    async def _attach_zones(self, group, zones, already=()):
        for z in zones:
            if z not in already:
                await self.cloud.attach_zone(group, z, await self.zone_spec(group, z))

    async def zone_spec(self, group, zone) -> dict:
        z = await self.store.get("zones", zone) or {}
        w = await self.worker_config(group, zone)
        return {"region": z.get("region", ""), "size": normalize_size(w.get("size")) or "s", "count": w.get("count", 1),
                "allowed_sizes": w.get("allowed_sizes", []), "service_account": w.get("service_account")}

    async def update_env(self, group, name, **fields) -> dict:
        e = await self.get_env(group, name)
        if fields.get("zones") is not None:
            await self._check_zones(fields["zones"])
            await self._attach_zones(group, fields["zones"], already=e.get("zones", []))
        e.update({k: v for k, v in fields.items() if v is not None})
        return await self.store.put("environments", e["id"], e)

    async def delete_env(self, group, name) -> None:
        e = await self.get_env(group, name)
        await self.store.delete("environments", e["id"])

    async def rebalance(self, group, zone):
        if not await self.store.get("groups", group):
            raise not_found("group")
        if not await self.store.get("zones", zone):
            raise not_found("zone")
        return await self.cloud.rebalance(group, zone)

    async def worker_config(self, group, zone) -> dict:
        if not await self.store.get("zones", zone):
            raise not_found("zone")
        return await self.store.get("workers", f"{group}:{zone}") or {
            "id": f"{group}:{zone}", "group": group, "zone": zone, "count": 1, "size": "s", "allowed_sizes": []}

    async def set_workers(self, group, zone, p: Principal, count=None, size=None, allowed_sizes=None) -> dict:
        w = await self.worker_config(group, zone)
        if allowed_sizes is not None:
            if p.role != "super_admin":
                raise forbidden("only super admins set allowed sizes")
            bad = [s for s in allowed_sizes if not normalize_size(s)]
            if bad:
                raise invalid(f"unknown sizes: {', '.join(bad)} (use s|m|l)")
            w["allowed_sizes"] = [normalize_size(s) for s in allowed_sizes]
        if size is not None:
            if p.role != "super_admin" and normalize_size(size) not in w.get("allowed_sizes", []):
                raise forbidden("only super admins change worker sizes (or sizes outside the allowed list)")
            if not normalize_size(size):
                raise invalid("size must be one of s|m|l")
            w["size"] = size
        if count is not None:
            if count < 1:
                raise invalid("count must be >= 1")
            w["count"] = count
        doc = await self.store.put("workers", w["id"], w)
        doc["cloud"] = await self.cloud.scale(group, zone, await self.zone_spec(group, zone))
        return doc

    async def refresh(self) -> dict:
        r = await self.cloud.refresh()
        for z in r.get("zones", []):
            if not (z.get("group") and z.get("zone")):
                continue
            w = await self.store.get("workers", f"{z['group']}:{z['zone']}") or {
                "id": f"{z['group']}:{z['zone']}", "group": z["group"], "zone": z["zone"], "count": 1, "size": "s", "allowed_sizes": []}
            w["live_state"] = {k: z.get(k) for k in ("namespace", "replicas", "ready", "canary_replicas", "canary_ready")}
            if z.get("service_account"):
                w["service_account"] = z["service_account"]
            await self.store.put("workers", w["id"], w)
        return r

    async def set_ip_rules(self, group, zone, cidrs: list[str]) -> dict:
        bad = [c for c in cidrs if not is_cidr(c)]
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
        stored = await self.secrets_backend.put(group, env, zone, name, value, kind)
        doc = {"group": group, "name": name, "env": env, "zone": zone, "kind": kind, "created": now(), "created_by": by,
               "backend": self.secrets_backend.kind, **stored}
        return public(await self.store.put("secrets", uid(), doc))

    async def delete_secret(self, group, sid, kind="secret") -> None:
        s = await self.store.get("secrets", sid)
        if not s or s["group"] != group or s.get("kind") != kind:
            raise not_found("secret")
        await self.secrets_backend.delete(s)
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
            val = s.get("ref") or s.get("value")
            if s.get("kind") == "mcp_key":
                mcp.append(val)
            else:
                vars_[f"RAMEN_SECRET_{group.upper().replace('-', '_')}__{s['name']}"] = val
                if s["name"] == "GITHUB_TOKEN":
                    token = val
        return vars_, token, mcp
