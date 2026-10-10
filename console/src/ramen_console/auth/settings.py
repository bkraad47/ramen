"""Auth settings (CONTRACTS §9): yaml/env defaults, overridden by the store doc `config/auth` (super-admin toggle)."""

import os
from dataclasses import dataclass, field

from .. import rbac

TRUE = ("1", "true", "yes", "on")
PREFIX = "RAMEN_AUTH_OAUTH_"
SOURCES = ("claim", "lookup")


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
    if not rbac.is_role(role):
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
    groups_source: dict[str, str] = field(default_factory=dict)  # §21.3: provider → claim | lookup

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
            if k.startswith("RAMEN_OAUTH_") and k.endswith("_GROUPS") and k.count("_") == 3:
                source = v.strip().lower()
                if source not in SOURCES:
                    raise ValueError(f"{k} must be claim or lookup, not {v!r}")
                s.groups_source[k[len("RAMEN_OAUTH_") : -len("_GROUPS")].lower()] = source
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
        """Store doc over env: toggles replace, role rules (D38) merge per provider with the doc's rule winning."""
        doc = doc or {}
        role_claim = {**self.role_claim, **{p: str(c) for p, c in (doc.get("role_claim") or {}).items() if c}}
        role_map = {p: dict(t) for p, t in self.role_map.items()}
        for provider, rules in (doc.get("role_map") or {}).items():
            table = role_map.setdefault(provider, {})
            for value, rule in (rules or {}).items():
                if rbac.is_role((rule or {}).get("role")):
                    table[str(value).lower()] = (rule["role"], [str(g) for g in rule.get("groups") or []])
        sources = dict(self.groups_source)
        for provider, source in (doc.get("groups_source") or {}).items():
            if source in SOURCES:
                sources[str(provider)] = source
        return AuthSettings(
            password_login=bool(doc.get("password_login", self.password_login)),
            magic_link=bool(doc.get("magic_link", self.magic_link)),
            force_password=self.force_password,
            admin_email=self.admin_email,
            role_claim=role_claim,
            role_map=role_map,
            groups_source=sources,
        )

    def source_of(self, provider: str) -> str:
        """Where a provider's groups come from: the ID token / userinfo (`claim`) or the provider's API (`lookup`)."""
        return self.groups_source.get(provider, "claim")

    def has_mapping(self, provider: str) -> bool:
        """True once a claim is named for the provider: its rules then decide role and groups on every login."""
        return bool(self.role_claim.get(provider))

    def can_password(self, email: str) -> bool:
        return self.password_login or (
            self.force_password and bool(email) and email.strip().lower() == self.admin_email
        )

    def map_memberships(self, provider: str, claims: dict) -> tuple[bool, dict[str, str]]:
        """(super admin?, {group: role}) for an SSO user from the `role_claim` value(s) looked up in `role_map`
        (D41: each matching rule adds its groups at its role; the highest role wins per group)."""
        claim, table = self.role_claim.get(provider), self.role_map.get(provider, {})
        values = claims.get(claim) if claim else None
        values = values if isinstance(values, list) else [values] if values is not None else []
        super_, memberships = False, {}
        for v in values:
            hit = table.get(str(v).lower())
            if not hit:
                continue
            role, groups = hit
            if role == "super_admin":
                super_ = True
                continue
            for g in groups:
                if g not in memberships or rbac.rank(role) > rbac.rank(memberships[g]):
                    memberships[g] = role
        return super_, memberships

    def map_role(self, provider: str, claims: dict) -> tuple[str, list[str]]:
        """The old summary of `map_memberships`: (highest role, every group) — viewer/no groups when nothing matches."""
        super_, m = self.map_memberships(provider, claims)
        if super_:
            return "super_admin", []
        best = max(m.values(), key=rbac.rank) if m else "viewer"
        return best, sorted(m)

    def public(self) -> dict:
        return {
            "password_login": self.password_login,
            "magic_link": self.magic_link,
            "break_glass": self.force_password,
            "role_claim": dict(self.role_claim),
            "role_map": {p: {k: {"role": r, "groups": g} for k, (r, g) in m.items()} for p, m in self.role_map.items()},
            "groups_source": dict(self.groups_source),
        }
