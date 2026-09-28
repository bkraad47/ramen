"""Auth settings (CONTRACTS §9): yaml/env defaults, overridden by the store doc `config/auth` (super-admin toggle)."""

import os
from dataclasses import dataclass, field

from ..rbac import ROLES

TRUE = ("1", "true", "yes", "on")
PREFIX = "RAMEN_AUTH_OAUTH_"


def _bool(v: str | None, default: bool) -> bool:
    return default if v is None else v.strip().lower() in TRUE


def parse_role_map(text: str) -> dict[str, tuple[str, list[str]]]:
    """`admins=super_admin;devs=group_admin:demo,other` → {claim value (lower): (role, groups)}."""
    out = {}
    for item in filter(None, (s.strip() for s in text.split(";"))):
        k, _, v = item.partition("=")
        out[k.strip().lower()] = parse_role(v)
    return out


def parse_role(v: str) -> tuple[str, list[str]]:
    role, _, groups = v.strip().partition(":")
    role = role.strip() or "viewer"
    if role not in ROLES:
        raise ValueError(f"role_map: unknown role {role!r}")
    return role, [g.strip() for g in groups.split(",") if g.strip()]


@dataclass
class AuthSettings:
    password_login: bool = True
    magic_link: bool = False
    force_password: bool = False
    admin_email: str = ""
    role_claim: dict[str, str] = field(default_factory=dict)
    role_map: dict[str, dict[str, tuple[str, list[str]]]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env=None) -> AuthSettings:
        env = env or os.environ
        s = cls(
            password_login=_bool(env.get("RAMEN_AUTH_PASSWORD_LOGIN"), True),
            magic_link=_bool(env.get("RAMEN_AUTH_MAGIC_LINK"), False),
            force_password=_bool(env.get("RAMEN_ADMIN_FORCE_PASSWORD"), False),
            admin_email=(env.get("RAMEN_ADMIN_EMAIL") or "").strip().lower(),
        )
        for k, v in env.items():
            if not k.startswith(PREFIX):
                continue
            name, _, rest = k[len(PREFIX) :].partition("_")
            name = name.lower()
            if rest == "ROLE_CLAIM":
                s.role_claim[name] = v.strip()
            elif rest == "ROLE_MAP":
                s.role_map.setdefault(name, {}).update(parse_role_map(v))
            elif rest.startswith("ROLE_MAP_"):
                s.role_map.setdefault(name, {})[rest[len("ROLE_MAP_") :].lower()] = parse_role(v)
        return s

    def with_doc(self, doc: dict | None) -> AuthSettings:
        doc = doc or {}
        return AuthSettings(
            password_login=bool(doc.get("password_login", self.password_login)),
            magic_link=bool(doc.get("magic_link", self.magic_link)),
            force_password=self.force_password,
            admin_email=self.admin_email,
            role_claim=self.role_claim,
            role_map=self.role_map,
        )

    def can_password(self, email: str) -> bool:
        return self.password_login or (
            self.force_password and bool(email) and email.strip().lower() == self.admin_email
        )

    def map_role(self, provider: str, claims: dict) -> tuple[str, list[str]]:
        """Role + groups for a new SSO user: `role_claim` value(s) looked up in `role_map`, else viewer/no groups."""
        claim, table = self.role_claim.get(provider), self.role_map.get(provider, {})
        values = claims.get(claim) if claim else None
        values = values if isinstance(values, list) else [values] if values is not None else []
        best: tuple[str, list[str]] | None = None
        for v in values:
            hit = table.get(str(v).lower())
            if hit and (best is None or ROLES.index(hit[0]) < ROLES.index(best[0])):
                best = (hit[0], list(hit[1]))
            elif hit:
                best[1].extend(g for g in hit[1] if g not in best[1])
        return best or ("viewer", [])

    def public(self) -> dict:
        return {
            "password_login": self.password_login,
            "magic_link": self.magic_link,
            "break_glass": self.force_password,
            "role_claim": dict(self.role_claim),
            "role_map": {p: {k: {"role": r, "groups": g} for k, (r, g) in m.items()} for p, m in self.role_map.items()},
        }
