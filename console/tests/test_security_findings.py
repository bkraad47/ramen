"""Security audit v0.3.0 (test skill, angle 4). Each test captures a CONFIRMED high/critical finding and is
marked xfail with the finding id; when the fix lands the test starts passing (flip to strict or drop the marker).
See reports/security-v0.3.0.md for full write-ups and repros."""

import pytest
from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.auth.sessions import SessionSigner
from ramen_console.cloud import gcp_api
from ramen_console.storage import make_store
from tests.test_api import app, client, cloud, demo, login, make_user, root  # noqa: F401


@pytest.mark.xfail(
    reason="SEC-01: signing secret falls back to the public constant 'dev-insecure' when "
    "RAMEN_SESSION_SECRET and RAMEN_FERNET_KEY are unset; sessions/tokens become forgeable",
    strict=False,
)
def test_sec01_no_dev_insecure_signing_fallback(monkeypatch):
    monkeypatch.delenv("RAMEN_SESSION_SECRET", raising=False)
    monkeypatch.delenv("RAMEN_FERNET_KEY", raising=False)
    monkeypatch.setenv("RAMEN_STORE", "memory")
    app = create_app(store=make_store())
    # An attacker who knows the source constant can mint any session; the server must NOT accept it.
    forged = SessionSigner("dev-insecure").sign({"uid": "any-super-admin-id"})
    assert app.state.signer.load(forged) is None, "console signs sessions with the public 'dev-insecure' constant"


@pytest.mark.xfail(
    reason="SEC-02: `worker` (and `zone`) log-query params are interpolated unescaped into the "
    "Cloud Logging filter / Logs Insights queryString, enabling cross-namespace log reads",
    strict=False,
)
def test_sec02_log_query_injection_is_neutralised():
    captured = {}

    class FakeGcp:
        def list_entries(self, filter_=None, **kw):
            captured["f"] = filter_
            return []

    inj = 'x" OR resource.labels.namespace_name="ramen-victim-prod'
    gcp_api.fetch_logs(FakeGcp(), "ramen-demo-local", inj, 100)
    # The victim namespace must never appear in the filter for a caller scoped to ramen-demo-local.
    assert "ramen-victim-prod" not in captured["f"], f"injectable log filter: {captured['f']}"


@pytest.mark.xfail(
    reason="SEC-03: post-login redirect accepts protocol-relative `next` (e.g. //evil.example.com), "
    "an open redirect usable for phishing",
    strict=False,
)
def test_sec03_login_next_open_redirect(demo):  # noqa: F811
    c: TestClient = demo
    r = c.post(
        "/login",
        data={"email": "root@ramen.local", "password": "rootpw", "next": "//evil.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    assert not loc.startswith("//") and "evil.example.com" not in loc, f"open redirect to {loc!r}"
