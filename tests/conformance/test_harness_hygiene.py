"""S7: a harness session leaves no residue — every user, MCP key, API key, OAuth client and group it creates through a
`Console` (helpers or raw `console.http`), and every branch it tracks, is removed by `ledger.sweep` at the end of the
session (the `admin` fixture's teardown). The ledger itself always runs; the residue check runs against a real local
console (skips without the console venv)."""

import subprocess

import httpx
import pytest

from ramen_tests import ledger
from ramen_tests.console import Console
from ramen_tests.localconsole import REDIRECT, LocalConsole

pytestmark = pytest.mark.conformance
PW = "Hy-" + "x9" * 6 + "Aa1!"


@pytest.fixture
def clean_ledger(monkeypatch):
    monkeypatch.setattr(ledger, "RECORDS", [])
    monkeypatch.setattr(ledger, "BRANCHES", [])
    monkeypatch.delenv("RAMEN_KEEP_RESIDUE", raising=False)
    monkeypatch.delenv("RAMEN_KEEP_GROUPS", raising=False)
    monkeypatch.delenv("RAMEN_E2E_GROUP", raising=False)


def fake_console(answers):
    """A Console whose transport answers from `answers[(method, path)]` and logs every request."""
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path))
        status, body = answers.get((req.method, req.url.path), (404, {"detail": "not found"}))
        return httpx.Response(status, json=body)

    c = Console("http://console.test")
    c.http._transport = httpx.MockTransport(handler)
    return c, seen


def test_the_ledger_records_every_create_and_sweeps_in_order(clean_ledger):
    c, seen = fake_console(
        {
            ("POST", "/api/v1/users"): (201, {"id": "u1"}),
            ("POST", "/api/v1/groups/demo/mcp-keys"): (201, {"id": "k1", "key": "rmk_x"}),
            ("POST", "/api/v1/api-keys"): (201, {"id": "a1", "key": "rmn_x"}),
            ("POST", "/api/v1/oauth/clients"): (201, {"client_id": "c1"}),
            ("POST", "/api/v1/groups"): (201, {"name": "probe-1"}),
            ("DELETE", "/api/v1/users/u1"): (200, {"ok": True}),
            ("DELETE", "/api/v1/groups/demo/mcp-keys/k1"): (200, {"ok": True}),
            ("DELETE", "/api/v1/api-keys/a1"): (500, {"detail": "boom"}),
            ("DELETE", "/api/v1/oauth/clients/c1"): (200, {"ok": True}),
            ("DELETE", "/api/v1/groups/probe-1"): (200, {"ok": True}),
        }
    )
    c.create_user("p@ramen.local", PW, "viewer")
    c.create_mcp_key("demo", "t")
    c.create_api_key("t")
    c.http.post("/api/v1/oauth/clients", json={"name": "t", "redirect_uris": [REDIRECT]})  # raw http counts too
    c.create_group("probe-1", "https://example.com/x.git")
    c.create_group("demo", "https://example.com/x.git")  # 404 here: nothing recorded
    c.http.get("/api/v1/users")  # reads are never recorded
    assert {r["kind"] for r in ledger.RECORDS} == {"user", "mcp_key", "api_key", "oauth_client", "group"}
    out = ledger.sweep(c)
    deletes = [p for m, p in seen if m == "DELETE"]
    assert deletes == [
        "/api/v1/oauth/clients/c1",
        "/api/v1/groups/demo/mcp-keys/k1",
        "/api/v1/api-keys/a1",
        "/api/v1/users/u1",
        "/api/v1/groups/probe-1",
    ]
    assert len(out["deleted"]) == 4 and len(out["left"]) == 1 and out["left"][0].startswith("api_key a1: 500 ")
    assert [r["kind"] for r in ledger.RECORDS] == ["api_key"]  # kept for a retry; the summary lists it
    c.close()


def test_kept_groups_other_consoles_and_keep_residue(clean_ledger, monkeypatch):
    c, _ = fake_console({("POST", "/api/v1/groups"): (201, {"name": "demo"})})
    c.create_group("demo", "https://example.com/x.git")  # the run's own group: never deleted
    monkeypatch.setenv("RAMEN_KEEP_GROUPS", "shared")
    c2, _ = fake_console({("POST", "/api/v1/groups"): (201, {})})
    c2.create_group("shared", "https://example.com/x.git")  # name from the request when the answer has none
    c2.create_group("other", "https://example.com/x.git")
    assert [r["id"] for r in ledger.RECORDS] == ["other"]
    ledger.RECORDS.append({"base": "http://elsewhere:1", "kind": "user", "id": "u9"})
    ledger.forget("http://elsewhere:1/")
    assert [r["id"] for r in ledger.RECORDS] == ["other"]
    monkeypatch.setenv("RAMEN_KEEP_RESIDUE", "1")
    assert ledger.sweep(c2) == {"deleted": [], "left": ["group other (RAMEN_KEEP_RESIDUE)"]}
    c.close()
    c2.close()


def test_tracked_branches_are_deleted(clean_ledger, tmp_path):
    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git("init", "--bare", "-q", str(remote))
    git("init", "-q", str(work))
    git("-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x", cwd=work)
    git("push", "-q", str(remote), "HEAD:refs/heads/probe-h1", cwd=work)
    ledger.track_branch(str(remote), "probe-h1")
    ledger.track_branch(str(remote), "probe-h1")
    ledger.track_branch(str(remote), "never-pushed")  # already gone: counts as deleted
    c, _ = fake_console({})
    out = ledger.sweep(c)
    assert out == {"deleted": ["branch probe-h1", "branch never-pushed"], "left": []} and not ledger.BRANCHES
    assert "probe-h1" not in git("branch", "-a", cwd=remote).stdout
    c.close()


def lists(a: Console) -> dict:
    """What a harness run could leave behind, as the super admin sees it."""

    def ids(key, field="id", **kw):
        r = a.get(key, **kw)
        assert r.status_code == 200, (key, r.status_code, r.text[:200])
        body = r.json()
        rows = body if isinstance(body, list) else next(v for v in body.values() if isinstance(v, list))
        return sorted(str(x.get(field)) for x in rows)

    return {
        "users": ids("users"),
        "api_keys": ids("api_keys"),
        "groups": ids("groups", "name"),
        "mcp_keys": ids("mcp_keys", group="demo"),
        "clients": sorted(str(x["client_id"]) for x in a.http.get("/api/v1/oauth/clients").json()),
    }


def test_a_harness_session_leaves_no_residue_on_a_real_console(clean_ledger):
    with LocalConsole() as lc:
        a = lc.admin
        assert a.create_group("demo", "https://example.com/demo.git").status_code in (201, 409)
        before = lists(a)
        # what the cloud suites do: users (helper and raw), keys of both kinds, an OAuth client, a throwaway group
        assert a.create_user("hy-1@ramen.local", PW, "viewer", ["demo"]).status_code == 201
        raw = a.http.post("/api/v1/users", json={"email": "hy-2@ramen.local", "password": PW, "memberships": {}})
        assert raw.status_code == 201
        assert a.create_mcp_key("demo", "hy").status_code == 201
        assert a.create_api_key("hy", groups=["demo"], role="viewer", client_type="devops").status_code == 201
        assert lc.register_client("hy")
        assert a.create_group("hy-probe", "https://example.com/probe.git").status_code == 201
        assert lists(a) != before
        out = ledger.sweep(a)
        assert out["left"] == [] and len(out["deleted"]) == 6, out
        assert lists(a) == before
