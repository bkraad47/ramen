"""The console as OAuth 2.1 authorization server for end users of a worker (CONTRACTS §16.3, D34).

Pre-registered public clients (no secret; PKCE S256 is the proof), authorization code + consent, HS256 access
tokens the node verifies with the zone's session secret — key = HMAC-SHA256(secret, b"oauth"), exactly
`node-rs/src/token.rs::derived_key("oauth")` — and opaque refresh tokens rotated on every use and tied to the
user's session epoch, so anything that revokes a user (V1.4) revokes their refresh tokens too.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlparse, urlunparse

from .accounts import Accounts
from .errors import ApiError, invalid, not_found
from .rbac import Principal, can_connect, principal_from
from .services import Services
from .util import now, uid

ACCESS_TTL = 3600
CODE_TTL = 600
REFRESH_TTL = 30 * 24 * 3600
SCOPE_RE = re.compile(r"^mcp:([a-z][a-z0-9-]{0,39}):([a-z][a-z0-9-]{0,39})$")


class OAuthError(ApiError):
    """RFC 6749 §5.2: `{"error": ..., "error_description": ...}` with the status the spec names."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(status, description)
        self.error = error

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.detail}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def scope_parts(scope: str) -> tuple[str, str]:
    """`mcp:<group>:<zone>` → (group, zone); exactly one scope, or the request is malformed."""
    parts = (scope or "").split()
    if len(parts) != 1 or not (m := SCOPE_RE.match(parts[0])):
        raise OAuthError("invalid_scope", "scope must be exactly one mcp:<group>:<zone>")
    return m.group(1), m.group(2)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def redirect_ok(uri: str) -> bool:
    u = urlparse(uri)
    if u.scheme not in ("http", "https") or not u.netloc or u.fragment:
        return False
    # loopback may be plain http (RFC 8252); anything else must be https
    return u.scheme == "https" or u.hostname in LOOPBACK


def redirect_matches(registered: list[str], uri: str) -> bool:
    """Exact match, except that a registered plain-http loopback URI matches any port (RFC 8252 §7.3: a native app
    binds whatever port is free) and any loopback name — `localhost`, `127.0.0.1` and `::1` are the same
    interface (0.5.95: Claude Code registers `localhost`, the bridge binds `127.0.0.1`). Path and query must match."""
    if uri in registered:
        return True
    u = urlparse(uri)
    if u.scheme != "http" or u.hostname not in LOOPBACK:
        return False
    return any(
        (r := urlparse(reg)).scheme == "http" and r.hostname in LOOPBACK and (r.path, r.query) == (u.path, u.query)
        for reg in registered
    )


def with_params(uri: str, params: dict) -> str:
    """Append to the redirect URI's query, keeping any query it already carries (RFC 6749 §3.1.2)."""
    u = urlparse(uri)
    query = f"{u.query}&{urlencode(params)}" if u.query else urlencode(params)
    return urlunparse(u._replace(query=query))


def token_id(raw: str) -> str:
    """Codes and refresh tokens are stored under their SHA-256, so a copy of the store is not a copy of the tokens
    (security review 0.5.0 M5)."""
    return hashlib.sha256((raw or "").encode()).hexdigest()


