"""kubectl helpers for the kind proofs (CONTRACTS §12.3): read-only, but for the kind-only writers at the end.

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


def hpa_names(ns: str) -> list[str]:
    """HorizontalPodAutoscalers in a namespace. Since 0.7.5 (CONTRACTS §21.7) the console scales workers itself and
    writes none, so a zone namespace must answer []."""
    return sorted(h["metadata"]["name"] for h in get("horizontalpodautoscaler", ns=ns).get("items", []))


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


# -- v1.0.0 (§24): the only helpers that write, and only ever to a kind cluster -----------------------------------
def kind_context() -> str:
    """The context for a write. A kubeconfig can hold cloud contexts next to kind's: never touch one of those."""
    ctx = context()
    assert ctx.startswith("kind-"), f"refusing to write to kube context {ctx!r}: test fixtures go to kind only"
    return ctx


def apply(manifest: str, timeout: float = 60) -> None:
    r = subprocess.run(
        ["kubectl", "--context", kind_context(), "apply", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert r.returncode == 0, f"kubectl apply failed: {r.stderr.strip()[:400]}"


def write(*args: str, timeout: float = 120) -> str:
    """A mutating kubectl call (`create`, `delete`, `rollout`), kind contexts only."""
    r = subprocess.run(
        ["kubectl", "--context", kind_context(), *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert r.returncode == 0, f"kubectl {' '.join(args[:4])} failed: {r.stderr.strip()[:400]}"
    return r.stdout


def logs(ns: str, pod: str, container: str | None = None, tail: int = 2000) -> str:
    args = ["logs", "-n", ns, pod, f"--tail={tail}"] + (["-c", container] if container else [])
    return kubectl(*args, timeout=120)


def deployment_or_none(ns: str, name: str) -> dict | None:
    r = subprocess.run(
        ["kubectl", "--context", context(), "get", "deployment", name, "-n", ns, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return json.loads(r.stdout) if r.returncode == 0 else None
