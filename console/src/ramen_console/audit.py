from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .auth.sessions import COOKIE
from .auth.apikeys import HEADER
from .util import now, uid

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "-")


async def write_audit(store, *, user, ip, action, target, ok, tags):
    doc = {"ts": now(), "user": user, "ip": ip, "action": action, "target": target, "ok": ok, "tags": list(tags)}
    await store.put("audit", uid(), doc)


def note(request: Request, action: str, target: str = "", tags=(), user: str | None = None):
    request.state.audit = {"action": action, "target": target, "tags": list(tags), "user": user}


class AuthAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        st = request.app.state
        request.state.principal = None
        request.state.audit = None
        if request.headers.get(HEADER):
            request.state.principal = await st.accounts.principal_for_key(request.headers[HEADER])
        else:
            sess = st.signer.load(request.cookies.get(COOKIE))
            if sess:
                request.state.principal = await st.accounts.principal_for_user(sess.get("uid"))
        response = await call_next(request)
        if request.state.audit or (request.method in MUTATING and request.url.path.startswith("/api/")):
            p = request.state.principal
            meta = dict(request.state.audit or {"action": f"{request.method} {request.url.path}", "target": request.url.path, "tags": []})
            user = meta.pop("user", None) or (p.name if p else "-")
            await write_audit(st.store, user=user, ip=client_ip(request), ok=response.status_code < 400, **meta)
        return response
