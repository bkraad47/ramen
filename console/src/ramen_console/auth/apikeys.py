import hashlib
import hmac
import secrets

PREFIX = "rmn"
HEADER = "X-Ramen-Api-Key"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint() -> tuple[str, str, str]:
    kid, sec = secrets.token_hex(6), secrets.token_urlsafe(32).replace("_", "-")
    return f"{PREFIX}_{kid}_{sec}", kid, hash_secret(sec)


def parse(key: str) -> tuple[str, str] | None:
    parts = key.split("_") if key else []
    if len(parts) != 3 or parts[0] != PREFIX or not all(parts):
        return None
    return parts[1], parts[2]


def verify(secret: str, secret_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), secret_hash)
