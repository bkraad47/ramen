"""0.5.92: the `mcp_user` role — a person who only connects MCP clients (OAuth) to the workers of their groups and
has no console at all."""

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_server import RESOURCE, authorize, consent, exchange, pkce, register


def test_mcp_user_has_no_console_but_may_connect_to_its_groups(demo):
    make_user(demo, "bot@x", "mcp_user", ["demo"])
    cid = register(demo)
    with TestClient(demo.app) as u:
        login(u, "bot@x", PW)
        home = u.get("/")
        assert home.status_code == 200 and "MCP User" in home.text and "demo" in home.text
        assert "Dashboard" not in home.text and "/groups" not in home.text  # no navigation into the console
        for path in ("/groups", "/groups/demo", "/logs", "/api-keys", "/users", "/config"):
            assert u.get(path, follow_redirects=False).status_code == 403, path
        for path in ("/api/v1/groups", "/api/v1/groups/demo", "/api/v1/zones", "/api/v1/users"):
            assert u.get(path).status_code == 403, path
        assert (
            u.post("/api/v1/api-keys", json={"name": "k", "client_type": "agent", "groups": ["demo"]}).status_code
            == 403
        )
        assert u.get("/api/v1/me").json()["role"] == "mcp_user"
        verifier, challenge = pkce()
        assert authorize(u, cid, challenge).status_code == 200  # consent page for a zone of their group
        code = consent(u, cid, challenge)
    tok = exchange(demo, cid, code, verifier)
    assert tok.status_code == 200 and tok.json()["scope"] == RESOURCE
    make_user(demo, "none@x", "mcp_user", [])
    with TestClient(demo.app) as u:  # a group that is not theirs is refused at consent, like any other role
        login(u, "none@x", PW)
        r = authorize(u, cid, pkce()[1])
        assert r.status_code == 403 and "no access to group demo" in r.text


def test_mcp_user_is_grantable_within_scope_and_mappable(demo, monkeypatch):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        r = ga.post("/api/v1/users", json={"email": "m1@x", "password": PW, "role": "mcp_user", "groups": ["demo"]})
        assert r.status_code == 201 and r.json()["role"] == "mcp_user"
        r = ga.post("/api/v1/users", json={"email": "m2@x", "password": PW, "role": "mcp_user", "groups": ["other"]})
        assert r.status_code == 403
        assert "MCP User" in ga.get("/users").text
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_CLIENT_SECRET", "sec")
    monkeypatch.setenv("RAMEN_OAUTH_OIDC_METADATA_URL", "https://issuer/.well-known/openid-configuration")
    from ramen_console.auth.oauth import OAuthRegistry

    demo.app.state.oauth = OAuthRegistry.from_env()
    rm = "/api/v1/config/auth/role-map/oidc"
    assert demo.put(rm, json={"claim": "roles"}).status_code == 200
    r = demo.put(rm, json={"value": "AD-MCP-Users", "role": "mcp_user", "groups": "demo"})
    assert r.status_code == 200 and r.json()["role_map"]["oidc"]["ad-mcp-users"]["role"] == "mcp_user"
    page = demo.get("/config").text
    assert "MCP User" in page and 'value="mcp_user"' in page
