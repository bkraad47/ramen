"""Hardening added after the v0.3.0 security audit (CONTRACTS §9 'Hardening')."""

import logging

import pytest
from fastapi.testclient import TestClient

from ramen_console import security
from ramen_console.app import create_app
from ramen_console.cloud import aws_k8s, gcp_k8s
from ramen_console.cloud.gcp_api import k8s_name
from ramen_console.errors import ApiError
from ramen_console.web.helpers import _csv_safe


def _app(monkeypatch, **env):
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.test")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "pw-root-1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app())


def test_signing_secret_never_a_constant(monkeypatch):
    monkeypatch.delenv("RAMEN_SESSION_SECRET", raising=False)
    monkeypatch.delenv("RAMEN_FERNET_KEY", raising=False)
    a, b = security.resolve_signing_secret(), security.resolve_signing_secret()
    assert a == b and a != "dev-insecure" and len(a) > 40
    monkeypatch.setenv("RAMEN_SESSION_SECRET", "explicit")
    assert security.resolve_signing_secret() == "explicit"


def test_security_headers_present(monkeypatch):
    with _app(monkeypatch) as c:
        h = c.get("/login").headers
        assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in h["Content-Security-Policy"] and "Strict-Transport-Security" not in h
    with _app(monkeypatch, RAMEN_COOKIE_SECURE="1") as c:
        assert "max-age=" in c.get("/login").headers["Strict-Transport-Security"]


def test_login_rate_limited_on_failures_only(monkeypatch):
    with _app(monkeypatch, RAMEN_LOGIN_RATE_LIMIT="3") as c:
        good = {"email": "root@ramen.test", "password": "pw-root-1"}

        def login_ok():
            c.cookies.clear()  # a cookie-less POST /login needs no CSRF token (scripted clients)
            return c.post("/login", data=good, follow_redirects=False).status_code == 303

        assert all(login_ok() for _ in range(10))
        codes = [
            c.post("/login", data={"email": "x@y", "password": "bad"}, follow_redirects=False).status_code
            for _ in range(5)
        ]
        assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
        c.cookies.clear()
        assert c.post("/login", data=good, follow_redirects=False).status_code == 429  # locked for the window
    with _app(monkeypatch, RAMEN_LOGIN_RATE_LIMIT="0") as c:
        assert all(c.post("/login", data={"email": "x@y", "password": "bad"}).status_code == 401 for _ in range(30))


@pytest.mark.parametrize("bad", ["//evil.example.com", "/\\evil", "http://evil", "", None, "/a\r\nb"])
def test_safe_next_rejects_offsite(bad):
    assert security.safe_next(bad) == "/"


def test_safe_next_keeps_local_paths():
    assert security.safe_next("/groups/demo?x=1") == "/groups/demo?x=1"


def test_access_log_redacts_tokens():
    rec = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/1.1" %d', None, None)
    rec.args = ("1.2.3.4", "POST", "/auth/reset/abc.def.ghi", 303)
    assert security._RedactTokens().filter(rec) and rec.args[2] == "/auth/reset/<redacted>"


def test_k8s_name_rejects_injection():
    assert k8s_name("ramen-demo-a", "ns") == "ramen-demo-a"
    for bad in ['x" OR a="b', "UPPER", "a b", "", "-lead", "x" * 300]:
        with pytest.raises(ApiError) as e:
            k8s_name(bad, "worker")
        assert e.value.status_code == 422


def test_csv_formula_cells_are_neutralised():
    assert (
        _csv_safe("=1+1") == "'=1+1"
        and _csv_safe("+x") == "'+x"
        and _csv_safe("-x") == "'-x"
        and _csv_safe("@x") == "'@x"
    )
    assert _csv_safe("plain") == "plain" and _csv_safe(3) == 3 and _csv_safe("") == ""


def test_worker_manifests_are_hardened():
    docs = gcp_k8s.manifests("demo", "a", {"count": 1}, "img", "gs://b/demo")
    kinds = [d["kind"] for d in docs]
    assert "NetworkPolicy" in kinds
    np = next(d for d in docs if d["kind"] == "NetworkPolicy")
    peers = np["spec"]["ingress"][0]["from"]
    assert peers[0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "ramen-system"
    assert {p["ipBlock"]["cidr"] for p in peers[1:]} == set(gcp_k8s.GCP_LB_CIDRS)
    for d in docs:
        if d["kind"] == "Deployment":
            sc = d["spec"]["template"]["spec"]["containers"][0]["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"] == {"drop": ["ALL"]}


def test_worker_pods_are_labelled_for_external_log_monitors():
    """N3: Cloud Logging / CloudWatch (and anything reading from them, e.g. Datadog) enriches shipped log
    lines with pod labels automatically; group/zone must be on the pod template itself, not just the
    Deployment, or an external monitor has no way to filter a worker's logs by group or zone."""
    for docs in (
        gcp_k8s.manifests("demo", "a", {"count": 1}, "img", "gs://b/demo"),
        aws_k8s.manifests("demo", "a", {"count": 1}, "img", "s3://b/demo"),
    ):
        deployments = [d for d in docs if d["kind"] == "Deployment"]
        assert deployments
        for d in deployments:
            labels = d["spec"]["template"]["metadata"]["labels"]
            assert labels.get("ramen.io/group") == "demo" and labels.get("ramen.io/zone") == "a"


def test_oidc_signs_in_an_email_without_email_verified_and_refuses_an_anonymous_token(monkeypatch):
    """D51 (0.7.5): Entra never sends `email_verified`; an email identifies the person, a stable id otherwise."""
    with _app(
        monkeypatch,
        RAMEN_OAUTH_OIDC_CLIENT_ID="cid",
        RAMEN_OAUTH_OIDC_CLIENT_SECRET="sec",
        RAMEN_OAUTH_OIDC_METADATA_URL="https://issuer/.well-known/openid-configuration",
    ) as c:
        answers = {"userinfo": {"email": "sso@x"}}  # no email_verified claim

        class FakeClient:
            async def authorize_access_token(self, request):
                return answers

            async def userinfo(self, token):
                return answers["userinfo"]

        monkeypatch.setattr(c.app.state.oauth, "client", lambda name: FakeClient() if name == "oidc" else None)
        assert c.get("/auth/oidc/callback?code=x&state=y", follow_redirects=False).status_code == 303
        answers["userinfo"] = {"name": "anonymous"}  # neither email nor sub: nobody to attribute calls to
        assert c.get("/auth/oidc/callback?code=x&state=y", follow_redirects=False).status_code == 403


def test_oauth_token_endpoint_is_rate_limited_and_authorize_queries_are_redacted():
    """Security review 0.5.0 L5 and I1."""
    assert "/oauth/token" in security.RateLimitMiddleware.PATHS
    rec = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        0,
        '%s - "%s %s HTTP/1.1" %d',
        ("1.2.3.4", "GET", "/oauth/authorize?client_id=c&code_challenge=SECRET&state=S", 200),
        None,
    )
    assert security._RedactTokens().filter(rec) and rec.args[2] == "/oauth/authorize?<redacted>"
