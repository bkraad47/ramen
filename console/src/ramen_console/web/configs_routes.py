"""§31.1 acceptance routes: the configs page (every file verbatim, Accept, `?next=`), its form POST, and the JSON
`POST /api/v1/groups/{g}/configs/accept {version}` for the bridge and the API; all for any member of the group."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .. import configs
from ..audit import note
from ..auth import csrf
from ..baseuri import link
from ..errors import not_found
from ..rbac import Principal, can, require
from ..security import safe_next
from .helpers import respond, svc
from .pages import render

r = APIRouter()
member = require("mcp_user", "group")


class Accept(BaseModel):
    version: str


async def _loaded(request: Request, group: str) -> dict:
    lc = await configs.load(svc(request), group)
    if lc is None:
        raise not_found("LLM configs for this group")
    return lc


@r.get("/groups/{group}/configs")
async def configs_page(request: Request, group: str, next: str = "/", p: Principal = Depends(member)):
    lc = await _loaded(request, group)
    user = await svc(request).store.get("users", p.id) or {}
    raw = await svc(request).cloud.read_file(group, f"{configs.DIR}/{configs.MANIFEST}")
    return render(
        request,
        "configs.html",
        group=group,
        c=lc,
        manifest_text=(raw or b"").decode("utf-8", "replace"),
        accepted=configs.accepted_version(user, group),
        next=safe_next(next),
        csrf_token=csrf.token(request),
        page="groups",
    )


@r.post("/groups/{group}/configs/accept")
async def accept_form(
    request: Request,
    group: str,
    version: str = Form(""),
    next: str = Form("/"),
    p: Principal = Depends(member),
    _=Depends(csrf.csrf_form),
):
    rec = await configs.accept(svc(request), p.id, group, version)
    note(request, "configs.accept", group, [f"group:{group}", f"version:{rec['version']}", "via:page"])
    user = await svc(request).store.get("users", p.id) or {}
    next_ = safe_next(next)
    if next_.startswith("/groups/") and not can(p, "viewer", group):  # an MCP user has no group page (the bridge's
        next_ = "/"  # consent URL names it): their own page instead of a 403
    # another group's configs may still be waiting: the sign-in chain continues there before `next`
    return RedirectResponse(link(request, await configs.signin_target(svc(request), user, next_)), 303)


@r.post("/api/v1/groups/{group}/configs/accept")
async def accept_api(request: Request, group: str, body: Accept, p: Principal = Depends(member)):
    rec = await configs.accept(svc(request), p.id, group, body.version)
    note(request, "configs.accept", group, [f"group:{group}", f"version:{rec['version']}", "via:api"])
    return respond(request, rec)
