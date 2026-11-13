import asyncio
import logging
import os
import time

from . import autoscale, imagebuild, otel, regions, sleeping
from .audit import write_audit
from .cloud.base import Cloud
from .cloud.gcp_k8s import normalize_size
from .errors import conflict, forbidden, invalid, not_found
from .keyid import key_id
from .policy import permissions as perm
from .rbac import Principal, RuleClash, can, check_clash
from .secrets.base import SecretsBackend, StoreBackend
from .security import generate_key_secret
from .storage.base import Store
from .util import KEYNAME_RE, NAME_RE, SECRET_RE, is_cidr, now, parse_image, public, uid

REPO_SCHEMES = ("https", "http", "ssh", "git")


def validate_repo_url(url: str) -> str:
    """0.8.0 SC2: a group repo is a network git URL with a host. `ext::` (a remote helper that runs a command),
    scp-like `git@host:path`, a leading `-` (git reads it as an option) and whitespace are refused everywhere; a
    filesystem path or `file://` URL only on the local adapter (`RAMEN_CLOUD=local`: a laptop or the compose stack,
    where a checkout on disk is how tools are developed). Git itself is held to these protocols in
    `cloud.local.git_env`."""
    if not url:
        return url
    msg = "repo_url must be an https://, http://, ssh:// or git:// URL with a host"
    if url != url.strip() or any(c.isspace() for c in url) or url.startswith("-") or url.startswith("ext:"):
        raise invalid(msg)
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme in REPO_SCHEMES and u.hostname:
        return url
    local = os.environ.get("RAMEN_CLOUD", "local") == "local" and (url.startswith("/") or u.scheme == "file")
    if not local:
        raise invalid(msg + " (a path or file:// only on the local adapter)")
    return url


