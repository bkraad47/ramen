"""CONTRACTS §24 (v1.0.0), console half, on a real console process (memory store, local cloud adapter — whose
sleep/wake only flip the zone state, which is exactly what these cases read).

§24.1: `sleep_after_secs` validation and who may set it; sleep / wake by hand; the internal wake endpoint the
activator calls (worker admin key, refused when it came through an edge, one wake however many callers).
§24.2: `build_image` cannot be turned on without a builder.
§24.3: `config/otel` round trip with write-only headers that are in no response and no backup; `telemetry` in the
`/oauth/token` answer; the consent checkbox on the OAuth consent page and the `tel` claim, down to a real node that
serves that token without asking; the console's own `ramen.deploy` span."""

import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ramen_tests import env as E
from ramen_tests.console import Console, items
from ramen_tests.fakes import Collector
from ramen_tests.localconsole import REDIRECT, LocalConsole, pkce
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import ALL_INPUT, Modern
from ramen_tests.tokens import claims_of

pytestmark = pytest.mark.conformance
PW = secrets.token_hex(10) + "Aa1!"
WORKER_ADMIN = secrets.token_hex(16)
OWN_HEADER = "own-" + secrets.token_hex(12)  # what Ramen sends its collector: write-only
CLIENT_HEADER = "client-" + secrets.token_hex(12)  # what agents are told to send: theirs to see, still write-only here
INVALID = (400, 422)


@pytest.fixture(scope="module")
def con():
    with Collector() as collector, LocalConsole(env={"RAMEN_ADMIN_KEY": WORKER_ADMIN}) as c:
        c.seed_group()
        a = c.admin
        assert a.create_group("other", E.DEMO_REPO).status_code in (201, 409)
        assert a.create_environment("other", "dev", ["local"]).status_code in (201, 409)
        c.collector = collector
        c.users = {}
        for name, role, groups in (
            ("gadmin", "group_admin", ["demo"]),
            ("outsider", "group_admin", ["other"]),
            ("viewer", "viewer", ["demo"]),
        ):
            email = f"{name}-{secrets.token_hex(3)}@ramen.test"
            assert a.create_user(email, PW, role, groups).status_code == 201
            client = Console(c.url)
            assert client.login(email, PW).status_code in (200, 303)
            c.users[name] = client
        yield c
        for client in c.users.values():
            client.close()


def workers(c: Console, group: str = "demo") -> dict:
    r = c.get("workers", group=group, zone="local")
    assert r.status_code == 200, r.text
    return r.json()


def put_sleep(c: Console, secs, group: str = "demo") -> httpx.Response:
    return c.put("workers", {"sleep_after_secs": secs}, group=group, zone="local")


def audit(con, action: str) -> list[dict]:
    return [e for e in items(con.admin.audit()) if e.get("action") == action]


def set_otel(con, **body) -> httpx.Response:
    return con.admin.put("config_otel", body)


@pytest.fixture
def otel_off(con):
    con.admin.put("group", {"require_telemetry_consent": False}, group="demo")
    assert set_otel(con, enabled=False).status_code == 200
    return con


@pytest.fixture
def otel_on(con):
    r = set_otel(
        con,
        enabled=True,
        endpoint=con.collector.url,
        headers={"authorization": OWN_HEADER},
        client_endpoint="https://otel.example:4318",
        client_headers={"x-team": CLIENT_HEADER},
    )
    assert r.status_code == 200, r.text
    yield con
    con.admin.put("group", {"require_telemetry_consent": False}, group="demo")
    set_otel(con, enabled=False)


# --- §24.1 the setting ------------------------------------------------------------------------------------------------
def test_sleep_after_secs_defaults_to_never(con):
    w = workers(con.admin, "other")
    assert w.get("sleep_after_secs", 0) == 0 and w.get("state", "awake") == "awake", w


@pytest.mark.parametrize("secs", [60, 61, 3600, 0])
def test_sleep_after_secs_accepts_zero_or_a_minute_and_more(con, secs):
    r = put_sleep(con.admin, secs)
    assert r.status_code == 200, r.text
    assert workers(con.admin)["sleep_after_secs"] == secs


