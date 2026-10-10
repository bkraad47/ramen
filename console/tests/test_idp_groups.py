"""CONTRACTS §21.3 (0.7.5, D47): identity federation — a provider's group source (`claim` or `lookup` against Microsoft
Graph / Google Cloud Identity), memberships from both sides (`idp_memberships` rewritten at every SSO login,
`manual_memberships` set in the console and winning per group), and the two providers side by side on any adapter."""

import asyncio
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import ramen_console.cloud as cloud_pkg
from ramen_console import rbac
from ramen_console.auth import groups
from ramen_console.auth.oauth import OAuthRegistry
from ramen_console.auth.settings import AuthSettings
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_flow import ISSUER, FakeIdp, start
from tests.test_oauth_server import consent, exchange, pkce, refresh, register
from tests.test_tool_access import _claims

GRAPH = "https://graph.test/v1.0"
CI = "https://ci.test/v1"


@pytest.fixture(autouse=True)
def _clean_registry():
    rbac.set_custom_roles({})
    yield
    rbac.set_custom_roles({})


# --- the lookup itself ----------------------------------------------------------------------------------------------


class FakeGraph:
    def __init__(self):
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.headers["Authorization"] == "Bearer at-1"
        if req.url.path == "/v1.0/me/memberOf" and "skip" not in str(req.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"@odata.type": "#microsoft.graph.group", "id": "g-1", "displayName": "Ramen Admins"},
                        {"@odata.type": "#microsoft.graph.directoryRole", "id": "r-1", "displayName": "Global Reader"},
                    ],
                    "@odata.nextLink": f"{GRAPH}/me/memberOf?skip=2",
                },
            )
        if req.url.path == "/v1.0/me/memberOf":
            return httpx.Response(200, json={"value": [{"id": "g-2", "displayName": "Analysts"}]})
        return httpx.Response(404)


def test_entra_lookup_reads_every_page_of_member_of_and_returns_ids_and_names():
    g = FakeGraph()
    out = asyncio.run(groups.lookup("entra", "at-1", "a@x", GRAPH, transport=httpx.MockTransport(g.handler)))
    assert out == ["g-1", "Ramen Admins", "r-1", "Global Reader", "g-2", "Analysts"]
    assert len(g.calls) == 2 and "$select=id,displayName" in str(g.calls[0].url).replace("%24", "$")


def test_google_lookup_searches_direct_groups_by_email():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer at-1"
        assert req.url.path == "/v1/groups/-/memberships:searchDirectGroups"
        assert req.url.params["query"] == "member_key_id=='a@corp.test'"
        if req.url.params.get("pageToken") == "p2":
            return httpx.Response(
                200, json={"memberships": [{"groupKey": {"id": "ops@corp.test"}, "group": "groups/2"}]}
            )
        return httpx.Response(
            200,
            json={
                "memberships": [
                    {"groupKey": {"id": "devs@corp.test"}, "group": "groups/1", "displayName": "Developers"},
                ],
                "nextPageToken": "p2",
            },
        )

    out = asyncio.run(groups.lookup("google", "at-1", "a@corp.test", CI, transport=httpx.MockTransport(handler)))
    # §21.3: the group email (`groupKey.id`), the `group` resource name, and the display name when Google sends one
    assert out == ["devs@corp.test", "groups/1", "Developers", "ops@corp.test", "groups/2"]


def test_lookup_failures_are_a_502_and_other_providers_cannot_lookup():
    def down(req):
        return httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})

    with pytest.raises(groups.LookupError, match="could not read your groups from entra"):
        asyncio.run(groups.lookup("entra", "at-1", "a@x", GRAPH, transport=httpx.MockTransport(down)))

    def boom(req):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(groups.LookupError, match="google"):
        asyncio.run(groups.lookup("google", "at-1", "a@x", CI, transport=httpx.MockTransport(boom)))
    with pytest.raises(ValueError, match="lookup"):
        asyncio.run(groups.lookup("okta", "at-1", "a@x", None))
    assert groups.SCOPES == {
        "entra": "User.Read",
        "google": "https://www.googleapis.com/auth/cloud-identity.groups.readonly",
    }
    assert groups.BASE["entra"] == "https://graph.microsoft.com/v1.0"
    assert groups.BASE["google"] == "https://cloudidentity.googleapis.com/v1"


