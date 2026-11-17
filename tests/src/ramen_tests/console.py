"""httpx client for the console (CONTRACTS §4). Routes mirror console/tests/test_api.py; edit ROUTES if they move."""

import os
import re
import time

import httpx

from . import ledger
from .env import console_verify, env, no_cloud, strip

ROUTES = {
    "login": "/login",  # form POST {email,password} → 303 (401 on bad password)
    "logout": "/logout",
    "me": "/api/v1/me",
    "users": "/api/v1/users",  # POST {email,password,role,groups} → 201 {id,...}
    "user": "/api/v1/users/{id}",
    "user_password": "/api/v1/users/{id}/password",  # POST {password} ("me" = self)
    "groups": "/api/v1/groups",  # POST {name,repo_url,ref} → 201
    "group": "/api/v1/groups/{group}",
    "zones": "/api/v1/zones",  # POST {name,provider,region,environments} → 201 {...zone, jobs} (§27.2; groups → 422)
    "zone": "/api/v1/zones/{zone}",  # GET | PUT {region?, environments?} → {...zone, jobs} | DELETE (§27.2)
    "group_zones": "/api/v1/groups/{group}/zones",  # GET → {zones:[...]} (viewers of the group; §26.1, derived §27.1)
    "environments": "/api/v1/groups/{group}/environments",  # POST {name,ref,zones:[...]} → 201 (per-group shortcut)
    "environment": "/api/v1/groups/{group}/environments/{env}",
    "environments_all": "/api/v1/environments",  # ?group= ; POST {name,ref?,groups} → 201 by-name object (§27.2)
    "environments_by_name": "/api/v1/environments/by-name",  # GET → [{name,ref,ref_differs,groups,zones,last_deploy}]
    "environment_by_name": "/api/v1/environments/{name}",  # PUT {ref?,groups?} → 200 | DELETE → 204 (§27.2)
    "deploy": "/api/v1/groups/{group}/environments/{env}/deploy",  # POST {canary,zone?} → 202 {id,status}
    "job": "/api/v1/jobs/{id}",
    "workers": "/api/v1/groups/{group}/zones/{zone}/workers",  # GET {live:[{load}],count,size}
    "rebalance": "/api/v1/groups/{group}/zones/{zone}/rebalance",
    "ip_rules": "/api/v1/groups/{group}/zones/{zone}/ip-rules",  # PUT {cidrs:[...]}
    "service_account": "/api/v1/groups/{group}/zones/{zone}/service-account",  # POST → {name,...} (super admin)
    "sa_restrictions": "/api/v1/groups/{group}/sa-restrictions",  # PUT {rules:[...]}
    "env_verbose": "/api/v1/groups/{group}/environments/{env}/verbose",  # POST {verbose}
    "secrets": "/api/v1/groups/{group}/secrets",  # POST {name,value,env?,zone?} → 201 {id,name}
    "secret": "/api/v1/groups/{group}/secrets/{id}",
    "mcp_keys": "/api/v1/groups/{group}/mcp-keys",  # POST {name} → 201 {id,key:"rmk_..."}
    "mcp_key": "/api/v1/groups/{group}/mcp-keys/{id}",
    "api_keys": "/api/v1/api-keys",  # POST {name,role?,groups?} → 201 {id,key:"rmn_..."}
    "api_key": "/api/v1/api-keys/{id}",
    "audit": "/api/v1/audit",
    "backups": "/api/v1/backups",  # POST {target:"local"} → 201 {id,release_version}
    "backup_download": "/api/v1/backups/{id}/download",
    "config": "/api/v1/config",
    "config_reload": "/api/v1/config/reload",
    "sa_rules": "/api/v1/config/sa-rules",
    "refresh": "/api/v1/refresh",
    "dashboard": "/api/v1/dashboard",
    "logs": "/api/v1/logs",  # ?group&zone&worker?&tail&download=1 → text/plain
    "healthz": "/healthz",
    # v0.3.0 (CONTRACTS §9)
    "requests": "/api/v1/requests",  # POST {role,group?} | {group,zone,permission} → 201; GET (super admin)
    "request_approve": "/api/v1/requests/{id}/approve",  # POST → applied SA permissions / role granted
    "policy_permissions": "/api/v1/policy/permissions",  # GET catalogue [{permission,desc,gcp,aws}]
    "env_blocked": "/api/v1/groups/{group}/environments/{env}/blocked",  # PUT {blocked:[names]}
    "env_tool_access": "/api/v1/groups/{group}/environments/{env}/tool-access",  # PUT {tool:{list,call}} (0.7.2 C10)
    # v0.4.0 (CONTRACTS §12.1)
    "env_zone_blocked": "/api/v1/groups/{group}/environments/{env}/zones/{zone}/blocked",  # PUT {blocked:[names]}
    "config_auth": "/api/v1/config/auth",  # GET|PUT {password_login?, magic_link?} (super admin)
    "config_scheduler": "/api/v1/config/scheduler",  # GET|PUT {enabled?, interval_seconds?} (super admin, N1)
    "auth_reset": "/auth/reset",  # form POST {email} → 200 always
    "auth_reset_token": "/auth/reset/{token}",  # form POST {password} → 303 /login
    "auth_magic": "/auth/magic",  # form POST {email} (auth.magic_link)
    "auth_magic_token": "/auth/magic/{token}",  # GET → 303 + session
    # v0.4.1 (CONTRACTS §13)
    "backup_restore": "/api/v1/backups/{id}/restore",  # POST {dry_run?,prune?,reconcile?,force?} (super admin)
    "request_deny": "/api/v1/requests/{id}/deny",  # POST → status denied (pending only)
    "request_revoke": "/api/v1/requests/{id}/revoke",  # POST → status revoked (approved only)
    "zone_permission": "/api/v1/groups/{group}/zones/{zone}/permissions/{permission}",  # DELETE → remaining set
    "images": "/api/v1/groups/{group}/images",  # GET history; POST {tag,digest?,note?} → 201 (super admin)
    "image_current": "/api/v1/groups/{group}/images/current",  # PUT {id} recall; DELETE unpin (super admin)
    # v1.0.0 (CONTRACTS §24)
    "zone_sleep": "/api/v1/groups/{group}/zones/{zone}/sleep",  # POST (group admin) → worker record, state asleep
    "zone_wake": "/api/v1/groups/{group}/zones/{zone}/wake",  # POST (group admin) → worker record, state awake
    "internal_wake": "/api/v1/internal/zones/{group}/{zone}/wake",  # POST, x-ramen-admin-key; in-cluster only
    "config_otel": "/api/v1/config/otel",  # GET|PUT {enabled,endpoint,headers,client_endpoint,client_headers}
    # v1.0.0b (CONTRACTS §28.1): {blocked, regions, clusters:[{provider,region,name,host,project?}]}; PUT {clusters}
    # → 409 while a zone uses a dropped satellite's region
    "config_regions": "/api/v1/config/regions",
    # v1.0.0c (CONTRACTS §29): {anthropic: {api_key}, openai: {api_key, base_url?}, azure_openai: {api_key, host}};
    # PUT merges, "" clears a key; GET shows set/not set, never a key
    "config_llm_providers": "/api/v1/config/llm-providers",
    "group_evals": "/api/v1/groups/{group}/evals",  # PUT {gate?, required?} (group admin) → the group's `evals`
    "policy": "/.well-known/ramen-policy",  # GET ?group=&zone= → {telemetry: {version, required}, evals: {...}}
    # v1.0.0c Phase 2 (CONTRACTS §30): PUT {gate?, required?, llm?} (group admin) → the group's `skillspector`
    "group_skillspector": "/api/v1/groups/{group}/skillspector",
    "scan_report": "/groups/{group}/scans/{job}/{zone}",  # GET page (group member): the zone's SkillSpector report
    "evals_report": "/groups/{group}/evals/{job}/{zone}",  # GET page (group member): the zone's evals run, every case
    # v1.0.0c Phase 3 (CONTRACTS §31): POST {version} (any member, audited `configs.accept`) → the user's acceptance
    "configs_accept": "/api/v1/groups/{group}/configs/accept",
    "configs_page": "/groups/{group}/configs",  # GET page (member): manifest + every file verbatim + Accept
    "otlp_traces": "/otlp/v1/traces",  # POST OTLP/HTTP (protobuf | JSON, ≤ 4 MiB), bearer = user token or MCP key
    "otlp_metrics": "/otlp/v1/metrics",
    "otlp_logs": "/otlp/v1/logs",
    # v1.0.0c Phase 2.5 (CONTRACTS §36.2): the group's own providers (same shape as the org's, + `allowed_models`) and
    # `guardrails {llm, model, required}` (group admin; `model` must be in the provider's `allowed_models` → 422)
    "group_llm_providers": "/api/v1/groups/{group}/llm-providers",
    "group_guardrails": "/api/v1/groups/{group}/guardrails",
    # v1.0.0c (CONTRACTS §37, D84): live model lists per scope, the policy cards' model slot, the member pages
    "config_llm_models": "/api/v1/config/llm-providers/{provider}/models",  # GET (super admin) {models, source}
    "group_llm_models": "/api/v1/groups/{group}/llm-providers/{provider}/models",  # GET (group admin)
    "guardrail_models_ui": "/ui/groups/{group}/guardrails/models",  # GET ?kind=guardrails|scans&llm= (group admin)
    "group_page": "/groups/{group}",  # GET page (member): zones, packages, apps and skills, the policy cards (admins)
    "app_preview": "/groups/{group}/apps/{zone}/{name}",  # GET page (member): one MCP app in a sandboxed frame (§33.1)
    "oauth_login": "/auth/{name}/login",
    "oauth_callback": "/auth/{name}/callback",
}
API_KEY_HEADER = "X-Ramen-Api-Key"
CSRF_COOKIE, CSRF_HEADER = "ramen_csrf", "X-Ramen-CSRF"
SESSION_COOKIE = "ramen_session"  # console/src/ramen_console/auth/sessions.py::COOKIE


