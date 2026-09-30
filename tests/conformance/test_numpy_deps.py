"""v0.5.5 I13: a group's mcp/requirements.txt reaches the worker's real `pip install`, not just the
stdlib-only path every other fixture exercises. Real ramen-node + Python sidecar, real pip, real numpy import
at call time — the fixture's tool fails outright if the package didn't actually land on sys.path."""

import json
import shutil

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode

pytestmark = pytest.mark.conformance


def test_requirements_txt_pip_installs_numpy_and_the_tool_runs(tmp_path):
    bucket = tmp_path / "bucket"
    shutil.copytree(E.FIXTURES / "numpy_group", bucket)
    with LocalNode(bucket=bucket) as n:
        n.wait_serving()
        assert "numpy_stats_tool" in {t["name"] for t in n.node.list_tools()}
        r = n.node.call_tool("numpy_stats_tool", {"values": "1,2,3,4,5"})
        assert r["isError"] is False, r
        result = json.loads(r["content"][0]["text"])
        assert result["mean"] == 3.0
        assert result["numpy_version"]  # real numpy, not a stub
