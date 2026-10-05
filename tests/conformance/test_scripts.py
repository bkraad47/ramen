"""scripts/check_versions.py (CONTRACTS §6), 0.7.0: the demo group repo's VERSION joins the table when that repo sits
beside `ramen` (ramen-master checks it out as `../ramen-demo-mcp`); a mismatch fails like any other row."""

import subprocess
import sys

import pytest

from ramen_tests import env as E

SCRIPT = E.RAMEN_DIR / "scripts" / "check_versions.py"
ROW = "../ramen-demo-mcp/VERSION"
pytestmark = pytest.mark.conformance


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_demo_repo_version_is_a_row_that_fails_on_mismatch(tmp_path):
    version = (E.RAMEN_DIR / "VERSION").read_text().strip()
    base = run("--demo", str(tmp_path / "absent" / "VERSION"))
    assert ROW not in base.stdout, "no demo checkout → no row (CI of the public repo has none)"
    same = tmp_path / "same"
    same.write_text(version + "\n")
    r = run("--demo", str(same))
    assert r.returncode == base.returncode and f"{ROW}" in r.stdout and version in r.stdout
    other = tmp_path / "other"
    other.write_text("9.9.9\n")
    r = run("--demo", str(other))
    assert r.returncode == 1 and f"{ROW}: 9.9.9 != {version}" in r.stderr, r.stderr