# --- settings and registry ------------------------------------------------------------------------------------------


def _env(name="entra", **extra):
    return {
        f"RAMEN_OAUTH_{name.upper()}_ISSUER": ISSUER,
        f"RAMEN_OAUTH_{name.upper()}_CLIENT_ID": "cid",
        f"RAMEN_OAUTH_{name.upper()}_CLIENT_SECRET": "sec",
        **extra,
    }


def test_groups_source_comes_from_env_then_the_store_doc():
    s = AuthSettings.from_env({"RAMEN_OAUTH_ENTRA_GROUPS": "lookup", "RAMEN_OAUTH_GOOGLE_GROUPS": "claim"})
    assert s.groups_source == {"entra": "lookup", "google": "claim"}
    assert s.source_of("entra") == "lookup" and s.source_of("okta") == "claim"
    with pytest.raises(ValueError, match="claim or lookup"):
        AuthSettings.from_env({"RAMEN_OAUTH_ENTRA_GROUPS": "graph"})
    s2 = s.with_doc({"groups_source": {"entra": "claim", "google": "lookup"}})
    assert s2.groups_source == {"entra": "claim", "google": "lookup"}  # the doc wins per provider
    assert s2.public()["groups_source"] == {"entra": "claim", "google": "lookup"}
    assert s.with_doc({"groups_source": {"entra": "bogus"}}).source_of("entra") == "lookup"  # junk ignored


def test_registry_adds_the_lookup_scope_and_refuses_lookup_on_an_unknown_provider():
    idp = FakeIdp({})
    reg = OAuthRegistry.from_env(_env(RAMEN_OAUTH_ENTRA_GROUPS="lookup"), transport=httpx.MockTransport(idp.handler))
    assert "User.Read" in reg.scope("entra").split() and "openid" in reg.scope("entra").split()
    reg = OAuthRegistry.from_env(
        _env("google", RAMEN_OAUTH_GOOGLE_GROUPS="lookup"), transport=httpx.MockTransport(idp.handler)
    )
    assert groups.SCOPES["google"] in reg.scope("google").split()
    reg = OAuthRegistry.from_env(_env(), transport=httpx.MockTransport(idp.handler))
    assert "User.Read" not in reg.scope("entra")
    reg.set_groups_source("entra", "lookup")  # the Config page flips it at runtime: the client is re-registered
    assert "User.Read" in reg.scope("entra").split()
    reg.set_groups_source("entra", "claim")
    assert "User.Read" not in reg.scope("entra")
    with pytest.raises(ValueError, match="lookup"):
        OAuthRegistry.from_env(_env("okta", RAMEN_OAUTH_OKTA_GROUPS="lookup"))
    assert reg.groups_url("entra") is None
    reg = OAuthRegistry.from_env(_env(RAMEN_OAUTH_ENTRA_GROUPS_URL=GRAPH))
    assert reg.groups_url("entra") == GRAPH


# --- the login flow with a lookup -----------------------------------------------------------------------------------


def _wire(demo, idp: FakeIdp, graph: FakeGraph, name="entra", source="lookup"):
    env = _env(name, **{f"RAMEN_OAUTH_{name.upper()}_GROUPS": source, f"RAMEN_OAUTH_{name.upper()}_GROUPS_URL": GRAPH})
    demo.app.state.oauth = OAuthRegistry.from_env(env, transport=httpx.MockTransport(idp.handler))
    demo.app.state.groups_transport = httpx.MockTransport(graph.handler)
    demo.app.state.auth_env = AuthSettings.from_env(env)


def _sso(demo, idp, name="entra"):
    with TestClient(demo.app) as anon:
        state = start(anon, idp, name)
        r = anon.get(f"/auth/{name}/callback?code=good-code&state={state}", follow_redirects=False)
        return r, (anon.get("/api/v1/me").json() if r.status_code == 303 else None)


