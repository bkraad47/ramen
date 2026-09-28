import re
import uuid
from datetime import UTC, datetime

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
KEYNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_cidr(value: str) -> bool:
    """IPv4 or IPv6 network in CIDR form (host bits allowed)."""
    import ipaddress

    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return "/" in value


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def uid() -> str:
    return uuid.uuid4().hex


def public(
    doc: dict, hidden=("password_hash", "secret_hash", "value", "github_token", "reset_nonce", "magic_nonce")
) -> dict:
    return {k: v for k, v in doc.items() if k not in hidden}
