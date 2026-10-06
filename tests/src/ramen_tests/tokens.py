"""Mint the access token the console issues (§16.3) without a console: the same derivation as
console/src/ramen_console/oauth_server.py::mint_jwt, which node-rs/src/token.rs verifies."""

import base64
import hashlib
import hmac
import json
import time


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint_console_token(claims: dict, session_secret: str) -> str:
    """HS256 with key = HMAC-SHA256(session secret, b"oauth"); the console's own test pins its mint to this."""
    head = b64url(b'{"alg":"HS256","typ":"JWT"}')
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    key = hmac.new(session_secret.encode(), b"oauth", hashlib.sha256).digest()
    return f"{head}.{body}.{b64url(hmac.new(key, f'{head}.{body}'.encode(), hashlib.sha256).digest())}"


def user_token(
    secret: str,
    issuer: str,
    ttl: float,
    sub: str = "u-42",
    jti: str = "j1",
    group: str = "demo",
    zone: str = "local",
    **claims: object,
) -> tuple[str, float]:
    """A token for `sub` on `mcp:<group>:<zone>` that expires `ttl` seconds from now → (token, exp).
    `**claims` are added verbatim (0.7.2 C10: `role="viewer"` is the kind the node reads for tool access)."""
    now = int(time.time())
    exp = now + int(ttl)
    scope = f"mcp:{group}:{zone}"
    base = {"iss": issuer, "sub": sub, "email": f"{sub}@x", "iat": now, "exp": exp, "jti": jti, "aud": scope}
    return mint_console_token({**base, "scope": scope, **claims}, secret), exp


def legacy_session_id(secret: str, key: str, ttl: float = 600, nonce: bytes = b"\x01" * 16) -> str:
    """A 0.7.1-style 3-part `Mcp-Session-Id` for an `rmk_` key: `nonce.expiry.mac`, `mac = HMAC-SHA256(secret,
    nonce ‖ "\\n" ‖ expiry ‖ "\\n" ‖ binding)` with binding = sha256 hex of the key (node-rs session.rs / grpc.rs).
    0.7.2 C11 says a node still verifies it (treated as `hash12 = ""`)."""
    n = b64url(nonce)
    expiry = int(time.time() + ttl)
    binding = hashlib.sha256(key.encode()).hexdigest()
    mac = hmac.new(secret.encode(), f"{n}\n{expiry}\n{binding}".encode(), hashlib.sha256).digest()
    return f"{n}.{expiry}.{b64url(mac)}"


def claims_of(token: str) -> dict:
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
