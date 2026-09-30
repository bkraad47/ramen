"""v0.5.5 I9: cloud log fetchers must hand logview.parse_log valid JSON lines, not `ts severity pod {json}`
text that breaks json.loads and renders every row as "-" for consumer/ts (GCP and AWS shared the same bug).
Also: the local adapter's "All workers" view must not silently look for a literal "worker.log" that only
one hand-written docker-compose demo happens to use."""

import datetime
import json

from ramen_console.cloud import aws_api, gcp_api
from ramen_console.logview import parse_log

WORKER_LINE = json.dumps(
    {"ts": "2026-09-30T10:00:00Z", "status": "ok", "key_id": "k1", "method": "tools/call", "name": "calc"}
)


class _Resource:
    def __init__(self, pod):
        self.labels = {"pod_name": pod}


class _Entry:
    def __init__(self, payload, pod="worker-abc"):
        self.payload = payload
        self.resource = _Resource(pod)
        self.timestamp = datetime.datetime(2026, 9, 30, 10, 0, 0, tzinfo=datetime.UTC)
        self.severity = "INFO"


class FakeGcpLogging:
    def __init__(self, entries):
        self._entries = entries

    def list_entries(self, filter_=None, order_by=None, max_results=None, page_size=None):
        return self._entries


def test_gcp_fetch_logs_produces_lines_parse_log_can_read():
    text = gcp_api.fetch_logs(FakeGcpLogging([_Entry(WORKER_LINE)]), "ns", None, 100)
    entry = parse_log(text)[0]
    assert entry["consumer"] == "k1" and entry["outcome"] == "success" and entry["ts"]


def test_gcp_fetch_logs_keeps_non_json_lines_readable_not_corrupted():
    text = gcp_api.fetch_logs(FakeGcpLogging([_Entry("worker starting up")]), "ns", None, 100)
    assert "worker starting up" in text


class FakeCloudWatchLogs:
    def __init__(self, rows):
        self._rows = rows

    def start_query(self, **kw):
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        return {"status": "Complete", "results": self._rows}


def test_aws_fetch_logs_produces_lines_parse_log_can_read():
    rows = [[{"field": "@timestamp", "value": "2026-09-30 10:00:00.000"}, {"field": "log", "value": WORKER_LINE}]]
    text = aws_api.fetch_logs(FakeCloudWatchLogs(rows), "lg", "ns", None, 100, poll=0, timeout=1)
    entry = parse_log(text)[0]
    assert entry["consumer"] == "k1" and entry["outcome"] == "success" and entry["ts"]
