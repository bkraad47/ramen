"""U25: the security assumptions behind groups, roles and key types, asserted as a matrix.

Three axes, every combination checked:
  * role      super_admin / group_admin / viewer
  * group     a caller's own group / another group it was never given
  * key type  a cookie session, a `devops` API key (`rmn_`), an `agent` key (`rmk_`)  — D21

and the revocation rule (V1.4, folded into U25): a session dies the moment the user behind it is deleted, has
their role or groups changed, has their password reset, or the auth configuration changes.

Nothing here needs a cloud: two throwaway groups and four throwaway identities against any console URL.
The two cases that need a worker (`devops` key refused over gRPC) skip without RAMEN_NODE_URL.
"""

import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.console import SESSION_COOKIE, Console
from ramen_tests.mcp_client import Node

pytestmark = pytest.mark.conformance
PW = "Passw0rd!-for-tests"


# -- fixtures ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def two_groups(admin, suffix, demo_repo) -> tuple[str, str]:
    names = (f"secmxa{suffix}", f"secmxb{suffix}")
    for g in names:
        r = admin.create_group(g, demo_repo)
        assert r.status_code in (201, 409), f"{g}: {r.status_code} {r.text[:300]}"
    return names


@pytest.fixture(scope="module")
def people(admin, two_groups, suffix, console_url) -> dict[str, Console]:
    """One logged-in console client per (role, groups) identity under test."""
    a, b = two_groups
    spec = {
        "ga_a": ("group_admin", [a]),
        "ga_b": ("group_admin", [b]),
        "viewer_a": ("viewer", [a]),
    }
    clients: dict[str, Console] = {}
    for who, (role, groups) in spec.items():
        email = f"{who}-{suffix}@ramen.test"
        r = admin.create_user(email, PW, role, groups)
        assert r.status_code in (201, 409), f"{email}: {r.status_code} {r.text[:300]}"
        c = Console(console_url)
        lr = c.login(email, PW)
        assert lr.status_code in (200, 303), f"{email} login: {lr.status_code} {lr.text[:200]}"
        assert c.me().status_code == 200, f"{email} has no session"
        clients[who] = c
    yield clients
    for c in clients.values():
        c.close()


@pytest.fixture(scope="module")
def keys(admin, two_groups, suffix, console_url) -> dict[str, Console]:
    """A console client per API-key identity. `agent` keys are expected to be refused everywhere, which is the point."""
    a, _ = two_groups
    made: dict[str, Console] = {}
    for who, (role, groups, ctype) in {
        "devops_admin_a": ("group_admin", [a], "devops"),
        "devops_viewer_a": ("viewer", [a], "devops"),
        "agent_a": ("viewer", [a], "agent"),
    }.items():
        r = admin.create_api_key(f"{who}-{suffix}", groups=groups, role=role, client_type=ctype)
        assert r.status_code == 201, f"{who}: {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("client_type") == ctype, body
        made[who] = Console(console_url, api_key=body["key"])
        made[who].raw_key = body["key"]
    yield made
    for c in made.values():
        c.close()


def forbidden(r) -> bool:
    return r.status_code == 403


# -- role x group -----------------------------------------------------------------------------------------------
def test_group_admin_reaches_only_its_own_group(people, two_groups):
    a, b = two_groups
    ga = people["ga_a"]
    assert ga.get("group", group=a).status_code == 200
    assert forbidden(ga.get("group", group=b)), "group_admin read another group"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c, g: c.put("group", {"ref": "main"}, group=g), id="update-group"),
        pytest.param(lambda c, g: c.create_environment(g, "x", []), id="create-env"),
        pytest.param(lambda c, g: c.post("mcp_keys", {"name": "x"}, group=g), id="mint-mcp-key"),
        pytest.param(lambda c, g: c.add_secret(g, "X", "y"), id="add-secret"),
        pytest.param(lambda c, g: c.get("secrets", group=g), id="read-secrets"),
        pytest.param(lambda c, g: c.post("rebalance", {}, group=g, zone="any"), id="rebalance"),
        pytest.param(lambda c, g: c.put("ip_rules", {"cidrs": ["10.0.0.0/8"]}, group=g, zone="any"), id="ip-rules"),
        pytest.param(lambda c, g: c.put("workers", {"count": 2}, group=g, zone="any"), id="scale-workers"),
        pytest.param(lambda c, g: c.put("env_blocked", {"blocked": ["x"]}, group=g, env="dev"), id="block-env"),
        pytest.param(
            lambda c, g: c.put("env_zone_blocked", {"blocked": ["x"]}, group=g, env="dev", zone="any"),
            id="block-zone",
        ),
        pytest.param(lambda c, g: c.put("sa_restrictions", {"rules": []}, group=g), id="sa-restrictions"),
        pytest.param(lambda c, g: c.deploy(g, "dev"), id="deploy"),
        pytest.param(lambda c, g: c.delete("group", group=g), id="delete-group"),
    ],
)
def test_every_cross_group_action_is_refused(people, two_groups, call):
    a, b = two_groups
    assert forbidden(call(people["ga_a"], b)), "group_admin acted on a group it does not hold"
    assert forbidden(call(people["ga_b"], a)), "group_admin acted on a group it does not hold"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c, g: c.put("group", {"ref": "main"}, group=g), id="update-group"),
        pytest.param(lambda c, g: c.create_environment(g, "x", []), id="create-env"),
        pytest.param(lambda c, g: c.post("mcp_keys", {"name": "x"}, group=g), id="mint-mcp-key"),
        pytest.param(lambda c, g: c.add_secret(g, "X", "y"), id="add-secret"),
        pytest.param(lambda c, g: c.post("rebalance", {}, group=g, zone="any"), id="rebalance"),
        pytest.param(lambda c, g: c.put("workers", {"count": 2}, group=g, zone="any"), id="scale-workers"),
        pytest.param(lambda c, g: c.deploy(g, "dev"), id="deploy"),
        pytest.param(lambda c, g: c.create_user(f"x-{g}@ramen.test", PW, "viewer", [g]), id="create-user"),
        pytest.param(lambda c, g: c.create_api_key("x", groups=[g], role="viewer"), id="mint-api-key"),
    ],
)
def test_a_viewer_cannot_change_even_its_own_group(people, two_groups, call):
    a, _ = two_groups
    assert forbidden(call(people["viewer_a"], a)), "viewer performed an admin action on its own group"


