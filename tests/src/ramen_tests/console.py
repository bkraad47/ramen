"""httpx client for the console (CONTRACTS §4). Routes mirror console/tests/test_api.py; edit ROUTES if they move."""
import time

import httpx

from .env import env, strip, tls_verify

ROUTES = {
    "login": "/login",                      # form POST {email,password} → 303 (401 on bad password)
    "logout": "/logout",
    "me": "/api/v1/me",
    "users": "/api/v1/users",               # POST {email,password,role,groups} → 201 {id,...}
    "user": "/api/v1/users/{id}",
    "groups": "/api/v1/groups",             # POST {name,repo_url,ref} → 201
    "group": "/api/v1/groups/{group}",
    "zones": "/api/v1/zones",               # POST {name,provider,region} → 201 (super admin)
    "zone": "/api/v1/zones/{zone}",
    "environments": "/api/v1/groups/{group}/environments",   # POST {name,ref,zones:[...]} → 201
    "environment": "/api/v1/groups/{group}/environments/{env}",
    "environments_all": "/api/v1/environments",              # ?group=
    "deploy": "/api/v1/groups/{group}/environments/{env}/deploy",  # POST {canary,zone?} → 202 {id,status}
    "job": "/api/v1/jobs/{id}",
    "workers": "/api/v1/groups/{group}/zones/{zone}/workers",       # GET {live:[{load}],count,size}
    "rebalance": "/api/v1/groups/{group}/zones/{zone}/rebalance",
    "ip_rules": "/api/v1/groups/{group}/zones/{zone}/ip-rules",             # PUT {cidrs:[...]}
    "service_account": "/api/v1/groups/{group}/zones/{zone}/service-account",  # POST → {name,...} (super admin)
    "sa_restrictions": "/api/v1/groups/{group}/sa-restrictions",           # PUT {rules:[...]}
    "env_verbose": "/api/v1/groups/{group}/environments/{env}/verbose",    # POST {verbose}
    "secrets": "/api/v1/groups/{group}/secrets",             # POST {name,value,env?,zone?} → 201 {id,name}
    "secret": "/api/v1/groups/{group}/secrets/{id}",
    "mcp_keys": "/api/v1/groups/{group}/mcp-keys",           # POST {name} → 201 {id,key:"rmk_..."}
    "mcp_key": "/api/v1/groups/{group}/mcp-keys/{id}",
    "api_keys": "/api/v1/api-keys",                          # POST {name,role?,groups?} → 201 {id,key:"rmn_..."}
    "api_key": "/api/v1/api-keys/{id}",
    "audit": "/api/v1/audit",
    "backups": "/api/v1/backups",                            # POST {target:"local"} → 201 {id,release_version}
    "backup_download": "/api/v1/backups/{id}/download",
    "config": "/api/v1/config",
    "config_reload": "/api/v1/config/reload",
    "sa_rules": "/api/v1/config/sa-rules",
    "refresh": "/api/v1/refresh",
    "dashboard": "/api/v1/dashboard",
    "logs": "/api/v1/logs",                                  # ?group&zone&worker?&tail&download=1 → text/plain
    "healthz": "/healthz",
}
API_KEY_HEADER = "X-Ramen-Api-Key"


class Console:
    def __init__(self, base: str, api_key: str | None = None, timeout: float = 60):
        headers = {API_KEY_HEADER: api_key} if api_key else {}
        self.http = httpx.Client(base_url=strip(base), verify=tls_verify(), timeout=timeout, headers=headers)
        self.email: str | None = None

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

    def get(self, key: str, **kw) -> httpx.Response:
        params = kw.pop("params", None)
        return self.http.get(self.url(key, **kw), params=params)

    def post(self, key: str, body: dict | None = None, **kw) -> httpx.Response:
        return self.http.post(self.url(key, **kw), json=body if body is not None else {})

    def put(self, key: str, body: dict, **kw) -> httpx.Response:
        return self.http.put(self.url(key, **kw), json=body)

    def delete(self, key: str, **kw) -> httpx.Response:
        return self.http.delete(self.url(key, **kw))

    def page(self, path: str) -> httpx.Response:
        return self.http.get(path)

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
        return self.post("environments", {"name": name, "ref": ref, "zones": list(zones)}, group=group)

    def deploy(self, group, env, canary=True, zone=None):
        body = {"canary": canary}
        if zone:
            body["zone"] = zone
        return self.post("deploy", body, group=group, env=env)

    def wait_job(self, job_id: str, timeout: float = 300) -> dict:
        deadline = time.monotonic() + timeout
        job = {}
        while time.monotonic() < deadline:
            r = self.get("job", id=job_id)
            assert r.status_code == 200, f"job {job_id}: {r.status_code} {r.text[:300]}"
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

    def create_api_key(self, name, groups=None, role=None):
        body = {"name": name}
        if groups is not None:
            body["groups"] = groups
        if role:
            body["role"] = role
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
