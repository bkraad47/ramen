from dataclasses import dataclass, field
from fnmatch import fnmatch

from fastapi import HTTPException, Request

ROLES = ("super_admin", "group_admin", "viewer", "mcp_user")
RANK = {r: i for i, r in enumerate(reversed(ROLES))}
GROUP_ROLES = ("group_admin", "viewer", "mcp_user")  # what a membership can be; super admin is global
# How a role is written for a person (U6/U10); the key is also the CSS token suffix on `.role-*`.
ROLE_LABELS = {"super_admin": "Super Admin", "group_admin": "Group Admin", "viewer": "Viewer", "mcp_user": "MCP User"}
AGENT_KEY_REFUSED = "Agent key cannot call the console API"
MCP_USER_REFUSED = "MCP users connect MCP clients to their groups' workers; the console is not available to them"


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, (role or "").replace("_", " ").title())


def highest(roles) -> str | None:
    best = None
    for r in roles:
        if r in RANK and (best is None or RANK[r] > RANK[best]):
            best = r
    return best


def memberships_of(doc: dict) -> dict[str, str]:
    """A user document's per-group roles (D41). Documents from before 0.5.95 carry `role` + `groups`: the same role
    in every listed group. A super admin has none (the role is global)."""
    if doc.get("role") == "super_admin":
        return {}
    m = doc.get("memberships")
    if m is None:
        role = doc.get("role") if doc.get("role") in GROUP_ROLES else "viewer"
        m = {g: role for g in doc.get("groups") or []}
    return {g: r for g, r in m.items() if r in GROUP_ROLES}


def summarize(doc: dict) -> dict:
    """Set the derived `role` (the highest membership) and `groups` (every group with one) on a document."""
    if doc.get("role") == "super_admin":
        doc["memberships"], doc["groups"] = {}, []
        return doc
    m = memberships_of(doc)
    doc["memberships"] = m
    doc["groups"] = sorted(m)
    # nothing anywhere: the role the account was given stands as its label (it still opens nothing)
    doc["role"] = highest(m.values()) or (doc.get("role") if doc.get("role") in GROUP_ROLES else "viewer")
    return doc


@dataclass
class Principal:
    id: str
    name: str
    role: str
    groups: list[str] = field(default_factory=list)
    kind: str = "user"
    client_type: str = "devops"
    memberships: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.memberships and self.role != "super_admin" and self.groups:
            self.memberships = {g: self.role for g in self.groups if self.role in GROUP_ROLES}

    @property
    def label(self) -> str:
        return role_label(self.role)

    def role_in(self, group: str) -> str | None:
        return "super_admin" if self.role == "super_admin" else self.memberships.get(group)

    def to_dict(self):
        d = {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "groups": self.groups,
            "memberships": dict(self.memberships),
            "kind": self.kind,
        }
        if self.kind == "user":
            d["email"] = self.name
        else:
            d["client_type"] = self.client_type
        return d


def principal_from(doc: dict) -> Principal:
    m = memberships_of(doc)
    role = "super_admin" if doc.get("role") == "super_admin" else (highest(m.values()) or doc.get("role") or "viewer")
    return Principal(doc["id"], doc["email"], role, sorted(m), memberships=m)


def can(p: Principal | None, role: str, group: str | None = None) -> bool:
    """The one rule (D41): a super admin may do anything; otherwise the membership in `group` must rank at least
    `role`; with no group named, a membership of that rank anywhere is enough (page-level gates)."""
    if p is None:
        return False
    if p.role == "super_admin":
        return True
    if role not in RANK:
        return False
    if group:
        r = p.memberships.get(group)
        return r is not None and RANK[r] >= RANK[role]
    return any(RANK[r] >= RANK[role] for r in p.memberships.values())


def can_connect(p: Principal | None, group: str) -> bool:
    """May this person's OAuth client reach `group`'s workers? Any membership there, as long as it exists."""
    return can(p, "mcp_user", group)


def require(role: str, group_param: str | None = None):
    async def dep(request: Request) -> Principal:
        p = getattr(request.state, "principal", None)
        if p is None:
            if not request.url.path.startswith("/api/"):
                from urllib.parse import quote

                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                from .baseuri import link

                raise HTTPException(303, headers={"Location": link(request, f"/login?next={quote(target, safe='')}")})
            raise HTTPException(401, "Authentication required")
        if p.kind == "apikey" and p.client_type == "agent":  # D21: agent keys belong to workers, not the console
            raise HTTPException(403, AGENT_KEY_REFUSED)
        group = request.path_params.get(group_param) if group_param else None
        if role == "mcp_user" and not group:  # "any signed-in person": their own page, /me, the consent flow
            return p
        if role != "mcp_user" and not can(p, "viewer"):  # an MCP user (or nobody yet) has no console at all
            raise HTTPException(403, MCP_USER_REFUSED)
        if not can(p, role, group):
            raise HTTPException(403, f"Requires {role_label(role)}" + (f" on group {group}" if group else ""))
        return p

    return dep


class RuleClash(Exception):
    pass


def _valid(rules):
    for r in rules:
        if not isinstance(r, dict) or r.get("effect") not in ("allow", "deny") or not r.get("permission"):
            raise RuleClash(f"malformed rule: {r!r}")


def check_clash(super_rules: list[dict], group_rules: list[dict]) -> None:
    _valid(super_rules)
    _valid(group_rules)
    denied = [r["permission"] for r in super_rules if r["effect"] == "deny"]
    for r in group_rules:
        if r["effect"] == "allow":
            hit = next((d for d in denied if fnmatch(r["permission"], d)), None)
            if hit:
                raise RuleClash(f"group rule allows {r['permission']!r} but super-admin rule denies {hit!r}")
