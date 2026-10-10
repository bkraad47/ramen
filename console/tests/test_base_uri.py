"""0.6.1: `base_uri` (Config page, super admin) — the console's public address, path prefix included. When set, every
link the console generates goes through it, so it works behind a proxy that forwards `<base_uri>/…` to its root.
Unset, every page and redirect is byte-identical to before."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console.baseuri import normalize
from ramen_console.mail import Mailer
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_auth_flows import maildir, mails  # noqa: F401 - pytest fixture
from tests.test_oauth_server import authorize, consent, pkce, register

TEMPLATES = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/templates")
BASE = "https://proxy.example/ramen"
LINK = re.compile(r"""\b(href|action|src|hx-get|hx-post|hx-put|hx-delete|hx-patch)=["'](/[^"']*)["']""")


def set_base(c, value=BASE):
    r = c.put("/api/v1/config/base-uri", json={"base_uri": value})
    assert r.status_code == 200, r.text
    return r


def unprefixed(html: str) -> list[str]:
    return [f"{a}={u}" for a, u in LINK.findall(html) if not u.startswith("/ramen/") and u != "/ramen"]


@pytest.mark.parametrize(
    "raw, want",
    [
        ("", ""),
        ("  ", ""),
        ("https://x.example", "https://x.example"),
        ("https://x.example/", "https://x.example"),
        ("http://x.example:8080/a/b/", "http://x.example:8080/a/b"),
    ],
)
def test_normalize_accepts(raw, want):
    assert normalize(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "x.example/ramen",
        "/ramen",
        "ftp://x.example",
        "https://",
        "https://x.example/r?a=1",
        "https://x.example/r#top",
        "https://u:p@x.example",
        "https://x.example/a b",
        "https://x.example:99999",
        "javascript:alert(1)",
    ],
)
def test_normalize_refuses(raw):
    with pytest.raises(ValueError):
        normalize(raw)


def test_config_save_validate_audit_and_super_admin_only(demo):
    assert demo.get("/api/v1/config/base-uri").json() == {"base_uri": ""}
    r = demo.put("/api/v1/config/base-uri", json={"base_uri": "nope"})
    assert r.status_code == 422 and "http" in r.json()["detail"]
    assert set_base(demo, BASE + "/").json() == {"base_uri": BASE}
    assert demo.get("/api/v1/config/base-uri").json() == {"base_uri": BASE}
    page = demo.get("/config").text
    assert "Public address (base URI)" in page and f'value="{BASE}"' in page
    assert 'hx-put="/ramen/api/v1/config/base-uri"' in page
    audit = demo.get("/api/v1/audit").json()
    assert any(a["action"] == "config.base_uri" and f"base_uri:{BASE}" in a["tags"] for a in audit)
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get("/api/v1/config/base-uri").status_code == 403
        assert ga.put("/api/v1/config/base-uri", json={"base_uri": ""}).status_code == 403
    assert set_base(demo, "").json() == {"base_uri": ""}  # empty = unset
    assert 'hx-put="/api/v1/config/base-uri"' in demo.get("/config").text


def test_no_template_hardcodes_a_root_path():
    """Source sweep: a future template that writes `href="/x"` instead of `href="{{ base }}/x"` fails here."""
    bad = [f"{f.name}: {m.group(0)}" for f in TEMPLATES.rglob("*.html") for m in LINK.finditer(f.read_text())]
    assert bad == [], bad
    literal = re.compile(r"""(?<!u)[(\[,]\s*'/(api|ui|static)/""")  # a path handed to a macro, not via u()
    bad = [f"{f.name}: {m.group(0)}" for f in TEMPLATES.rglob("*.html") for m in literal.finditer(f.read_text())]
    assert bad == [], bad


def _pages(c) -> dict[str, str]:
    paths = [
        "/",
        "/ui/dashboard",
        "/groups",
        "/groups/demo",
        "/environments",
        "/zones",
        "/secrets",
        "/users",
        "/api-keys",
        "/logs?group=demo&zone=zone-a",
        "/audit",
        "/ui/audit",
        "/backups",
        "/config",
        "/ui/groups/demo/zones/zone-a/workers",
        "/ui/jobs/nope",
    ]
    out = {}
    for p in paths:
        r = c.get(p, follow_redirects=False)
        assert r.status_code == 200, (p, r.status_code)
        out[p] = r.text
    return out


def test_every_rendered_page_routes_links_through_the_base(demo, maildir):
    demo.app.state.mailer = Mailer.from_env()
    demo.post("/api/v1/backups", json={})
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "k", "env": "prod", "zone": "zone-a"})
    unset = _pages(demo)
    set_base(demo)
    pages = _pages(demo)
    with TestClient(demo.app) as anon:
        pages["/login"] = anon.get("/login").text
        pages["/auth/reset"] = anon.get("/auth/reset").text
        pages["/auth/reset/x"] = anon.get("/auth/reset/x").text
    make_user(demo, "mu@x", "mcp_user", ["demo"])
    cid = register(demo)
    _, challenge = pkce()
    with TestClient(demo.app) as mu:
        login(mu, "mu@x", PW)
        pages["mcp_user"] = mu.get("/").text
        pages["consent"] = authorize(mu, cid, challenge).text
    for p, html in pages.items():
        assert unprefixed(html) == [], p
        assert len(LINK.findall(html)) > 0 or p.startswith("/ui/"), p
    assert 'href="/ramen/static/ramen.css?v=' in pages["/groups"] and 'href="/ramen/"' in pages["/groups"]
    assert 'href="/ramen/api/v1/groups?format=csv"' in pages["/groups"]
    assert f'"url":"{BASE}/mcp"' in pages["mcp_user"] and f"--oauth {BASE} " in pages["/groups/demo"]
    # unset: today's root paths, untouched
    assert 'href="/static/ramen.css?v=' in unset["/groups"] and 'hx-get="/ui/dashboard"' in unset["/"]
    assert not any("/ramen/" in html for html in unset.values())


