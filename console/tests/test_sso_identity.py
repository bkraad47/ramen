"""D51 (0.7.5, from the live Entra run): no `email_verified` requirement. An email from the provider identifies the
person; without one the account is keyed on the provider's stable id (Entra `oid` within `tid`, Google/OIDC `sub`)."""

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_flow import FakeIdp, registry, start

ENTRA_LIKE = {
    "email": "Dev@Corp.test",
    "oid": "o-1",
    "tid": "t-1",
    "preferred_username": "dev@corp.test",
}  # no email_verified


def _sign_in(demo, idp):
    with TestClient(demo.app) as anon:
        state = start(anon, idp)
        r = anon.get(f"/auth/idp/callback?code=good-code&state={state}", follow_redirects=False)
        return r, (anon.get("/api/v1/me").json() if r.status_code == 303 else None)


def test_entra_shaped_claims_without_email_verified_sign_in(demo):
    idp = FakeIdp(ENTRA_LIKE)
    demo.app.state.oauth = registry(idp)
    r, me = _sign_in(demo, idp)
    assert r.status_code == 303 and me["email"] == "dev@corp.test"
    u = next(u for u in demo.get("/api/v1/users").json() if u["email"] == "dev@corp.test")
    assert u["provider"] == "idp" and "provider_key" not in u  # internal key never leaves the API
    audit = demo.get("/api/v1/audit").json()
    assert any(a["action"] == "login.oauth" and a["user"] == "dev@corp.test" and a["ok"] for a in audit)


def test_no_email_creates_an_account_keyed_on_the_provider_id_and_finds_it_again(demo):
    idp = FakeIdp({"oid": "o-9", "tid": "t-1"})
    demo.app.state.oauth = registry(idp)
    r, me = _sign_in(demo, idp)
    assert r.status_code == 303 and me["email"] == "idp:t-1/o-9"
    r, me2 = _sign_in(demo, idp)
    assert me2["id"] == me["id"]  # the same account, not a second one
    assert len([u for u in demo.get("/api/v1/users").json() if u["email"] == "idp:t-1/o-9"]) == 1
    # the person's email appears later: the account follows the stable id and takes the email
    idp.claims = {"email": "late@corp.test", "oid": "o-9", "tid": "t-1"}
    r, me3 = _sign_in(demo, idp)
    assert me3["id"] == me["id"] and me3["email"] == "late@corp.test"


def test_an_existing_password_account_is_linked_by_email_as_before(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    idp = FakeIdp({"email": "ga@x", "sub": "s-1"})  # plain OIDC: `sub`, no email_verified
    demo.app.state.oauth = registry(idp)
    r, me = _sign_in(demo, idp)
    assert r.status_code == 303 and me["role"] == "group_admin" and me["groups"] == ["demo"]
    assert len([u for u in demo.get("/api/v1/users").json() if u["email"] == "ga@x"]) == 1
    with TestClient(demo.app) as c:  # the password still works too
        assert c.post("/login", data={"email": "ga@x", "password": PW}, follow_redirects=False).status_code == 303


def test_neither_email_nor_subject_is_refused(demo):
    idp = FakeIdp({"name": "nobody"}, id_token=False)  # userinfo without sub or email
    idp.handler_orig = idp.handler

    def handler(req):
        if req.url.path == "/userinfo":
            import httpx

            return httpx.Response(200, json={"name": "nobody"})
        return idp.handler_orig(req)

    idp.handler = handler
    demo.app.state.oauth = registry(idp, RAMEN_OAUTH_IDP_SCOPES="email")
    r, me = _sign_in(demo, idp)
    assert r.status_code == 403 and "neither an email nor a stable subject" in r.text
