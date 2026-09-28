"""Logs (CONTRACTS §4a/§7): /api/v1/logs returns worker log lines for group/zone (one JSON line per call), supports
tail/worker filters and download. Needs RAMEN_CONSOLE_URL (+RAMEN_NODE_URL to generate traffic)."""

import json

import pytest

from .conftest import GROUP, PING, ZONE, ok, poll

pytestmark = pytest.mark.cloud


def _lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"raw": line})
    return out


@pytest.fixture(scope="module")
def traffic(node_grpc, world, mcp_key_opt):
    for _ in range(3):
        node_grpc.status(PING, key=mcp_key_opt or "nope")
    return 3


def test_logs_endpoint_returns_worker_lines(admin, traffic):
    def fetch():
        r = admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 200})
        assert r.status_code == 200, r.text[:200]
        assert r.headers.get("content-type", "").startswith("text/plain")
        return r.text if r.text.strip() else None

    text = poll(fetch, timeout=60, what="worker log lines visible in console")
    lines = _lines(text)
    assert any("ping" in json.dumps(x) or "status" in x for x in lines), text[-500:]


def test_logs_tail_limits_lines(admin, traffic):
    r = ok(admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 2}))
    assert len([x for x in r.text.splitlines() if x.strip()]) <= 2


def test_logs_download_is_attachment(admin, traffic):
    r = ok(admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 50, "download": 1}))
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and f"{GROUP}-{ZONE}" in cd, cd


def test_logs_per_worker(admin, traffic):
    live = ok(admin.get("workers", group=GROUP, zone=ZONE)).json()["live"]
    assert live
    r = admin.get("logs", params={"group": GROUP, "zone": ZONE, "worker": live[0]["id"], "tail": 20})
    assert r.status_code == 200


def test_logs_never_contain_key_values(admin, traffic, mcp_key_opt):
    if not mcp_key_opt:
        pytest.skip("no MCP key to check against")
    r = ok(admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 500}))
    assert mcp_key_opt not in r.text


def test_logs_require_group_access(console_url, world):
    from ramen_tests.console import Console

    with Console(console_url) as c:
        assert c.get("logs", params={"group": GROUP, "zone": ZONE}).status_code == 401
