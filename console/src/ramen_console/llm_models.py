"""1.0.0c: the model ids a configured LLM provider offers, for the Allowed models control. Asked live (5 s), cached
10 min per scope and provider; Vertex falls back to the Gemini ids the console knows (the Gemini list needs a key the
console has only as `GOOGLE_API_KEY`), Bedrock to its static list when the console has no AWS credentials; any other
failure is `unavailable` with a short reason. Keys travel in request headers only — never in a log or the answer."""

import asyncio
import os
import time
from urllib.parse import urlsplit

import httpx

from . import llm_providers as lp
from . import tls

TRANSPORT = None  # tests point the HTTP calls at a fake provider
TIMEOUT = 5.0
TTL = 600.0
ANTHROPIC_VERSION = "2023-06-01"
AZURE_API_VERSION = "2024-10-21"
OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1/models"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
STATIC = {
    "vertex": [
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
    ],
    "bedrock": [
        "anthropic.claude-3-haiku-20240307-v1:0",
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "anthropic.claude-sonnet-4-20250514-v1:0",
        "amazon.nova-micro-v1:0",
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
        "meta.llama3-3-70b-instruct-v1:0",
        "mistral.mistral-large-2407-v1:0",
    ],
}
_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


class Static(Exception):
    """The console cannot ask this provider; the static list stands in."""


def forget(scope: str, provider: str | None = None) -> None:
    """Drop the cached list after a save (the credential or base URL may have changed)."""
    for k in [k for k in _cache if k[0] == scope and provider in (None, k[1])]:
        _cache.pop(k, None)


def request_for(provider: str, entry: dict) -> tuple[str, dict[str, str]] | None:
    """The GET that lists the provider's models, or None when the console cannot ask (Vertex without a key)."""
    key = entry.get("api_key", "")
    if provider == "openai":
        return f"{entry.get('base_url') or OPENAI_URL}/models", {"Authorization": f"Bearer {key}"}
    if provider == "openai_compatible":
        headers = dict(lp.headers_of(entry))
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return f"{entry['base_url']}/models", headers
    if provider == "anthropic":
        return ANTHROPIC_URL, {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if provider in ("azure_openai", "foundry"):
        base = entry["endpoint"] if provider == "foundry" else f"https://{entry['host']}"
        return f"{base}/openai/deployments?api-version={AZURE_API_VERSION}", {"api-key": key}
    if provider == "vertex" and (gkey := os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        return f"{GEMINI_URL}?pageSize=1000", {"x-goog-api-key": gkey}
    return None


def parse(provider: str, data) -> list[str]:
    """OpenAI, Anthropic, a compatible server and the Azure deployments list answer `data[].id`; Gemini
    `models[].name` as `models/<id>`."""
    rows = data.get("models" if provider == "vertex" else "data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no model list in the answer")
    ids = [str(r.get("name" if provider == "vertex" else "id") or "") for r in rows if isinstance(r, dict)]
    ids = [i.removeprefix("models/") for i in ids]
    return sorted({i for i in ids if lp.MODEL.match(i)})


def boto_models(region: str) -> list[str] | None:
    """`bedrock:ListFoundationModels` with the console's own AWS credentials; None when it has none."""
    import boto3

    session = boto3.Session(region_name=region)
    if session.get_credentials() is None:
        return None
    out = session.client("bedrock").list_foundation_models()
    return sorted({m["modelId"] for m in out.get("modelSummaries", []) if lp.MODEL.match(m.get("modelId", ""))})


async def addresses(host: str) -> list[str]:
    """What `host` resolves to (an IP literal: itself; [] when it does not resolve — the GET then fails on its own)."""
    if TRANSPORT is not None:  # tests answer from a fake provider, never DNS
        return []
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return []
    return [i[4][0] for i in infos]


async def _ask(provider: str, entry: dict) -> list[str]:
    if provider == "bedrock":
        models = await asyncio.wait_for(asyncio.to_thread(boto_models, entry["region"]), TIMEOUT)
        if models is None:
            raise Static("no AWS credentials on the console")
        return models
    req = request_for(provider, entry)
    if req is None:
        raise Static("no GOOGLE_API_KEY on the console")
    url, headers = req
    host = urlsplit(url).hostname or ""
    if lp.metadata_host(host) or any(lp.metadata_host(a) for a in await addresses(host)):
        raise ValueError("the URL resolves to the cloud metadata service: refused")
    async with httpx.AsyncClient(verify=tls.client_verify(), transport=TRANSPORT, timeout=TIMEOUT) as http:
        r = await http.get(url, headers=headers)
    if r.status_code >= 300:
        raise ValueError(f"HTTP {r.status_code}")
    return parse(provider, r.json())


async def list_models(cfg: dict, provider: str, scope: str) -> dict:
    """`{"models": [ids], "source": "live"|"cached"|"static"|"unavailable", "error"?: str}` for one provider of one
    scope (`"org"` or `"group:<g>"`). A provider that is not set is `unavailable`; Vertex and Bedrock answer their
    static list (`"static"`) when the console cannot ask."""
    if not lp.is_configured(cfg, provider):
        return {"models": [], "source": "unavailable", "error": "provider not set"}
    entry = cfg[provider]
    hit = _cache.get((scope, provider))
    if hit and hit[0] > time.monotonic():
        return {"models": list(hit[1]), "source": "cached"}
    try:
        models = await _ask(provider, entry)
    except Static as e:
        return {"models": list(STATIC[provider]), "source": "static", "error": str(e)}
    except Exception as e:  # a timeout, a refused connection, a 401, a malformed answer: the class name, never a body
        reason = str(e) if isinstance(e, ValueError) and not isinstance(e, httpx.HTTPError) else e.__class__.__name__
        return {"models": list(STATIC.get(provider, [])), "source": "unavailable", "error": reason[:120]}
    _cache[(scope, provider)] = (time.monotonic() + TTL, models)
    return {"models": models, "source": "live"}