@pytest.mark.parametrize("secs", [1, 59, -1, -60, "soon", 1.5])
def test_sleep_after_secs_refuses_anything_under_a_minute(con, secs):
    assert put_sleep(con.admin, 120).status_code == 200
    r = put_sleep(con.admin, secs)
    assert r.status_code in INVALID, f"{secs!r}: {r.status_code} {r.text[:200]}"
    assert workers(con.admin)["sleep_after_secs"] == 120, "a refused value changes nothing"
    put_sleep(con.admin, 0)


def test_only_a_super_admin_or_an_admin_of_the_group_sets_it(con):
    assert put_sleep(con.admin, 300).status_code == 200
    assert put_sleep(con.users["gadmin"], 600).status_code == 200
    assert workers(con.admin)["sleep_after_secs"] == 600
    for who in ("outsider", "viewer"):
        assert put_sleep(con.users[who], 60).status_code == 403, who
    with Console(con.url) as anon:
        assert put_sleep(anon, 60).status_code in (401, 403)
    assert workers(con.admin)["sleep_after_secs"] == 600
    last = audit(con, "workers.sleep_after")
    assert last and any("to:600" in e["tags"] and "group:demo" in e["tags"] for e in last), last[:2]
    put_sleep(con.admin, 0)


def test_others_never_see_the_control(con):
    assert put_sleep(con.admin, 600).status_code == 200
    admin_page = con.users["gadmin"].page("/groups/demo").text
    viewer_page = con.users["viewer"].page("/groups/demo").text
    assert "sleep_after" in admin_page, "the group admin's Zone actions carry the setting"
    assert "sleep_after" not in viewer_page, "a viewer never sees the control"
    put_sleep(con.admin, 0)


# --- §24.1 sleep / wake by hand ---------------------------------------------------------------------------------------
def test_sleep_and_wake_by_hand_flip_the_state_and_are_audited(con):
    a = con.users["gadmin"]
    r = a.post("zone_sleep", group="demo", zone="local")
    assert r.status_code == 200, r.text
    assert workers(con.admin)["state"] == "asleep"
    r = a.post("zone_wake", group="demo", zone="local")
    assert r.status_code == 200, r.text
    assert workers(con.admin).get("state", "awake") == "awake"
    for action in ("workers.sleep", "workers.wake"):
        rows = [e for e in audit(con, action) if "group:demo" in e["tags"] and "trigger:manual" in e["tags"]]
        assert rows and rows[0]["ok"] is True, f"{action} with trigger:manual: {audit(con, action)[:2]}"


def test_sleep_and_wake_need_the_same_roles(con):
    for who in ("outsider", "viewer"):
        for route in ("zone_sleep", "zone_wake"):
            assert con.users[who].post(route, group="demo", zone="local").status_code == 403, (who, route)
    assert workers(con.admin).get("state", "awake") == "awake"


def test_the_workers_table_shows_the_zone_state(con):
    assert con.admin.post("zone_sleep", group="demo", zone="local").status_code == 200
    try:
        page = con.admin.page("/ui/groups/demo/zones/local/workers").text
        assert "asleep" in page, page[:400]
    finally:
        assert con.admin.post("zone_wake", group="demo", zone="local").status_code == 200


# --- §24.1 the internal wake endpoint ---------------------------------------------------------------------------------
def internal_wake(con, key: str | None = WORKER_ADMIN, group: str = "demo", headers: dict | None = None):
    h = {**({"x-ramen-admin-key": key} if key else {}), **(headers or {})}
    return httpx.post(f"{con.url}/api/v1/internal/zones/{group}/local/wake", headers=h, timeout=30)


@pytest.fixture
def asleep(con):
    assert con.admin.post("zone_sleep", group="demo", zone="local").status_code == 200
    assert workers(con.admin)["state"] == "asleep"
    yield con
    con.admin.post("zone_wake", group="demo", zone="local")


def wait_state(con, want: str, timeout: float = 20) -> str:
    deadline, state = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        state = workers(con.admin).get("state", "awake")
        if state == want:
            break
        time.sleep(0.2)
    return state


def test_the_wake_endpoint_needs_the_worker_admin_key(asleep):
    con = asleep
    for key in (None, "wrong", secrets.token_hex(16), con.creds[1]):
        r = internal_wake(con, key)
        assert r.status_code in (401, 403, 404), f"{key!r}: {r.status_code}"
    r = con.admin.http.post(f"{con.url}/api/v1/internal/zones/demo/local/wake")
    assert r.status_code in (401, 403, 404), "a signed-in super admin is not a zone's activator"
    for name in ("x-ramen-api-key", "authorization"):
        assert internal_wake(con, None, headers={name: WORKER_ADMIN}).status_code in (401, 403, 404), name
    assert workers(con.admin)["state"] == "asleep", "nothing woke"


