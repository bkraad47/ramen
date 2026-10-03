from .cloud.base import Cloud
from .cloud.gcp_k8s import normalize_size
from .errors import conflict, forbidden, invalid, not_found
from .keyid import key_id
from .policy import permissions as perm
from .rbac import Principal, RuleClash, check_clash
from .secrets.base import SecretsBackend, StoreBackend
from .security import generate_key_secret
from .storage.base import Store
from .util import KEYNAME_RE, NAME_RE, SECRET_RE, is_cidr, now, parse_image, public, uid


def clean_blocked(names) -> list[str]:
    out = []
    for n in names:
        n = str(n).strip()
        if "," in n or "\n" in n:
            raise invalid("Blocked names must not contain commas")
        if n and n not in out:
            out.append(n)
    return out


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

    async def create_group(
        self, name, repo_url="", ref="main", by="", github_token=None, github_app_installation_id=None
    ) -> dict:
        if not NAME_RE.match(name or ""):
            raise invalid("Group name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("groups", name):
            raise conflict(f"Group {name} exists")
        doc = {
            "name": name,
            "repo_url": repo_url,
            "ref": ref,
            "created": now(),
            "created_by": by,
            "mcp_auth": {"mode": "bearer"},
            "sa_restrictions": [],
        }
        if github_token:  # private GitHub or GitLab repos (I2); encrypted at rest, see storage/encrypted.py
            doc["github_token"] = github_token
        if github_app_installation_id:  # N5: no stored token at all, a fresh installation token per deploy
            doc["github_app_installation_id"] = github_app_installation_id
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
            raise conflict(str(e)) from e
        return await self.update_group(name, sa_restrictions=rules)

    async def set_sa_rules(self, rules: list[dict]) -> dict:
        try:
            check_clash(rules, [])
        except RuleClash as e:
            raise invalid(str(e)) from e
        return await self.store.put("config", "sa_rules", {"rules": rules})

    async def check_permission_request(self, p: Principal, group, zone, permission) -> None:
        """422 unknown, 403 not an admin of the group, 409 denied by super-admin or group rules (CONTRACTS §9)."""
        if not perm.known(permission or ""):
            raise invalid(f"Unknown permission {permission!r}; see /api/v1/policy/permissions")
        if not group or not zone:
            raise invalid("Permission requests need a group and a zone")
        g = await self.get_group(group)
        if not await self.store.get("zones", zone):
            raise not_found("zone")
        if p.role != "super_admin" and (p.role != "group_admin" or group not in p.groups):
            raise forbidden(f"Requires Group Admin on group {group}")
        rules = (await self.store.get("config", "sa_rules") or {}).get("rules", [])
        why = perm.evaluate(permission, rules, g.get("sa_restrictions", []))
        if why:
            raise conflict(f"Permission {permission!r} denied: {why}")

    async def apply_sa_permissions(self, group, zone, permission, scope=("*",)) -> dict:
        """Approved request → union with what the zone SA already has → Cloud.apply_sa_permissions. The scope (the
        resource names the request was for) travels with it; a new request for the same permission replaces it."""
        w = await self.worker_config(group, zone)
        perms, previous = list(w.get("sa_permissions", [])), dict(w.get("sa_scopes") or {})
        if permission not in perms:
            perms.append(permission)
        scopes = {**previous, permission: list(scope or ["*"])}
        result = await self.cloud.apply_sa_permissions(group, zone, perms, scopes, previous)
        w["sa_permissions"], w["sa_scopes"] = perms, scopes
        if result.get("service_account"):
            w["service_account"] = result["service_account"]
        await self.store.put("workers", w["id"], w)
        return result

    async def session_secret(self, group) -> str:
        """§16.2: one secret per group, generated on first use, handed to every zone's deploy Secret so any pod
        verifies any session id and any console-issued token. Encrypted at rest, never returned by the API."""
        import os
        import secrets

        if local := os.environ.get("RAMEN_LOCAL_SESSION_SECRET"):
            # the local stack: the compose file gives the one worker its secret directly, and the console the same
            # value, because the bucket file the local adapter writes is readable by the runtime (review 0.5.0 L6)
            return local
        g = await self.get_group(group)
        if not g.get("session_secret"):
            g["session_secret"] = secrets.token_urlsafe(32)
            await self.store.put("groups", group, g)
        return g["session_secret"]

    async def revoke_sa_permission(self, group, zone, permission) -> dict:
        """§13.2: drop one granted permission and re-apply the remaining set, so the cloud roles shrink with it."""
        w = await self.worker_config(group, zone)
        perms = list(w.get("sa_permissions", []))
        if permission not in perms:
            raise not_found(f"permission {permission} in {group}/{zone}")
        perms = [q for q in perms if q != permission]
        previous = dict(w.get("sa_scopes") or {})
        scopes = {k: v for k, v in previous.items() if k != permission}
        result = await self.cloud.apply_sa_permissions(group, zone, perms, scopes, previous)
        w["sa_permissions"], w["sa_scopes"] = perms, scopes
        await self.store.put("workers", w["id"], w)
        await self._mark_revoked(group, zone, permission)
        return {"group": group, "zone": zone, "revoked": permission, "permissions": perms, "cloud": result}

    async def _mark_revoked(self, group, zone, permission) -> int:
        """A permission can be revoked directly, so the approval record has to follow the grant, not the other way."""
        rows = [
            q
            for q in await self.store.list("activity", {"kind": "permission_request"})
            if q.get("status") == "approved"
            and (q.get("group"), q.get("zone"), q.get("permission")) == (group, zone, permission)
        ]
        for q in rows:
            q.update(status="revoked", revoked=now())
            await self.store.put("activity", q["id"], q)
        return len(rows)

    async def zones(self) -> list[dict]:
        return sorted(await self.store.list("zones"), key=lambda z: z["name"])

    async def create_zone(self, name, provider="local", region="") -> dict:
        if not NAME_RE.match(name or ""):
            raise invalid("Zone name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("zones", name):
            raise conflict(f"Zone {name} exists")
        return await self.store.put(
            "zones", name, {"name": name, "provider": provider, "region": region, "created": now()}
        )

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
            raise invalid(f"Unknown zones: {', '.join(bad)}")

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
            raise invalid("Environment name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("environments", f"{group}:{name}"):
            raise conflict(f"Environment {name} exists in {group}")
        await self._check_zones(zones)
        doc = {
            "group": group,
            "name": name,
            "ref": ref or g.get("ref", "main"),
            "zones": list(zones),
            "verbose": False,
            "blocked": [],
            "blocked_zones": {},
            "created": now(),
            "last_deploy": None,
        }
        await self._attach_zones(group, zones)
        return await self.store.put("environments", f"{group}:{name}", doc)

    async def _attach_zones(self, group, zones, already=()):
        for z in zones:
            if z not in already:
                await self.cloud.attach_zone(group, z, await self.zone_spec(group, z))

    async def zone_spec(self, group, zone) -> dict:
        z = await self.store.get("zones", zone) or {}
        w = await self.worker_config(group, zone)
        return {
            "region": z.get("region", ""),
            "size": normalize_size(w.get("size")) or "s",
            "count": w.get("count", 1),
            "allowed_sizes": w.get("allowed_sizes", []),
            "service_account": w.get("service_account"),
            "image": await self.image_for(group),  # §13.3: None means the release image the adapter was given
        }

    # --- per-group worker images (§13.3, F9.3) --------------------------------
    async def image_for(self, group) -> str | None:
        return ((await self.store.get("groups", group) or {}).get("image") or {}).get("ref")

    async def images(self, group) -> list[dict]:
        g = await self.get_group(group)
        pinned = (g.get("image") or {}).get("id")
        # `created` has second resolution, so two builds recorded in the same second need `seq` to stay ordered.
        rows = sorted(
            await self.store.list("images", {"group": group}),
            key=lambda d: (d["created"], d.get("seq", 0)),
            reverse=True,
        )
        return [{**d, "current": d["id"] == pinned} for d in rows]

    async def record_image(self, group, tag, digest=None, note="", by="") -> dict:
        """Record a build that already exists in a registry and pin it. The console never builds (§13.3)."""
        await self.get_group(group)
        tag, digest, ref = parse_image(tag, digest)
        doc = await self.store.put(
            "images",
            uid(),
            {
                "group": group,
                "seq": len(await self.store.list("images", {"group": group})) + 1,
                "tag": tag,
                "digest": digest,
                "ref": ref,
                "note": (note or "").strip(),
                "created": now(),
                "created_by": by,
            },
        )
        return {**await self._pin(group, doc), "current": True}

    async def recall_image(self, group, image_id) -> dict:
        doc = await self.store.get("images", image_id)
        if not doc or doc.get("group") != group:
            raise not_found("image")
        return {**await self._pin(group, doc), "current": True}

    async def unpin_image(self, group) -> dict:
        """Back to the release image the adapter was configured with; the history is kept."""
        g = await self.get_group(group)
        g.pop("image", None)
        await self.store.put("groups", group, g)
        return {"ok": True, "image": None}

    async def _pin(self, group, doc) -> dict:
        g = await self.get_group(group)
        g["image"] = {"id": doc["id"], "ref": doc["ref"]}
        await self.store.put("groups", group, g)
        return doc

    async def update_env(self, group, name, **fields) -> dict:
        e = await self.get_env(group, name)
        if fields.get("blocked") is not None:
            fields["blocked"] = clean_blocked(fields["blocked"])
        if fields.get("zones") is not None:
            await self._check_zones(fields["zones"])
            await self._attach_zones(group, fields["zones"], already=e.get("zones", []))
        e.update({k: v for k, v in fields.items() if v is not None})
        return await self.store.put("environments", e["id"], e)

    async def set_zone_blocked(self, group, env, zone, names) -> dict:
        """U5: `environments[].blocked_zones[zone]` adds to the environment-wide `blocked` list (§9)."""
        e = await self.get_env(group, env)
        if zone not in e.get("zones", []):
            raise invalid(f"Zone {zone} is not attached to {env}")
        by_zone = dict(e.get("blocked_zones") or {})
        cleaned = clean_blocked(names)
        if cleaned:
            by_zone[zone] = cleaned
        else:
            by_zone.pop(zone, None)
        e["blocked_zones"] = by_zone
        return await self.store.put("environments", e["id"], e)

    @staticmethod
    def blocked_for_zone(env: dict, zone: str) -> list[str]:
        """What the next deploy writes into that zone's `RAMEN_BLOCKED`: the environment list plus the zone's."""
        out = list(env.get("blocked") or [])
        for n in (env.get("blocked_zones") or {}).get(zone, []):
            if n not in out:
                out.append(n)
        return out

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
            "id": f"{group}:{zone}",
            "group": group,
            "zone": zone,
            "count": 1,
            "size": "s",
            "allowed_sizes": [],
        }

    async def set_workers(self, group, zone, p: Principal, count=None, size=None, allowed_sizes=None) -> dict:
        w = await self.worker_config(group, zone)
        if allowed_sizes is not None:
            if p.role != "super_admin":
                raise forbidden("Only super admins set allowed sizes")
            bad = [s for s in allowed_sizes if not normalize_size(s)]
            if bad:
                raise invalid(f"Unknown sizes: {', '.join(bad)} (use s|m|l)")
            w["allowed_sizes"] = [normalize_size(s) for s in allowed_sizes]
        if size is not None:
            if p.role != "super_admin" and normalize_size(size) not in w.get("allowed_sizes", []):
                raise forbidden("Only super admins change worker sizes, or sizes outside the allowed list")
            if not normalize_size(size):
                raise invalid("Size must be one of s|m|l")
            w["size"] = size
        if count is not None:
            if count < 1:
                raise invalid("Count must be 1 or more")
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
                "id": f"{z['group']}:{z['zone']}",
                "group": z["group"],
                "zone": z["zone"],
                "count": 1,
                "size": "s",
                "allowed_sizes": [],
            }
            w["live_state"] = {
                k: z.get(k) for k in ("namespace", "replicas", "ready", "canary_replicas", "canary_ready")
            }
            if z.get("service_account"):
                w["service_account"] = z["service_account"]
            await self.store.put("workers", w["id"], w)
        return r

    async def set_ip_rules(self, group, zone, cidrs: list[str]) -> dict:
        bad = [c for c in cidrs if not is_cidr(c)]
        if bad:
            raise invalid(f"Invalid CIDRs: {', '.join(bad)}")
        w = await self.worker_config(group, zone)
        w["cidrs"] = cidrs
        await self.store.put("workers", w["id"], w)
        return await self.cloud.set_ip_rules(group, zone, cidrs)

    async def set_item_throttle(self, group, zone, redis_url=None, ip_per_min=None, token_per_min=None) -> dict:
        """N7: zone-local Redis throttling repeat calls to the same tool/resource/prompt."""
        w = await self.worker_config(group, zone)
        if redis_url is not None:
            w["redis_item_url"] = redis_url
        if ip_per_min is not None:
            w["item_ip_per_min"] = ip_per_min
        if token_per_min is not None:
            w["item_token_per_min"] = token_per_min
        return public(await self.store.put("workers", w["id"], w))

    async def set_scope_throttle(self, group, redis_url=None, ip_per_min=None, token_per_min=None) -> dict:
        """N7: one Redis shared by every zone of the group, throttling repeat calls to the group/environment."""
        g = await self.get_group(group)
        if redis_url is not None:
            g["redis_scope_url"] = redis_url
        if ip_per_min is not None:
            g["scope_ip_per_min"] = ip_per_min
        if token_per_min is not None:
            g["scope_token_per_min"] = token_per_min
        return public(await self.store.put("groups", group, g))

    async def secrets(self, group, env=None, zone=None, kind="secret") -> list[dict]:
        f = {"group": group, "kind": kind}
        if env:
            f["env"] = env
        if zone:
            f["zone"] = zone
        return sorted((public(s) for s in await self.store.list("secrets", f)), key=lambda s: s["name"])

    async def add_secret(self, group, name, value, env=None, zone=None, by="", kind="secret", extra=None) -> dict:
        await self.get_group(group)
        if kind == "secret" and not SECRET_RE.match(name or ""):
            raise invalid("Secret name must match ^[A-Z][A-Z0-9_]{0,63}$")
        if kind != "secret" and not KEYNAME_RE.match(name or ""):
            raise invalid("Key name must match ^[A-Za-z0-9_-]{1,64}$")
        env, zone = env or None, zone or None
        for s in await self.store.list("secrets", {"group": group, "name": name, "kind": kind}):
            if s.get("env") == env and s.get("zone") == zone:
                raise conflict(f"Secret {name} exists for that scope")
        stored = await self.secrets_backend.put(group, env, zone, name, value, kind)
        doc = {
            "group": group,
            "name": name,
            "env": env,
            "zone": zone,
            "kind": kind,
            "created": now(),
            "created_by": by,
            "backend": self.secrets_backend.kind,
            **(extra or {}),
            **stored,
        }
        if kind == "mcp_key":  # so the Logs page can name the consumer behind a worker log line (U4)
            doc["key_id"] = key_id(value)
        return public(await self.store.put("secrets", uid(), doc))

    async def delete_secret(self, group, sid, kind="secret") -> None:
        s = await self.store.get("secrets", sid)
        if not s or s["group"] != group or s.get("kind") != kind:
            raise not_found("secret")
        await self.secrets_backend.delete(s)
        await self.store.delete("secrets", sid)

    async def mint_mcp_key(self, group, name, by="") -> dict:
        """A group MCP key is the group page's agent key (D21): `rmk_`, workers only, never the console API."""
        key = "rmk_" + generate_key_secret(32)
        doc = await self.add_secret(group, name, key, by=by, kind="mcp_key", extra={"client_type": "agent"})
        return {**doc, "key": key}

    # --- agent API keys reaching workers (U9 / D21) ---------------------------
    async def mirror_agent_key(self, groups, name, raw, kid, by="") -> list[dict]:
        """An `agent` key has to reach the workers of every group it names, so it is recorded as that group's MCP
        key — the same record the group page's key form writes — and deploy unions it into `RAMEN_MCP_KEYS`."""
        safe = "".join(c if KEYNAME_RE.match(c) else "-" for c in (name or "agent"))[:48]
        return [
            await self.add_secret(g, f"{safe}-{kid[:6]}", raw, by=by, kind="mcp_key", extra={"api_key": kid})
            for g in groups
        ]

    async def revoke_agent_key(self, kid) -> int:
        """Revoking the API key must also stop it reaching workers on the next deploy."""
        docs = await self.store.list("secrets", {"kind": "mcp_key", "api_key": kid})
        for s in docs:
            await self.secrets_backend.delete(s)
            await self.store.delete("secrets", s["id"])
        return len(docs)

    async def mcp_key_names(self, group) -> dict[str, str]:
        """`key_id` -> key name, for resolving the consumer on the Logs page (U4)."""
        out = {}
        for s in await self.store.list("secrets", {"group": group, "kind": "mcp_key"}):
            kid = s.get("key_id") or (key_id(s["value"]) if s.get("value") else None)
            if kid:
                out[kid] = s["name"]
        return out

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
