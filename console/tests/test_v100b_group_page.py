"""1.0.0b group page (the user's review): no horizontal scroll — long cells wrap; only the last five deploy jobs are
listed, the rest stay in Audit and Logs."""

import asyncio
import re
from pathlib import Path

from ramen_console.deploy import Jobs
from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - fixtures

CSS = (Path(__file__).parents[1] / "src/ramen_console/static/ramen.css").read_text()


def run(coro):
    return asyncio.run(coro)


def test_the_group_page_lists_the_last_five_jobs_and_points_at_audit_and_logs(demo):  # noqa: F811
    jobs: Jobs = demo.app.state.jobs
    for _ in range(7):
        run(jobs.create("deploy", "demo/prod"))
    page = demo.get("/groups/demo").text
    assert len(re.findall(r'id="job-', page)) == 5
    assert len(run(jobs.recent("demo/"))) >= 7  # the API still answers more
    note = page[page.index("<h2>Deploy jobs</h2>") : page.index("<h2>Drift</h2>")]
    assert "last 5" in note and 'href="/audit' in note and 'href="/logs?group=demo' in note


def test_cards_never_scroll_sideways_and_long_cells_wrap():
    assert not re.search(r"\.card\{[^}]*overflow-x:auto", CSS)
    assert not re.search(r"td \.row-actions[^{]*\{[^}]*flex-wrap:nowrap", CSS)
    assert "td code{white-space:nowrap}" not in CSS
    assert re.search(r"\btd\{[^}]*overflow-wrap:anywhere", CSS)
    assert re.search(r"\bpre\{[^}]*white-space:pre-wrap", CSS)
    assert ".env-table .zone-row td:nth-child(2){min-width:400px}" not in CSS


def test_checkbox_dropdown_options_never_wrap_inside_a_wrapping_cell():
    """The user's screenshot (Tool access): one letter per line once cells wrap. The panel sizes to its longest
    option and every option stays on one line; the cell rule for wrapping must not reach into it."""
    assert re.search(r"details\.multi>\.options\{[^}]*white-space:nowrap", CSS)
    assert re.search(r"details\.multi>\.options\{[^}]*overflow-wrap:normal", CSS)
    assert re.search(r"details\.multi>\.options\{[^}]*width:max-content", CSS)
    assert re.search(r"\.option\{[^}]*white-space:nowrap", CSS)


def test_the_stylesheet_url_changes_with_its_content(demo):  # noqa: F811
    """The user kept seeing the old dropdown after a redeploy: every build of one version linked ramen.css?v=1.0.0,
    so the browser kept its cached sheet. The link now carries a hash of the file."""
    import hashlib

    digest = hashlib.sha256(CSS.encode()).hexdigest()[:10]
    assert "/static/ramen.css?v=" in demo.get("/login").text and f'-{digest}"' in demo.get("/login").text
