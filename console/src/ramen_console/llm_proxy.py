"""§38.1 (D85, F1) the LLM judge proxy: provider keys never leave the console. Workers and the bridge call
`POST /llm/v1/chat/completions` (OpenAI-compatible, no streaming) with a judge token (`rjt_`, per zone and deploy) or a
person's token of the group; the console resolves the group's provider (§36.2), enforces the allowed models, a per-group
rate limit and a token cap, adds the credential and maps the call to the provider. Bodies and keys never reach a log.

Judge token: `rjt_<b64url(group/zone/job)>.<HMAC(group session secret, payload)>` — derived like the evals key, never
stored; live while its job is a running deploy of the group or one of the zone's last two good deploys
(`last_deploy.judge[zone]`, recorded by the deploy)."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

from . import llm_models, tls
from . import llm_providers as lp

PREFIX = "rjt_"
PATH = "/llm/v1"
PER_MIN_ENV, DEFAULT_PER_MIN = "RAMEN_LLM_PROXY_PER_MIN", 120
INTERNAL_ENV = "RAMEN_CONSOLE_INTERNAL_URL"
MAX_TOKENS, MAX_N, TIMEOUT, AUDIT_EVERY = 4096, 4, 60.0, 100
MAX_BODY = 1024 * 1024
TRANSPORT = None  # tests point the upstream calls at a fake provider
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ROLES = ("system", "developer", "user", "assistant", "tool", "function")
FORWARD = (
    "messages",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "user",
)
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "content_filter"}


class Upstream(Exception):
    """The provider failed: answered as 502 `{error, upstream_status}`."""

    def __init__(self, status: int | None, why: str):
        super().__init__(why)
        self.status, self.why = status, why


class NoUrl(RuntimeError):
    def __init__(self):
        super().__init__(
            "the LLM judge proxy has no console URL: set RAMEN_CONSOLE_INTERNAL_URL, the base URI or RAMEN_PUBLIC_URL"
        )


# ---- judge tokens ----------------------------------------------------------------------------------------------------
def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _mac(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), f"judge:{payload}".encode(), hashlib.sha256).hexdigest()[:40]


def judge_token(session_secret: str, group: str, zone: str, job: str) -> str:
    payload = _b64(f"{group}/{zone}/{job}")
    return f"{PREFIX}{payload}.{_mac(session_secret, payload)}"


def judge_claims(token: str) -> tuple[str, str, str] | None:
    """(group, zone, job) as the token names them — unverified; None when it is not a judge token."""
    if not token.startswith(PREFIX) or "." not in token:
        return None
    payload = token[len(PREFIX) :].split(".", 1)[0]
    try:
        parts = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("/")
    except (ValueError, UnicodeDecodeError):
        return None
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(parts) else None


def judge_ok(token: str, session_secret: str) -> bool:
    payload, _, mac = token[len(PREFIX) :].partition(".")
    return hmac.compare_digest(_mac(session_secret, payload), mac)


def live_jobs(env: dict, zone: str) -> list[str]:
    """The deploy jobs whose judge tokens the zone's workers may hold: the recorded ones, else the last deploy's."""
    last = env.get("last_deploy") or {}
    judge = (last.get("judge") or {}).get(zone)
    if judge is not None:
        return list(judge)
    return [last["job"]] if last.get("job") else []


def judge_record(prev: dict, results: dict, job_id: str) -> dict[str, list[str]]:
    """`last_deploy.judge` after a deploy: a zone that took this job keeps it and the one before (pods of the old
    stable track drain); a zone it failed on keeps what its stable track already runs."""
    out = {z: list(v) for z, v in (prev.get("judge") or {}).items()}
    for z, r in (results or {}).items():
        before = out.get(z) if z in out else ([prev["job"]] if prev.get("job") else [])
        ok = isinstance(r, dict) and r.get("ok")
        out[z] = [job_id, *[j for j in before if j != job_id][:1]] if ok else before
    return out


async def judge_live(svc, group: str, zone: str, job: str) -> bool:
    doc = await svc.store.get("jobs", job)
    if doc and doc.get("kind") == "deploy" and doc.get("status") == "running":
        if str(doc.get("target") or "").startswith(f"{group}/"):  # the canary of the deploy now running
            return True
    for env in await svc.environments(group):
        if zone in (env.get("zones") or []) and job in live_jobs(env, zone):
            return True
    return False


def proxy_url(issuer: str) -> str:
    """Where workers reach the proxy: the console's in-cluster Service (the worker egress policy allows the console
    pods), else its public URL; "" when neither is known."""
    base = (os.environ.get(INTERNAL_ENV) or "").strip() or (issuer or "").strip()
    return f"{base.rstrip('/')}{PATH}" if base else ""


# ---- limits -------------------------------------------------------------------------------------------------------
def per_min() -> int:
    try:
        return int(os.environ.get(PER_MIN_ENV, "") or DEFAULT_PER_MIN)
    except ValueError:
        return DEFAULT_PER_MIN


class Limiter:
    """Requests per group: this minute's count and the running total (for the audit cadence). Per replica."""

    def __init__(self, window: float = 60.0):
        self.window, self.lock = window, threading.Lock()
        self.hits: dict[str, tuple[float, int]] = {}
        self.totals: dict[str, int] = {}

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()
            self.totals.clear()

    def count(self, key: str) -> tuple[int, int]:
        now = time.monotonic()
        with self.lock:
            start, n = self.hits.get(key, (now, 0))
            if now - start > self.window:
                start, n = now, 0
            self.hits[key] = (start, n + 1)
            self.totals[key] = self.totals.get(key, 0) + 1
            return n + 1, self.totals[key]


