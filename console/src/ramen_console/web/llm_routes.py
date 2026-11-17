"""§38.1 (D85, F1) `/llm/v1`: the LLM judge proxy's routes (`llm_proxy` holds the mapping). The bearer is a judge
token of the group's zone, or what the OTLP ingest accepts (a person's OAuth token, an MCP key of the group, a console
session naming its group); `ramen-group`, when sent, must be the caller's group."""

import hashlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import guardrails, llm_proxy, llm_scope
from .. import llm_providers as lp
from ..audit import note
from ..errors import ApiError
from . import otlp_routes
from .helpers import svc

r = APIRouter(prefix=llm_proxy.PATH)


async def caller(request: Request) -> dict:
    """`{group, zone, user, key}` of the bearer; 401 unknown or stale, 403 another group."""
    s = svc(request)
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if bearer.startswith(llm_proxy.PREFIX):
        claims = llm_proxy.judge_claims(bearer)
        if claims is None or not await s.store.get("groups", claims[0]):
            raise ApiError(401, "unknown judge token")
        group, zone, job = claims
        if not llm_proxy.judge_ok(bearer, await s.session_secret(group)):
            raise ApiError(401, "unknown judge token")
        if not await llm_proxy.judge_live(s, group, zone, job):
            raise ApiError(401, "judge token is no longer current: the zone was deployed again")
        key = hashlib.sha256(bearer.encode()).hexdigest()
        who = {"group": group, "zone": zone, "user": f"judge:{zone}", "key": key}
    else:
        who = await otlp_routes.resolve(request)
    named = request.headers.get("ramen-group") or request.query_params.get("group")
    if named and named != who["group"]:
        raise ApiError(403, f"no access to group {named}")
    return who


async def _scopes(s, group: str) -> tuple[dict, dict, dict]:
    return await s.store.get("groups", group) or {}, await lp.load(s.store, group), await lp.load(s.store)


@r.get("/models")
async def models(request: Request):
    who = await caller(request)
    g, group_cfg, org_cfg = await _scopes(svc(request), who["group"])
    opts = llm_scope.options(group_cfg, org_cfg, guardrails.settings(g)["llm"] or None)
    if opts is None:
        raise ApiError(403, llm_scope.MESSAGE)
    owner = f"{opts['provider']} ({opts['scope']} scope)"
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": owner} for m in opts["models"]]}


async def _json(request: Request):
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise ApiError(400, "bad Content-Length") from None
    if declared > llm_proxy.MAX_BODY:
        raise ApiError(413, "body over 1 MiB")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > llm_proxy.MAX_BODY:
            raise ApiError(413, "body over 1 MiB")
    try:
        return json.loads(bytes(buf))
    except ValueError:
        raise ApiError(400, "the body is not JSON") from None


@r.post("/chat/completions")
async def chat(request: Request):
    who = await caller(request)
    n, total = llm_proxy.LIMIT.count(who["group"])
    if n > llm_proxy.per_min():
        return JSONResponse({"detail": "Too many LLM calls for this group"}, 429, headers={"Retry-After": "60"})
    try:
        payload, model = llm_proxy.clean(await _json(request))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    s = svc(request)
    g, group_cfg, org_cfg = await _scopes(s, who["group"])
    want = guardrails.settings(g)["llm"] or None
    try:
        res = llm_scope.resolve(g, group_cfg, org_cfg, "guardrails", want, model)
    except (llm_scope.NoProvider, llm_scope.BadModel) as e:
        raise ApiError(403, str(e)) from None
    if total % llm_proxy.AUDIT_EVERY == 1 or llm_proxy.AUDIT_EVERY == 1:
        tags = [f"group:{who['group']}", f"model:{res['model']}", f"scope:{res['scope']}", f"count:{total}"]
        if who.get("zone"):
            tags.append(f"zone:{who['zone']}")
        note(request, "llm.proxy", who["group"], tags, user=who["user"])
    try:
        out = await llm_proxy.forward(res, payload)
    except llm_proxy.Upstream as e:
        return JSONResponse({"error": e.why, "upstream_status": e.status}, 502)
    return JSONResponse(out)
