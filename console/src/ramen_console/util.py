import re
import uuid
from datetime import datetime, timezone

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
KEYNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CIDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid() -> str:
    return uuid.uuid4().hex


def public(doc: dict, hidden=("password_hash", "secret_hash", "value", "github_token")) -> dict:
    return {k: v for k, v in doc.items() if k not in hidden}
