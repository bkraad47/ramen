from dataclasses import dataclass, field
from fnmatch import fnmatch

from fastapi import HTTPException, Request

ROLES = ("super_admin", "group_admin", "viewer")
RANK = {r: i for i, r in enumerate(reversed(ROLES))}


@dataclass
class Principal:
    id: str
    name: str
    role: str
    groups: list[str] = field(default_factory=list)
    kind: str = "user"

    def to_dict(self):
        d = {"id": self.id, "name": self.name, "role": self.role, "groups": self.groups, "kind": self.kind}
        if self.kind == "user":
            d["email"] = self.name
        return d


def can(p: Principal | None, role: str, group: str | None = None) -> bool:
    if p is None:
        return False
    if p.role == "super_admin":
        return True
    if RANK.get(p.role, -1) < RANK[role]:
        return False
    return group in p.groups if group else True


def require(role: str, group_param: str | None = None):
    async def dep(request: Request) -> Principal:
        p = getattr(request.state, "principal", None)
        if p is None:
            if not request.url.path.startswith("/api/"):
                raise HTTPException(303, headers={"Location": f"/login?next={request.url.path}"})
            raise HTTPException(401, "authentication required")
        group = request.path_params.get(group_param) if group_param else None
        if not can(p, role, group):
            raise HTTPException(403, f"requires {role}" + (f" on group {group}" if group else ""))
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
