"""scripts/check_versions.py (CONTRACTS §6), 0.7.0: the demo group repo's VERSION joins the table when that repo sits
beside `ramen` (ramen-master checks it out as `../ramen-demo-mcp`); a mismatch fails like any other row."""

import shutil
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


def test_the_runtime_python_pin_and_the_image_bases_must_agree(tmp_path):
    """§21.1 (D45): one Python minor for the runtime, and every image that installs it builds on that minor."""
    root = tmp_path / "ramen"
    skip = shutil.ignore_patterns(".git", ".venv", "target", "site", "__pycache__", "node_modules", ".demo")
    shutil.copytree(E.RAMEN_DIR, root, ignore=skip)
    ok = run("--root", str(root))
    assert ok.returncode == 0 and "runtime-py python" in ok.stdout and "glama/Dockerfile python" in ok.stdout, ok.stdout
    dockerfile = root / "glama" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace("FROM python:3.12", "FROM python:3.14"))
    r = run("--root", str(root))
    assert r.returncode == 1 and "glama/Dockerfile: FROM python:3.14 but the runtime is pinned to 3.12" in r.stderr
    py = root / "runtime-py" / "pyproject.toml"
    py.write_text(py.read_text().replace('">=3.12,<3.13"', '">=3.12"'))
    r = run("--root", str(root))
    assert r.returncode == 1 and "must pin one minor" in r.stderr, r.stderr
