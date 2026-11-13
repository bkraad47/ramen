"""§24.3 OpenTelemetry: the `config/otel` settings, what workers and connected agents are told, and the console's own
span — one per deploy job, OTLP/HTTP JSON, sent from the deploy's background task; a collector that is down is logged,
never raised.

Stored doc: `{"enabled", "endpoint", "client_endpoint", "otel_headers", "otel_client_headers"}` — the two header maps
as JSON strings so the store encrypts them (storage/encrypted.py) and backups drop them like every secret.
"""

import json
import logging
import re
import secrets
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from . import __version__
from .errors import invalid

log = logging.getLogger("ramen.otel")
DOC = "otel"
TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
TRANSPORT = None  # tests point the exporter at a fake collector
TIMEOUT = 5.0


def _headers(raw) -> dict[str, str]:
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in doc.items()} if isinstance(doc, dict) else {}


async def get_config(store) -> dict:
    """The settings with both header maps decoded. Never return this to a client: use `public_config`."""
    doc = await store.get("config", DOC) or {}
    return {
        "enabled": bool(doc.get("enabled")),
        "endpoint": doc.get("endpoint") or "",
        "client_endpoint": doc.get("client_endpoint") or "",
        "headers": _headers(doc.get("otel_headers")),
        "client_headers": _headers(doc.get("otel_client_headers")),
    }


