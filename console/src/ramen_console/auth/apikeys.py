"""API keys and their client type (U9 / D21, CONTRACTS §12.1).

A key is `<prefix>_<id>_<secret>`. The prefix carries the client type: `rmn_` keys drive the console's devops
API, `rmk_` keys are handed to language models and are only ever accepted by a worker. Neither side accepts the
other's keys.
"""

import hashlib
import hmac

from ..security import generate_key_secret

PREFIX = "rmn"  # devops: /api/v1/*
AGENT_PREFIX = "rmk"  # agent: ramen.v1.Mcp on a worker
HEADER = "X-Ramen-Api-Key"
CLIENT_TYPES = ("devops", "agent")
_BY_PREFIX = {PREFIX: "devops", AGENT_PREFIX: "agent"}
_BY_TYPE = {v: k for k, v in _BY_PREFIX.items()}


def prefix_for(client_type: str) -> str:
    return _BY_TYPE.get(client_type, PREFIX)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(client_type: str = "devops") -> tuple[str, str, str]:
    """`(raw key, id, secret hash)`. The secret meets the U3 strength rule and carries no `_` or `,`."""
    import secrets as pysecrets

    kid, sec = pysecrets.token_hex(6), generate_key_secret(32)
    return f"{prefix_for(client_type)}_{kid}_{sec}", kid, hash_secret(sec)


def parse(key: str) -> tuple[str, str, str] | None:
    """`(id, secret, prefix)` for a well-formed key of either kind, else None."""
    parts = key.split("_") if key else []
    if len(parts) != 3 or parts[0] not in _BY_PREFIX or not all(parts):
        return None
    return parts[1], parts[2], parts[0]


def client_type_of(doc: dict, prefix: str | None = None) -> str:
    """Migration rule for keys minted before 0.4.0: `devops` when the key starts `rmn_`, `agent` otherwise."""
    return doc.get("client_type") or _BY_PREFIX.get(prefix or doc.get("prefix") or PREFIX, "agent")


def verify(secret: str, secret_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), secret_hash)
