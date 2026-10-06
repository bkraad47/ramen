"""httpx client for the console (CONTRACTS §4). Routes mirror console/tests/test_api.py; edit ROUTES if they move."""

import time

import httpx

from .env import env, no_cloud, strip, tls_verify

ROUTES = {
    "login": "/login",  # form POST {email,password} → 303 (401 on bad password)
    "logout": "/logout",
    "me": "/api/v1/me",
    "users": "/api/v1/users",  # POST {email,password,role,groups} → 201 {id,...}
    "user": "/api/v1/users/{id}",
    "user_password": "/api/v1/users/{id}/password",  # POST {password} ("me" = self)
    "groups": "/api/v1/groups",  # POST {name,repo_url,ref} → 201
    "group": "/api/v1/groups/{group}",
    "zones": "/api/v1/zones",  # POST {name,provider,region} → 201 (super admin)
    "zone": "/api/v1/zones/{zone}",
    "environments": "/api/v1/groups/{group}/environments",  # POST {name,ref,zones:[...]} → 201
    "environment": "/api/v1/groups/{group}/environments/{env}",
    "environments_all": "/api/v1/environments",  # ?group=
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
        self.http = httpx.Client(base_url=strip(base), verify=tls_verify(), timeout=timeout, headers=headers)
        self.http.event_hooks["request"].append(self._csrf)  # cookie sessions must echo the csrf cookie (CONTRACTS §9)
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

    def create_zone(self, name, provider=None, region=None):
        provider = provider or env("RAMEN_ZONE_PROVIDER", "local")
        region = region or env("RAMEN_ZONE_REGION", "local")
        return self.post("zones", {"name": name, "provider": provider, "region": region})

    def create_environment(self, group, name, zones, ref="main"):
        r = self.post("environments", {"name": name, "ref": ref, "zones": list(zones)}, group=group)
        # Attaching a zone to a *new* group creates that group's cloud identity (CONTRACTS §7). With RAMEN_NO_CLOUD=iam
        # (kind) there is none to create, and only the zones pre-annotated in deploy/kind/zones.yaml can be attached.
        if r.status_code == 502 and no_cloud("iam") and "credential" in r.text.lower():
            import pytest

            pytest.skip(f"no cloud IAM here: attaching a zone to a new group needs one ({r.text[:120]})")
        return r

    def deploy(self, group, env, canary=True, zone=None):
        body = {"canary": canary}
        if zone:
            body["zone"] = zone
        return self.post("deploy", body, group=group, env=env)

    def wait_job(self, job_id: str, timeout: float = 300) -> dict:
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

    def create_mcp_key(self, group, name="tests"):
        return self.post("mcp_keys", {"name": name}, group=group)

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


def items(r: httpx.Response) -> list:
    body = r.json()
    if isinstance(body, list):
        return body
    for k in ("items", "results", "data", "live"):
        if isinstance(body.get(k), list):
            return body[k]
    raise AssertionError(f"no list in response: {body!r}")