def test_the_wake_endpoint_refuses_a_request_that_came_through_an_edge(asleep):
    con = asleep
    for xff in ("203.0.113.9", "10.0.0.1, 203.0.113.9", "127.0.0.1"):
        r = internal_wake(con, headers={"X-Forwarded-For": xff})
        assert r.status_code in (403, 404), f"X-Forwarded-For {xff}: {r.status_code} {r.text[:120]}"
        assert "state" not in r.text
    time.sleep(0.5)
    assert workers(con.admin)["state"] == "asleep", "the right key behind a proxy still wakes nothing"


def test_the_wake_endpoint_wakes_the_zone_and_audits_the_call(asleep):
    con = asleep
    before = len([e for e in audit(con, "workers.wake") if "trigger:call" in e["tags"]])
    r = internal_wake(con)
    assert r.status_code == 200, r.text
    assert r.json()["state"] in ("waking", "awake"), r.json()
    assert wait_state(con, "awake") == "awake"
    rows = [e for e in audit(con, "workers.wake") if "trigger:call" in e["tags"] and "group:demo" in e["tags"]]
    assert len(rows) == before + 1, rows[:3]
    assert WORKER_ADMIN not in json.dumps(items(con.admin.audit()))


def test_concurrent_wake_calls_start_exactly_one_wake(asleep):
    con = asleep
    before = len([e for e in audit(con, "workers.wake") if "trigger:call" in e["tags"]])
    n = 12
    barrier, out = threading.Barrier(n), []

    def one():
        barrier.wait()
        out.append(internal_wake(con))

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert [r.status_code for r in out] == [200] * n, [r.status_code for r in out]
    assert all(r.json()["state"] in ("waking", "awake") for r in out), [r.json() for r in out][:3]
    assert sum(1 for r in out if r.json().get("started")) == 1, "one caller started the wake, the rest joined it"
    assert wait_state(con, "awake") == "awake"
    assert len([e for e in audit(con, "workers.wake") if "trigger:call" in e["tags"]]) == before + 1


def test_waking_an_awake_zone_changes_nothing(con):
    assert workers(con.admin).get("state", "awake") == "awake"
    before = len(audit(con, "workers.wake"))
    for _ in range(3):
        r = internal_wake(con)
        assert r.status_code == 200 and r.json()["state"] == "awake" and not r.json().get("started"), r.text
    assert len(audit(con, "workers.wake")) == before


def test_the_wake_endpoint_knows_only_real_zones(con):
    assert internal_wake(con, group="no-such-group").status_code == 404


def test_a_deploy_wakes_a_sleeping_zone_first(asleep):
    con = asleep
    job = con.admin.deploy("demo", "dev").json()
    con.admin.wait_job(job["id"], timeout=300)
    assert workers(con.admin).get("state", "awake") == "awake", "a deploy on a sleeping zone wakes it first"
    rows = [e for e in audit(con, "workers.wake") if "trigger:deploy" in e["tags"]]
    assert rows, audit(con, "workers.wake")[:3]


# --- §24.2 build_image ------------------------------------------------------------------------------------------------
def test_build_image_cannot_be_turned_on_without_a_builder(con):
    r = con.admin.put("group", {"build_image": True}, group="demo")
    assert r.status_code == 422, f"{r.status_code} {r.text[:200]}"
    assert "builder" in r.text, "the refusal names the missing builder"
    assert con.admin.get("group", group="demo").json().get("build_image") in (None, False)
    assert con.admin.put("group", {"build_image": False}, group="demo").status_code == 200
    assert con.users["viewer"].put("group", {"build_image": False}, group="demo").status_code == 403


# --- §24.3 config/otel ------------------------------------------------------------------------------------------------
def test_config_otel_round_trip_keeps_header_values_write_only(otel_on):
    con = otel_on
    doc = con.admin.get("config_otel").json()
    assert doc["enabled"] is True and doc["endpoint"] == con.collector.url, doc
    assert doc["client_endpoint"] == "https://otel.example:4318"
    assert OWN_HEADER not in json.dumps(doc) and CLIENT_HEADER not in json.dumps(doc), "header values are write-only"
    # a later PUT that does not mention the headers keeps them (they could not be read back to re-send)
    assert set_otel(con, client_endpoint="https://otel2.example:4318").status_code == 200
    cid = con.register_client()
    told = con.mint(cid)["telemetry"]
    assert told["otlp_endpoint"] == "https://otel2.example:4318" and told["headers"] == {"x-team": CLIENT_HEADER}


