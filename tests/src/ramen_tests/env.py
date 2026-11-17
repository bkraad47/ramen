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


def install_ca() -> str | None:
    """D86 (CONTRACTS §39): RAMEN_INSTALL_CA = the install CA bundle of a stack whose every hop is TLS (local compose:
    deploy/local/.certs/ca/ca.crt; kind: ConfigMap ramen-ca, saved by deploy/kind/test.sh). Set, workers and the
    console are dialled over TLS and verified against it."""
    return env("RAMEN_INSTALL_CA") or None


def console_verify() -> bool | str:
    return install_ca() or tls_verify()


def engines_auth() -> tuple[str, str] | None:
    """D92 (CONTRACTS §39.7): Basic credentials for Jaeger's / promptfoo's TLS front — `RAMEN_ENGINES_PASSWORD` (Secret
    ramen-engines `password`; compose: deploy/local/.env)."""
    pw = env("RAMEN_ENGINES_PASSWORD")
    return ("admin", pw) if pw else None


def phoenix_headers() -> dict[str, str]:
    """D92: Phoenix's REST/GraphQL take its admin secret as a bearer (`RAMEN_PHOENIX_ADMIN_SECRET`, Secret ramen-engines
    `phoenix-admin-secret`)."""
    s = env("RAMEN_PHOENIX_ADMIN_SECRET")
    return {"authorization": f"Bearer {s}"} if s else {}


def strip(url: str) -> str:
    return url.rstrip("/")


def node_target(url: str) -> tuple[str, bool]:
    """RAMEN_NODE_URL → (host:port, tls). CONTRACTS §11: the node/LB is a gRPC target, not a URL. Accepted spellings:
    `host:port`, `grpc://host:port` (plaintext), `grpcs://`/`https://` (TLS); RAMEN_NODE_TLS=1 forces TLS.
    Any path is dropped (routing is by metadata, not by path)."""
    u = strip(url).strip()
    tls = env("RAMEN_NODE_TLS", "0") == "1" or install_ca() is not None
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


def edge_url(host: str) -> str:
    """A cluster's edge hostname (CONTRACTS §28.1: a zone's `host`, the satellite's own address + certificate) → the
    spelling `node_target`/`node_http_url` read. A bare host is TLS on 443, like every Ramen edge; a scheme is kept."""
    h = strip(host).strip()
    return h if "://" in h else f"https://{h}"


def edge_ca(host: str | None) -> str | None:
    """The PEM to verify an edge against: RAMEN_NODE_CA for the home edge (`host` None), RAMEN_SATELLITE_CA for a
    satellite's (an EKS satellite's ALB serves its own certificate); None = the system store."""
    return env("RAMEN_SATELLITE_CA") if host else (env("RAMEN_NODE_CA") or install_ca())


def region_of(provider: str, zone_region: str) -> str:
    """A zone's region: GCP `us-central1-a` → `us-central1`, AWS `ap-southeast-2a` → `ap-southeast-2`, Azure
    `australiaeast-1` → `australiaeast` (console `regions.zone_name` read backwards)."""
    if provider == "aws":
        return zone_region.rstrip("abcdefghijklmnopqrstuvwxyz")
    return zone_region.rsplit("-", 1)[0]


def zone_in(provider: str, region: str, letter: str) -> str:
    return f"{region}{letter}" if provider == "aws" else f"{region}-{letter}"


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
