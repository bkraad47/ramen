"""N5: GitHub App installation tokens. A group with `github_app_installation_id` needs no stored PAT — a
fresh, short-lived token is minted per deploy from the console-wide App credentials (`config/github_app`:
`app_id` + `private_key`, set once by a super admin, private key Fernet-encrypted at rest)."""

import base64
import json
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

APP_DEFAULTS = {"app_id": "", "github_app_private_key": ""}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def get_app_config(store) -> dict:
    doc = await store.get("config", "github_app") or {}
    return {k: doc.get(k, v) for k, v in APP_DEFAULTS.items()}


async def set_app_config(store, *, app_id=None, private_key=None) -> dict:
    doc = await get_app_config(store)
    updates = {"app_id": app_id, "github_app_private_key": private_key}
    doc.update({k: v for k, v in updates.items() if v is not None})
    await store.put("config", "github_app", doc)
    return doc


def public_app_config(doc: dict) -> dict:
    doc = dict(doc)
    doc["private_key_set"] = bool(doc.pop("github_app_private_key", ""))
    return doc


def app_jwt(app_id: str, private_key_pem: str) -> str:
    """A GitHub App authenticates as itself with a JWT it signs, valid at most 10 minutes (GitHub's limit)."""
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    sig = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64url(sig)}"


async def installation_token(app_id: str, private_key: str, installation_id: str) -> str:
    """Exchanges the App JWT for a token scoped to one installation, valid about an hour."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt(app_id, private_key)}",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        return r.json()["token"]


async def resolve_token(store, group: dict) -> str | None:
    """None when the group has no installation configured, or the App itself is not set up; the caller
    falls back to a stored PAT (`github_token`)."""
    installation_id = group.get("github_app_installation_id")
    if not installation_id:
        return None
    cfg = await get_app_config(store)
    if not cfg["app_id"] or not cfg["github_app_private_key"]:
        return None
    return await installation_token(cfg["app_id"], cfg["github_app_private_key"], installation_id)