def test_the_header_values_are_in_no_response_and_no_backup(otel_on):
    con = otel_on
    b = con.admin.post("backups", {"target": "local"})
    assert b.status_code == 201, b.text
    responses = [
        con.admin.get("config_otel"),
        con.admin.get("config"),
        con.admin.get("backups"),
        con.admin.get("backup_download", id=b.json()["id"]),
        con.admin.audit(),
        con.admin.get("group", group="demo"),
        con.admin.page("/config"),
        con.admin.page("/groups/demo"),
        con.admin.page("/audit"),
    ]
    for r in responses:
        assert r.status_code == 200, f"{r.request.url}: {r.status_code}"
        for what, value in (("Ramen's own header", OWN_HEADER), ("the client header", CLIENT_HEADER)):
            assert value not in r.text, f"{what} leaked by {r.request.method} {r.request.url}"
    backup = responses[3].json()
    assert OWN_HEADER not in json.dumps(backup) and CLIENT_HEADER not in json.dumps(backup)


def test_config_otel_is_validated(otel_off):
    con = otel_off
    assert set_otel(con, enabled=True, endpoint="").status_code in INVALID, "on needs an endpoint"
    for bad in ("jaeger:4318", "ftp://jaeger:4318", "http://", "http://jaeger:4318/?x=1", "http://a b:4318"):
        assert set_otel(con, endpoint=bad).status_code in INVALID, bad
    assert set_otel(con, headers={"bad header": "x"}).status_code in INVALID
    assert set_otel(con, headers={"x-ok": "line\nbreak"}).status_code in INVALID
    assert con.admin.get("config_otel").json()["enabled"] is False


def test_config_otel_is_super_admin_only_and_audited(otel_on):
    con = otel_on
    for who in ("gadmin", "viewer"):
        assert con.users[who].get("config_otel").status_code == 403, who
        assert con.users[who].put("config_otel", {"enabled": False}).status_code == 403, who
    assert con.admin.get("config_otel").json()["enabled"] is True
    rows = audit(con, "config.otel")
    assert rows and OWN_HEADER not in json.dumps(rows) and CLIENT_HEADER not in json.dumps(rows)


# --- §24.3 telling the client ----------------------------------------------------------------------------------------
def test_the_token_answer_carries_telemetry_when_enabled(otel_on):
    con = otel_on
    cid = con.register_client()
    tok = con.mint(cid)
    assert tok["telemetry"] == {
        "otlp_endpoint": "https://otel.example:4318",
        "headers": {"x-team": CLIENT_HEADER},
        "resource_attributes": {"ramen.group": "demo", "ramen.zone": "local"},
    }, tok.get("telemetry")
    assert OWN_HEADER not in json.dumps(tok), "never Ramen's own headers"
    again = con.refresh(tok["refresh_token"], cid)
    assert again["telemetry"] == tok["telemetry"], "a refresh says the same"


def test_the_client_endpoint_defaults_to_the_endpoint_with_no_headers(otel_off):
    con = otel_off
    r = set_otel(con, enabled=True, endpoint=con.collector.url, client_endpoint="", client_headers={})
    assert r.status_code == 200, r.text
    tok = con.mint(con.register_client())
    assert tok["telemetry"]["otlp_endpoint"] == con.collector.url and tok["telemetry"]["headers"] == {}
    assert OWN_HEADER not in json.dumps(tok)
    set_otel(con, enabled=False)


def test_the_token_answer_has_no_telemetry_when_disabled(otel_off):
    tok = otel_off.mint(otel_off.register_client())
    assert "telemetry" not in tok


# --- §24.3 consent on the OAuth page ----------------------------------------------------------------------------------
def authorize_query(cid: str) -> tuple[dict, str]:
    verifier, challenge = pkce()
    scope = "mcp:demo:local"
    return {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": scope,
    }, verifier