log = logging.getLogger("ramen.autoscale")


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
        # §24.3: golden cases must pass a group's telemetry-consent gate — the adapter's gRPC client presents the
        # worker admin key on `Mcp/Call` too (it already does on `Admin/*`)
        rpc = getattr(cloud, "rpc", None)
        if rpc is not None and hasattr(rpc, "admin_key") and not rpc.admin_key:
            rpc.admin_key = getattr(cloud, "admin_key", "") or None

    async def visible_groups(self, p: Principal) -> list[dict]:
        groups = await self.store.list("groups")
        if p.role != "super_admin":
            groups = [g for g in groups if can(p, "viewer", g["id"])]
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
            "repo_url": validate_repo_url(repo_url),
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
        if fields.get("repo_url") is not None:
            validate_repo_url(fields["repo_url"])
        if fields.get("build_image") and not await imagebuild.builder_available(self.cloud):
            raise invalid(imagebuild.missing_builder())  # §24.2
        if fields.get("require_telemetry_consent") and not (await otel.get_config(self.store))["enabled"]:
            raise invalid("Telemetry consent can only be required while telemetry is enabled (Config → Telemetry)")
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
        for z in await self.store.list("zones"):  # §26.1: the zone no longer lists the group
            if name in (z.get("groups") or []):
                z["groups"] = [g for g in z["groups"] if g != name]
                await self.store.put("zones", z["id"], z)
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

    async def blocked_regions(self) -> dict[str, list[str]]:
        """C7: `config/regions` → `{"gcp": [...], "aws": [...], "azure": [...]}`, every provider always present."""
        doc = (await self.store.get("config", regions.DOC) or {}).get("blocked") or {}
        return {p: list(doc.get(p) or []) for p in regions.PROVIDERS}

    async def region_block(self, provider, region) -> str | None:
        """The blocked region a zone's `region` falls in, or None."""
        return regions.blocked_region(provider, region or "", await self.blocked_regions())

    async def create_zone(self, name, provider="local", region="", groups=()) -> dict:
        if not NAME_RE.match(name or ""):
            raise invalid("Zone name must match ^[a-z][a-z0-9-]{0,39}$")
        if await self.store.get("zones", name):
            raise conflict(f"Zone {name} exists")
        if hit := await self.region_block(provider, region):
            raise forbidden(regions.message(hit))
        groups = list(dict.fromkeys(groups))
        await self._check_groups(groups)
        doc = {"name": name, "provider": provider, "region": region, "created": now(), "groups": groups}
        z = await self.store.put("zones", name, doc)
        for g in groups:  # §26.1: binding creates the worker record
            await self._bind(g, name)
        return z

    async def detach_zone(self, group, zone) -> dict:
        """Tear one group's deployment in one zone down for real (0.6.0): the cloud namespace, workers and zone
        identity, then the worker record. Errors from the cloud abort, so nothing is forgotten while it still runs."""
        result = await self.cloud.detach_zone(group, zone)
        if w := await self.store.get("workers", f"{group}:{zone}"):
            await self.store.delete("workers", w["id"])
        return result

    async def _zone_users(self, zone, except_env: str | None = None) -> set[str]:
        """Groups with an environment (other than `except_env`) still attached to `zone`."""
        return {
            e["group"]
            for e in await self.store.list("environments")
            if zone in e.get("zones", []) and e["id"] != except_env
        }

    async def delete_zone(self, name) -> None:
        if not await self.store.get("zones", name):
            raise not_found("zone")
        groups = await self._zone_users(name)
        groups |= {w["group"] for w in await self.store.list("workers", {"zone": name})}
        for g in sorted(groups):
            await self.detach_zone(g, name)
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

    # --- §26.1: zones bound to groups (D61) -------------------------------------------------------------------
    @staticmethod
    def default_worker(group, zone) -> dict:
        doc = {"id": f"{group}:{zone}", "group": group, "zone": zone, "count": 1, "max_count": 1}
        return {**doc, "size": "s", "allowed_sizes": []}

    async def group_zones(self, group) -> list[str]:
        """The group's zones: the zones listing it."""
        await self.get_group(group)
        return sorted(z["id"] for z in await self.store.list("zones") if group in (z.get("groups") or []))

    async def _check_groups(self, groups):
        known = {g["id"] for g in await self.store.list("groups")}
        if bad := [g for g in groups if g not in known]:
            raise invalid(f"Unknown groups: {', '.join(bad)}")

    async def _bind(self, group, zone) -> None:
        if not await self.store.get("workers", f"{group}:{zone}"):
            await self.store.put("workers", f"{group}:{zone}", self.default_worker(group, zone))

    async def set_zone_groups(self, zone, groups) -> dict:
        """Replace the groups a zone serves. Every group must exist (422); a group whose environment still uses the
        zone cannot go (409); a group that goes is detached (namespace, workers, identity, record)."""
        z = await self.store.get("zones", zone)
        if not z:
            raise not_found("zone")
        groups = list(dict.fromkeys(groups))
        await self._check_groups(groups)
        gone = [g for g in z.get("groups") or [] if g not in groups]
        in_use = [
            f"{e['name']} uses {zone}" for g in gone for e in await self.environments(g) if zone in e.get("zones", [])
        ]
        if in_use:
            raise conflict(f"Remove the zone from the environment first: {'; '.join(in_use)}")
        for g in gone:
            await self.detach_zone(g, zone)
        for g in groups:
            await self._bind(g, zone)
        z["groups"] = groups
        return await self.store.put("zones", zone, z)

    async def _check_bound(self, group, zones) -> None:
        """An environment may name only the group's zones — whoever asks (a super admin binds first)."""
        bound = await self.group_zones(group)
        if extra := [z for z in zones if z not in bound]:
            raise forbidden(
                f"Zone(s) {', '.join(extra)} are not bound to group {group}; "
                "a super admin binds a zone to a group on the Zones page"
            )

    async def migrate_zone_groups(self) -> int:
        """Zones from before 1.0.0a get the groups whose `allowed_zones` named them or whose environments use them,
        once; a zone with `groups` (even empty) is left alone. Every bound pair has a worker record; `allowed_zones`
        goes. Returns the number of zones migrated."""
        groups, envs, n = await self.store.list("groups"), await self.store.list("environments"), 0
        for z in await self.store.list("zones"):
            if "groups" not in z:
                users = {g["id"] for g in groups if z["id"] in (g.get("allowed_zones") or [])}
                users |= {e["group"] for e in envs if z["id"] in e.get("zones", [])}
                z["groups"] = sorted(users)
                await self.store.put("zones", z["id"], z)
                n += 1
            for g in z["groups"]:
                await self._bind(g, z["id"])
        for g in groups:
            if "allowed_zones" in g:
                g.pop("allowed_zones")
                await self.store.put("groups", g["id"], g)
        return n

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
        await self._check_bound(group, zones)
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
        """§24.2 precedence: an admin's pinned image → the image built for the group on deploy → None (the base)."""
        g = await self.store.get("groups", group) or {}
        return (g.get("image") or {}).get("ref") or (g.get("built_image") or {}).get("ref")

    async def builds_images(self, group_doc: dict) -> bool:
        return await imagebuild.enabled(self.cloud, group_doc)

    async def images(self, group) -> list[dict]:
        g = await self.get_group(group)
        pinned = (g.get("image") or {}).get("id")
        built = None if pinned else (g.get("built_image") or {}).get("id")
        # `created` has second resolution, so two builds recorded in the same second need `seq` to stay ordered.
        rows = sorted(
            await self.store.list("images", {"group": group}),
            key=lambda d: (d["created"], d.get("seq", 0)),
            reverse=True,
        )
        return [{**d, "current": d["id"] == pinned, "in_use": d["id"] in (pinned, built)} for d in rows]

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
        if not doc or doc.get("group") != group or not doc.get("ref"):  # a failed build has nothing to pin
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
        g["image"] = {"id": doc["id"], "ref": doc["ref"], "source": "manual"}
        await self.store.put("groups", group, g)
        return doc

    async def update_env(self, group, name, **fields) -> dict:
        e = await self.get_env(group, name)
        if fields.get("blocked") is not None:
            fields["blocked"] = clean_blocked(fields["blocked"])
        before = list(e.get("zones", []))
        if fields.get("zones") is not None:
            await self._check_zones(fields["zones"])
            await self._check_bound(group, fields["zones"])
            await self._attach_zones(group, fields["zones"], already=before)
        e.update({k: v for k, v in fields.items() if v is not None})
        saved = await self.store.put("environments", e["id"], e)
        if fields.get("zones") is not None:  # a zone no environment of the group uses any more is torn down (0.6.0)
            for z in sorted(set(before) - set(fields["zones"])):
                if group not in await self._zone_users(z):
                    await self.detach_zone(group, z)
        return saved

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
        for z in sorted(e.get("zones", [])):  # 0.6.0: its zones are torn down unless another environment uses them
            if group not in await self._zone_users(z):
                await self.detach_zone(group, z)

    async def rebalance(self, group, zone, user: str = "-", ip: str = "-"):
        """The load-balancer leg (the adapter), then §21.7 autoscaling: `autoscale.decide` on the zone's live metrics,
        and on up/down the stable Deployment's replicas through `cloud.scale`. Every up/down is audited
        `workers.autoscale` under `user` (a person, a key, or `scheduler`) and logged `ramen.autoscale`."""
        if not await self.store.get("groups", group):
            raise not_found("group")
        if not await self.store.get("zones", zone):
            raise not_found("zone")
        if sleeping.state_of(await self.worker_config(group, zone)) != sleeping.AWAKE:
            await self.wake(group, zone, "manual", user=user, ip=ip)  # §24.1: a rebalance wakes a sleeping zone first
        out = await self.cloud.rebalance(group, zone)
        out["autoscale"] = await self.autoscale(group, zone, user=user, ip=ip)
        return out

    def _zone_lock(self, group, zone) -> asyncio.Lock:
        locks = self.__dict__.setdefault("_autoscale_locks", {})
        return locks.setdefault(f"{group}/{zone}", asyncio.Lock())

    async def autoscale(self, group, zone, user: str = "-", ip: str = "-", now: float | None = None) -> dict:
        """One read-decide-write per zone at a time (0.8.0 SC4): the scheduler's tick and a Rebalance click must not
        both read the pre-change count and both scale."""
        async with self._zone_lock(group, zone):
            return await self._autoscale(group, zone, user, ip, now)

    async def _autoscale(self, group, zone, user: str, ip: str, now: float | None) -> dict:
        now = time.time() if now is None else now
        w = await self.worker_config(group, zone)
        if (state := sleeping.state_of(w)) != sleeping.AWAKE:  # §24.1: a sleeping zone has 0 workers on purpose
            return {"action": "hold", "from": w.get("count", 1), "to": w.get("count", 1), "reason": f"zone is {state}"}
        try:
            live = await self.cloud.workers(group, zone)
        except Exception as e:  # noqa: BLE001 - a dead API must not fail the rebalance; nothing is changed
            return {"action": "hold", "from": w.get("count", 1), "to": w.get("count", 1), "reason": f"no metrics: {e}"}
        decision, state = autoscale.decide(live, w.get("autoscale_state"), w, now)
        w["autoscale_state"], w["last_autoscale"] = state, decision
        if decision["action"] != "hold":
            w["count"] = decision["to"]
        await self.store.put("workers", w["id"], w)
        target = f"{group}/{zone}"
        if decision["action"] != "hold":
            decision["cloud"] = await self.cloud.scale(group, zone, await self.zone_spec(group, zone))
            tags = [f"group:{group}", f"zone:{zone}", f"action:{decision['action']}", f"from:{decision['from']}"]
            tags += [f"to:{decision['to']}", f"reason:{decision['reason']}"]
            await write_audit(
                self.store, user=user, ip=ip, action="workers.autoscale", target=target, ok=True, tags=tags
            )
            log.info(
                "autoscale %s %s %s→%s (%s)",
                target,
                decision["action"],
                decision["from"],
                decision["to"],
                decision["reason"],
            )
        elif "at max_count" in decision["reason"]:
            log.warning("autoscale %s pinned at max_count under load: %s", target, decision["reason"])
        return decision

    async def worker_config(self, group, zone) -> dict:
        z = await self.store.get("zones", zone)
        if not z:
            raise not_found("zone")
        if group not in (z.get("groups") or []):  # §26.1: a worker record exists only for a bound pair
            raise not_found(f"worker: zone {zone} is not bound to group {group}")
        return await self.store.get("workers", f"{group}:{zone}") or self.default_worker(group, zone)

    # --- §24.1 sleeping workers ---------------------------------------------------------------------------------
    async def _audit_sleep(self, action, group, zone, trigger, user, ip, ok=True, extra=()):
        tags = [f"group:{group}", f"zone:{zone}", f"trigger:{trigger}", *extra]
        await write_audit(
            self.store, user=user or "-", ip=ip, action=action, target=f"{group}/{zone}", ok=ok, tags=tags
        )

    async def sleep(self, group, zone, trigger: str = "manual", user: str | None = None, ip: str = "-") -> dict:
        """Activator up, workers and canary to 0, state `asleep`; the count and canary count are remembered. A zone
        that is not awake is left alone. Audited `workers.sleep` when `user` is given (a route audits itself)."""
        async with self._zone_lock(group, zone):
            w = await self.worker_config(group, zone)
            if sleeping.state_of(w) != sleeping.AWAKE:
                return w
            try:
                rt = await self.cloud.zone_runtime(group, zone) or {}
                result = await self.cloud.sleep_zone(group, zone) or {}
                # the canary replicas asked for before the sleep (kube adapters say so), else what was Ready
                canary = (result.get("previous") or {}).get("canary", rt.get("canary"))
                saved = {"count": max(1, int(w.get("count") or 1)), "canary": int(canary or 0)}
            except Exception as e:
                await self._audit_sleep("workers.sleep", group, zone, trigger, user, ip, False, [f"error:{e}"[:200]])
                raise
            w.update(state=sleeping.ASLEEP, state_since=time.time(), sleep_saved=saved)
            w["last_sleep"] = {"trigger": trigger, "at": now()}
            w = await self.store.put("workers", w["id"], w)
        log.info("sleep %s/%s (%s)", group, zone, trigger)
        if user is not None:
            await self._audit_sleep("workers.sleep", group, zone, trigger, user, ip)
        return {**w, "cloud": result}

    async def wake_begin(self, group, zone) -> tuple[dict, bool]:
        """Flip an `asleep` zone to `waking`. (record, started): `started` is True for exactly one caller per wake —
        a call that finds the zone `waking` or `awake` gets the record as it is, at once, and must not scale."""
        w = await self.worker_config(group, zone)
        st = sleeping.state_of(w)
        if st == sleeping.AWAKE or (st == sleeping.WAKING and not self._stale(w)):
            return w, False
        async with self._zone_lock(group, zone):
            w = await self.worker_config(group, zone)
            st = sleeping.state_of(w)
            if st == sleeping.AWAKE or (st == sleeping.WAKING and not self._stale(w)):
                return w, False
            w.update(state=sleeping.WAKING, state_since=time.time())
            return await self.store.put("workers", w["id"], w), True

    @staticmethod
    def _stale(w: dict) -> bool:
        return time.time() - float(w.get("state_since") or 0) > sleeping.WAKE_STALE

    async def wake_finish(self, group, zone, trigger: str, user: str | None = None, ip: str = "-") -> dict:
        """The scale behind a `wake_begin` that returned started: workers back to the zone's count, the canary to
        what it was, wait Ready, state `awake`. A failed wake leaves the zone `asleep` so the next call tries again."""
        async with self._zone_lock(group, zone):
            w = await self.worker_config(group, zone)
            if sleeping.state_of(w) != sleeping.WAKING:
                return w
            saved = w.get("sleep_saved") or {}
            count = max(1, int(w.get("count") or saved.get("count") or 1))
            try:
                result = await self.cloud.wake_zone(group, zone, count, int(saved.get("canary") or 0))
            except Exception as e:
                w.update(state=sleeping.ASLEEP, state_since=time.time())
                await self.store.put("workers", w["id"], w)
                await self._audit_sleep("workers.wake", group, zone, trigger, user, ip, False, [f"error:{e}"[:200]])
                raise
            w.update(state=sleeping.AWAKE, state_since=time.time())
            w["last_wake"] = {"trigger": trigger, "at": now()}
            w.pop("sleep_saved", None)
            w = await self.store.put("workers", w["id"], w)
        log.info("wake %s/%s (%s) to %s", group, zone, trigger, count)
        if user is not None:
            await self._audit_sleep("workers.wake", group, zone, trigger, user, ip, extra=[f"count:{count}"])
        return {**w, "cloud": result}

    async def wake(self, group, zone, trigger: str = "manual", user: str | None = None, ip: str = "-") -> dict:
        w, started = await self.wake_begin(group, zone)
        return await self.wake_finish(group, zone, trigger, user, ip) if started else w

    async def touch_zone(self, group, zone) -> None:
        """A deploy counts as activity (§24.1 idle): the zone is not put to sleep sooner than its setting after it."""
        w = await self.worker_config(group, zone)
        if int(w.get("sleep_after_secs") or 0) > 0:
            w["state_since"] = time.time()
            await self.store.put("workers", w["id"], w)

    async def sleep_if_idle(self, group, zone, at: float | None = None) -> dict | None:
        """The scheduler's check: put an awake zone to sleep when §24.1 calls it idle. None when it stays awake."""
        w = await self.worker_config(group, zone)
        secs = int(w.get("sleep_after_secs") or 0)
        if secs <= 0 or sleeping.state_of(w) != sleeping.AWAKE:
            return None
        at = time.time() if at is None else at
        if at - float(w.get("state_since") or 0) < secs:
            return None
        ok, _ = sleeping.idle(await self.cloud.workers(group, zone), secs, at, float(w.get("state_since") or 0))
        return await self.sleep(group, zone, "idle", user="scheduler") if ok else None

    async def set_workers(
        self,
        group,
        zone,
        p: Principal,
        count=None,
        size=None,
        allowed_sizes=None,
        max_count=None,
        sleep_after_secs=None,
        ip: str = "-",
    ) -> dict:
        w = await self.worker_config(group, zone)
        if sleep_after_secs is not None:  # §24.1: the route admits super admins and the group's admins only
            if sleep_after_secs != 0 and sleep_after_secs < sleeping.MIN_SECS:
                raise invalid(f"sleep_after_secs must be 0 (never) or {sleeping.MIN_SECS} or more")
            w["sleep_after_secs"] = sleep_after_secs
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
        w.setdefault("max_count", max(1, int(w.get("count") or 1)))
        if max_count is not None:  # §21.7: super admins and group admins (the route's gate); min is always 1
            if max_count < 1:
                raise invalid("max_count must be 1 or more (the minimum is always 1)")
            w["max_count"] = max_count
            if int(w.get("count") or 1) > max_count:  # a lower max clamps the running count down at once
                w["count"] = max_count
        if count is not None:
            if count < 1 or count > int(w["max_count"]):
                raise invalid(f"Count must be between 1 and max_count ({w['max_count']})")
            w["count"] = count
        doc = await self.store.put("workers", w["id"], w)
        sizing = any(v is not None for v in (count, size, allowed_sizes, max_count))
        if sleeping.state_of(w) != sleeping.AWAKE:
            if count is None and size is None:  # nothing to run it on: the setting is applied when the zone wakes
                return {**doc, "cloud": {"ok": True, "note": f"zone is {sleeping.state_of(w)}; applied when it wakes"}}
            await self.wake(group, zone, "manual", user=p.name, ip=ip)  # a count change wakes the zone first
            doc = await self.worker_config(group, zone)
        if sizing or sleep_after_secs is None:
            doc["cloud"] = await self.cloud.scale(group, zone, await self.zone_spec(group, zone))
        return doc

    async def refresh(self) -> dict:
        r = await self.cloud.refresh()
        bound = {(g, z["id"]) for z in await self.store.list("zones") for g in z.get("groups") or []}
        for z in r.get("zones", []):
            if not (z.get("group") and z.get("zone")) or (z["group"], z["zone"]) not in bound:
                continue  # §26.1: only bound pairs have a record
            w = await self.store.get("workers", f"{z['group']}:{z['zone']}") or self.default_worker(
                z["group"], z["zone"]
            )
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
