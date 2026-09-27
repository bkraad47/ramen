from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE = "ramen_session"


class SessionSigner:
    def __init__(self, secret: str):
        self._s = URLSafeTimedSerializer(secret, salt="ramen-session")

    def sign(self, data: dict) -> str:
        return self._s.dumps(data)

    def load(self, token: str | None, max_age: int = 12 * 3600) -> dict | None:
        if not token:
            return None
        try:
            return self._s.loads(token, max_age=max_age)
        except BadSignature:
            return None