def decide(con, q: dict, **form) -> httpx.Response:
    a = con.admin
    data = {**q, "csrf_token": a.http.cookies.get("ramen_csrf", ""), **form}
    return a.http.post(f"{con.url}/oauth/authorize", data=data, follow_redirects=False)


def redirect_params(r: httpx.Response) -> dict:
    """Where the consent decision sends the browser: a 303, or the hand-back page that names the target."""
    if "location" in r.headers:
        target = r.headers["location"]
    else:
        start = r.text.index(REDIRECT)
        target = r.text[start : r.text.index("'", start)].replace("&amp;", "&")
    return {k: v[0] for k, v in parse_qs(urlparse(target).query).items()}


def exchange(con, cid: str, code: str, verifier: str) -> dict:
    return con.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }
    )


def require_consent(con, on: bool = True) -> httpx.Response:
    return con.admin.put("group", {"require_telemetry_consent": on}, group="demo")


def test_consent_can_only_be_required_while_telemetry_is_enabled(otel_off):
    r = require_consent(otel_off)
    assert r.status_code in INVALID, f"{r.status_code} {r.text[:200]}"
    assert not otel_off.admin.get("group", group="demo").json().get("require_telemetry_consent")


def test_requiring_consent_is_the_group_admins_call(otel_on):
    con = otel_on
    assert con.users["viewer"].put("group", {"require_telemetry_consent": True}, group="demo").status_code == 403
    assert con.users["outsider"].put("group", {"require_telemetry_consent": True}, group="demo").status_code == 403
    assert con.users["gadmin"].put("group", {"require_telemetry_consent": True}, group="demo").status_code == 200
    assert con.admin.get("group", group="demo").json()["require_telemetry_consent"] is True


def test_the_consent_page_shows_the_notice_and_a_required_checkbox(otel_on):
    con = otel_on
    cid = con.register_client()
    q, _ = authorize_query(cid)
    plain = con.admin.http.get(f"{con.url}/oauth/authorize", params=q).text
    assert "telemetry_consent" not in plain, "no checkbox for a group that does not require consent"
    assert require_consent(con).status_code == 200
    page = con.admin.http.get(f"{con.url}/oauth/authorize", params=q)
    assert page.status_code == 200
    assert 'name="telemetry_consent"' in page.text and "required" in page.text
    assert "otel.example:4318" in page.text, "the notice names where the telemetry goes"
    assert "never arguments or results" in page.text
    assert OWN_HEADER not in page.text and CLIENT_HEADER not in page.text


def test_approving_without_the_checkbox_is_impossible(otel_on):
    con = otel_on
    assert require_consent(con).status_code == 200
    cid = con.register_client()
    q, _ = authorize_query(cid)
    for form in ({}, {"telemetry_consent": "0"}, {"telemetry_consent": ""}, {"telemetry_consent": "on"}):
        r = decide(con, q, decision="allow", **form)
        assert r.status_code == 400 or "error" in redirect_params(r), f"{form}: {r.status_code} {r.text[:200]}"
        assert "code=" not in r.headers.get("location", "") and "code=" not in r.text, f"{form}: a code was issued"


def test_declining_ends_the_flow_as_before(otel_on):
    con = otel_on
    assert require_consent(con).status_code == 200
    q, _ = authorize_query(con.register_client())
    r = decide(con, q, decision="deny", telemetry_consent="1")
    params = redirect_params(r)
    assert params.get("error") == "access_denied" and "code" not in params, params


def test_approving_with_the_checkbox_mints_a_token_with_tel(otel_on):
    con = otel_on
    assert require_consent(con).status_code == 200
    cid = con.register_client()
    q, verifier = authorize_query(cid)
    params = redirect_params(decide(con, q, decision="allow", telemetry_consent="1"))
    assert "code" in params, params
    tok = exchange(con, cid, params["code"], verifier)
    assert claims_of(tok["access_token"]).get("tel") is True, claims_of(tok["access_token"])
    again = con.refresh(tok["refresh_token"], cid)
    assert claims_of(again["access_token"]).get("tel") is True, "the acceptance travels with the grant"
    rows = [e for e in audit(con, "oauth.authorize") if "telemetry:accepted" in e["tags"]]
    assert rows, "the acceptance is audited"


def test_a_group_that_does_not_require_it_mints_no_tel(otel_on):
    con = otel_on
    assert require_consent(con, False).status_code == 200
    tok = con.mint(con.register_client())
    assert "tel" not in claims_of(tok["access_token"])


