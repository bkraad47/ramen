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


def test_console_may_ask_which_web_acl_guards_the_alb():
    """0.6.1 AWS run: IP rules answered 502 'not authorized to perform wafv2:GetWebACLForResource on resource
    arn:aws:wafv2:<region>:<account>:regional/webacl'. The request names no web ACL, so IAM checks the bare
    `regional/webacl` ARN, which the scoped `regional/webacl/ramen/*` never matches. It is read-only: `*`."""
    import re

    tf = (ROOT / "deploy/terraform/aws/iam.tf").read_text()
    stmts = re.findall(r"statement \{[^{}]*\}", tf)
    get = [s for s in stmts if "wafv2:GetWebACLForResource" in s]
    assert get and all('resources = ["*"]' in s for s in get), get
    assert any("wafv2:AssociateWebACL" in s and "local.waf_web_acl" in s for s in stmts)  # writes stay scoped
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    console = cf[cf.index("ConsoleRole:") :]
    block = re.search(r"Action: \[[^\]]*wafv2:GetWebACLForResource[^\]]*\]\n\s*Resource: (.*)", console)
    assert block and block.group(1).strip() == '"*"', block and block.group(0)


def _fake_aws(tmp_path, enis: str):
    """A fake `aws`: `describe-network-interfaces` prints `enis` (id<TAB>status<TAB>description lines), deletes log."""
    log = tmp_path / "aws-calls"
    aws = tmp_path / "bin" / "aws"
    aws.parent.mkdir(exist_ok=True)
    data = tmp_path / "enis"
    data.write_text(enis)
    aws.write_text(
        f'#!/bin/sh\necho "$*" >> {log}\ncase "$*" in\n'
        f"  *describe-network-interfaces*|*describe-security-groups*) cat {data} ;;\nesac\n"
    )
    aws.chmod(0o755)
    return log


def test_eni_sweep_deletes_only_detached_cni_interfaces(tmp_path):
    """0.7.5 AWS teardown: a terminated node's VPC CNI interface stayed `available` and blocked the subnet delete."""
    log = _fake_aws(
        tmp_path,
        "eni-cni\tavailable\taws-K8S-i-0d99\neni-alb\tavailable\tELB app/k8s-ramen\neni-busy\tin-use\taws-K8S-i-0aa\n",
    )
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "RAMEN_ENI_WAIT_TRIES": "1"}
    r = subprocess.run(
        ["bash", str(ROOT / "deploy/terraform/aws/scripts/eni_sweep.sh"), "vpc-1", "us-east-1"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "vpc-id,Values=vpc-1" in calls and "--region us-east-1" in calls
    assert "--network-interface-id eni-cni" in calls
    assert "id eni-alb" not in calls  # load balancer interfaces belong to AWS, not to the sweep
    assert "id eni-busy" not in calls


def test_terraform_destroy_sweeps_enis_between_node_group_and_subnets():
    tf = (ROOT / "deploy/terraform/aws/main.tf").read_text()
    assert 'resource "terraform_data" "eni_sweep"' in tf
    block = tf.split('resource "terraform_data" "eni_sweep"')[1].split("\nresource ")[0]
    assert "when       = destroy" in block or "when = destroy" in block
    assert "scripts/eni_sweep.sh" in block and "aws_subnet.public" in block
    node = tf.split('resource "aws_eks_node_group" "ramen"')[1].split("\nresource ")[0]
    assert "terraform_data.eni_sweep" in node  # the node group goes first, then the sweep, then the subnets


def test_sweep_deletes_the_leftover_eks_cluster_security_group(tmp_path):
    """0.7.5: EKS left `eks-cluster-sg-<name>-<n>` behind after the cluster was gone; it blocks deleting the VPC."""
    log = _fake_aws(tmp_path, "")
    sgs = tmp_path / "enis"
    sgs.write_text("sg-eks\teks-cluster-sg-ramen-1082\nsg-def\tdefault\nsg-other\teks-cluster-sg-other-1\n")
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "RAMEN_ENI_WAIT_TRIES": "1"}
    script = ROOT / "deploy/terraform/aws/scripts/eni_sweep.sh"
    r = subprocess.run(
        ["bash", str(script), "vpc-1", "us-east-1", "cluster-sg", "ramen"], env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "describe-security-groups" in calls and "--group-id sg-eks" in calls
    assert "sg-def" not in calls and "sg-other" not in calls


def test_terraform_destroy_sweeps_the_cluster_sg_between_cluster_and_vpc():
    tf = (ROOT / "deploy/terraform/aws/main.tf").read_text()
    block = tf.split('resource "terraform_data" "sg_sweep"')[1].split("\nresource ")[0]
    assert "when       = destroy" in block and "cluster-sg" in block and "aws_vpc.ramen" in block
    cluster = tf.split('resource "aws_eks_cluster" "ramen"')[1].split("\nresource ")[0]
    assert "terraform_data.sg_sweep" in cluster