LIMIT = Limiter()


# ---- the request ----------------------------------------------------------------------------------------------------
def _int(body: dict, key: str, low: int, high: int | None = None) -> int:
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, int) or v < low:
        raise ValueError(f"{key} must be an integer ≥ {low}")
    if high is not None and v > high:
        if key == "n":
            raise ValueError(f"n must be at most {high}")
        return high
    return v


def clean(body) -> tuple[dict, str | None]:
    """The forwarded fields (no tools, no streaming) with `max_tokens` capped, and the requested model."""
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    if body.get("stream"):
        raise ValueError("streaming is off: send stream: false")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise ValueError("messages must be a non-empty list")
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ROLES:
            raise ValueError(f"each message needs a role ({', '.join(ROLES)})")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string")
    out = {k: body[k] for k in FORWARD if k in body}
    for k in ("max_tokens", "max_completion_tokens"):
        if k in out:
            out[k] = _int(out, k, 1, MAX_TOKENS)
    if "max_tokens" not in out and "max_completion_tokens" not in out:
        out["max_tokens"] = MAX_TOKENS
    if "n" in out:
        out["n"] = _int(out, "n", 1, MAX_N)
    return out, (model or "").strip() or None


# ---- Anthropic Messages API ---------------------------------------------------------------------------------------
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else json.dumps(content)


def to_anthropic(payload: dict) -> dict:
    """system/developer messages → `system`; user/assistant (tool results as user) turns, consecutive ones merged."""
    system, turns = [], []
    for m in payload["messages"]:
        text = _text(m.get("content"))
        if m["role"] in ("system", "developer"):
            system.append(text)
            continue
        role = "assistant" if m["role"] == "assistant" else "user"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    if not turns:
        turns, system = [{"role": "user", "content": "\n\n".join(system)}], []
    out = {
        "model": payload["model"],
        "max_tokens": payload.get("max_tokens") or payload.get("max_completion_tokens") or MAX_TOKENS,
        "messages": turns,
    }
    if system:
        out["system"] = "\n\n".join(system)
    if "temperature" in payload:
        out["temperature"] = payload["temperature"]
    elif "top_p" in payload:
        out["top_p"] = payload["top_p"]
    if stop := payload.get("stop"):
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    return out