def test_a_grant_made_before_the_requirement_cannot_be_refreshed_into_it(otel_on):
    """A refresh token from before the group required consent must not mint tokens for it: the person never saw the
    notice. They authorize again (and tick the box)."""
    con = otel_on
    assert require_consent(con, False).status_code == 200
    cid = con.register_client()
    old = con.mint(cid)
    assert require_consent(con).status_code == 200
    r = con.token_response({"grant_type": "refresh_token", "refresh_token": old["refresh_token"], "client_id": cid})
    if r.status_code == 200:
        assert "tel" not in claims_of(r.json()["access_token"]), "a tel claim nobody consented to"
    else:
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant", r.text


def test_a_node_serves_the_tel_token_without_asking_and_asks_the_one_without(otel_on):
    """End to end: tokens minted by the real console, verified by a real node of a group that requires consent."""
    con = otel_on
    cid = con.register_client()
    assert require_consent(con, False).status_code == 200
    without = con.mint(cid)["access_token"]  # issued before the group required consent
    assert require_consent(con).status_code == 200
    q, verifier = authorize_query(cid)
    code = redirect_params(decide(con, q, decision="allow", telemetry_consent="1"))["code"]
    with_tel = exchange(con, cid, code, verifier)["access_token"]
    env = {
        "RAMEN_SESSION_SECRET": con.session_secret,
        "RAMEN_OAUTH_ISSUER": con.url,
        "RAMEN_TELEMETRY_CONSENT": "1",
        "RAMEN_TELEMETRY_NOTICE": "otel.example:4318",
    }
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", env=env) as n:
        n.wait_serving()
        for transport in ("grpc", "http"):
            accepted = Modern(n.for_transport(transport), {}, key=with_tel)
            assert text_of(accepted.call("plain_tool", {"text": "tel"})) == "tel", "tel: true is never prompted"
            asked = Modern(n.for_transport(transport), ALL_INPUT, key=without).call("plain_tool", {"text": "x"})
            assert asked["resultType"] == "input_required", asked
            assert list(asked["inputRequests"]) == ["ramen_telemetry_consent"]


# --- §24.3 what a deploy hands the workers, and the console's own span -----------------------------------------------
def env_file(con) -> dict:
    path = con.dir / "buckets" / "demo" / ".ramen" / "env-local"
    assert path.exists(), con.output()
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_a_deploy_hands_the_workers_the_telemetry_settings_and_exports_a_span(otel_on):
    con = otel_on
    assert require_consent(con).status_code == 200
    job = con.admin.deploy("demo", "dev").json()
    done = con.admin.wait_job(job["id"], timeout=300)
    env = env_file(con)
    assert env.get("RAMEN_OTEL_ENDPOINT") == con.collector.url, sorted(env)
    assert env.get("RAMEN_TELEMETRY_CONSENT") == "1"
    assert env.get("RAMEN_TELEMETRY_NOTICE", "").startswith("otel"), "the client endpoint's host"
    span = con.collector.wait_spans(lambda s: s.get("name") == "ramen.deploy", timeout=20)[-1]
    a = span["attrs"]
    assert span["resource"]["service.name"] == "ramen-console" and span["resource"].get("service.version")
    assert (a["ramen.group"], a["ramen.env"]) == ("demo", "dev"), a
    assert "ramen.zones" in a and "ramen.compat" in a and "ramen.image" in a, sorted(a)
    assert "ramen.golden.passed" in a and "ramen.golden.failed" in a, sorted(a)
    outcome = next((v for k, v in a.items() if k.endswith("outcome")), None)
    assert outcome == ("ok" if done.get("status") == "ok" else "error"), (a, done.get("status"))
    posts = [p for p in con.collector.posts if p["path"] == "/v1/traces"]
    assert posts and all(p["headers"].get("authorization") == OWN_HEADER for p in posts), "sent with Ramen's headers"
    body = b"\n".join(p["body"] for p in posts).decode(errors="replace")
    assert CLIENT_HEADER not in body and OWN_HEADER not in body and con.creds[1] not in body


