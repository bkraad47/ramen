"""Read-only kubectl helpers for the kind proofs (CONTRACTS §12.3).

Only `tests/kind` uses these: the cloud suites assert through the console API and gcloud, never kubectl.
Every helper skips (never fails) when kubectl or the cluster is not there.
"""

import json
import shutil
import subprocess

import pytest

from . import env as E


def context() -> str:
    return E.env("KIND_CONTEXT") or f"kind-{E.env('KIND_CLUSTER', 'ramen')}"


def ns_name(group: str, zone: str) -> str:
    """Zone = namespace `ramen-<group>-<zone>` (CONTRACTS §7)."""
    return f"ramen-{group}-{zone}"


def available() -> bool:
    if shutil.which("kubectl") is None:
        return False
    r = subprocess.run(
        ["kubectl", "--context", context(), "version", "-o", "json"], capture_output=True, text=True, timeout=30
    )
    return r.returncode == 0


def require() -> None:
    if not available():
        pytest.skip(f"kubectl context {context()} not reachable (make kind-up)")


def kubectl(*args: str, timeout: float = 60) -> str:
    r = subprocess.run(
        ["kubectl", "--context", context(), *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert r.returncode == 0, f"kubectl {' '.join(args)} failed: {r.stderr.strip()[:400]}"
    return r.stdout


def get(kind: str, name: str | None = None, ns: str | None = None) -> dict:
    args = ["get", kind]
    if name:
        args.append(name)
    if ns:
        args += ["-n", ns]
    return json.loads(kubectl(*args, "-o", "json"))


def deployment(ns: str, name: str = "worker") -> dict:
    return get("deployment", name, ns)


def replicas(ns: str, name: str = "worker") -> tuple[int, int]:
    """(spec.replicas, status.readyReplicas)."""
    d = deployment(ns, name)
    return int(d["spec"].get("replicas", 0)), int((d.get("status") or {}).get("readyReplicas", 0))


def hpa(ns: str, name: str = "worker") -> dict:
    return get("horizontalpodautoscaler", name, ns)


def hpa_bounds(ns: str, name: str = "worker") -> tuple[int, int]:
    h = hpa(ns, name)
    return int(h["spec"]["minReplicas"]), int(h["spec"]["maxReplicas"])


def pods(ns: str, selector: str = "app=worker") -> list[dict]:
    args = ["get", "pods", "-n", ns] + (["-l", selector] if selector else [])
    out = json.loads(kubectl(*args, "-o", "json"))
    return [p for p in out.get("items", []) if not p["metadata"].get("deletionTimestamp")]


def pod_nodes(ns: str, selector: str = "app=worker") -> list[str]:
    return sorted(p["spec"].get("nodeName", "?") for p in pods(ns, selector))


def node_zone(node: str) -> str | None:
    return (get("node", node)["metadata"].get("labels") or {}).get("topology.kubernetes.io/zone")


def secret_keys(ns: str, name: str = "ramen-deploy") -> list[str]:
    """Key names only. Values are never read: a test must not be able to print a worker key."""
    return sorted((get("secret", name, ns).get("data") or {}).keys())


def metrics_ready() -> bool:
    r = subprocess.run(
        ["kubectl", "--context", context(), "top", "pods", "-n", "ramen-system"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return r.returncode == 0