def from_anthropic(data: dict, model: str) -> dict:
    blocks = data.get("content") if isinstance(data.get("content"), list) else []
    text = "".join(b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    pin, pout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return {
        "id": str(data.get("id") or "msg"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(data.get("model") or model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": FINISH.get(str(data.get("stop_reason")), "stop"),
            }
        ],
        "usage": {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout},
    }


# ---- cloud identities (the console's own: Workload Identity / IRSA / ADC) --------------------------------------------
_ADC: dict = {}


def vertex_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request

    if "creds" not in _ADC:
        _ADC["creds"], _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds = _ADC["creds"]
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def aws_credentials():
    import botocore.session

    return botocore.session.get_session().get_credentials()


def bedrock_token(region: str) -> str:
    """Bedrock's short-term API key (a 12 h SigV4-presigned `CallWithBearerToken`), as the runtime signs it."""
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    creds = aws_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials on the console")
    host = f"bedrock.{region}.amazonaws.com"
    req = AWSRequest(
        method="POST", url=f"https://{host}/", params={"Action": "CallWithBearerToken"}, headers={"host": host}
    )
    SigV4QueryAuth(creds.get_frozen_credentials(), "bedrock", region, expires=43200).add_auth(req)
    return "bedrock-api-key-" + base64.b64encode(f"{req.url.removeprefix('https://')}&Version=1".encode()).decode()


async def _cloud_token(fetch, *args) -> str:
    try:
        return await asyncio.to_thread(fetch, *args)
    except Exception as e:  # noqa: BLE001 - no identity, or the metadata server is down: the class name only
        raise Upstream(None, f"no cloud credentials on the console: {type(e).__name__}") from None


# ---- forwarding -----------------------------------------------------------------------------------------------------
async def target(res: dict, payload: dict) -> tuple[str, dict[str, str], dict]:
    """(url, headers, body) of the provider call for the resolved scope (§36.1 endpoints)."""
    p, e, model = res["provider"], res["entry"], res["model"]
    body = {**payload, "model": model}
    key = e.get("api_key") or ""
    if p == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": llm_models.ANTHROPIC_VERSION}
        return ANTHROPIC_URL, headers, to_anthropic(body)
    if p == "openai":
        url = f"{e.get('base_url') or llm_models.OPENAI_URL}/chat/completions"
        return url, {"Authorization": f"Bearer {key}"}, body
    if p == "openai_compatible":
        headers = dict(lp.headers_of(e))
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return f"{e['base_url']}/chat/completions", headers, body
    if p in ("azure_openai", "foundry"):
        base = f"https://{e['host']}" if p == "azure_openai" else e["endpoint"]
        version = os.environ.get("RAMEN_LLM_API_VERSION", "").strip() or llm_models.AZURE_API_VERSION
        url = f"{base}/openai/deployments/{quote(model, safe='')}/chat/completions?api-version={version}"
        return url, {"api-key": key}, body
    if p == "vertex":
        location = e.get("location") or "us-central1"
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        url = f"https://{host}/v1/projects/{e['project']}/locations/{location}/endpoints/openapi/chat/completions"
        body["model"] = model if "/" in model else f"google/{model}"  # the OpenAI-compatible API wants the publisher
        return url, {"Authorization": f"Bearer {await _cloud_token(vertex_token)}"}, body
    region = e["region"]  # bedrock
    url = f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/chat/completions"
    return url, {"Authorization": f"Bearer {await _cloud_token(bedrock_token, region)}"}, body


async def _refuse_metadata(url: str) -> None:
    host = urlsplit(url).hostname or ""
    addrs = [] if TRANSPORT is not None else await llm_models.addresses(host)
    if lp.metadata_host(host) or any(lp.metadata_host(a) for a in addrs):
        raise Upstream(None, "the provider URL resolves to the cloud metadata service: refused")


async def forward(res: dict, payload: dict) -> dict:
    """One provider call; the OpenAI-shaped answer, or Upstream. Anthropic models that take no sampling settings are
    asked once more without them."""
    url, headers, body = await target(res, payload)
    await _refuse_metadata(url)
    anthropic = res["provider"] == "anthropic"
    try:
        async with httpx.AsyncClient(verify=tls.client_verify(), transport=TRANSPORT, timeout=TIMEOUT) as http:
            r = await http.post(url, json=body, headers=headers)
            sampling = [k for k in ("temperature", "top_p") if k in body]
            if anthropic and r.status_code == 400 and sampling and any(k in r.text for k in sampling):
                r = await http.post(url, json={k: v for k, v in body.items() if k not in sampling}, headers=headers)
    except httpx.HTTPError as e:
        raise Upstream(None, f"provider unreachable: {type(e).__name__}") from None
    if r.status_code >= 400:
        raise Upstream(r.status_code, f"provider answered HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise Upstream(r.status_code, "provider answered no JSON") from None
    if not isinstance(data, dict):
        raise Upstream(r.status_code, "provider answered no JSON object")
    return from_anthropic(data, res["model"]) if anthropic else data
