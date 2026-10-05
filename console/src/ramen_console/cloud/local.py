import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

from .. import grpcclient
from ..grpcclient import GrpcError, target_of
from .base import Cloud, GateError


def git_env(token: str | None) -> dict[str, str]:
    """Credentials for one git invocation only: `http.extraheader` via GIT_CONFIG_* env (never argv, never the remote
    URL, never `.git/config`). SEC-12."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
        )
    return env


def run_git(cmd: list[str], token: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=git_env(token))


def redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


# Never written into the bucket's `.ramen/env*`: the runtime reads the bucket, and these are the node's alone. The
# local worker takes them from its own environment instead (docker-compose.yml). Security review 0.5.0 L6.
NODE_ONLY = ("RAMEN_SESSION_SECRET", "RAMEN_OAUTH_ISSUER", "RAMEN_PUBLIC_URL")


class LocalCloud(Cloud):
    def __init__(
        self,
        bucket_root,
        log_root=None,
        workers=None,
        default_worker="worker:8080",
        admin_key="",
        rpc=None,
        timeout=10.0,
    ):
        self.root = Path(bucket_root)
        self.log_root = Path(log_root) if log_root else self.root / "_logs"
        self.workers_map = {k: [target_of(u)[0] for u in v] for k, v in (workers or {}).items()}
        self.default_worker, self.admin_key = target_of(default_worker)[0], admin_key
        self.rpc = rpc or grpcclient.Client(deadline=timeout)

    @classmethod
    def from_env(cls):
        workers = {}
        for item in filter(None, os.environ.get("RAMEN_LOCAL_WORKERS", "").split(",")):
            k, _, v = item.partition("=")
            workers[k.strip()] = [u.strip() for u in v.split("|") if u.strip()]
        return cls(
            os.environ.get("RAMEN_BUCKET_ROOT", "./buckets"),
            os.environ.get("RAMEN_LOG_ROOT"),
            workers,
            os.environ.get("RAMEN_WORKER_URL", "worker:8080"),
            os.environ.get("RAMEN_ADMIN_KEY", ""),
            rpc=grpcclient.Client(
                tls=worker_tls_from_env(), deadline=float(os.environ.get("RAMEN_WORKER_DEADLINE", 10))
            ),
        )

    def _bucket(self, group: str) -> Path:
        return self.root / group

    def _targets(self, group, zone) -> list[str]:
        return self.workers_map.get(f"{group}/{zone}") or [self.default_worker]

    async def sync_repo(self, group, repo_url, ref, token):
        dest = self._bucket(group)
        if (dest / ".git").exists():
            cmds = [
                ["git", "-C", str(dest), "remote", "set-url", "origin", repo_url],
                ["git", "-C", str(dest), "fetch", "-q", "origin", ref],
                ["git", "-C", str(dest), "checkout", "-q", "-f", "FETCH_HEAD"],
            ]
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            cmds = [["git", "clone", "-q", "--depth", "1", "--branch", ref, repo_url, str(dest)]]
        for cmd in cmds:
            r = await asyncio.to_thread(run_git, cmd, token)
            if r.returncode != 0:
                raise RuntimeError(f"git failed: {redact(r.stderr, token)}".strip())
        return str(dest)

    def _rules_path(self, group, zone) -> Path:
        return self._bucket(group) / ".ramen" / f"ip_rules_{zone}.json"

    async def set_ip_rules(self, group, zone, cidrs):
        p = self._rules_path(group, zone)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cidrs))
        return {"ok": True, "cidrs": cidrs}

    async def read_file(self, group, path):
        p = self._bucket(group) / path
        return p.read_bytes() if p.is_file() else None

    async def deploy(self, group, env, zone, canary=True, config=None, spec=None, log=None, gate=None):
        vars_ = {
            "RAMEN_GROUP": group,
            "RAMEN_ENV": env,
            "RAMEN_ZONE": zone,
            "RAMEN_CANARY": "1" if canary else "0",
            "RAMEN_BUCKET": str(self._bucket(group)),
            **(config or {}),
        }
        rp = self._rules_path(group, zone)
        if rp.exists():
            vars_["RAMEN_ALLOWED_CIDRS"] = ",".join(json.loads(rp.read_text()))
        d = self._bucket(group) / ".ramen"
        d.mkdir(parents=True, exist_ok=True)
        text = "".join(f"{k}={v}\n" for k, v in vars_.items() if k not in NODE_ONLY)
        (d / "env").write_text(text)
        (d / f"env-{zone}").write_text(text)
        results = []
        mcp_key = (vars_.get("RAMEN_MCP_KEYS") or "").split(",")[0].strip() or None
        for target in self._targets(group, zone):
            try:
                body = await self.rpc.reload(target, self.admin_key)
                if gate:  # no canary track here: the gate runs on every worker's reload (0.7.0)
                    await gate(body, self._caller(target, mcp_key, group, zone) if mcp_key else None)
                results.append({"id": target, "ok": True, "status": "OK", "result": body})
            except GateError as e:
                results.append({"id": target, "ok": False, "status": "GATE", "error": str(e), "result": body})
            except (GrpcError, ValueError) as e:
                results.append(
                    {"id": target, "ok": False, "status": getattr(e, "code", None) and e.code.name, "error": str(e)}
                )
        return {"ok": all(w["ok"] for w in results), "workers": results, "env_file": str(d / f"env-{zone}")}

    def _caller(self, target, mcp_key, group, zone):
        async def call(msg):
            return await self.rpc.call(target, mcp_key, group, zone, msg)

        return call

    async def workers(self, group, zone):
        out = []
        for target in self._targets(group, zone):
            try:
                m = await self.rpc.metrics(target, self.admin_key)
                out.append({"id": target, "load": m.get("load", "unknown"), "metrics": m})
            except (GrpcError, ValueError) as e:
                out.append({"id": target, "load": "down", "metrics": {}, "error": f"{type(e).__name__}: {e}"})
        return out

    async def logs(self, group, zone, worker=None, tail=500):
        d = self.log_root / group / zone
        if worker:
            name = worker.replace("://", "_").replace("/", "_").replace(":", "_")
            files = [d / f"{name}.log"]
        else:
            # "All workers": every log file in the zone, not just one hardcoded name (a real zone can have
            # more than one worker, each mirroring RAMEN_LOG_FILE to its own path)
            files = sorted(d.glob("*.log")) if d.is_dir() else []
        lines = []
        for p in files:
            if p.exists():
                lines.extend(p.read_text().splitlines(keepends=True))
        return "".join(lines[-tail:])

    async def rebalance(self, group, zone):
        return {"ok": True, "note": "local adapter: no load balancer to rebalance"}

    async def detach_zone(self, group, zone):
        removed = []
        for p in (self._bucket(group) / ".ramen").glob(f"*_{zone}.json"):
            p.unlink()
            removed.append(str(p))
        return {"ok": True, "removed": removed, "note": "local adapter: the compose worker is not managed per zone"}

    async def apply_sa_permissions(self, group, zone, permissions, scopes=None, previous=None):
        p = self._bucket(group) / ".ramen" / f"sa_permissions_{zone}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(permissions))
        return {
            "ok": True,
            "applied": [],
            "permissions": list(permissions),
            "scopes": dict(scopes or {}),
            "recorded": str(p),
            "note": "local adapter: recorded only, no cloud IAM",
        }

    async def create_service_account(self, group, zone):
        return {"name": f"local-sa-{group}-{zone}", "note": "local adapter: no cloud IAM"}

    async def refresh(self):
        groups = (
            sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("_"))
            if self.root.exists()
            else []
        )
        return {"groups": groups, "workers": {k: v for k, v in self.workers_map.items()}}


def worker_tls_from_env():
    """RAMEN_WORKER_TLS=1 → TLS to nodes (RAMEN_WORKER_CA / _CERT / _KEY as PEM paths); unset → h2c."""
    if os.environ.get("RAMEN_WORKER_TLS", "0") != "1":
        return None
    tls = {k: os.environ.get(f"RAMEN_WORKER_{k.upper()}") for k in ("ca", "cert", "key")}
    return {k: v for k, v in tls.items() if v} or True
