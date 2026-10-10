import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, alerts, baseuri, rbac, roles, scheduler
from .accounts import Accounts
from .audit import AuthAuditMiddleware
from .auth.bootstrap import ensure_super_admin
from .auth.csrf import CsrfMiddleware
from .auth.csrf import token as csrf_token
from .auth.oauth import OAuthRegistry
from .auth.sessions import SessionSigner
from .auth.settings import AuthSettings
from .auth.tokens import Tokens
from .backup import Backups, release_version
from .cloud import make_cloud
from .config import apply_config
from .deploy import Jobs
from .mail import Mailer, build_mailer
from .oauth_server import OAuthError, OAuthServer
from .rbac import role_label
from .secrets import make_secrets_backend
from .security import (
    PASSWORD_RULE,
    HttpsOnlyMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    redact_tokens_in_access_log,
    resolve_signing_secret,
)
from .services import Services
from .storage import make_store
from .web import api, api_admin, auth_routes, oauth_routes, pages

HERE = Path(__file__).parent


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api/")


def create_app(store=None, cloud=None, secrets=None) -> FastAPI:
    apply_config()
    logging.basicConfig(level=os.environ.get("RAMEN_LOG_LEVEL", "INFO"))
    ramen_logger = logging.getLogger("ramen")
    if alerts.HANDLER not in ramen_logger.handlers:
        ramen_logger.addHandler(alerts.HANDLER)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ensure_super_admin(app.state.store)
        await roles.load(app.state.store, force=True)  # D48: custom roles before any membership is read
        await app.state.accounts.migrate_memberships()  # 0.5.95 (D41) and 0.7.5 (§21.3) membership shapes
        app.state.mailer = await build_mailer(app.state.store)  # N2: layer any persisted config/smtp over env
        tasks = [
            asyncio.create_task(scheduler.run_forever(app.state.services)),
            asyncio.create_task(alerts.run_forever(app.state.services, app.state)),
        ]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    app = FastAPI(title="Ramen console", version=__version__, lifespan=lifespan, docs_url="/api/docs")
    st = app.state
    st.store = store or make_store()
    st.cloud = cloud or make_cloud()
    st.secrets = secrets or make_secrets_backend(st.cloud)
    st.services = Services(st.store, st.cloud, st.secrets)
    st.accounts = Accounts(st.store)
    st.backups = Backups(st.services)
    st.jobs = Jobs()
    signing_secret = resolve_signing_secret()
    st.signer = SessionSigner(signing_secret)
    st.tokens = Tokens(signing_secret)
    st.oauth = OAuthRegistry.from_env()
    st.auth_env = AuthSettings.from_env()
    st.mailer = Mailer.from_env()
    st.cookie_secure = os.environ.get("RAMEN_COOKIE_SECURE", "0") == "1"
    st.base_uri = baseuri.Cache()
    st.templates = Jinja2Templates(directory=str(HERE / "templates"), context_processors=[baseuri.context])
    st.templates.env.globals.update(
        version=release_version(),
        tojson=json.dumps,
        csrf_token=csrf_token,
        role_label=role_label,
        custom_roles=rbac.custom_roles,  # D48: templates list custom roles after the built-ins
        password_rule=f"Use {PASSWORD_RULE}.",
        u=baseuri.u,
    )

    app.add_middleware(CsrfMiddleware)  # inner: a rejected token is still audited by the outer middleware
    app.add_middleware(AuthAuditMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=st.cookie_secure)
    if st.cookie_secure:  # 0.6.0: only https reaches a public console
        app.add_middleware(HttpsOnlyMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=signing_secret, https_only=st.cookie_secure)
    app.add_middleware(baseuri.BaseUriMiddleware)  # outermost: every layer below may build links
    redact_tokens_in_access_log()
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    st.oauth_server = OAuthServer(st.services, st.accounts)
    for router in (auth_routes.r, pages.r, api.r, api_admin.r, oauth_routes.r):
        app.include_router(router)

    @app.exception_handler(OAuthError)
    async def oauth_error(request: Request, exc: OAuthError):
        """RFC 6749 §5.2 shape on the token endpoint; a plain page on the consent flow (never a redirect: §16.3)."""
        if request.url.path == "/oauth/token" or not _wants_html(request):
            return JSONResponse(exc.body(), exc.status_code, headers={"Cache-Control": "no-store"})
        return PlainTextResponse(f"{exc.error}: {exc.detail}", exc.status_code)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], 303)
        if request.headers.get("HX-Request") == "true" or _wants_html(request):
            return PlainTextResponse(str(exc.detail), exc.status_code)
        return JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        if request.headers.get("HX-Request") == "true":
            return PlainTextResponse(msg, 422)
        return JSONResponse({"detail": msg}, 422)

    @app.exception_handler(NotImplementedError)
    async def not_impl(request: Request, exc: NotImplementedError):
        if request.headers.get("HX-Request") == "true":
            return PlainTextResponse(str(exc), 501)
        return JSONResponse({"detail": str(exc)}, 501)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/readyz")
    async def readyz():
        try:
            admins = await st.store.list("users", {"role": "super_admin"})
        except Exception as e:  # noqa: BLE001 - probe reports the failure
            return JSONResponse({"ok": False, "error": f"store: {type(e).__name__}: {e}"}, 503)
        if not admins:
            return JSONResponse(
                {"ok": False, "error": "no super admin yet (set RAMEN_ADMIN_EMAIL/RAMEN_ADMIN_PASSWORD)"}, 503
            )
        return {"ok": True, "store": os.environ.get("RAMEN_STORE", "memory"), "version": __version__}

    return app


app = create_app() if os.environ.get("RAMEN_APP_AUTOCREATE", "1") == "1" else None
