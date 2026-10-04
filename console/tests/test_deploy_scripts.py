"""Deploy helper scripts, driven with a fake `gcloud` on PATH (0.6.1 cloud run findings)."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fake_gcloud(tmp_path, state):
    log = tmp_path / "calls"
    gc = tmp_path / "bin" / "gcloud"
    gc.parent.mkdir()
    gc.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {log}\n'
        'case "$*" in\n'
        f'  "projects describe"*) [ -n "{state}" ] || exit 1; echo "{state}" ;;\n'
        "esac\n"
    )
    gc.chmod(0o755)
    return log


def _run(tmp_path, *args, **env):
    e = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **env}
    e["RAMEN_GCP_PROJECT_FILE"] = str(tmp_path / "state")
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "gcp_test_project.sh"), *args], env=e, capture_output=True, text=True
    )


def test_create_refuses_a_project_pending_deletion(tmp_path):
    log = _fake_gcloud(tmp_path, "DELETE_REQUESTED")
    r = _run(tmp_path, "create", RAMEN_GCP_PROJECT="ramen-test-x", RAMEN_BILLING_ACCOUNT="b")
    assert r.returncode != 0
    assert "DELETE_REQUESTED" in r.stderr and "RAMEN_GCP_PROJECT" in r.stderr
    assert "billing" not in log.read_text()


def test_create_reuses_an_active_project_and_creates_a_missing_one(tmp_path):
    log = _fake_gcloud(tmp_path, "ACTIVE")
    r = _run(tmp_path, "create", RAMEN_GCP_PROJECT="ramen-test-x", RAMEN_BILLING_ACCOUNT="b")
    assert r.returncode == 0, r.stderr
    assert "projects create" not in log.read_text() and "billing projects link" in log.read_text()

    (tmp_path / "bin" / "gcloud").unlink()
    (tmp_path / "bin").rmdir()
    log = _fake_gcloud(tmp_path, "")
    r = _run(tmp_path, "create", RAMEN_GCP_PROJECT="ramen-test-y", RAMEN_BILLING_ACCOUNT="b")
    assert r.returncode == 0, r.stderr
    assert "projects create ramen-test-y" in log.read_text()


def test_cloud_smoke_login_survives_a_base64_password(tmp_path):
    """The deploy guides make the admin password with `openssl rand -base64`; a `+` posted with plain `-d` arrives
    as a space and the smoke run fails at login."""
    calls = tmp_path / "curl-args"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir()
    curl.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {calls}\necho 401\n')
    curl.chmod(0o755)
    e = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "cloud_smoke.sh"), "https://c", "a@b", "p+w/=", "k"],
        env=e,
        capture_output=True,
        text=True,
    )
    assert "FAIL: login" in r.stderr
    args = calls.read_text().splitlines()
    assert "password=p+w/=" in args and args[args.index("password=p+w/=") - 1] == "--data-urlencode"
    assert "email=a@b" in args and args[args.index("email=a@b") - 1] == "--data-urlencode"
