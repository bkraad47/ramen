import re
import uuid
from datetime import UTC, datetime

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
KEYNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# CONTRACTS §13.3: a worker image reference. Repo part: lowercase registry/path with an optional registry port.
IMAGE_REPO_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(:\d{2,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$")
IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


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


def parse_image(tag: str, digest: str | None = None) -> tuple[str, str | None, str]:
    """Split a worker image reference into (tag, digest, ref) or raise 422 (CONTRACTS §13.3).

    `ref` is what the manifests run: the tag, plus `@<digest>` when one is recorded. A reference must name a tag or
    a digest — an implicit `:latest` would make "recall this build" meaningless."""
    from .errors import invalid

    t = (tag or "").strip()
    if not t or len(t) > 512 or any(c.isspace() for c in t):
        raise invalid("Image tag is required and must be a registry reference with no spaces")
    head, _, last = t.rpartition(":")
    repo, label = (head, last) if head and "/" not in last else (t, "")
    if not IMAGE_REPO_RE.match(repo) or (label and not IMAGE_TAG_RE.match(label)):
        raise invalid(f"Image {t!r} is not a registry reference like registry/path:tag")
    d = (digest or "").strip() or None
    if d and not DIGEST_RE.match(d):
        raise invalid("Digest must look like sha256:<64 hex characters>")
    if not label and not d:
        raise invalid(f"Image {t!r} needs a :tag or a recorded digest")
    return t, d, f"{t}@{d}" if d else t
