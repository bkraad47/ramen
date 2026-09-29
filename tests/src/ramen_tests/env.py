"""Environment lookup. Missing env → pytest.skip, never a failure."""

import os
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[2]
RAMEN_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
DEMO_REPO = "https://github.com/bkraad47/ramen-demo-mcp-group"


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def require(name: str) -> str:
    v = env(name)
    if v is None:
        pytest.skip(f"{name} not set")
    return v


def tls_verify() -> bool:
    return env("RAMEN_TLS_INSECURE", "0") != "1"


def strip(url: str) -> str:
    return url.rstrip("/")


def node_target(url: str) -> tuple[str, bool]:
    """RAMEN_NODE_URL → (host:port, tls). CONTRACTS §11: the node/LB is a gRPC target, not a URL. Accepted spellings:
    `host:port`, `grpc://host:port` (plaintext), `grpcs://`/`https://` (TLS); RAMEN_NODE_TLS=1 forces TLS.
    Any path is dropped (routing is by metadata, not by path)."""
    u = strip(url).strip()
    tls = env("RAMEN_NODE_TLS", "0") == "1"
    for scheme, is_tls in (("grpcs://", True), ("https://", True), ("grpc://", False), ("http://", False)):
        if u.startswith(scheme):
            u, tls = u[len(scheme) :], tls or is_tls
            break
    host = u.split("/", 1)[0]
    if ":" not in host.rsplit("]", 1)[-1]:
        host += ":443" if tls else ":8080"
    return host, tls


def node_http_url(url: str) -> str:
    """RAMEN_NODE_URL → the Streamable HTTP endpoint (§16.1): scheme from the spelling (grpcs/https → https), the
    same host:port, path `/mcp` (an explicit path in the URL is kept)."""
    host, tls = node_target(url)
    u = strip(url).strip()
    path = "/mcp"
    for scheme in ("grpcs://", "https://", "grpc://", "http://"):
        if u.startswith(scheme):
            rest = u[len(scheme) :]
            if "/" in rest and rest.split("/", 1)[1]:
                path = "/" + rest.split("/", 1)[1]
            break
    return f"{'https' if tls else 'http'}://{host}{path}"


def routing_metadata() -> list[tuple[str, str]]:
    """`ramen-group` / `ramen-zone` sent on every call so an LB can route by headers (§11)."""
    return [("ramen-group", env("RAMEN_E2E_GROUP", "demo")), ("ramen-zone", env("RAMEN_E2E_ZONE", "local"))]


def no_cloud(feature: str) -> bool:
    """`RAMEN_NO_CLOUD=iam,logs,...` — cloud services the target deployment genuinely does not have.

    Only `deploy/kind/test.sh` sets it: a kind cluster runs the GCP adapter's Kubernetes paths but has no IAM,
    Cloud Logging, Cloud Armor or compute API. Cases that need one of those then **skip with a reason** instead of
    failing, and stay failures everywhere else, including GKE. Never set it for a cloud run.
    """
    wanted = {f.strip().lower() for f in (env("RAMEN_NO_CLOUD", "") or "").split(",") if f.strip()}
    return feature.lower() in wanted