class Console:
    def __init__(
        self, base: str, api_key: str | None = None, timeout: float = 180
    ):  # cloud ops (Armor/backends) retry for minutes
        headers = {API_KEY_HEADER: api_key} if api_key else {}
        self.http = httpx.Client(base_url=strip(base), verify=console_verify(), timeout=timeout, headers=headers)
        self.http.event_hooks["request"].append(self._csrf)  # cookie sessions must echo the csrf cookie (CONTRACTS §9)
        self.http.event_hooks["response"].append(ledger.observe)  # S7: what this run creates is removed at its end
        self.email: str | None = None

    def _csrf(self, request: httpx.Request) -> None:
        t = self.http.cookies.get(CSRF_COOKIE)
        if t and CSRF_HEADER not in request.headers:
            request.headers[CSRF_HEADER] = t

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def url(self, key: str, **kw) -> str:
        return ROUTES[key].format(**kw)

    def login(self, email: str, password: str) -> httpx.Response:
        r = self.http.post(self.url("login"), data={"email": email, "password": password}, follow_redirects=False)
        if r.status_code in (200, 303):
            self.email = email
        return r

    def me(self) -> httpx.Response:
        return self.http.get(self.url("me"))

    # A single-replica console behind a cloud LB drops requests for ~1 min when its pod is rescheduled (Autopilot node
    # churn) or right after a rollout: the LB answers 502/503/504 with an envoy-style upstream error. Retry those; the
    # API is idempotent for our purposes (creates answer 409 on a repeat and every caller accepts 409).
    TRANSIENT = ("upstream connect error", "no healthy upstream", "upstream request timeout", "connection termination")

    def _send(self, method: str, url: str, retries: int = 12, **kw) -> httpx.Response:
        for attempt in range(retries + 1):
            r = self.http.request(method, url, **kw)
            body = r.text[:300].lower()
            if r.status_code not in (502, 503, 504) or not any(t in body for t in self.TRANSIENT) or attempt == retries:
                return r
            time.sleep(5)
        return r  # pragma: no cover

    def get(self, key: str, **kw) -> httpx.Response:
        params = kw.pop("params", None)
        return self._send("GET", self.url(key, **kw), params=params)

    def post(self, key: str, body: dict | None = None, **kw) -> httpx.Response:
        return self._send("POST", self.url(key, **kw), json=body if body is not None else {})

    def put(self, key: str, body: dict, **kw) -> httpx.Response:
        return self._send("PUT", self.url(key, **kw), json=body)

    def delete(self, key: str, **kw) -> httpx.Response:
        return self._send("DELETE", self.url(key, **kw))

    def page(self, path: str) -> httpx.Response:
        return self.http.get(path)

    def form(self, key: str, data: dict, **kw) -> httpx.Response:
        return self.http.post(self.url(key, **kw), data=data, follow_redirects=False)

    # convenience
    def create_user(self, email, password, role, groups=None):
        return self.post("users", {"email": email, "password": password, "role": role, "groups": groups or []})

    def create_group(self, name, repo_url, ref="main"):
        return self.post("groups", {"name": name, "repo_url": repo_url, "ref": ref})

    def create_zone(self, name, provider=None, region=None, environments=None):
        """§27.2: a zone names its environments; its groups are derived. The answer carries `jobs` (one canary deploy
        per `<g>/<env>` pair the environments yield, §27.3)."""
        provider = provider or env("RAMEN_ZONE_PROVIDER", "local")
        region = region or env("RAMEN_ZONE_REGION", "local")
        body = {"name": name, "provider": provider, "region": region, "environments": list(environments or [])}
        return self.post("zones", body)

    def zone(self, name) -> dict:
        """`GET /zones/{zone}` → the zone doc: `{name, provider, region, environments, groups, created}` plus, since
        §28.1, `cluster` ("home" or the satellite's name) and `host` (the edge clients reach it at)."""
        r = self.get("zone", zone=name)
        assert r.status_code == 200, f"zone {name}: {r.status_code} {r.text[:300]}"
        return r.json()

    def regions_config(self) -> dict:
        r = self.get("config_regions")
        assert r.status_code == 200, f"config/regions: {r.status_code} {r.text[:300]}"
        return r.json()

    def clusters(self) -> list[dict]:
        """§28.1: the satellite clusters `[{provider, region, name, host, project?}]` (the home cluster is implicit)."""
        return list(self.regions_config().get("clusters") or [])

    def blocked_regions(self) -> dict[str, list[str]]:
        """C7: `{"gcp": [regions], "aws": [...], "azure": [...]}` no zone may be created in or deployed to."""
        return dict(self.regions_config().get("blocked") or {})

    def set_blocked_regions(self, provider, regions) -> httpx.Response:
        """`PUT /config/regions {<provider>: [regions]}`; the other providers' lists are kept."""
        return self.put("config_regions", {provider: list(regions)})

    def set_clusters(self, clusters) -> httpx.Response:
        """`PUT /config/regions {clusters}` replaces the satellite list; 409 while a zone uses a dropped region."""
        return self.put("config_regions", {"clusters": list(clusters)})

    def bind_zone(self, zone, group) -> None:
        """Gone with §27: a zone is bound to a group through the environments it names (`create_environment` with the
        zone, `update_zone(environments=...)`). Kept as a no-op so older call sites still read."""
        return None

    def update_zone(self, zone, region=None, environments=None):
        """§27.2 `PUT /zones/{zone}`: `environments` replaces the set (attach + deploy / detach); `region` moves every
        bound pair (D66). Answer `{...zone, jobs}`."""
        body = {}
        if region is not None:
            body["region"] = region
        if environments is not None:
            body["environments"] = list(environments)
        return self.put("zone", body, zone=zone)

    def create_environment(self, group, name, zones, ref="main", wait=True):
        """§27.2 per-group shortcut: adds `group` to environment `name` (creating it) and attaches it to `zones`
        (the caller must be allowed to edit them). The attach starts a deploy per pair (§27.3); `wait` blocks on the
        jobs the answer lists so a following explicit deploy is not refused as "already running"."""
        r = self.post("environments", {"name": name, "ref": ref, "zones": list(zones)}, group=group)
        # Attaching a zone to a *new* group creates that group's cloud identity (CONTRACTS §7). With RAMEN_NO_CLOUD=iam
        # (kind) there is none to create, and only the zones pre-annotated in deploy/kind/zones.yaml can be attached.
        if r.status_code == 502 and no_cloud("iam") and "credential" in r.text.lower():
            import pytest

            pytest.skip(f"no cloud IAM here: attaching a zone to a new group needs one ({r.text[:120]})")
        if wait and r.status_code in (200, 201):
            self.wait_jobs(jobs_of(r))
        return r

    def create_environment_by_name(self, name, ref=None, groups=()):
        """§27.2 `POST /environments {name, ref?, groups}` → 201 the by-name object (409 when the name exists)."""
        body = {"name": name, "groups": list(groups)}
        if ref is not None:
            body["ref"] = ref
        return self.post("environments_all", body)

    def update_environment_by_name(self, name, ref=None, groups=None):
        body = {}
        if ref is not None:
            body["ref"] = ref
        if groups is not None:
            body["groups"] = list(groups)
        return self.put("environment_by_name", body, name=name)

    def environments_by_name(self) -> httpx.Response:
        return self.get("environments_by_name")

    def delete_environment_by_name(self, name) -> httpx.Response:
        """§27.2: 204; a zone only this environment used is deleted with it."""
        return self.delete("environment_by_name", name=name)

    def deploy(self, group, env, canary=True, zone=None, breaking=None):
        """`breaking` accepts a breaking tool-schema change (CONTRACTS §23.3); default from RAMEN_DEPLOY_BREAKING=1."""
        body = {"canary": canary}
        if zone:
            body["zone"] = zone
        if breaking or (breaking is None and os.environ.get("RAMEN_DEPLOY_BREAKING") == "1"):
            body["breaking"] = True
        r = self.post("deploy", body, group=group, env=env)
        running = re.search(r"already running \(job ([\w-]+)\)", r.text) if r.status_code == 409 else None
        if running:  # §27.3: an attach started a deploy of this target a moment ago; let it finish, then ours
            self.wait_job(running.group(1))
            r = self.post("deploy", body, group=group, env=env)
        return r

    def wait_jobs(self, ids, timeout: float | None = None) -> list[dict]:
        """Every §27.3 attach job (`jobs` of a zone/environment write) to its end; the finished jobs in order."""
        return [self.wait_job(i, timeout) for i in ids]

    def wait_job(self, job_id: str, timeout: float | None = None) -> dict:
        # RAMEN_JOB_TIMEOUT: a cluster that adds a node per deploy (GKE Autopilot under quota) needs 15+ min
        timeout = float(os.environ.get("RAMEN_JOB_TIMEOUT", 300)) if timeout is None else timeout
        deadline = time.monotonic() + timeout
        job, bad = {}, 0
        while time.monotonic() < deadline:
            r = self.get("job", id=job_id)
            if r.status_code >= 500 and bad < 5:  # transient LB/console reset: retry a few times
                bad += 1
                time.sleep(3)
                continue
            assert r.status_code == 200, f"job {job_id}: {r.status_code} {r.text[:300]}"
            bad = 0
            job = r.json()
            if job.get("status") != "running":
                return job
            time.sleep(2)
        raise AssertionError(f"job {job_id} still running after {timeout}s: {job}")

    def add_secret(self, group, name, value, env=None, zone=None):
        body = {"name": name, "value": value}
        if env:
            body["env"] = env
        if zone:
            body["zone"] = zone
        return self.post("secrets", body, group=group)

    def create_mcp_key(self, group, name="tests", accept_configs=True):
        """§31.1: minting answers `403 configs not accepted` until the minter accepted the group's LLM configs; the
        harness accepts (what an admin does on the configs page) and mints again. `accept_configs=False` keeps the 403
        for the tests of that rule."""
        r = self.post("mcp_keys", {"name": name}, group=group)
        if r.status_code == 403 and accept_configs and "configs" in r.text and self.accept_configs(group):
            r = self.post("mcp_keys", {"name": name}, group=group)
        return r

    def create_api_key(self, name, groups=None, role=None, client_type=None):
        body = {"name": name}
        if groups is not None:
            body["groups"] = groups
        if role:
            body["role"] = role
        if client_type:
            body["client_type"] = client_type  # devops | agent (CONTRACTS §12.1, D21)
        return self.post("api_keys", body)

    def audit(self):
        return self.get("audit")

    # v1.0.0c (CONTRACTS §29)
    def llm_providers(self) -> dict:
        """§29.1 `GET /config/llm-providers`: per provider whether a key is set (`{"anthropic": {"api_key": true}, …}`
        in whatever spelling the console uses); never a key value."""
        r = self.get("config_llm_providers")
        assert r.status_code == 200, f"config/llm-providers: {r.status_code} {r.text[:300]}"
        return r.json()

    def set_llm_providers(self, providers: dict) -> httpx.Response:
        """§29.1 `PUT /config/llm-providers` (super admin): merges; an empty-string key clears that provider's key."""
        return self.put("config_llm_providers", providers)

    def set_group_evals(self, group, gate=None, required=None) -> httpx.Response:
        """§29.2 `PUT /groups/{g}/evals {gate?, required?}` (group admin): `gate` fails a deploy on failed evals,
        `required` makes the workers demand the evals version on every gated call (§29.3)."""
        body = {}
        if gate is not None:
            body["gate"] = bool(gate)
        if required is not None:
            body["required"] = bool(required)
        return self.put("group_evals", body, group=group)

    def set_group_skillspector(self, group, gate=None, required=None, llm=None) -> httpx.Response:
        """§30.2 `PUT /groups/{g}/skillspector {gate?, required?, llm?}` (group admin): `gate` fails a deploy on
        `DO_NOT_INSTALL`, `required` makes the workers demand the policy version (§30.3), `llm` names a configured
        provider or `""` (static rules only)."""
        body = {}
        if gate is not None:
            body["gate"] = bool(gate)
        if required is not None:
            body["required"] = bool(required)
        if llm is not None:
            body["llm"] = llm
        return self.put("group_skillspector", body, group=group)

    def evals_report(self, group, job, zone) -> httpx.Response:
        """§29.2 the page of a deploy job's evals run on `zone` (group member): every case, the promptfoo run id."""
        return self.page(ROUTES["evals_report"].format(group=group, job=job, zone=zone))

    def scan_report(self, group, job, zone) -> httpx.Response:
        """§30.2 the report page of a deploy job's scan of `zone` (group member)."""
        return self.page(ROUTES["scan_report"].format(group=group, job=job, zone=zone))

    def policy(self, group, zone) -> dict:
        """§29.3 `GET /.well-known/ramen-policy?group=&zone=` (public, no secrets): what the bridge checks offline,
        `{telemetry: {version, required}, evals: {version, required}}` (`evals` absent when the repo has no file;
        §30.3 adds `skillspector: {version, required}` when the engine runs or the group requires it, §31.1
        `configs: {version, required}` when the repo has `mcp/llm-config/`)."""
        r = self.get("policy", params={"group": group, "zone": zone})
        assert r.status_code == 200, f"ramen-policy: {r.status_code} {r.text[:300]}"
        return r.json()

    # v1.0.0c Phase 3 (CONTRACTS §31)
    def configs_version(self, group, zone=None) -> str | None:
        """The group's LLM configs version from the public policy (`zone`: any zone of the group; without one the
        group's zones are looked up), None when the repo has no `mcp/llm-config/` or the console predates §31."""
        # `/groups/{g}/zones` answers names or zone objects depending on the console's era
        zones = [zone] if zone else [z.get("name") if isinstance(z, dict) else z for z in self.group_zones(group)]
        for z in zones:
            r = self.get("policy", params={"group": group, "zone": z})
            if r.status_code == 200 and isinstance((r.json() or {}).get("configs"), dict):
                return r.json()["configs"].get("version")
        return None

    def group_zones(self, group) -> list[dict]:
        r = self.get("group_zones", group=group)
        return list((r.json() or {}).get("zones") or []) if r.status_code == 200 else []

    def accept(self, group, version) -> httpx.Response:
        """§31.1 `POST /groups/{g}/configs/accept {version}` (any member; audited `configs.accept`)."""
        return self.post("configs_accept", {"version": version}, group=group)

    def accept_configs(self, group, zone=None) -> str | None:
        """Accept the group's current LLM configs as this user (idempotent; no-op without configs). The version
        accepted, or None."""
        v = self.configs_version(group, zone)
        if v:
            r = self.accept(group, v)
            assert r.status_code in (200, 201, 204), f"configs/accept {group} {v}: {r.status_code} {r.text[:300]}"
        return v

    def configs_page(self, group) -> httpx.Response:
        return self.page(ROUTES["configs_page"].format(group=group))

    # v1.0.0c Phase 2.5 (CONTRACTS §36.2)
    def set_group_llm_providers(self, group, providers: dict) -> httpx.Response:
        """§36.2 `PUT /groups/{g}/llm-providers` (group admin): the org card's shape per provider (`api_key`,
        `base_url`, … and `allowed_models`); merges, an empty string clears a key."""
        return self.put("group_llm_providers", providers, group=group)

    def group_llm_providers(self, group) -> dict:
        """§36.2 `GET /groups/{g}/llm-providers`: set / not set per provider plus `allowed_models`; never a key."""
        r = self.get("group_llm_providers", group=group)
        assert r.status_code == 200, f"groups/{group}/llm-providers: {r.status_code} {r.text[:300]}"
        return r.json()

    def set_group_guardrails(self, group, llm=None, model=None, required=None) -> httpx.Response:
        """§36.2 `PUT /groups/{g}/guardrails {llm?, model?, required?}` (group admin): `llm` a provider of the group
        or the org (`""` = none), `model` one of its `allowed_models` (else 422), `required` the §36.3 gate switch."""
        body = {}
        if llm is not None:
            body["llm"] = llm
        if model is not None:
            body["model"] = model
        if required is not None:
            body["required"] = bool(required)
        return self.put("group_guardrails", body, group=group)

    def group_guardrails(self, group) -> dict:
        """The group's `guardrails` settings as `GET /groups/{g}` records them (`{}` before any write)."""
        r = self.get("group", group=group)
        assert r.status_code == 200, f"groups/{group}: {r.status_code} {r.text[:300]}"
        return dict(r.json().get("guardrails") or {})

    def allowed_models(self, provider, group=None) -> list[str]:
        """§36.2: a provider's `allowed_models` on the org card (`group=None`) or on the group's own card; `[]` when
        the provider is not set or lists none (= its default model only)."""
        doc = self.group_llm_providers(group) if group else self.llm_providers()
        entry = (doc.get("providers") or doc).get(provider) if isinstance(doc, dict) else None
        return list(entry.get("allowed_models") or []) if isinstance(entry, dict) else []

    def guardrails_version(self, group, zone) -> str | None:
        """§36.3: the group's guardrails policy version from the public policy; None when the repo has no
        `mcp/guardrails.yaml` or the console predates §36."""
        r = self.get("policy", params={"group": group, "zone": zone})
        g = (r.json() or {}).get("guardrails") if r.status_code == 200 else None
        return g.get("version") if isinstance(g, dict) else None

    def set_otel(self, **body) -> httpx.Response:
        """§24.3 `PUT /config/otel {enabled?, endpoint?, headers?, client_endpoint?, client_headers?}` (super admin)."""
        return self.put("config_otel", body)

    def otlp(self, signal, token, body, content_type="application/json", gzip=False, **headers) -> httpx.Response:
        """§31.4 `POST /otlp/v1/<signal>` with a user token or an MCP key as bearer; `body` bytes or a JSON dict."""
        import gzip as gz
        import json as js

        raw = body if isinstance(body, bytes | bytearray) else js.dumps(body).encode()
        h = {"Content-Type": content_type, **headers}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if gzip:
            raw, h["Content-Encoding"] = gz.compress(bytes(raw)), "gzip"
        return self.http.post(self.url(f"otlp_{signal}"), content=bytes(raw), headers=h)


