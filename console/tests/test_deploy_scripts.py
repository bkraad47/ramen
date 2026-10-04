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


def test_cloud_smoke_deploy_poll_rides_out_a_non_json_answer(tmp_path):
    """0.6.1 GKE run: one poll of the deploy job got a non-JSON answer through the load balancer and the smoke
    failed with an empty error while the job went on to succeed."""
    n = tmp_path / "polls"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir()
    curl.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done\n'
        'case "$url" in\n'
        "  */login) echo 303 ;;\n"
        '  */mcp-keys) echo \'{"key":"rmk_x"}\' ;;\n'
        '  */deploy) echo \'{"id":"j1"}\' ;;\n'
        f"  */jobs/j1) echo x >> {n}; c=$(wc -l < {n});\n"
        "     if [ $c -eq 1 ]; then echo '<html>502</html>'; elif [ $c -eq 2 ]; then echo '{\"status\":\"running\"}';\n"
        '     else echo \'{"status":"ok"}\'; fi ;;\n'
        '  */workers) echo \'{"live":[{"load":0}]}\' ;;\n'
        "  *) echo '{}' ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)
    e = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "RAMEN_SMOKE_POLL": "0"}
    e.pop("RAMEN_NODE_URL", None)
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "cloud_smoke.sh"), "https://c", "a@b", "pw", "k"],
        env=e,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "PASS (console only" in r.stdout


def test_eks_default_version_is_in_standard_support():
    """0.6.1 AWS run: the default 1.31 had left EKS standard support (2025-11-26), so every new cluster paid the
    extended-support control plane ($0.60/h, not the $0.10/h the guide quotes). Terraform and CloudFormation agree."""
    import re

    tf = (ROOT / "deploy/terraform/aws/variables.tf").read_text()
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    v_tf = re.search(r'variable "kubernetes_version" \{[^}]*default\s*=\s*"([\d.]+)"', tf).group(1)
    v_cf = re.search(r'KubernetesVersion:[^\n]*\n(?:[^\n]*\n)*?\s*Default:\s*"([\d.]+)"', cf).group(1)
    assert v_tf == v_cf
    assert tuple(map(int, v_tf.split("."))) >= (1, 35), "standard support of 1.34 ends 2026-12-02"


def test_eks_default_node_group_holds_a_zone_with_its_canary():
    """0.6.1 AWS run: the default two t3.small nodes land one per availability zone, and zone a's node also carries
    the console and CoreDNS, so the guide's first canary deploy stayed Unschedulable ("1 Insufficient memory").
    Workers are pinned to their AZ: two nodes per AZ (four over the two subnets) is the smallest default that runs
    the guide."""
    import re

    tf = (ROOT / "deploy/terraform/aws/variables.tf").read_text()
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()

    def tf_default(name):
        return int(re.search(rf'variable "{name}" \{{[^}}]*default\s*=\s*(\d+)', tf).group(1))

    def cf_default(name):
        return int(re.search(rf"\n  {name}:\n(?:    [^\n]*\n)*?    Default: \"?(\d+)", cf).group(1))

    assert tf_default("node_count") == cf_default("NodeCount") >= 4
    assert tf_default("node_max") == cf_default("NodeMax") > tf_default("node_count")
