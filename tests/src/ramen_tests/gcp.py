"""gcloud shell-outs for GCP-only assertions. Every helper skips (never fails) when gcloud is unavailable."""

import json
import shutil
import subprocess

import pytest


def available() -> bool:
    return shutil.which("gcloud") is not None


def run(*args: str, project: str, timeout: float = 120) -> subprocess.CompletedProcess:
    if not available():
        pytest.skip("gcloud not on PATH")
    cmd = ["gcloud", *args, f"--project={project}", "--format=json", "--quiet"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get(*args: str, project: str, timeout: float = 120):
    """→ parsed JSON, or None when the command fails (resource absent, no permission, ...)."""
    r = run(*args, project=project, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def gsa(project: str, group: str, zone: str) -> str:
    return f"ramen-{group}-{zone}@{project}.iam.gserviceaccount.com"


def namespace(group: str, zone: str) -> str:
    return f"ramen-{group}-{zone}"


def secret_name(group: str, env: str, zone: str, name: str) -> str:
    return f"ramen-{group}-{env}-{zone}-{name}"


def roles_of(policy: dict | None, member: str) -> set[str]:
    """Roles bound to `member` in an IAM policy (conditional bindings included)."""
    return {b["role"] for b in (policy or {}).get("bindings", []) if member in b.get("members", [])}
