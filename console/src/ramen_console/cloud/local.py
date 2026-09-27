import asyncio
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .base import Cloud


class LocalCloud(Cloud):
    def __init__(self, bucket_root, log_root=None, workers=None, default_worker="http://worker:8080",
                 admin_key="", transport=None, timeout=10.0):
        self.root = Path(bucket_root)
        self.log_root = Path(log_root) if log_root else self.root / "_logs"
        self.workers_map = workers or {}
        self.default_worker, self.admin_key = default_worker, admin_key
        self._client_kw = {"transport": transport, "timeout": timeout}

    @classmethod
    def from_env(cls):
        workers = {}
        for item in filter(None, os.environ.get("RAMEN_LOCAL_WORKERS", "").split(",")):
            k, _, v = item.partition("=")
            workers[k.strip()] = [u.strip() for u in v.split("|") if u.strip()]
        return cls(os.environ.get("RAMEN_BUCKET_ROOT", "./buckets"), os.environ.get("RAMEN_LOG_ROOT"), workers,
                   os.environ.get("RAMEN_WORKER_URL", "http://worker:8080"), os.environ.get("RAMEN_ADMIN_KEY", ""))

    @staticmethod
    def auth_url(url: str, token: str | None) -> str:
        parts = urlsplit(url)
        if not token or not parts.netloc:
            return url
        return urlunsplit(parts._replace(netloc=f"x-access-token:{token}@{parts.netloc}"))

    def _bucket(self, group: str) -> Path:
        return self.root / group

    def _urls(self, group, zone) -> list[str]:
        return self.workers_map.get(f"{group}/{zone}") or [self.default_worker]

    async def sync_repo(self, group, repo_url, ref, token):
        dest = self._bucket(group)
        url = self.auth_url(repo_url, token)
        if (dest / ".git").exists():
            cmds = [["git", "-C", str(dest), "remote", "set-url", "origin", url],
                    ["git", "-C", str(dest), "fetch", "-q", "origin", ref],
                    ["git", "-C", str(dest), "checkout", "-q", "-f", "FETCH_HEAD"]]
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            cmds = [["git", "clone", "-q", "--depth", "1", "--branch", ref, url, str(dest)]]
        for cmd in cmds:
            r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"git failed: {r.stderr.replace(token, '***') if token else r.stderr}".strip())
        return str(dest)

    def _rules_path(self, group, zone) -> Path:
        return self._bucket(group) / ".ramen" / f"ip_rules_{zone}.json"

    async def set_ip_rules(self, group, zone, cidrs):
        p = self._rules_path(group, zone)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cidrs))
        return {"ok": True, "cidrs": cidrs}

    async def deploy(self, group, env, zone, canary=True, config=None, spec=None, log=None):
        vars_ = {"RAMEN_GROUP": group, "RAMEN_ENV": env, "RAMEN_ZONE": zone, "RAMEN_CANARY": "1" if canary else "0",
                 "RAMEN_BUCKET": str(self._bucket(group)), **(config or {})}
        rp = self._rules_path(group, zone)
        if rp.exists():
            vars_["RAMEN_ALLOWED_CIDRS"] = ",".join(json.loads(rp.read_text()))
        d = self._bucket(group) / ".ramen"
        d.mkdir(parents=True, exist_ok=True)
        text = "".join(f"{k}={v}\n" for k, v in vars_.items())
        (d / "env").write_text(text)
        (d / f"env-{zone}").write_text(text)
        results = []
        async with httpx.AsyncClient(**self._client_kw) as c:
            for url in self._urls(group, zone):
                try:
                    r = await c.post(f"{url}/admin/reload", headers={"X-Ramen-Admin-Key": self.admin_key})
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    results.append({"id": url, "ok": r.status_code == 200, "status": r.status_code, "result": body})
                except httpx.HTTPError as e:
                    results.append({"id": url, "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": all(w["ok"] for w in results), "workers": results, "env_file": str(d / f"env-{zone}")}

    async def workers(self, group, zone):
        out = []
        async with httpx.AsyncClient(**self._client_kw) as c:
            for url in self._urls(group, zone):
                try:
                    m = (await c.get(f"{url}/metrics")).json()
                    out.append({"id": url, "load": m.get("load", "unknown"), "metrics": m})
                except (httpx.HTTPError, ValueError) as e:
                    out.append({"id": url, "load": "down", "metrics": {}, "error": f"{type(e).__name__}: {e}"})
        return out

    async def logs(self, group, zone, worker=None, tail=500):
        name = worker.replace("://", "_").replace("/", "_").replace(":", "_") if worker else "worker"
        p = self.log_root / group / zone / f"{name}.log"
        if not p.exists():
            return ""
        lines = p.read_text().splitlines(keepends=True)
        return "".join(lines[-tail:])

    async def rebalance(self, group, zone):
        return {"ok": True, "note": "local adapter: no load balancer to rebalance"}

    async def apply_sa_permissions(self, group, zone, permissions):
        p = self._bucket(group) / ".ramen" / f"sa_permissions_{zone}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(permissions))
        return {"ok": True, "applied": [], "permissions": list(permissions), "recorded": str(p),
                "note": "local adapter: recorded only, no cloud IAM"}

    async def create_service_account(self, group, zone):
        return {"name": f"local-sa-{group}-{zone}", "note": "local adapter: no cloud IAM"}

    async def refresh(self):
        groups = sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("_")) if self.root.exists() else []
        return {"groups": groups, "workers": {k: v for k, v in self.workers_map.items()}}