def test_deploy_job_partial_is_prefixed(demo, monkeypatch, tmp_path):
    async def fake_sync(group, repo_url, ref, token):
        (tmp_path / "buckets" / group).mkdir(parents=True, exist_ok=True)
        return str(tmp_path / "buckets" / group)

    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", fake_sync)
    set_base(demo)
    r = demo.post(
        "/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 202 and unprefixed(r.text) == []


def test_redirects_go_through_the_base(demo):
    set_base(demo)
    cid = register(demo)
    _, challenge = pkce()
    make_user(demo, "pw@x", "mcp_user", ["demo"])
    with TestClient(demo.app) as anon:
        r = anon.get("/groups", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/ramen/login?next=%2Fgroups"
        r = authorize(anon, cid, challenge)
        assert r.headers["location"].startswith("/ramen/login?next=%2Foauth%2Fauthorize")
        r = anon.post(
            "/login", data={"email": "pw@x", "password": PW, "next": "/oauth/authorize?x=1"}, follow_redirects=False
        )
        assert r.status_code == 303 and r.headers["location"] == "/ramen/oauth/authorize?x=1"
        r = anon.post(
            "/login", data={"email": "pw@x", "password": PW, "next": "//evil.example"}, follow_redirects=False
        )
        assert r.headers["location"] == "/ramen/"  # safe_next still decides first
        r = anon.get("/logout", follow_redirects=False)
        assert r.headers["location"] == "/ramen/login"
    # the consent redirect goes to the client, never through the base
    with TestClient(demo.app) as mu:
        login(mu, "pw@x", PW)
        r = mu.post(
            "/oauth/authorize",
            data={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": "http://127.0.0.1:9999/callback",
                "scope": "mcp:demo:zone-a",
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "decision": "deny",
                "csrf_token": mu.cookies.get("ramen_csrf", ""),
            },
            follow_redirects=False,
        )
        assert r.headers["location"].startswith("http://127.0.0.1:9999/callback?")


def test_unset_redirects_are_unchanged(client):
    r = client.get("/groups", follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Fgroups"
    r = client.post("/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert client.get("/logout", follow_redirects=False).headers["location"] == "/login"


def test_oauth_metadata_and_token_issuer_follow_the_base(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_PUBLIC_URL", "https://env.example")
    assert demo.get("/.well-known/oauth-authorization-server").json()["issuer"] == "https://env.example"
    set_base(demo)
    md = demo.get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == BASE
    assert md["authorization_endpoint"] == f"{BASE}/oauth/authorize" and md["token_endpoint"] == f"{BASE}/oauth/token"
    cid = register(demo)
    verifier, challenge = pkce()
    code = consent(demo, cid, challenge)
    from tests.test_oauth_server import exchange

    tok = exchange(demo, cid, code, verifier).json()
    import base64
    import json

    claims = json.loads(base64.urlsafe_b64decode(tok["access_token"].split(".")[1] + "=="))
    assert claims["iss"] == BASE


def test_mail_links_follow_the_base(demo, maildir):
    demo.app.state.mailer = Mailer.from_env()
    set_base(demo)
    demo.post("/api/v1/users", json={"email": "new@x", "password": PW, "role": "viewer", "groups": ["demo"]})
    with TestClient(demo.app) as anon:
        anon.post("/auth/reset", data={"email": "new@x"})
    demo.put("/api/v1/config/auth", json={"magic_link": True})
    with TestClient(demo.app) as anon:
        anon.post("/auth/magic", data={"email": "new@x"})
    sent = mails(maildir)
    assert len(sent) == 3
    for kind, msg in zip(("reset", "reset", "magic"), sent, strict=True):
        assert f"{BASE}/auth/{kind}/" in msg.get_content()
        assert "https://console.test" not in msg.get_content()  # the setting wins over RAMEN_PUBLIC_URL


def test_oauth_login_callback_follows_the_base(demo, monkeypatch):
    seen = {}

    class Client:
        async def authorize_redirect(self, request, redirect_uri):
            seen["uri"] = redirect_uri
            from starlette.responses import Response

            return Response("ok")

    monkeypatch.setattr(demo.app.state.oauth, "client", lambda name: Client())
    demo.get("/auth/google/login")
    assert seen["uri"] == "http://testserver/auth/google/callback"
    set_base(demo)
    demo.get("/auth/google/login")
    assert seen["uri"] == f"{BASE}/auth/google/callback"


def test_deploy_hands_workers_the_base(demo, monkeypatch, tmp_path):
    async def fake_sync(group, repo_url, ref, token):
        (tmp_path / "buckets" / group).mkdir(parents=True, exist_ok=True)
        return str(tmp_path / "buckets" / group)

    handed: dict[str, dict] = {}
    real_deploy = demo.app.state.cloud.deploy

    async def spy_deploy(group, env, zone, canary=True, config=None, **kw):
        handed[zone] = dict(config or {})
        return await real_deploy(group, env, zone, canary, config, **kw)

    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", fake_sync)
    monkeypatch.setattr(demo.app.state.cloud, "deploy", spy_deploy)
    monkeypatch.setenv("RAMEN_PUBLIC_URL", "https://env.example")
    set_base(demo)
    assert demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).status_code == 202
    assert handed["zone-a"]["RAMEN_OAUTH_ISSUER"] == BASE and handed["zone-a"]["RAMEN_PUBLIC_URL"] == BASE


def test_https_redirect_keeps_the_prefix(demo, monkeypatch):
    from ramen_console.app import create_app

    set_base(demo)
    monkeypatch.setenv("RAMEN_COOKIE_SECURE", "1")
    app2 = create_app(store=demo.app.state.store, cloud=demo.app.state.cloud)
    with TestClient(app2) as c:
        r = c.get("/groups?x=1", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "https://proxy.example/ramen/groups?x=1"


async def test_a_store_outage_keeps_the_last_known_base():
    from ramen_console.baseuri import Cache

    class Store:
        doc: dict | Exception = {"base_uri": BASE}

        async def get(self, coll, key):
            if isinstance(self.doc, Exception):
                raise self.doc
            return self.doc

    store, cache = Store(), Cache()
    assert await cache.refresh(store) == BASE
    store.doc = {"base_uri": ""}
    assert await cache.refresh(store) == BASE  # within the TTL: cached
    cache.loaded, store.doc = 0.0, RuntimeError("store down")
    assert await cache.refresh(store) == BASE  # a blip keeps the last known value
    cache.loaded, store.doc = 0.0, {}
    assert await cache.refresh(store) == ""
