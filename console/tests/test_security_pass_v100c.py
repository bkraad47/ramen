"""1.0.0c Phase 7 security pass (CONTRACTS §33.4, D77): regressions for what the independent review probed on the
console — hostile repo text on the configs, consent and scan-report pages, a hostile app in the preview, a job record
read across groups, and an OTLP body that arrives chunked without a Content-Length."""

import json

from fastapi.testclient import TestClient

from tests.conftest import APP, APP_HTML, LOAD, SKILL_MD
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixture
from tests.test_oauth_server import authorize, pkce, register
from tests.test_v100c_configs import GOOD, collector, member, traces_json, write_configs  # noqa: F401 - fixture
from tests.test_v100c_skillspector import issue, report, runner, skill_row, wrap, write_skill  # noqa: F401 - fixture

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
HOSTILE = '</pre><script>alert("x")</script><img src=x onerror=alert(1)>'
ESCAPED = '&lt;/pre&gt;&lt;script&gt;alert("x")&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;'


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


def outsider(demo):  # noqa: F811
    """A viewer of `other`, never of `demo`."""
    make_user(demo, "o@x", "viewer", ["other"])
    c = TestClient(demo.app)
    return login(c, "o@x", PW)


# --- escaping: repo-controlled text is inert on every page that shows it ---------------------------------------------


def test_a_hostile_config_fragment_is_inert_on_the_configs_and_consent_pages(demo, tmp_path):
    files = {
        **GOOD,
        "rc.txt": HOSTILE + "\n",
        "manifest.yaml": GOOD["manifest.yaml"].replace(
            "description: demo LLM configs", "description: " + json.dumps(HOSTILE)
        ),
    }
    write_configs(tmp_path, files)
    m = member(demo)
    page = m.get("/groups/demo/configs").text
    assert "<script>alert" not in page and "<img src=x" not in page
    assert ESCAPED in page  # the `pre` filter: only `& < >` touched, quotes verbatim
    assert "&lt;img src=x onerror=alert(1)&gt;" in page  # the manifest description: autoescaped
    cid = register(demo)
    _, challenge = pkce()
    r = authorize(m, cid, challenge)
    assert r.status_code == 200, r.text[:200]
    assert "<script>alert" not in r.text and "<img src=x" not in r.text and ESCAPED in r.text


def test_a_hostile_scan_finding_is_inert_on_the_report_page(demo, runner, tmp_path):
    write_skill(tmp_path)
    bad = issue(rule='"><script>alert(2)</script>', file=HOSTILE, msg=HOSTILE)
    row = skill_row(HOSTILE, 30, [bad])
    row["risk_severity"] = "MEDIUM"
    runner.out = {"exit_code": 0, "log": wrap(report(row, skill_row("ramen-served-zone-a", 2)))}
    job = deploy(demo)
    assert job["status"] == "ok", job
    page = demo.get(f"/groups/demo/scans/{job['id']}/zone-a")
    assert page.status_code == 200
    assert "<script>alert" not in page.text and "<img src=x" not in page.text
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page.text  # the rule, autoescaped
    assert ESCAPED.replace('"', "&#34;") in page.text  # the message, autoescaped (quotes too)
    group = demo.get("/groups/demo").text  # the Packages card repeats the skill name and verdict
    assert "<script>alert" not in group and "<img src=x" not in group


def test_a_hostile_app_title_and_html_are_inert_on_the_group_page_and_the_preview(demo, ws):  # noqa: F811
    for w in ws.values():
        w.mcp_keys = None
        w.load_result = {
            **LOAD,
            "apps": [{"name": "hello", "uri": APP["uri"], "title": HOSTILE, "description": HOSTILE}],
        }
        w.resources[APP["uri"]] = {"mimeType": APP["mimeType"], "text": HOSTILE + APP_HTML, "_meta": APP["_meta"]}
        w.resources[SKILL_MD["uri"]] = {"mimeType": "text/markdown", "text": "---\nname: noodle-math\n---\n"}
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    assert deploy(demo, canary=False)["status"] == "ok"
    page = demo.get("/groups/demo").text
    assert "<script>alert" not in page and "<img src=x" not in page
    r = demo.get("/groups/demo/apps/zone-a/hello")
    assert r.status_code == 200, r.text
    assert "<script>alert" not in r.text and "<img src=x" not in r.text  # only inside the escaped srcdoc attribute
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in r.text


# --- a job record is the group's ------------------------------------------------------------------------------------


def test_a_job_record_is_readable_by_its_group_only(demo, ws):  # noqa: F811
    job = deploy(demo, canary=False)
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.get(f"/api/v1/jobs/{job['id']}").status_code == 200
        assert v.get(f"/ui/jobs/{job['id']}").status_code == 200
    with outsider(demo) as o:
        assert o.get(f"/api/v1/jobs/{job['id']}").status_code == 403
        assert o.get(f"/ui/jobs/{job['id']}").status_code == 403
        assert o.get("/api/v1/jobs/nope").status_code == 404
    assert demo.get(f"/api/v1/jobs/{job['id']}").status_code == 200  # the super admin


# --- OTLP ingest: a chunked body without Content-Length is still bounded ----------------------------------------------


def test_ingest_stops_reading_a_chunked_body_at_the_limit(demo, collector, monkeypatch):
    seen, _ = collector
    monkeypatch.setattr("ramen_console.web.otlp_routes.MAX_BODY", 64 * 1024)
    demo.post("/api/v1/config/otel", json={"enabled": True, "endpoint": "http://collector"})
    demo.put("/api/v1/config/otel", json={"enabled": True, "endpoint": "http://collector"})
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()["key"]

    def chunks():
        yield b'{"resourceSpans":['
        for _ in range(200):
            yield b" " * 1024
        yield b"]}"

    r = demo.post(
        "/otlp/v1/traces",
        content=chunks(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text
    assert not seen
    small = demo.post(
        "/otlp/v1/traces",
        content=iter([json.dumps(traces_json()).encode()]),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    assert small.status_code == 200, small.text
    assert len(seen) == 1
