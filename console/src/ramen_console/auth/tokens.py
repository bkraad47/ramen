"""Signed one-time tokens for password reset / invite / magic link. Single use: the token carries a nonce
stored on the user doc, cleared when redeemed."""
from itsdangerous import BadSignature, URLSafeTimedSerializer

AGES = {"reset": 24 * 3600, "magic": 15 * 60}


class Tokens:
    def __init__(self, secret: str):
        self._s = URLSafeTimedSerializer(secret, salt="ramen-auth-token")

    def issue(self, kind: str, uid: str, nonce: str) -> str:
        return self._s.dumps({"k": kind, "u": uid, "n": nonce})

    def load(self, kind: str, token: str) -> tuple[str, str] | None:
        try:
            d = self._s.loads(token, max_age=AGES[kind])
        except (BadSignature, KeyError):
            return None
        return (d["u"], d["n"]) if d.get("k") == kind else None