def test_lookup_groups_feed_the_mapping_and_the_idp_side_of_memberships(demo):
    idp, graph = FakeIdp({"email": "dev@corp.test", "email_verified": True, "groups": ["stale-claim"]}), FakeGraph()
    _wire(demo, idp, graph)
    rm = "/api/v1/config/auth/role-map/entra"
    assert demo.put(rm, json={"claim": "groups"}).status_code == 200
    assert demo.put(rm, json={"value": "g-1", "role": "group_admin", "groups": ["demo"]}).status_code == 200
    assert demo.put(rm, json={"value": "Analysts", "role": "viewer", "groups": ["other"]}).status_code == 200
    assert demo.put(rm, json={"value": "stale-claim", "role": "group_admin", "groups": ["other"]}).status_code == 200
    r, me = _sso(demo, idp)
    assert r.status_code == 303, r.text
    # the token's own `groups` claim is replaced by the lookup: `stale-claim` never matched
    assert me["memberships"] == {"demo": "group_admin", "other": "viewer"}
    users = {u["email"]: u for u in demo.get("/api/v1/users").json()}
    u = users["dev@corp.test"]
    assert u["sources"] == {"demo": "idp", "other": "idp"} and u["provider"] == "entra"
    members = {m["email"]: m for m in demo.get("/api/v1/groups/demo/members").json()}
    assert members["dev@corp.test"]["source"] == "idp"
    assert any(a["action"] == "login.oauth" and "groups:lookup" in a["tags"] for a in demo.get("/api/v1/audit").json())


def test_lookup_failure_signs_nobody_in_with_stale_groups(demo):
    idp, graph = FakeIdp({"email": "dev@corp.test", "email_verified": True}), FakeGraph()
    _wire(demo, idp, graph)
    demo.put("/api/v1/config/auth/role-map/entra", json={"claim": "groups"})
    demo.put("/api/v1/config/auth/role-map/entra", json={"value": "g-1", "role": "viewer", "groups": ["demo"]})
    r, me = _sso(demo, idp)
    assert me["memberships"] == {"demo": "viewer"}

    def down(req):
        return httpx.Response(500, text="boom")

    demo.app.state.groups_transport = httpx.MockTransport(down)
    r, me = _sso(demo, idp)
    assert r.status_code == 502 and "could not read your groups from entra" in r.text.lower() and me is None
    # the stored memberships were not touched by the failed attempt
    u = next(u for u in demo.get("/api/v1/users").json() if u["email"] == "dev@corp.test")
    assert u["memberships"] == {"demo": "viewer"}


def test_memberships_from_both_sides_and_removal_from_the_idp_group(demo):
    idp, graph = FakeIdp({"email": "dev@corp.test", "email_verified": True}), FakeGraph()
    _wire(demo, idp, graph)
    demo.put("/api/v1/config/auth/role-map/entra", json={"claim": "groups"})
    demo.put("/api/v1/config/auth/role-map/entra", json={"value": "g-1", "role": "viewer", "groups": ["demo"]})
    _, me = _sso(demo, idp)
    assert me["memberships"] == {"demo": "viewer"}
    uid = next(u["id"] for u in demo.get("/api/v1/users").json() if u["email"] == "dev@corp.test")
    # an admin overrides the person in `demo` and adds `other` by hand
    assert demo.put(f"/api/v1/groups/demo/members/{uid}", json={"role": "group_admin"}).status_code == 200
    assert demo.put(f"/api/v1/groups/other/members/{uid}", json={"role": "mcp_user"}).status_code == 200
    _, me = _sso(demo, idp)
    assert me["memberships"] == {"demo": "group_admin", "other": "mcp_user"}  # manual wins, idp still there under it
    u = next(u for u in demo.get("/api/v1/users").json() if u["email"] == "dev@corp.test")
    assert u["sources"] == {"demo": "manual", "other": "manual"}
    # taking the manual entry away uncovers the IdP's viewer again
    assert demo.delete(f"/api/v1/groups/demo/members/{uid}").status_code == 200
    _, me = _sso(demo, idp)
    assert me["memberships"] == {"demo": "viewer", "other": "mcp_user"}
    # removed from the Entra group: the idp side is gone at the next login, the manual one stays
    graph_empty = FakeGraph()
    graph_empty.handler = lambda req: httpx.Response(200, json={"value": []})
    demo.app.state.groups_transport = httpx.MockTransport(graph_empty.handler)
    _, me = _sso(demo, idp)
    assert me["memberships"] == {"other": "mcp_user"}
    assert demo.get("/api/v1/groups/demo/members").json() == [
        m for m in demo.get("/api/v1/groups/demo/members").json() if m["email"] != "dev@corp.test"
    ]