def test_a_viewer_can_still_read_its_own_group(people, two_groups):
    a, _ = two_groups
    v = people["viewer_a"]
    assert v.get("group", group=a).status_code == 200
    assert v.me().status_code == 200
    assert v.get("dashboard").status_code == 200


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.create_zone("mxzone"), id="create-zone"),
        pytest.param(lambda c: c.delete("zone", zone="mxzone"), id="delete-zone"),
        pytest.param(lambda c: c.post("service_account", {}, group="g", zone="z"), id="create-service-account"),
        pytest.param(lambda c: c.put("config_auth", {"magic_link": True}), id="set-auth-config"),
        pytest.param(lambda c: c.get("config_auth"), id="read-auth-config"),
        pytest.param(lambda c: c.put("sa_rules", {"rules": []}), id="set-sa-rules"),
        pytest.param(lambda c: c.get("api_keys"), id="list-api-keys"),  # 0.5.95 (N19): API keys are super-admin only
        pytest.param(lambda c: c.get("audit"), id="read-audit"),
    ],
)
def test_super_admin_only_actions_are_refused_to_a_group_admin(people, call):
    assert forbidden(call(people["ga_a"])), "group_admin performed a super-admin action"


def test_a_group_admin_cannot_grant_beyond_itself(people, two_groups, suffix):
    a, b = two_groups
    ga = people["ga_a"]
    assert forbidden(ga.create_user(f"esc1-{suffix}@ramen.test", PW, "super_admin", [a])), "role escalation allowed"
    assert forbidden(ga.create_user(f"esc2-{suffix}@ramen.test", PW, "group_admin", [a])), "same-rank grant allowed"
    assert forbidden(ga.create_user(f"esc3-{suffix}@ramen.test", PW, "viewer", [b])), "grant into a foreign group"
    assert forbidden(ga.create_api_key(f"esc4-{suffix}", groups=[b], role="viewer")), "key into a foreign group"
    assert forbidden(ga.create_api_key(f"esc5-{suffix}", groups=[a], role="super_admin")), "key above own role"


# -- key type (D21) ---------------------------------------------------------------------------------------------
def test_a_devops_key_obeys_the_same_role_and_group_limits(keys, two_groups):
    a, b = two_groups
    admin_key, viewer_key = keys["devops_admin_a"], keys["devops_viewer_a"]
    assert admin_key.get("group", group=a).status_code == 200
    assert forbidden(admin_key.get("group", group=b)), "devops key read a foreign group"
    assert viewer_key.get("group", group=a).status_code == 200
    assert forbidden(viewer_key.post("mcp_keys", {"name": "x"}, group=a)), "viewer key performed an admin action"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c, g: c.me(), id="me"),
        pytest.param(lambda c, g: c.get("groups"), id="list-groups"),
        pytest.param(lambda c, g: c.get("group", group=g), id="read-group"),
        pytest.param(lambda c, g: c.get("dashboard"), id="dashboard"),
        pytest.param(lambda c, g: c.get("audit"), id="audit"),
        pytest.param(lambda c, g: c.post("mcp_keys", {"name": "x"}, group=g), id="mint-mcp-key"),
        pytest.param(lambda c, g: c.deploy(g, "dev"), id="deploy"),
        pytest.param(lambda c, g: c.get("logs", params={"group": g, "zone": "any"}), id="logs"),
    ],
)
def test_an_agent_key_is_refused_by_the_console_api(keys, two_groups, call):
    """D21: `rmk_` keys are worker credentials. The console must refuse them everywhere, read or write."""
    a, _ = two_groups
    r = call(keys["agent_a"], a)
    assert r.status_code == 403, f"agent key accepted by the console API: {r.status_code} {r.text[:200]}"
    assert "agent key" in r.text.lower(), f"refusal does not name the reason: {r.text[:200]}"


