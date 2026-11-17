"""1.0.0: the MCP user's page gives ready-to-paste commands — the console knows each group's zones and its OAuth client
id (a public PKCE client id, not a secret), so it fills them in instead of `<zone>` / `<client id>` placeholders. The
commands are the same whichever identity provider (Entra, Google) signed the person in: scope and client are Ramen's."""

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_v100b_envs_zones import as_user


def client_id(demo):  # noqa: F811
    r = demo.post("/api/v1/oauth/clients", json={"name": "claude", "redirect_uris": ["http://127.0.0.1:33418/cb"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def test_the_mcp_user_page_fills_in_the_zones_and_the_client_id(demo):  # noqa: F811
    cid = client_id(demo)
    page = as_user(demo.app, demo, "mu@ramen.local", "mcp_user", ["demo"]).get("/").text
    assert "&lt;zone&gt;" not in page and "&lt;client id&gt;" not in page
    for z in ("zone-a", "zone-b"):
        assert (
            f"mcp:demo:{z}" in page and f"&#34;ramen-zone&#34;: &#34;{z}&#34;" in page or f'"ramen-zone": "{z}"' in page
        )
    assert cid in page and "claude mcp add-json ramen-demo-zone-a" in page
    assert "--tls" in page, "printed bridge commands state TLS explicitly"


def test_without_an_oauth_client_the_page_says_to_ask_an_admin(demo):  # noqa: F811
    page = as_user(demo.app, demo, "mu@ramen.local", "mcp_user", ["demo"]).get("/").text
    assert "Ask an admin" in page and "&lt;client id&gt;" not in page and "mcp:demo:zone-a" in page


def test_the_commands_do_not_depend_on_the_identity_provider(demo):  # noqa: F811
    client_id(demo)
    a = as_user(demo.app, demo, "entra-user@ramen.local", "mcp_user", ["demo"]).get("/").text
    b = as_user(demo.app, demo, "google-user@ramen.local", "mcp_user", ["demo"]).get("/").text
    pick = [line for line in a.splitlines() if "claude mcp add-json" in line]
    assert pick and pick == [line for line in b.splitlines() if "claude mcp add-json" in line]


def test_a_group_without_a_deployed_zone_says_so(demo):  # noqa: F811
    assert demo.post(
        "/api/v1/groups", json={"name": "lonely", "repo_url": "https://example.com/r.git"}
    ).status_code in (
        200,
        201,
    )
    page = as_user(demo.app, demo, "lo@ramen.local", "mcp_user", ["lonely"]).get("/").text
    assert "no zone yet" in page.lower() and "mcp:lonely:" not in page


def test_the_group_card_names_each_zones_scope(demo):  # noqa: F811
    client_id(demo)
    html = demo.get("/groups/demo").text
    assert "<code>mcp:demo:zone-a</code>, <code>mcp:demo:zone-b</code>" in html and "mcp:demo:&lt;zone&gt;" not in html
