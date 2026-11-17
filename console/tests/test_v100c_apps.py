"""1.0.0c Phase 7 (CONTRACTS §33.1 / §33.2, console side): the Packages card lists the apps (`ui://`) and skills
(`skill://`) each zone serves with the last scan's verdict, and `/groups/{g}/apps/{zone}/{name}` previews an app in a
sandboxed iframe with the app's CSP and no credentials."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from ramen_console import apps
from tests.conftest import APP, APP_CSP, APP_HTML, LOAD, SKILL, SKILL_MD
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixture
from tests.test_v100c_skillspector import report, runner, skill_row, wrap, write_skill  # noqa: F401 - fixture

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
PREVIEW = "/groups/demo/apps/zone-a/hello"
MANIFEST = {k: v for k, v in LOAD.items() if k not in ("errors", "apps", "skills")}  # an older runtime: resources only


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"canary": False, **body}).json()["id"])


def ready(demo, workers):  # noqa: F811
    for w in workers.values():
        w.mcp_keys = None
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})


# pure functions --------------------------------------------------------------------------------------------------


def test_apps_are_the_apps_list_else_the_mcp_app_resources_of_the_manifest():
    hello = {"name": "hello", "uri": APP["uri"], "title": "", "description": "", "csp": APP_CSP}
    assert apps.apps_of(MANIFEST) == [hello]
    assert apps.apps_of(LOAD) == [{**hello, "title": "Hello", "description": "says hi"}]
    assert apps.apps_of({**LOAD, "apps": [{"name": "x", "uri": "ui://demo/x"}]}) == [
        {"name": "x", "uri": "ui://demo/x", "title": "", "description": "", "csp": apps.DEFAULT_CSP}
    ]
    plain = {"resources": [{"uri": "ramen://demo/readme", "mimeType": "text/html"}, {"uri": "ui://demo/x"}]}
    assert apps.apps_of(plain) == []  # the profile is what makes an app, not the scheme or the mime alone
    assert apps.apps_of({}) == [] and apps.apps_of(None) == []


def test_skills_come_from_the_skills_list_else_the_skill_resources_with_the_scan_verdict():
    rep = {"skills": [skill_row("noodle-math", 30), skill_row("ramen-served-zone-a", 2)]}
    rep["skills"][0]["recommendation"] = "CAUTION"
    rows = apps.skills_of(MANIFEST, rep)
    row = {"name": "noodle-math", "uri": SKILL_MD["uri"], "description": "", "version": None, "files": None}
    assert rows == [{**row, "scan": "CAUTION"}]
    assert apps.skills_of(LOAD, rep) == [
        {**row, "description": SKILL["description"], "version": SKILL["version"], "files": 1, "scan": "CAUTION"}
    ]
    assert apps.skills_of({"resources": [{"uri": "skill://x/other.md"}]}, None) == []  # only SKILL.md names a skill


def test_the_csp_is_cleaned_and_injected_once_before_any_script():
    assert apps.clean_csp(None) == apps.DEFAULT_CSP and apps.clean_csp({}) == apps.DEFAULT_CSP
    assert apps.clean_csp("default-src 'none'") == apps.DEFAULT_CSP  # the runtime hands a mapping, never a string
    assert apps.clean_csp({"script-src": ["'unsafe-inline'", "https://cdn.example.com"]}) == (
        "script-src 'unsafe-inline' https://cdn.example.com"
    )
    for bad in ({"default-src": ['x"><script>']}, {"default-src": "'self'"}, {"Default Src": ["'self'"]}):
        assert apps.clean_csp(bad) == apps.DEFAULT_CSP  # a hostile or malformed value is dropped whole
    doc = apps.with_csp("<html><HEAD lang=x><script>1</script></HEAD></html>", "default-src 'none'")
    assert doc.index('<meta http-equiv="Content-Security-Policy"') < doc.index("<script>") and doc.count("<meta") == 1
    assert apps.with_csp("<p>no head</p>", "default-src 'none'").startswith("<meta http-equiv=")
    assert apps.app_name("ui://demo/hello") == "hello" and apps.app_name("ui://demo") is None
    assert not apps.valid_name("../x") and not apps.valid_name("") and not apps.valid_name("a.b")
    assert apps.valid_name("iris_chart-2")


# the Packages card -------------------------------------------------------------------------------------------------


def test_the_packages_card_lists_apps_and_skills_per_zone(demo, workers):  # noqa: F811
    ready(demo, workers)
    assert deploy(demo)["status"] == "ok"
    page = demo.get("/groups/demo").text
    assert page.count("Apps and skills") == 2  # one block per zone
    assert f'href="{PREVIEW}"' in page and 'href="/groups/demo/apps/zone-b/hello"' in page
    assert "noodle-math" in page and "not scanned" in page and "says hi" in page and SKILL["version"] in page


def test_the_skill_row_carries_the_last_scan_verdict(demo, runner, tmp_path, workers):  # noqa: F811
    for w in workers.values():
        w.load_result = {**MANIFEST, "errors": []}  # `ws` (the runner's) swapped in a manifest without the skill
    write_skill(tmp_path)
    row = skill_row("noodle-math", 40)
    row["risk_severity"] = "MEDIUM"
    runner.out = {"exit_code": 0, "log": wrap(report(row, skill_row("ramen-served-zone-a", 2)))}
    job = wait_job(demo, demo.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])
    assert job["status"] == "ok", job
    page = demo.get("/groups/demo").text
    block = page[page.index("Apps and skills") :]
    assert re.search(r"noodle-math</code>.{0,400}scan: CAUTION", block, re.S), block[:1500]
    assert f"/groups/demo/scans/{job['id']}/zone-a" in block


# the preview page -----------------------------------------------------------------------------------------------------


def test_the_preview_sandboxes_the_app_with_its_csp_and_escapes_the_html_once(demo, workers):  # noqa: F811
    ready(demo, workers)
    job = deploy(demo)
    r = demo.get(PREVIEW)
    assert r.status_code == 200, r.text
    m = re.search(r'<iframe([^>]*) srcdoc="([^"]*)"', r.text)
    assert m, r.text
    attrs, srcdoc = m.group(1), m.group(2)
    assert 'sandbox="allow-scripts"' in attrs and "allow-same-origin" not in attrs
    assert 'referrerpolicy="no-referrer"' in attrs and "credentialless" in attrs
    # escaped exactly once: the hostile script is inert text in the attribute, and nothing is double-escaped
    assert "&lt;script&gt;document.cookie&lt;/script&gt;" in srcdoc and "&amp;lt;" not in srcdoc
    assert "<script>document.cookie" not in r.text
    assert srcdoc.count("&#34;") >= 2 and "&amp;" in srcdoc  # the app's own `"&` survive as entities
    # the app's own CSP from _meta["ui/csp"] rides in a meta before its script
    meta = "&lt;meta http-equiv=&#34;Content-Security-Policy&#34; content=&#34;" + APP_CSP
    meta = meta.replace("'", "&#39;")
    assert meta in srcdoc and srcdoc.index(meta) < srcdoc.index("&lt;script&gt;")
    # the page itself: strict headers, no frame-ancestors, nothing cached
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "frame-src 'self' about:" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    # §33.4a (S8): the worker was asked with the console's own credential — the last deploy's evals key, which every
    # worker of the zone holds (never a stored group key a worker may not know yet) — plus the admin key, no cookie
    calls = [c for c in workers["cold"].calls if c[0] == "Mcp/Call" and c[2].get("method") == "resources/read"]
    assert calls and calls[-1][2]["params"]["uri"] == APP["uri"]
    md, key = calls[-1][1], evals_key_of(job)
    assert "cookie" not in md and md["authorization"] == f"Bearer {key}" and key.startswith("rek_")
    assert md["x-ramen-admin-key"] == "adm" and "rmk_" not in md["authorization"]


def test_the_default_csp_when_the_app_names_none_or_a_hostile_one(demo, workers):  # noqa: F811
    ready(demo, workers)
    for w in workers.values():
        w.resources[APP["uri"]] = {"mimeType": APP["mimeType"], "text": APP_HTML}
        hostile = {**APP, "_meta": {"ui/csp": {"default-src": ['x"><script>']}}}
        w.load_result = {**LOAD, "resources": [hostile, SKILL_MD]}
    deploy(demo)
    r = demo.get(PREVIEW)
    assert r.status_code == 200 and apps.DEFAULT_CSP.replace("'", "&#39;") in r.text and "x&#34;&gt;" not in r.text


def test_unknown_apps_non_apps_and_non_members_are_refused(demo, workers):  # noqa: F811
    ready(demo, workers)
    deploy(demo)
    assert demo.get("/groups/demo/apps/zone-a/nope").status_code == 404
    assert demo.get("/groups/demo/apps/zone-a/readme").status_code == 404  # a resource, not an app
    assert demo.get("/groups/demo/apps/zone-a/..%2Fhello").status_code == 404
    assert demo.get("/groups/demo/apps/zone-c/hello").status_code == 404  # not a zone of the group
    assert demo.get("/groups/other/apps/zone-a/hello").status_code == 404
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.get(PREVIEW).status_code == 200
    with TestClient(demo.app) as u:
        login(u, "o@x", PW)
        assert u.get(PREVIEW).status_code == 403
    assert TestClient(demo.app).get(PREVIEW, follow_redirects=False).status_code == 303


def test_a_worker_that_cannot_answer_is_a_502_and_a_mismatched_answer_a_404(demo, workers):  # noqa: F811
    ready(demo, workers)
    deploy(demo)
    for w in workers.values():
        w.resources[APP["uri"]] = {"mimeType": "text/plain", "text": "not an app"}
    assert demo.get(PREVIEW).status_code == 404
    for w in workers.values():
        del w.resources[APP["uri"]]
    assert demo.get(PREVIEW).status_code == 404  # the worker says -32002
    for w in workers.values():
        w.resources[APP["uri"]] = {"mimeType": APP["mimeType"], "text": "x" * (apps.MAX_HTML + 1)}
    assert demo.get(PREVIEW).status_code == 502
    for w in workers.values():
        w.smoke_ok = False
    assert demo.get(PREVIEW).status_code == 502
    for w in workers.values():
        w.stop()
    assert demo.get(PREVIEW).status_code == 502


def test_the_preview_needs_no_group_key_and_survives_a_key_minted_after_the_deploy(demo, workers):  # noqa: F811
    """S8: the preview used the store's first MCP key; a key minted after the deploy is unknown to the workers and
    every member got a 502. Now the read is the console's own, so neither a missing nor a newer key matters."""
    for w in workers.values():
        w.mcp_keys = None  # the fake accepts any bearer here; the node's acceptance of RAMEN_EVALS_KEY is its own test
    job = deploy(demo)
    assert demo.get(PREVIEW).status_code == 200  # no group key at all
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "later"})  # minted after the deploy
    assert demo.get(PREVIEW).status_code == 200
    for w in workers.values():  # a worker that holds nothing but the deploy's own key
        w.mcp_keys = {evals_key_of(job)}
    assert demo.get(PREVIEW).status_code == 200


def evals_key_of(job: dict) -> str:
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    return next(ln.split("=", 1)[1] for ln in env_file.splitlines() if ln.startswith("RAMEN_EVALS_KEY="))