def clean_url(url: str, what: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or any(c.isspace() for c in url) or u.query or u.fragment:
        raise invalid(f"{what} must be an http(s):// OTLP/HTTP base URL like http://jaeger.ramen-system:4318")
    return url


def clean_headers(headers, what: str) -> dict[str, str]:
    if not isinstance(headers, dict):
        raise invalid(f"{what} must be a mapping of header name to value")
    out = {}
    for k, v in headers.items():
        k, v = str(k).strip(), str(v)
        if not TOKEN.match(k) or "\r" in v or "\n" in v:
            raise invalid(f"{what}: {k!r} is not a usable HTTP header")
        out[k] = v
    return out


async def set_config(store, *, enabled=None, endpoint=None, headers=None, client_endpoint=None, client_headers=None):
    cfg = await get_config(store)
    if endpoint is not None:
        cfg["endpoint"] = clean_url(endpoint, "endpoint")
    if client_endpoint is not None:
        cfg["client_endpoint"] = clean_url(client_endpoint, "client_endpoint")
    if headers is not None:
        cfg["headers"] = clean_headers(headers, "headers")
    if client_headers is not None:
        cfg["client_headers"] = clean_headers(client_headers, "client_headers")
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if cfg["enabled"] and not cfg["endpoint"]:
        raise invalid("Telemetry needs an endpoint before it can be turned on")
    await store.put(
        "config",
        DOC,
        {
            "enabled": cfg["enabled"],
            "endpoint": cfg["endpoint"],
            "client_endpoint": cfg["client_endpoint"],
            "otel_headers": json.dumps(cfg["headers"]) if cfg["headers"] else "",
            "otel_client_headers": json.dumps(cfg["client_headers"]) if cfg["client_headers"] else "",
        },
    )
    return cfg


def public_config(cfg: dict) -> dict:
    """Header values are write-only: only whether each map is set, and its header names, come back."""
    return {
        "enabled": cfg["enabled"],
        "endpoint": cfg["endpoint"],
        "client_endpoint": cfg["client_endpoint"],
        "headers_set": bool(cfg["headers"]),
        "header_names": sorted(cfg["headers"]),
        "client_headers_set": bool(cfg["client_headers"]),
        "client_header_names": sorted(cfg["client_headers"]),
    }


def host(url: str) -> str:
    u = urlsplit(url or "")
    return u.netloc.rsplit("@", 1)[-1] if u.netloc else ""


def client_info(cfg: dict, group: str, zone: str) -> dict | None:
    """What a connected agent is told to use — the client endpoint and headers, never Ramen's own `headers`."""
    if not cfg["enabled"]:
        return None
    return {
        "otlp_endpoint": cfg["client_endpoint"] or cfg["endpoint"],
        "headers": dict(cfg["client_headers"]),
        "resource_attributes": {"ramen.group": group, "ramen.zone": zone},
    }


def consent_host(cfg: dict, group_doc: dict) -> str | None:
    """The host named in the consent notice when this group requires telemetry consent, else None."""
    if not cfg["enabled"] or not group_doc.get("require_telemetry_consent"):
        return None
    return host(cfg["client_endpoint"] or cfg["endpoint"])


def worker_env(cfg: dict, group_doc: dict, group: str, zone: str) -> dict[str, str]:
    """The deploy config a zone's workers get on their next deploy; nothing when telemetry is off."""
    if not cfg["enabled"]:
        return {}
    env = {
        "RAMEN_OTEL_ENDPOINT": cfg["endpoint"],
        "RAMEN_OTEL_HEADERS": json.dumps(cfg["headers"], sort_keys=True, separators=(",", ":")),
        "RAMEN_OTEL_CLIENT": json.dumps(client_info(cfg, group, zone), sort_keys=True, separators=(",", ":")),
    }
    if (notice := consent_host(cfg, group_doc)) is not None:
        env["RAMEN_TELEMETRY_CONSENT"] = "1"
        env["RAMEN_TELEMETRY_NOTICE"] = notice
    return env


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, (list, tuple)):
        return {"key": key, "value": {"arrayValue": {"values": [{"stringValue": str(v)} for v in value]}}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _nanos(iso: str | None) -> str:
    try:
        return str(int(datetime.fromisoformat(iso).timestamp() * 1_000_000_000))
    except (TypeError, ValueError):
        return "0"


def deploy_span(job: dict, group: str, env: str, zones: list[str], image: str | None) -> dict:
    """The OTLP/JSON `ExportTraceServiceRequest` for one finished deploy job."""
    results = job.get("result") or {}
    golden = [r.get("golden") or {} for r in results.values() if isinstance(r, dict)]
    compats = [r.get("compat") or {} for r in results.values() if isinstance(r, dict)]
    compat = (
        "breaking"
        if any(c.get("breaking") for c in compats)
        else ("additive" if any(c.get("additive") for c in compats) else "none")
    )
    ok = job.get("status") == "ok"
    attrs = [
        _attr("ramen.group", group),
        _attr("ramen.env", env),
        _attr("ramen.zones", list(zones)),
        _attr("ramen.outcome", "ok" if ok else "error"),
        _attr("ramen.golden.passed", sum(int(g.get("passed") or 0) for g in golden)),
        _attr("ramen.golden.failed", sum(len(g.get("failed") or []) for g in golden)),
        _attr("ramen.compat", compat),
        _attr("ramen.image", image or "base"),
    ]
    span = {
        "traceId": secrets.token_hex(16),
        "spanId": secrets.token_hex(8),
        "name": "ramen.deploy",
        "kind": 1,
        "startTimeUnixNano": _nanos(job.get("started")),
        "endTimeUnixNano": _nanos(job.get("finished")),
        "attributes": attrs,
        "status": {"code": 1} if ok else {"code": 2, "message": str(job.get("error") or "")[:500]},
    }
    resource = [_attr("service.name", "ramen-console"), _attr("service.version", __version__)]
    scope = {"name": "ramen-console", "version": __version__}
    return {
        "resourceSpans": [{"resource": {"attributes": resource}, "scopeSpans": [{"scope": scope, "spans": [span]}]}]
    }


async def export_deploy(store, job: dict, group: str, env: str, zones: list[str], image: str | None = None) -> bool:
    """Send the job's span when telemetry is on. True when the collector took it; every failure is a warning."""
    try:
        cfg = await get_config(store)
        if not cfg["enabled"] or not cfg["endpoint"]:
            return False
        body = deploy_span(job, group, env, zones, image)
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=TIMEOUT) as http:
            r = await http.post(f"{cfg['endpoint']}/v1/traces", json=body, headers=cfg["headers"])
        if r.status_code >= 400:
            log.warning("otel: the collector refused the deploy span of %s/%s: HTTP %s", group, env, r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - telemetry never fails a deploy
        log.warning("otel: could not export the deploy span of %s/%s: %s: %s", group, env, type(e).__name__, e)
        return False