class OAuthServer:
    def __init__(self, services: Services, accounts: Accounts):
        self.svc, self.accounts, self.store = services, accounts, services.store

    # --- clients (pre-registered by a super admin; no dynamic registration in this release) -------------------------
    async def clients(self) -> list[dict]:
        return sorted(await self.store.list("oauth_clients"), key=lambda c: c["created"])

    async def register_client(self, name: str, redirect_uris: list[str], by: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 80:
            raise invalid("Client name must be 1 to 80 characters")
        uris = [u.strip() for u in redirect_uris if u and u.strip()]
        if not uris or any(not redirect_ok(u) for u in uris):
            raise invalid("Redirect URIs must be https, or http on 127.0.0.1 / localhost, with no fragment")
        doc = {"client_id": uid(), "name": name, "redirect_uris": uris, "created": now(), "created_by": by}
        return await self.store.put("oauth_clients", doc["client_id"], doc)

    async def delete_client(self, client_id: str) -> None:
        if not await self.store.get("oauth_clients", client_id):
            raise not_found("OAuth client")
        await self.store.delete("oauth_clients", client_id)
        for r in await self.store.list("oauth_refresh", {"client_id": client_id}):
            await self.store.delete("oauth_refresh", r["id"])

    async def client(self, client_id: str) -> dict:
        c = await self.store.get("oauth_clients", client_id or "")
        if not c:
            raise OAuthError("invalid_client", "unknown client_id", 401)
        return c

    # --- discovery (RFC 8414) -----------------------------------------------------------------------------------------
    async def metadata(self, issuer: str) -> dict:
        # no `scopes_supported`: it would list every group and zone to anyone on the internet (review 0.5.0 L7)
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "service_documentation": "https://bkraad47.github.io/ramen/how-tos/security/",
        }

    # --- authorization request ------------------------------------------------------------------------------
    async def validate_authorize(self, p: Principal, q: dict) -> dict:
        """The request a consent page may be shown for. Every failure is an error page, never a redirect: a redirect
        URI is only trusted once it has been matched against the registered client."""
        if p.kind != "user":  # a key is not a person; only a signed-in user can delegate their access (review 0.5.0 I9)
            raise OAuthError("access_denied", "sign in as a user to authorize a client; API keys cannot", 403)
        client = await self.store.get("oauth_clients", q.get("client_id") or "")
        if not client:  # an error PAGE, not a 401 challenge: nothing on the authorize endpoint authenticates a client
            raise OAuthError("invalid_request", "unknown client_id")
        if q.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "response_type must be code")
        redirect = q.get("redirect_uri") or ""
        if not redirect_matches(client["redirect_uris"], redirect):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client")
        if q.get("code_challenge_method") != "S256" or not re.fullmatch(
            r"[A-Za-z0-9_-]{43,128}", q.get("code_challenge") or ""
        ):
            raise OAuthError("invalid_request", "PKCE with S256 is required")
        group, zone = scope_parts(q.get("scope") or "")
        # RFC 8707: an MCP client names the worker URL it talks to (what the worker's metadata calls `resource`);
        # the scope's mcp:<group>:<zone> is also accepted. Anything else is a request for a different resource.
        resource = q.get("resource") or f"mcp:{group}:{zone}"
        if resource != f"mcp:{group}:{zone}" and not re.fullmatch(r"https?://[^\s?#]+", resource):
            raise OAuthError("invalid_target", "resource must be the worker URL or the scope's mcp:<group>:<zone>")
        if not await self.store.get("groups", group) or zone not in {
            z for e in await self.svc.environments(group) for z in e.get("zones", [])
        }:
            raise OAuthError("invalid_scope", f"{group}/{zone} is not a deployed zone")
        if not can_connect(p, group):
            raise OAuthError("access_denied", f"you have no access to group {group}", 403)
        return {
            "client": client,
            "redirect_uri": redirect,
            "group": group,
            "zone": zone,
            "scope": f"mcp:{group}:{zone}",
            "state": q.get("state") or "",
            "code_challenge": q["code_challenge"],
        }

    async def issue_code(self, p: Principal, a: dict) -> str:
        code = b64url(secrets.token_bytes(32))
        user = await self.store.get("users", p.id)
        await self.store.put(
            "oauth_codes",
            token_id(code),
            {
                "client_id": a["client"]["client_id"],
                "redirect_uri": a["redirect_uri"],
                "code_challenge": a["code_challenge"],
                "user": p.id,
                "email": p.name,
                "scope": a["scope"],
                "group": a["group"],
                "zone": a["zone"],
                "epoch": self.accounts.epoch_of(user or {}),
                "exp": int(time.time()) + CODE_TTL,
            },
        )
        return code

    # --- token endpoint -----------------------------------------------------------------------------------------------
    async def exchange(self, form: dict, issuer: str) -> dict:
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return await self._exchange_code(form, issuer)
        if grant == "refresh_token":
            return await self._refresh(form, issuer)
        raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")

    async def _exchange_code(self, form: dict, issuer: str) -> dict:
        client = await self.client(form.get("client_id"))
        async with self.store.transaction() as tx:  # M3: the read and the burn are one step, two racers get one token
            rec = await tx.get("oauth_codes", token_id(form.get("code")))
            if rec:  # one shot, whatever happens next: a code that reaches this line is never usable again
                await tx.delete("oauth_codes", rec["id"])
        if (
            not rec
            or rec["exp"] < time.time()
            or rec["client_id"] != client["client_id"]
            or rec["redirect_uri"] != (form.get("redirect_uri") or "")
        ):
            raise OAuthError("invalid_grant", "code is unknown, used, expired, or not for this client")
        verifier = form.get("code_verifier") or ""
        if not hmac.compare_digest(b64url(hashlib.sha256(verifier.encode()).digest()), rec["code_challenge"]):
            raise OAuthError("invalid_grant", "code_verifier does not match")
        user = await self._still_allowed(rec)
        return await self._tokens(client, user, rec["group"], rec["zone"], issuer, family=uid())

    async def _refresh(self, form: dict, issuer: str) -> dict:
        client = await self.client(form.get("client_id"))
        async with self.store.transaction() as tx:
            rec = await tx.get("oauth_refresh", token_id(form.get("refresh_token")))
            if rec and rec.get("used"):
                # a rotated-out token presented again means two parties hold the chain: the whole family dies (M3)
                for r in await tx.list("oauth_refresh", {"family": rec["family"]}):
                    await tx.delete("oauth_refresh", r["id"])
                raise OAuthError("invalid_grant", "refresh token reuse detected; every token of this grant is revoked")
            if rec:  # rotation: the presented token dies here, kept as a tombstone until the family expires
                await tx.put("oauth_refresh", rec["id"], {**rec, "used": True})
        if not rec or rec["client_id"] != client["client_id"] or rec["exp"] < time.time():
            raise OAuthError("invalid_grant", "refresh token is unknown, expired, or not for this client")
        user = await self._still_allowed(rec)
        return await self._tokens(client, user, rec["group"], rec["zone"], issuer, family=rec["family"])

    async def _still_allowed(self, rec: dict) -> dict:
        """M2: what was true at consent is re-checked at every mint — the user exists, can still log in, still has
        the group, and nothing has revoked their sessions since the grant."""
        user = await self.store.get("users", rec["user"])
        if not user or user.get("login_disabled") or self.accounts.epoch_of(user) != rec["epoch"]:
            raise OAuthError("invalid_grant", "the user's sessions were revoked; sign in again")
        if not can_connect(principal_from(user), rec["group"]):
            raise OAuthError("invalid_grant", f"the user no longer has access to group {rec['group']}")
        return user

    async def _tokens(self, client: dict, user: dict, group: str, zone: str, issuer: str, family: str) -> dict:
        scope = f"mcp:{group}:{zone}"
        iat = int(time.time())
        claims = {
            "iss": issuer,
            "sub": user["id"],
            "email": user.get("email"),
            "aud": scope,
            "scope": scope,
            "group": group,
            "zone": zone,
            "client_id": client["client_id"],
            "iat": iat,
            "exp": iat + ACCESS_TTL,
            "jti": uid(),
        }
        access = mint_jwt(claims, await self.svc.session_secret(group))
        for r in await self.store.list("oauth_refresh", {"family": family}):
            if r["exp"] < iat:  # tombstones and dead chains of this grant are swept as it renews
                await self.store.delete("oauth_refresh", r["id"])
        refresh = b64url(secrets.token_bytes(32))
        await self.store.put(
            "oauth_refresh",
            token_id(refresh),
            {
                "client_id": client["client_id"],
                "user": user["id"],
                "group": group,
                "zone": zone,
                "epoch": self.accounts.epoch_of(user),
                "family": family,
                "used": False,
                "exp": iat + REFRESH_TTL,
                "created": now(),
            },
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": scope,
        }


def token_key(session_secret: str) -> bytes:
    """HMAC-SHA256(secret, "oauth") — the node derives its verification key the same way (token.rs / session.rs)."""
    return hmac.new(session_secret.encode(), b"oauth", hashlib.sha256).digest()


def mint_jwt(claims: dict, session_secret: str) -> str:
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(token_key(session_secret), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64url(sig)}"