TRANSIENT = (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)


def test_a_devops_key_is_refused_by_a_worker(keys):
    """The other half of D21: a console credential is not an MCP credential.

    Through a cloud load balancer a call can come back UNAVAILABLE while pods are draining after a rollout, which
    says nothing about the key, so transport failures are retried rather than read as a verdict.
    """
    target, tls = E.node_target(E.require("RAMEN_NODE_URL"))
    code = None
    for attempt in range(6):
        with Node(
            target,
            keys["devops_admin_a"].raw_key,
            group=E.env("RAMEN_E2E_GROUP", "demo"),
            zone=E.env("RAMEN_E2E_ZONE", "local"),
            tls=tls,
            ca=E.env("RAMEN_NODE_CA"),
        ) as n:
            code = n.status({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        if code not in TRANSIENT:
            break
        time.sleep(5 * (attempt + 1))
    assert code == grpc.StatusCode.UNAUTHENTICATED, f"a worker accepted a devops key: {code.name}"


def test_a_key_cannot_be_replayed_under_the_other_prefix(keys, console_url):
    """`rmn_<id>_<secret>` and `rmk_<id>_<secret>` must not be interchangeable for the same stored key."""
    raw = keys["devops_admin_a"].raw_key
    swapped = "rmk_" + raw.split("_", 1)[1]
    with Console(console_url, api_key=swapped) as c:
        r = c.me()
    assert r.status_code in (401, 403), f"a devops key was accepted under the agent prefix: {r.status_code}"


# -- revocation (V1.4) ------------------------------------------------------------------------------------------
@pytest.fixture
def victim(admin, two_groups, suffix, console_url):
    """A fresh user and a live session for it, per revocation test."""
    import uuid

    a, _ = two_groups
    email = f"revoke-{uuid.uuid4().hex[:8]}@ramen.test"
    r = admin.create_user(email, PW, "viewer", [a])
    assert r.status_code == 201, r.text[:300]
    uid = r.json()["id"]
    c = Console(console_url)
    assert c.login(email, PW).status_code in (200, 303)
    assert c.me().status_code == 200
    yield {"id": uid, "email": email, "client": c}
    c.close()
    admin.delete("user", id=uid)


def test_deleting_a_user_ends_their_session_at_once(admin, victim):
    assert admin.delete("user", id=victim["id"]).status_code == 200
    assert victim["client"].me().status_code == 401, "a deleted user kept a working session"


def test_changing_a_role_ends_their_session_at_once(admin, victim):
    assert admin.put("user", {"role": "group_admin"}, id=victim["id"]).status_code == 200
    assert victim["client"].me().status_code == 401, "a role change left the old session usable"


def test_changing_groups_ends_their_session_at_once(admin, victim, two_groups):
    _, b = two_groups
    assert admin.put("user", {"groups": [b]}, id=victim["id"]).status_code == 200
    assert victim["client"].me().status_code == 401, "a group change left the old session usable"


def test_resetting_a_password_ends_their_session_at_once(admin, victim):
    r = admin.post("user_password", {"password": "An0ther!-Passw0rd"}, id=victim["id"])
    assert r.status_code == 200, r.text[:300]
    assert victim["client"].me().status_code == 401, "a password reset left the old session usable"


def test_changing_the_auth_config_ends_every_session(admin, victim, console_url):
    """An auth-configuration change bumps every epoch, so every session minted under the old rules is dead —
    including the caller's. The console re-issues the acting super admin a session on the new epoch in the same
    response (otherwise it would sign them out of the request they just authenticated), so the test is that the
    *old cookie value* stops working, not that the caller is locked out."""
    if E.env("RAMEN_ALLOW_SESSION_RESET") != "1":
        pytest.skip(
            "RAMEN_ALLOW_SESSION_RESET=1 not set: PUT /config/auth bumps EVERY user's session epoch, which kills the "
            "logged-in clients of any suite that ran before it in the same process. Run this file on its own."
        )
    before = admin.http.cookies.get(SESSION_COOKIE)
    assert before, "no session cookie to invalidate"

    r = admin.put("config_auth", {"magic_link": False})
    assert r.status_code == 200, r.text[:300]
    assert victim["client"].me().status_code == 401, "an auth-config change left other sessions usable"

    with Console(console_url) as replay:
        replay.http.cookies.set(SESSION_COOKIE, before)
        assert replay.me().status_code == 401, "the session cookie from before the auth-config change still works"

    after = admin.http.cookies.get(SESSION_COOKIE)
    assert after and after != before, "the caller was not re-issued a session on the new epoch"
    assert admin.me().status_code == 200, "the re-issued session does not work"
