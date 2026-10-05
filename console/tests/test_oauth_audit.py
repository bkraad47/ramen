"""D3 (0.7.0): the token endpoint audits mints, refreshes, refresh-token reuse and every denial with its reason."""

import secrets

from tests.test_api import app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_oauth_server import CLIENT, consent, pkce, register

REDIRECT = CLIENT["redirect_uris"][0]


def rows(client, action):
    return [a for a in client.get("/api/v1/audit").json() if a["action"] == action]


def mint(client, cid):
    verifier, challenge = pkce()
    code = consent(client, cid, challenge)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def refresh(client, cid, token):
    return client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": cid})


def test_mint_refresh_and_reuse_are_distinct_events(demo):
    cid = register(demo)
    tok = mint(demo, cid)
    minted = rows(demo, "oauth.token")
    assert len(minted) == 1 and minted[0]["ok"] and minted[0]["target"] == "mcp:demo:zone-a"
    assert set(minted[0]["tags"]) >= {"grant:authorization_code", f"client:{cid}", "group:demo", "zone:zone-a"}
    assert refresh(demo, cid, tok["refresh_token"]).status_code == 200
    refreshed = rows(demo, "oauth.refresh")
    assert len(refreshed) == 1 and refreshed[0]["ok"] and "group:demo" in refreshed[0]["tags"]
    assert rows(demo, "oauth.token") == minted  # a refresh is not a mint
    r = refresh(demo, cid, tok["refresh_token"])  # the rotated-out token again
    assert r.status_code == 400 and "reuse" in r.json()["error_description"]
    reuse = rows(demo, "oauth.refresh_reuse")
    assert len(reuse) == 1 and not reuse[0]["ok"] and {"reason:refresh_reuse", f"client:{cid}"} <= set(reuse[0]["tags"])
    assert rows(demo, "oauth.denied") == []  # reuse has its own event, it is not double-counted


def test_denials_carry_a_reason(demo):
    cid = register(demo)
    verifier, challenge = pkce()
    code = consent(demo, cid, challenge)
    bad = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": cid,
        "code_verifier": "wrong-verifier-wrong-verifier-wrong-verifier-123",
    }
    assert demo.post("/oauth/token", data=bad).status_code == 400
    assert demo.post("/oauth/token", data={**bad, "code_verifier": verifier}).status_code == 400  # burnt code
    assert demo.post("/oauth/token", data={"grant_type": "password", "client_id": cid}).status_code == 400
    assert demo.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "nope"}).status_code == 401
    assert refresh(demo, cid, "unknown").status_code == 400
    denied = rows(demo, "oauth.denied")
    reasons = sorted(t for a in denied for t in a["tags"] if t.startswith("reason:"))
    assert reasons == sorted(
        [
            "reason:pkce_mismatch",
            "reason:code_invalid",
            "reason:unsupported_grant_type",
            "reason:unknown_client",
            "reason:refresh_invalid",
        ]
    )
    assert all(not a["ok"] for a in denied)
    assert all(any(t.startswith("grant:") for t in a["tags"]) for a in denied)
    assert rows(demo, "oauth.token") == []


def test_a_revoked_user_is_denied_with_that_reason(demo):
    cid = register(demo)
    tok = mint(demo, cid)
    new_pw = "epoch-bump-" + secrets.token_urlsafe(8)  # not a literal: secret scanners flag fixture passwords
    demo.post("/api/v1/users/me/password", json={"password": new_pw})  # bumps the epoch (V1.4)
    assert refresh(demo, cid, tok["refresh_token"]).status_code == 400
    assert any("reason:sessions_revoked" in a["tags"] for a in rows(demo, "oauth.denied"))