# --- §24.3 end to end on the local stack: this console's worker after a deploy ----------------------------------------
@pytest.fixture(scope="module")
def wired():
    """What `docker compose up` gives: a console whose zone `local` is one real worker (`RAMEN_WORKER_URL`), reached
    by the deploy file in its bucket and `Admin/Reload`. Plus a collector for both to export to."""
    with Collector() as collector:
        con = LocalConsole(env={"RAMEN_ADMIN_KEY": WORKER_ADMIN})
        bucket = con.dir / "buckets" / "demo"
        bucket.mkdir(parents=True)
        node = LocalNode(bucket=bucket, admin_key=WORKER_ADMIN, env={"RAMEN_SESSION_SECRET": con.session_secret})
        con.env["RAMEN_WORKER_URL"] = node.target
        node.env["RAMEN_OAUTH_ISSUER"] = con.url
        with con, node:
            con.seed_group()
            r = con.admin.create_mcp_key("demo", "wired")
            assert r.status_code == 201, r.text
            job = con.admin.wait_job(con.admin.deploy("demo", "dev").json()["id"], timeout=300)
            assert job["status"] == "ok", job.get("error")
            node.wait_serving()
            yield con, node, collector, r.json()["key"]


def test_after_a_deploy_the_worker_exports_with_ramens_headers_and_tells_agents_the_client_settings(wired):
    """§24.3 "Workers get RAMEN_OTEL_ENDPOINT / RAMEN_OTEL_HEADERS on their next deploy" and discover "carries the
    same object" as the token answer — observed at the worker itself, however the adapter delivers the settings."""
    con, node, collector, key = wired
    m = Modern(node.http, ALL_INPUT, key=key)
    assert "io.ramen/telemetry" not in m.discover()["_meta"], "telemetry is off: nothing to tell"
    r = con.admin.put(
        "config_otel",
        {
            "enabled": True,
            "endpoint": collector.url,
            "headers": {"authorization": OWN_HEADER},
            "client_endpoint": "https://otel.example:4318",
            "client_headers": {"x-team": CLIENT_HEADER},
        },
    )
    assert r.status_code == 200, r.text
    job = con.admin.wait_job(con.admin.deploy("demo", "dev").json()["id"], timeout=300)
    assert job["status"] == "ok", job.get("error")
    told = con.mint(con.register_client())["telemetry"]
    assert text_of(m.call("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})) in ("5", "5.0")
    spans = collector.wait_spans(
        lambda s: s["resource"].get("service.name") == "ramen-node" and s["attrs"].get("mcp.name"), timeout=20
    )
    assert spans[0]["attrs"]["ramen.group"] == "demo"
    posts = [p for p in collector.posts if b"ramen-node" in p["body"]]
    assert posts and all(p["headers"].get("authorization") == OWN_HEADER for p in posts), (
        "the worker exports without the headers set on Config → Telemetry"
    )
    assert m.discover()["_meta"].get("io.ramen/telemetry") == told, "discover carries what the token answer carries"
    text = node.log.read_text() + node.output()
    assert OWN_HEADER not in text and CLIENT_HEADER not in text, "a header value in the worker's log"


def test_after_a_deploy_the_worker_asks_for_consent_when_the_group_requires_it(wired):
    con, node, _, key = wired
    assert con.admin.put("config_otel", {"enabled": True, "endpoint": "http://otel.example:4318"}).status_code == 200
    assert require_consent(con).status_code == 200
    job = con.admin.wait_job(con.admin.deploy("demo", "dev").json()["id"], timeout=300)
    assert job["status"] == "ok", f"the console's own golden calls must pass the gate: {job.get('error')}"
    first = Modern(node.http, ALL_INPUT, key=key).call("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
    assert first["resultType"] == "input_required" and list(first["inputRequests"]) == ["ramen_telemetry_consent"]
    assert "otel.example:4318" in first["inputRequests"]["ramen_telemetry_consent"]["params"]["message"]
    assert require_consent(con, False).status_code == 200
    con.admin.wait_job(con.admin.deploy("demo", "dev").json()["id"], timeout=300)
    done = Modern(node.http, {}, key=key).call("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
    assert done["resultType"] == "complete", "the requirement is lifted by the next deploy"


def test_a_deploy_without_telemetry_hands_the_workers_nothing(otel_off):
    con = otel_off
    job = con.admin.deploy("demo", "dev").json()
    con.admin.wait_job(job["id"], timeout=300)
    env = env_file(con)
    assert not [k for k in env if k.startswith(("RAMEN_OTEL_", "RAMEN_TELEMETRY_"))], sorted(env)