def test_removed_from_the_idp_group_a_refresh_token_from_before_dies_at_the_next_sign_in(demo):
    """Brief negative + §21.5-3: the MCP client's refresh token minted while the person was in the group stops minting
    once a sign-in has seen them removed (epoch bump), and the person can no longer consent for that group."""
    idp, graph = FakeIdp({"email": "dev@corp.test", "email_verified": True}), FakeGraph()
    _wire(demo, idp, graph)
    demo.put("/api/v1/config/auth/role-map/entra", json={"claim": "groups"})
    demo.put("/api/v1/config/auth/role-map/entra", json={"value": "g-1", "role": "mcp_user", "groups": ["demo"]})
    cid = register(demo)
    with TestClient(demo.app) as person:
        state = start(person, idp, "entra")
        assert (
            person.get(f"/auth/entra/callback?code=good-code&state={state}", follow_redirects=False).status_code == 303
        )
        verifier, challenge = pkce()
        tok = exchange(demo, cid, consent(person, cid, challenge), verifier).json()
    assert tok["access_token"] and tok["refresh_token"]
    tok = refresh(demo, cid, tok["refresh_token"]).json()  # still in the group: the client renews silently
    assert "refresh_token" in tok, tok
    demo.app.state.groups_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"value": []}))
    r, me = _sso(demo, idp)
    assert r.status_code == 303 and me["memberships"] == {}
    r = refresh(demo, cid, tok["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant", r.text
    assert any(
        a["action"] == "oauth.denied" and "reason:sessions_revoked" in a["tags"]
        for a in demo.get("/api/v1/audit").json()
    )


def test_old_documents_migrate_their_memberships_to_the_manual_side(demo):
    st = demo.app.state
    legacy = {
        "email": "old@x",
        "role": "viewer",
        "memberships": {"demo": "viewer"},
        "created": "0",
        "password_hash": "",
    }
    asyncio.run(st.store.put("users", "legacy", legacy))
    assert asyncio.run(st.accounts.migrate_memberships()) >= 1
    doc = asyncio.run(st.store.get("users", "legacy"))
    assert doc["manual_memberships"] == {"demo": "viewer"} and doc["idp_memberships"] == {}
    assert doc["memberships"] == {"demo": "viewer"} and rbac.sources_of(doc) == {"demo": "manual"}
    assert asyncio.run(st.accounts.migrate_memberships()) == 0


def test_a_mapped_super_admin_is_demoted_when_the_mapping_stops_saying_so(demo):
    idp, graph = FakeIdp({"email": "boss@corp.test", "email_verified": True}), FakeGraph()
    _wire(demo, idp, graph)
    demo.put("/api/v1/config/auth/role-map/entra", json={"claim": "groups"})
    demo.put("/api/v1/config/auth/role-map/entra", json={"value": "Ramen Admins", "role": "super_admin"})
    _, me = _sso(demo, idp)
    assert me["role"] == "super_admin"
    graph.handler = lambda req: httpx.Response(200, json={"value": []})
    demo.app.state.groups_transport = httpx.MockTransport(graph.handler)
    _, me = _sso(demo, idp)
    assert me["role"] == "viewer" and me["memberships"] == {}


# --- config page / API for the group source ----------------------------------------------------------------------


def test_super_admin_sets_the_group_source_per_provider(demo):
    idp = FakeIdp({})
    demo.app.state.oauth = OAuthRegistry.from_env(_env(), transport=httpx.MockTransport(idp.handler))
    assert demo.get("/api/v1/config/auth").json()["groups_source"] == {}
    r = demo.put("/api/v1/config/auth/groups-source/entra", json={"source": "lookup"})
    assert r.status_code == 200, r.text
    assert demo.get("/api/v1/config/auth").json()["groups_source"] == {"entra": "lookup"}
    assert "User.Read" in demo.app.state.oauth.scope("entra").split()
    assert demo.put("/api/v1/config/auth/groups-source/entra", json={"source": "graph"}).status_code == 422
    assert demo.put("/api/v1/config/auth/groups-source/nope", json={"source": "claim"}).status_code == 404
    page = demo.get("/config").text
    assert "Group source" in page and 'name="source"' in page
    assert any(
        a["action"] == "config.auth" and "groups_source:lookup" in a["tags"] for a in demo.get("/api/v1/audit").json()
    )
    # google cannot be flipped to lookup when it is not configured as a provider at all → 404 like any unknown name
    demo.app.state.oauth = OAuthRegistry.from_env(_env("okta"), transport=httpx.MockTransport(idp.handler))
    r = demo.put("/api/v1/config/auth/groups-source/okta", json={"source": "lookup"})
    assert r.status_code == 422 and "lookup" in r.json()["detail"]


# --- combination: IdP mapping → custom oauth_only role → token claim; password refused -------------------------------


def test_idp_group_maps_to_a_custom_oauth_only_role_that_the_token_carries(demo):
    demo.put("/api/v1/config/roles/analyst", json={"base": "mcp_user", "label": "Analyst", "oauth_only": True})
    make_user(demo, "an@corp.test", "viewer", ["other"])  # has a password; the provider links by verified email
    idp, graph = FakeIdp({"email": "an@corp.test", "email_verified": True}), FakeGraph()
    _wire(demo, idp, graph)
    demo.put("/api/v1/config/auth/role-map/entra", json={"claim": "groups"})
    assert (
        demo.put(
            "/api/v1/config/auth/role-map/entra", json={"value": "Analysts", "role": "analyst", "groups": ["demo"]}
        ).status_code
        == 200
    )
    with TestClient(demo.app) as anon:
        state = start(anon, idp, "entra")
        assert anon.get(f"/auth/entra/callback?code=good-code&state={state}", follow_redirects=False).status_code == 303
        me = anon.get("/api/v1/me").json()
        assert me["memberships"] == {"demo": "analyst", "other": "viewer"} and me["role"] == "viewer"
        assert _claims(demo, anon)["role"] == "analyst"  # the token names the role in THIS group
    # the same person still has a password and still cannot use it: one oauth_only role anywhere is enough
    with TestClient(demo.app) as c:
        r = c.post("/login", data={"email": "an@corp.test", "password": PW}, follow_redirects=False)
        assert r.status_code == 403 and "signs in with entra" in r.text


def test_both_providers_configured_on_the_local_adapter(demo):
    idp = FakeIdp({})
    env = {**_env("entra", RAMEN_OAUTH_ENTRA_GROUPS="lookup"), **_env("google", RAMEN_OAUTH_GOOGLE_GROUPS="lookup")}
    demo.app.state.oauth = OAuthRegistry.from_env(env, transport=httpx.MockTransport(idp.handler))
    page = TestClient(demo.app).get("/login").text
    assert "/auth/entra/login" in page and "/auth/google/login" in page
    assert demo.app.state.oauth.providers() == ["entra", "google"]
    # nothing cloud-specific: the adapter is `local` here, and the config page lists both the same way
    assert demo.get("/api/v1/config/auth").json()["providers"] == ["entra", "google"]


def test_no_cloud_adapter_reads_identity_provider_settings():
    """§21.3 "IdP and cloud independent": provider settings live in the console's auth layer only, so Entra works on GKE
    and EKS and Google Workspace on either; the adapters' worker env carries the console's own issuer, nothing else."""
    marker = re.compile(r"RAMEN_OAUTH_(?!ISSUER\b)|oauth_providers|groups_source|ROLE_MAP|ROLE_CLAIM|auth\.groups")
    hits = [
        f"{p.name}:{n}"
        for p in sorted(Path(cloud_pkg.__file__).parent.glob("*.py"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if marker.search(line)
    ]
    assert hits == [], hits