def evals_of(job_result, zone: str):
    """§29.2: the `evals` record of `zone` in a deploy job's `result` (the job dict or its `result` map):
    `"absent"` (the repo has no `mcp/evals/promptfooconfig.yaml` — the console records no `evals` key for the zone),
    `{"skipped": "no evals engine" | "no MCP key"}`, `{"error": …}` or `{"passed", "failed", "errors", "url"?}`;
    None when the zone has no result at all."""
    result = job_result.get("result", job_result) if isinstance(job_result, dict) else {}
    zone_result = (result or {}).get(zone)
    if not isinstance(zone_result, dict):
        return None
    return zone_result.get("evals", "absent")


def skillspector_of(job_result, zone: str):
    """§30.2: the `skillspector` record of `zone` in a deploy job's `result`: `"absent"` (no key — nothing to scan),
    `{"skipped": …}`, `{"error": …}` or `{score, severity, recommendation, skills, findings, url}`; None when the
    zone has no result at all."""
    result = job_result.get("result", job_result) if isinstance(job_result, dict) else {}
    zone_result = (result or {}).get(zone)
    if not isinstance(zone_result, dict):
        return None
    return zone_result.get("skillspector", "absent")


def jobs_of(r: httpx.Response) -> list[str]:
    """The §27.3 deploy job ids a zone/environment write answered with (`[]` when the answer has none)."""
    try:
        body = r.json()
    except ValueError:
        return []
    return list(body.get("jobs") or []) if isinstance(body, dict) else []


def items(r: httpx.Response) -> list:
    body = r.json()
    if isinstance(body, list):
        return body
    for k in ("items", "results", "data", "live"):
        if isinstance(body.get(k), list):
            return body[k]
    raise AssertionError(f"no list in response: {body!r}")
