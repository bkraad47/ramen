"""D92 (CONTRACTS §16.7): revocation that takes effect at once — a revoked `rmk_` key and a revoked OAuth access token
reach the group's running workers through the scoped config push (`Admin/Reload` carries `RAMEN_REVOKED`), every
deploy carries the list, an unreachable zone is a warning and is retried in the background."""

import asyncio
import base64
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from ramen_console import deploy as dep
from ramen_console import revocation as rv
from ramen_console.grpcclient import Client, GrpcError
from ramen_console.storage import make_store
from tests.fake_grpc import DOWN
from tests.test_api import PW, app, client, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import wait_job
from tests.test_oauth_server import consent, exchange, pkce, refresh, register
from tests.test_v100_otel import DEPLOY, cloud  # noqa: F401 - the stub cloud fixture

KEYS = "/api/v1/groups/demo/mcp-keys"


def sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def claims(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


def deployed(demo):
    assert wait_job(demo, demo.post(DEPLOY, json={}).json()["id"])["status"] == "ok"


def revoked_of(cfg: dict) -> dict:
    return json.loads(cfg[rv.ENV])


def drain(demo):
    demo.portal.call(demo.app.state.revocation.drain)


def entries(demo, group="demo") -> dict:
    return demo.portal.call(rv.entries, demo.app.state.store, group)


@pytest.fixture
def spy(monkeypatch):
    groups, real = [], dep.push_config

    async def push(svc, warnings=None, group=None):
        groups.append(group)
        return await real(svc, warnings, group)

    monkeypatch.setattr(dep, "push_config", push)
    return groups


# --- rmk_ keys ------------------------------------------------------------------------------------------------------


def test_a_revoked_key_is_refused_by_the_running_workers_at_once(demo, cloud, workers, spy):
    deployed(demo)
    r = demo.post(KEYS, json={"name": "k1"})
    assert r.status_code == 201
    raw, sid = r.json()["key"], r.json()["id"]
    assert r.json()["pushed"] == {"demo/zone-a": "ok", "demo/zone-b": "ok"}  # a new key works without a deploy
    assert raw in cloud.pushes[-1]["config"]["RAMEN_MCP_KEYS"]
    for w in workers.values():
        w.mcp_keys = {raw, "rmk_1"}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    rpc = Client(deadline=2)
    assert asyncio.run(rpc.call(workers["cold"].target, raw, "demo", "zone-a", call))["result"] == {"tools": []}
    spy.clear()
    n = len(cloud.pushes)
    r = demo.delete(f"{KEYS}/{sid}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["pushed"] == {"demo/zone-a": "ok", "demo/zone-b": "ok"} and "warning" not in r.json()
    assert spy == ["demo"]  # the group's zones only (§38 F4)
    pushed = cloud.pushes[n:]
    assert {p["zone"] for p in pushed} == {"zone-a", "zone-b"}
    for p in pushed:
        assert raw not in p["config"].get("RAMEN_MCP_KEYS", "") and sha(raw) in revoked_of(p["config"])["keys"]
    for w in workers.values():  # the reload itself carried the list: no restart needed for the refusal
        assert sha(raw) in json.loads(w.revoked[-1])["keys"] and raw not in w.mcp_keys
    with pytest.raises(GrpcError) as e:
        asyncio.run(rpc.call(workers["cold"].target, raw, "demo", "zone-a", call))
    assert e.value.code.name == "UNAUTHENTICATED"
    assert asyncio.run(rpc.call(workers["cold"].target, "rmk_1", "demo", "zone-a", call))["result"]
    # and every later deploy carries it
    deployed(demo)
    assert sha(raw) in revoked_of(cloud.deploys[-1]["config"])["keys"]


def test_an_unreachable_zone_is_a_warning_on_the_key_and_is_retried(demo, cloud, workers):
    deployed(demo)
    sid = demo.post(KEYS, json={"name": "k1"}).json()["id"]
    good = cloud.workers_map["demo/zone-b"]
    cloud.workers_map["demo/zone-b"] = [DOWN]
    r = demo.delete(f"{KEYS}/{sid}")
    assert r.status_code == 200 and r.json()["pushed"]["demo/zone-a"] == "ok"
    assert r.json()["pending"] == ["demo/zone-b"]
    assert "retried in the background" in r.json()["warning"]["demo/zone-b"]
    store = demo.app.state.store
    assert set(asyncio.run(rv.pending(store))["demo"]["zones"]) == {"zone-b"}
    page = demo.get("/groups/demo").text
    assert "Revocation pending" in page and "zone-b" in page
    demo.portal.call(demo.app.state.revocation.retry_pending)  # still down: still pending, tried again
    assert asyncio.run(rv.pending(store))["demo"]["tries"] == 2
    cloud.workers_map["demo/zone-b"] = good
    demo.portal.call(demo.app.state.revocation.retry_pending)
    assert asyncio.run(rv.pending(store)) == {}
    assert "Revocation pending" not in demo.get("/groups/demo").text


def test_an_agent_api_key_is_revoked_on_every_group_it_reached(demo, cloud, spy):
    deployed(demo)
    r = demo.post("/api/v1/api-keys", json={"name": "bot", "client_type": "agent", "groups": ["demo"]})
    assert r.status_code == 201, r.text
    raw = r.json()["key"]
    spy.clear()
    assert demo.delete(f"/api/v1/api-keys/{r.json()['id']}").status_code == 200
    assert spy == ["demo"] and sha(raw) in revoked_of(cloud.pushes[-1]["config"])["keys"]


# --- the list itself -------------------------------------------------------------------------------------------------


def test_entries_are_scoped_merged_and_expire(monkeypatch):
    store = make_store()

    async def go():
        t = time.time()
        assert await rv.deny_jti(store, "demo", "j-old", int(t) - 1, "x") is False  # already expired: nothing
        assert await rv.deny_jti(store, "demo", "j1", int(t) + 60, "x")
        await rv.deny_key(store, "demo", "rmk_a", "x")
        await store.put("oauth_refresh", "r1", {"user": "u1", "group": "demo", "access_exp": int(t) + 60, "exp": 0})
        await store.put("oauth_refresh", "r2", {"user": "u1", "group": "old", "access_exp": int(t) - 5, "exp": 0})
        assert await rv.deny_subject(store, "u1", "x") == {"demo"}  # only where a live token can be
        assert await rv.deny_subject(store, "nobody", "x") == set()
        e = await rv.entries(store, "demo")
        assert e["keys"] == [sha("rmk_a")] and e["jti"] == ["j1"] and int(t) - 1 <= e["sub"]["u1"] <= int(t) + 1
        assert await rv.entries(store, "other") == {"keys": [], "jti": [], "sub": {}}
        env = await rv.worker_env(store, "demo")
        assert json.loads(env["RAMEN_REVOKED"]) == e and " " not in env["RAMEN_REVOKED"]
        monkeypatch.setattr(rv.time, "time", lambda: t + 3601)  # past the token TTL: token entries are gone
        e = await rv.entries(store, "demo")
        assert e["jti"] == [] and e["sub"] == {} and e["keys"] == [sha("rmk_a")]
        monkeypatch.setattr(rv.time, "time", lambda: t + rv.KEY_KEEP + 1)
        assert await rv.entries(store, "demo") == {"keys": [], "jti": [], "sub": {}}
        assert len(await store.list(rv.COLL)) == 0  # pruned, not just hidden

    asyncio.run(go())


def test_the_subject_entry_lives_as_long_as_the_configured_token_ttl(monkeypatch):
    monkeypatch.setenv("RAMEN_OAUTH_ACCESS_TTL", "600")
    store = make_store()

    async def go():
        await store.put("oauth_refresh", "r1", {"user": "u1", "group": "demo", "access_exp": time.time() + 60})
        await rv.deny_subject(store, "u1", "x")
        (doc,) = await store.list(rv.COLL)
        assert doc["exp"] - doc["before"] == 600

    asyncio.run(go())


# --- OAuth access tokens ----------------------------------------------------------------------------------------------


def person(demo, email="u@x", role="mcp_user"):
    """A member of demo with a live access token for demo/zone-a; `(user doc, signed-in client, token answer, cid)`."""
    u = make_user(demo, email, role, ["demo"])
    c = TestClient(demo.app)
    c.__enter__()
    login(c, email, PW)
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(c, cid, challenge), verifier).json()
    assert "access_token" in t, t
    return u, c, t, cid


def subject_revoked(demo, uid, token) -> bool:
    drain(demo)
    before = entries(demo)["sub"].get(uid)
    return before is not None and claims(token["access_token"])["iat"] <= before


@pytest.mark.parametrize(
    "how",
    ["remove_member", "lower_role", "delete_user", "admin_sign_out", "self_sign_out", "password_reset_by_admin"],
)
def test_each_trigger_revokes_the_persons_tokens_and_pushes_the_group(demo, cloud, spy, how):
    deployed(demo)
    u, c, t, _ = person(demo, role="group_admin" if how == "lower_role" else "mcp_user")
    other, oc, ot, _ = person(demo, "bystander@x")
    spy.clear()
    if how == "remove_member":
        assert demo.delete(f"/api/v1/groups/demo/members/{u['id']}").status_code == 200
    elif how == "lower_role":
        assert demo.put(f"/api/v1/groups/demo/members/{u['id']}", json={"role": "viewer"}).status_code == 200
    elif how == "delete_user":
        assert demo.delete(f"/api/v1/users/{u['id']}").status_code == 200
    elif how == "admin_sign_out":
        r = demo.post(f"/api/v1/users/{u['id']}/sign-out")
        assert r.status_code == 200 and r.json()["revoked_groups"] == ["demo"]
    elif how == "self_sign_out":
        assert c.post("/api/v1/users/me/sign-out").status_code == 200
        assert c.get("/api/v1/me").status_code == 401  # the console session ended with them
    else:
        assert demo.post(f"/api/v1/users/{u['id']}/password", json={"password": "An0ther-Passw0rd!"}).status_code == 200
    assert subject_revoked(demo, u["id"], t)
    assert other["id"] not in entries(demo)["sub"]  # nobody else
    assert "demo" in spy
    assert u["id"] in revoked_of(cloud.pushes[-1]["config"])["sub"]
    for x in (c, oc):
        x.__exit__(None, None, None)


def test_a_person_may_not_sign_out_someone_else(demo):
    make_user(demo, "a@x", "mcp_user", ["demo"])
    b = make_user(demo, "b@x", "mcp_user", ["demo"])
    with TestClient(demo.app) as a:
        login(a, "a@x", PW)
        assert a.post(f"/api/v1/users/{b['id']}/sign-out").status_code == 403
    assert demo.post("/api/v1/users/nope/sign-out").status_code == 404


def test_rfc7009_revoke_an_access_token_or_a_whole_grant(demo, cloud, spy):
    deployed(demo)
    u, c, t, cid = person(demo)
    meta = demo.get("/.well-known/oauth-authorization-server").json()
    assert meta["revocation_endpoint"] == "http://testserver/oauth/revoke"
    assert meta["revocation_endpoint_auth_methods_supported"] == ["none"]
    with TestClient(demo.app) as anon:  # a public client: no session, no CSRF, form-encoded
        r = anon.post("/oauth/revoke", data={"token": t["access_token"], "client_id": cid})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        drain(demo)
        assert claims(t["access_token"])["jti"] in entries(demo)["jti"]
        assert "demo" in spy
        # unknown, garbage or already-expired tokens: 200 and nothing (RFC 7009 §2.2)
        assert anon.post("/oauth/revoke", data={"token": "x.y.z", "client_id": cid}).status_code == 200
        assert anon.post("/oauth/revoke", data={"token": "nope", "client_id": cid}).status_code == 200
        # another client's token is refused; an unknown client is invalid_client
        other = demo.post("/api/v1/oauth/clients", json={"name": "o", "redirect_uris": ["https://o.example/cb"]})
        r = anon.post("/oauth/revoke", data={"token": t["access_token"], "client_id": other.json()["client_id"]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_request"
        assert anon.post("/oauth/revoke", data={"token": "x", "client_id": "nope"}).json()["error"] == "invalid_client"
        # a refresh token: the whole grant goes, with every access token it minted
        t2 = refresh(demo, cid, t["refresh_token"]).json()
        r = anon.post(
            "/oauth/revoke", data={"token": t2["refresh_token"], "token_type_hint": "refresh_token", "client_id": cid}
        )
        assert r.status_code == 200
    drain(demo)
    assert claims(t2["access_token"])["jti"] in entries(demo)["jti"]
    assert refresh(demo, cid, t2["refresh_token"]).json()["error"] == "invalid_grant"
    c.__exit__(None, None, None)


def test_refresh_reuse_and_client_delete_revoke_the_access_tokens_too(demo, cloud):
    deployed(demo)
    u, c, t, cid = person(demo)
    t2 = refresh(demo, cid, t["refresh_token"]).json()
    assert refresh(demo, cid, t["refresh_token"]).json()["error"] == "invalid_grant"  # reuse
    drain(demo)
    jtis = entries(demo)["jti"]
    assert claims(t["access_token"])["jti"] in jtis and claims(t2["access_token"])["jti"] in jtis
    verifier, challenge = pkce()
    t3 = exchange(demo, cid, consent(c, cid, challenge), verifier).json()
    assert demo.delete(f"/api/v1/oauth/clients/{cid}").status_code == 200
    drain(demo)
    assert claims(t3["access_token"])["jti"] in entries(demo)["jti"]
    c.__exit__(None, None, None)


def test_the_access_token_ttl_is_configurable(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_OAUTH_ACCESS_TTL", "600")
    _, c, t, _ = person(demo)
    assert t["expires_in"] == 600
    k = claims(t["access_token"])
    assert k["exp"] - k["iat"] == 600
    c.__exit__(None, None, None)
