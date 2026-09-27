import pytest
from fastapi import HTTPException

from ramen_console.rbac import Principal, RuleClash, can, check_clash, require


def p(role, groups=()):
    return Principal(id="x", name="x", role=role, groups=list(groups))


def test_can():
    assert can(p("super_admin"), "super_admin")
    assert can(p("super_admin"), "viewer", "g")
    assert not can(p("group_admin", ["g"]), "super_admin")
    assert can(p("group_admin", ["g"]), "group_admin", "g")
    assert can(p("group_admin", ["g"]), "group_admin")
    assert not can(p("group_admin", ["g"]), "group_admin", "other")
    assert can(p("group_admin", ["g"]), "viewer", "g")
    assert can(p("viewer", ["g"]), "viewer", "g")
    assert can(p("viewer", ["g"]), "viewer")
    assert not can(p("viewer", ["g"]), "viewer", "o")
    assert not can(p("viewer", ["g"]), "group_admin", "g")
    assert can(p("viewer", []), "viewer")
    assert not can(p("viewer", []), "viewer", "g")
    assert not can(None, "viewer")


class Req:
    def __init__(self, principal, group=None, hx=False):
        self.state = type("S", (), {})()
        self.state.principal = principal
        self.path_params = {"group": group} if group else {}
        self.url = type("U", (), {"path": "/page" if hx else "/api/v1/x"})()


async def test_require():
    dep = require("group_admin", group_param="group")
    assert (await dep(Req(p("group_admin", ["g"]), "g"))).role == "group_admin"
    with pytest.raises(HTTPException) as e:
        await dep(Req(p("viewer", ["g"]), "g"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await dep(Req(None))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        await dep(Req(None, hx=True))
    assert e.value.status_code == 303 and e.value.headers["Location"].startswith("/login")


def test_check_clash():
    super_rules = [{"effect": "deny", "permission": "iam.*"}, {"effect": "deny", "permission": "storage.buckets.delete"}]
    check_clash(super_rules, [{"effect": "allow", "permission": "storage.objects.get"}])
    check_clash(super_rules, [{"effect": "deny", "permission": "iam.roles.create"}])
    with pytest.raises(RuleClash, match="iam.roles.create"):
        check_clash(super_rules, [{"effect": "allow", "permission": "iam.roles.create"}])
    with pytest.raises(RuleClash):
        check_clash(super_rules, [{"effect": "allow", "permission": "storage.buckets.delete"}])
    with pytest.raises(RuleClash, match="malformed"):
        check_clash(super_rules, [{"permission": "x"}])
    with pytest.raises(RuleClash, match="malformed"):
        check_clash([{"effect": "allow"}], [])
