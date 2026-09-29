"""U4 / CONTRACTS §12.1: the two-pane Logs page."""

import json
import re
from pathlib import Path

import pytest

from ramen_console.keyid import key_id
from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - pytest fixtures


def line(i, *, status="ok", kid=None, method="tools/call", name="calc"):
    return json.dumps(
        {
            "ts": f"2026-09-29T00:00:{i:02d}Z",
            "level": "info",
            "msg": "mcp",
            "ip": "10.0.0.1",
            "group": "demo",
            "zone": "zone-a",
            "method": method,
            "name": name,
            "status": status,
            "grpc_code": "OK" if status == "ok" else "UNAUTHENTICATED",
            "ms": 2,
            "key_id": kid,
        }
    )


@pytest.fixture
def logfile(demo, tmp_path):
    def write(lines):
        p = tmp_path / "logs" / "demo" / "zone-a" / "worker.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n")
        return p

    return write


@pytest.fixture
def mcp_key(demo):
    r = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "llm-agent"})
    assert r.status_code == 201, r.text
    return r.json()["key"]


def page(demo, **q):
    qs = "&".join(f"{k}={v}" for k, v in {"group": "demo", "zone": "zone-a", **q}.items())
    r = demo.get(f"/logs?{qs}")
    assert r.status_code == 200, r.text
    return r.text


def test_two_panes(demo, logfile):
    logfile([line(1)])
    html = page(demo)
    assert "log-panes" in html and "log-list" in html and 'class="log-body"' in html


def test_the_newest_entry_is_first_and_selected(demo, logfile):
    logfile([line(i) for i in range(1, 4)])
    html = page(demo)
    rows = re.findall(r'<button class="log-row([^"]*)"[^>]*>(.*?)</button>', html, re.S)
    assert "00:00:03" in rows[0][1]
    assert "selected" in rows[0][0]
    assert all("selected" not in cls for cls, _ in rows[1:])


def test_the_body_pane_shows_the_selected_entry(demo, logfile):
    logfile([line(1), line(2, name="other_tool")])
    html = page(demo)
    body = re.search(r'<div class="log-body">(.*?)</div>', html, re.S).group(1)
    assert "other_tool" in body


def test_the_newest_fifteen_are_eager_and_the_rest_are_scrollable(demo, logfile):
    logfile([line(i) for i in range(1, 26)])
    html = page(demo)
    eager = re.search(r'<div class="log-eager">(.*?)</div>\s*<div class="log-rest"', html, re.S).group(1)
    rest = re.search(r'<div class="log-rest">(.*?)</div>', html, re.S).group(1)
    assert eager.count("log-row") == 15
    assert rest.count("log-row") == 10
    css = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/static/ramen.css").read_text()
    assert re.search(r"\.log-rest\{[^}]*overflow-y:auto", css)


def test_without_enough_entries_there_is_no_scrollable_frame(demo, logfile):
    logfile([line(i) for i in range(1, 4)])
    assert 'class="log-rest"' not in page(demo)


def test_every_row_shows_consumer_timestamp_method_and_outcome(demo, logfile, mcp_key):
    logfile([line(1, kid=key_id(mcp_key))])
    row = re.search(r'<button class="log-row[^"]*"[^>]*>(.*?)</button>', page(demo), re.S).group(1)
    assert "llm-agent" in row  # key_id resolved to the key's name
    assert "2026-09-29T00:00:01Z" in row
    assert "tools/call" in row and "calc" in row
    assert "Success" in row


def test_a_failed_call_reads_as_failure(demo, logfile):
    logfile([line(1, status="denied")])
    row = re.search(r'<button class="log-row[^"]*"[^>]*>(.*?)</button>', page(demo), re.S).group(1)
    assert "Failure" in row and "Success" not in row


def test_an_unknown_key_id_shows_the_id_itself(demo, logfile):
    logfile([line(1, kid="deadbeef")])
    assert "deadbeef" in page(demo)


def test_a_call_with_no_key_reads_as_anonymous(demo, logfile):
    logfile([line(1, kid=None)])
    assert "anonymous" in page(demo).lower()


def test_the_worker_selector_lists_the_live_workers_and_defaults_to_all(demo, logfile):
    logfile([line(1)])
    html = page(demo)
    selector = re.search(r'<select name="worker">(.*?)</select>', html, re.S).group(1)
    assert '<option value="">All workers</option>' in selector
    assert "worker" in selector


def test_choosing_a_worker_narrows_the_query(demo, logfile, tmp_path):
    logfile([line(1, name="from_all")])
    p = tmp_path / "logs" / "demo" / "zone-a" / "only-one.log"
    p.write_text(line(2, name="from_one") + "\n")
    html = page(demo, worker="only-one")
    assert "from_one" in html and "from_all" not in html
    assert '<option value="only-one" selected>' in html


def test_download_still_works(demo, logfile):
    logfile([line(1)])
    assert "download=1" in page(demo)
    r = demo.get("/api/v1/logs?group=demo&zone=zone-a&download=1")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]


def test_an_empty_log_says_so_without_breaking(demo):
    html = page(demo)
    assert "No log entries" in html
    assert 'class="log-row' not in html


def test_a_viewer_of_another_group_sees_nothing(demo, app):  # noqa: F811
    from fastapi.testclient import TestClient

    from tests.test_api import PW, make_user

    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(app) as o:
        login(o, "o@x", PW)
        assert 'class="log-row' not in o.get("/logs?group=demo&zone=zone-a").text
