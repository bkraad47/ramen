"""Credentials inside values the console shows or logs (§34.3 N1): URL userinfo (`scheme://user:pass@host`,
`scheme://:pass@`, `scheme://token@`) becomes `scheme://***@host` — an ssh user alone (`ssh://git@host`) is not a
credential and stays — and query parameters named like a credential
(`password`, `api_key`, `token`, `secret`, `sig`, …) keep their name with the value `***`."""

import re

MASK = "***"
# env names whose whole value is a secret (N9: a DSN commonly embeds a password, RAMEN_POSTGRES_DSN)
SECRET_NAMES = ("SECRET", "PASSWORD", "KEY", "TOKEN", "DSN")
NOT_SECRET = {"RAMEN_SECRETS_BACKEND"}
# userinfo runs to the last `@` before the path; `[^\s/?#]` keeps a match inside one URL of a longer text
_USERINFO = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)([^\s/?#]*)@")
SSH_SCHEMES = ("ssh://", "git+ssh://", "ssh+git://")


def _credential(scheme: str, userinfo: str) -> bool:
    """A password always is; a bare user is a token except on ssh, where it names the account (`ssh://git@host`)."""
    return ":" in userinfo or scheme.lower() not in SSH_SCHEMES


def _mask(m: re.Match) -> str:
    return f"{m.group(1)}{MASK}@" if _credential(m.group(1), m.group(2)) else m.group(0)


_PARAM = re.compile(
    r"([?&;][^=&#\s]*(?:pass|pwd|secret|token|key|sig|credential|auth)[^=&#\s]*=)[^&#\s]*", re.IGNORECASE
)


def redact(text: str) -> str:
    """`text` with every URL credential masked; anything else unchanged."""
    if not text or not isinstance(text, str):
        return text
    return _PARAM.sub(rf"\1{MASK}", _USERINFO.sub(_mask, text))


def keep(submitted, stored):
    """A form posts back the masked value it was shown: that means "unchanged", so the stored value stays."""
    if submitted and stored and submitted != stored and redact(stored) == submitted:
        return stored
    return submitted


def url_secrets(text: str) -> list[str]:
    """The passwords (or bare tokens) in the userinfo of the URLs inside `text`, for `remember_secret`."""
    out = []
    for m in _USERINFO.finditer(text or ""):
        if not _credential(m.group(1), m.group(2)):
            continue
        user, colon, pw = m.group(2).partition(":")  # the user ends at the first `:`
        if secret := pw if colon else user:
            out.append(secret)
    return out


def secret_name(name: str) -> bool:
    return any(w in name for w in SECRET_NAMES) and name not in NOT_SECRET
