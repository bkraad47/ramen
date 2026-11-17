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
        '  */zones/a/groups) echo \'{"groups":["demo"]}\' ;;\n'  # §26.1: the smoke binds the zone first
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


def test_cloudformation_mirrors_per_group_builds():
    """D85 F2: the template shipped the shared ramen-builder (push on ramen/worker) after Terraform dropped it."""
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    assert "BuilderRole" not in cf and "ramen-builder" not in cf
    assert "ManagedPolicyName: ramen-build-boundary" in cf
    assert "repository/ramen/groups/${!aws:PrincipalTag/ramen-build}/*" in cf
    assert '"iam:PermissionsBoundary": [!Ref WorkerBoundary, !Ref BuildBoundary]' in cf
    assert "repository/ramen/groups/*" in cf[cf.index("ConsoleRole:") : cf.index("BuildBoundary:")]
    assert "Condition: WithConsoleBedrock" in cf and "bedrock:CallWithBearerToken" in cf


def test_cloudformation_mirrors_evals_identity_and_tag_keyed_worker_boundary():
    """§33.4a drift: the template had no ramen-evals role or ramen/skillspector repository, and its worker boundary
    let any worker read every group's bucket prefix and secrets (Terraform keys them on the role's `group` tag)."""
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    assert "RepositoryName: ramen/skillspector" in cf
    evals = cf[cf.index("  EvalsRole:") : cf.index("  # --- AWS Load Balancer Controller")]
    assert "system:serviceaccount:ramen-system:ramen-evals" in evals
    assert "Action: [logs:CreateLogStream, logs:PutLogEvents]" in evals
    assert not any(x in evals for x in ("ecr:", "s3:", "secretsmanager:"))
    assert "Value: !GetAtt EvalsRole.Arn" in cf
    boundary = cf[cf.index("  WorkerBoundary:") : cf.index("  ConsoleRole:")]
    assert '"s3:prefix": ["${aws:PrincipalTag/group}/*", "${aws:PrincipalTag/group}/"]' in boundary
    assert '"${GroupsBucket.Arn}/${!aws:PrincipalTag/group}/*"' in boundary
    assert "secret:ramen/${!aws:PrincipalTag/group}/*" in boundary
    assert '"${GroupsBucket.Arn}/*"' not in boundary and "secret:ramen/*" not in boundary


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


def _block(text, head):
    return text.split(head)[1].split("\nresource ")[0]


def test_eks_runs_the_vpc_cni_network_policy_agent():
    """S6b: the chart and console write NetworkPolicies, but EKS enforces them only with the VPC CNI's network-policy
    agent on; home, satellite and the CloudFormation template all install the managed addon with it, before nodes."""
    import re

    for d, name in (("aws", "ramen"), ("aws-satellite", "satellite")):
        tf = (ROOT / f"deploy/terraform/{d}/main.tf").read_text()
        addon = _block(tf, 'resource "aws_eks_addon" "vpc_cni"')
        assert f"aws_eks_cluster.{name}.name" in addon and 'addon_name                  = "vpc-cni"' in addon, d
        assert re.search(r"configuration_values\s*=\s*jsonencode\(\{\s*enableNetworkPolicy\s*=\s*\"true\"", addon), d
        assert 'resolve_conflicts_on_create = "OVERWRITE"' in addon, d  # adopts the self-managed aws-node
        assert "aws_eks_addon.vpc_cni" in _block(tf, f'resource "aws_eks_node_group" "{name}"'), d
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    addon = cf[cf.index("  VpcCni:") : cf.index("  NodeRole:")]
    assert "Type: AWS::EKS::Addon" in addon and "AddonName: vpc-cni" in addon and "ClusterName: !Ref Cluster" in addon
    assert '{"enableNetworkPolicy":"true",' in addon
    nodes = cf[cf.index("  NodeGroup:") :].split("\n  OidcProvider:")[0]
    assert "DependsOn: VpcCni" in nodes


def test_eks_pods_take_their_own_subnets_via_custom_networking():
    """D89: nodes, ALB ENIs and pods shared the public /24s, so an ipBlock admitting the ALB admitted every pod.
    Pods now take addresses from a private /20 per AZ (VPC CNI custom networking, ENIConfig per AZ from the addon);
    nodes and the ALB stay public; no NAT (pod egress leaves SNATed to the node's address)."""
    import ipaddress

    vpc = ipaddress.ip_network("10.42.0.0/16")
    pods = [str(n) for n in list(vpc.subnets(new_prefix=20))[4:6]]
    assert pods == ["10.42.64.0/20", "10.42.80.0/20"]  # what cidrsubnet(vpc, 4, 4 + i) yields; clear of the /24s
    for d, name in (("aws", "ramen"), ("aws-satellite", "satellite")):
        tf = (ROOT / f"deploy/terraform/{d}/main.tf").read_text()
        sub = _block(tf, 'resource "aws_subnet" "pods"')
        assert "count                   = 2" in sub and "cidrsubnet(var.vpc_cidr, 4, 4 + count.index)" in sub, d
        assert "map_public_ip_on_launch = false" in sub and "kubernetes.io/role/elb" not in sub, d  # ALB never there
        assert "route_table_id = aws_route_table.public.id" in _block(
            tf, 'resource "aws_route_table_association" "pods"'
        )
        assert "aws_nat_gateway" not in tf, d
        addon = _block(tf, 'resource "aws_eks_addon" "vpc_cni"')
        for s in (
            'AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = "true"',
            'ENI_CONFIG_LABEL_DEF               = "topology.kubernetes.io/zone"',
            "eniConfig = {",
            "create  = true",
            "aws_subnet.pods",
            f"aws_eks_cluster.{name}.vpc_config[0].cluster_security_group_id",
        ):
            assert s in addon, (d, s)
        ng = _block(tf, f'resource "aws_eks_node_group" "{name}"')
        assert "subnet_ids      = aws_subnet.public[*].id" in ng, d  # nodes stay public
        assert "-podnet" in ng and "create_before_destroy = true" in ng, d  # replaced once, new nodes first
        assert "aws_eks_addon.vpc_cni" in ng, d  # nodes launch after custom networking and the ENIConfigs
        assert "aws_subnet.pods" in _block(tf, 'resource "terraform_data" "eni_sweep"'), d  # pod ENIs swept first
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    for i, s in enumerate("AB"):
        sub = _cfn(cf, f"PodSubnet{s}")
        assert f"!Select [{4 + i}, !Cidr [!Ref VpcCidr, 6, 12]]" in sub and "MapPublicIpOnLaunch: false" in sub
        assert "kubernetes.io/role/elb" not in sub
        assert f"SubnetId: !Ref PodSubnet{s}, RouteTableId: !Ref RouteTable" in _cfn(cf, f"PodSubnet{s}Route")
    addon = cf[cf.index("  VpcCni:") : cf.index("  NodeRole:")]
    for s in (
        '"AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG":"true"',
        '"ENI_CONFIG_LABEL_DEF":"topology.kubernetes.io/zone"',
        '"eniConfig":{"create":true',
        '"id":"${PodSubnetA}"',
        '"id":"${PodSubnetB}"',
        "${Cluster.ClusterSecurityGroupId}",
    ):
        assert s in addon, s
    nodes = cf[cf.index("  NodeGroup:") :].split("\n  OidcProvider:")[0]
    assert 'NodegroupName: !Sub "${ClusterName}-podnet"' in nodes and "Subnets: [!Ref SubnetA, !Ref SubnetB]" in nodes
    assert "NatGateway" not in cf


def _cfn(text, name):
    """One CloudFormation resource: from `  <name>:` to the next resource at the same indent."""
    import re

    rest = text[text.index(f"\n  {name}:\n") + 1 :]
    return rest[: re.search(r"\n  [A-Za-z0-9]+:\n", rest).start()]


def test_aks_runs_a_network_policy_engine_on_home_and_satellite():
    """S6b: AKS left `network_policy` unset, so the policies were written and not enforced. Calico is the engine both
    CNI modes take (home: Azure CNI overlay; satellite: Azure CNI on the node subnet, where azurerm refuses Cilium
    without a pod subnet); the comment warns that setting it on a live cluster may replace it."""
    for d, name in (("azure", "ramen"), ("azure-satellite", "satellite")):
        tf = (ROOT / f"deploy/terraform/{d}/main.tf").read_text()
        profile = _block(tf, f'resource "azurerm_kubernetes_cluster" "{name}"').split("network_profile {")[1]
        profile = profile.split("}")[0]
        assert 'network_policy      = "calico"' in profile or 'network_policy    = "calico"' in profile, d
        assert 'network_plugin      = "azure"' in profile or 'network_plugin    = "azure"' in profile, d
        assert "replace" in tf.split("network_profile {")[0][-600:] + profile, d  # the upgrade warning


def test_aws_terraform_and_cloudformation_hand_the_alb_subnets_to_the_chart():
    """The ALB reaches pods from its public subnets. D89: the flag (`network_set` / `NetworkSet`) carries those
    subnets, not the VPC (which now also holds the pod subnets); a satellite emits its own for the home console."""
    tf = (ROOT / "deploy/terraform/aws/outputs.tf").read_text()
    assert 'output "network_set"' in tf and "awsVpcCidr" not in tf
    assert 'networkPolicy.awsAlbCidrs={${join(",", aws_subnet.public[*].cidr_block)}}' in tf
    assert 'output "alb_cidrs" { value = join(",", aws_subnet.public[*].cidr_block) }' in tf
    sat = (ROOT / "deploy/terraform/aws-satellite/outputs.tf").read_text()
    assert 'output "alb_cidrs" { value = join(",", aws_subnet.public[*].cidr_block) }' in sat
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    out = cf[cf.index("\nOutputs:") :]
    assert "  NetworkSet:" in out and "awsVpcCidr" not in out
    assert "--set networkPolicy.awsAlbCidrs={${SubnetA.CidrBlock},${SubnetB.CidrBlock}}" in out


def _fake_demo_tools(tmp_path, policy: str, job: str = '{"status":"ok"}', extra_env: dict | None = None):
    """`curl` answering the compose console and worker, `uv` standing in for runtime-py (it records what mcp_call.py
    would get), for `deploy/local/demo.sh`."""
    calls, meta = tmp_path / "curl-calls", tmp_path / "meta"
    b = tmp_path / "bin"
    b.mkdir()
    (b / "curl").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done\n'
        f'echo "$url $*" >> {calls}\n'
        'case "$url" in\n'
        "  */login) echo 303 ;;\n"
        f"  *ramen-policy*) echo '{policy}' ;;\n"
        '  */configs/accept) echo \'{"version":"v1"}\' ;;\n'
        '  */mcp-keys) echo \'{"key":"rmk_x"}\' ;;\n'
        '  */deploy) echo \'{"id":"j1"}\' ;;\n'
        f"  */jobs/j1) echo '{job}' ;;\n"
        "  */mcp) echo demo_calculator_tool ;;\n"
        "  *) echo '{}' ;;\n"
        "esac\n"
    )
    (b / "uv").write_text(
        f'#!/bin/sh\n[ "$1" = run ] || exit 0\nprintf "%s" "$RAMEN_MCP_META" > {meta}\n'
        "echo 'demo_calculator_tool({}) -> 5'\n"
    )
    for f in ("curl", "uv"):
        (b / f).chmod(0o755)
    e = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "RAMEN_ADMIN_PASSWORD": "pw"}
    e["RAMEN_DEMO_COOKIES"] = str(tmp_path / "cookies")
    e.update(extra_env or {})
    (tmp_path / "cookies").write_text("")
    r = subprocess.run(
        ["bash", str(ROOT / "deploy" / "local" / "demo.sh")], env=e, capture_output=True, text=True, timeout=60
    )
    return r, calls.read_text().splitlines(), meta


def test_demo_accepts_the_llm_configs_and_names_the_policy_versions_on_its_call(tmp_path):
    """§31.1/§31.2: with the demo repo's `mcp/llm-config/` a key is minted only after its configs are accepted, and a
    bare call without the versions is -32028."""
    import json

    policy = json.dumps(
        {
            "telemetry": {"version": "t0", "required": False},
            "evals": {"version": "e1", "required": True},
            "configs": {"version": "c1", "required": True},
        }
    )
    r, calls, meta = _fake_demo_tools(tmp_path, policy)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "PASS: demo_calculator_tool(2,3,add) -> 5" in r.stdout
    urls = [c.split(" ", 1)[0] for c in calls]
    accept = next(i for i, u in enumerate(urls) if u.endswith("/groups/demo/configs/accept"))
    assert '{"version":"c1"}' in calls[accept] or '{"version": "c1"}' in calls[accept]
    assert accept < next(i for i, u in enumerate(urls) if u.endswith("/mcp-keys"))
    assert json.loads(meta.read_text()) == {"io.ramen/evals": {"version": "e1"}, "io.ramen/configs": {"version": "c1"}}


def test_demo_still_runs_against_a_repo_without_llm_configs(tmp_path):
    r, calls, meta = _fake_demo_tools(tmp_path, '{"telemetry": {"version": "t0", "required": false}, "evals": null}')
    assert r.returncode == 0, r.stderr + r.stdout
    assert not [c for c in calls if "/configs/accept" in c.split(" ", 1)[0]]
    assert meta.read_text() in ("", "{}")


def test_demo_deploys_the_ref_it_is_given(tmp_path):
    """get-started: `RAMEN_DEMO_REF=v0.8.0 make demo` is the key-free try-out (the 1.0.0 demo's LLM rails need a
    provider on Config → LLM providers)."""
    r, calls, _ = _fake_demo_tools(tmp_path, "{}", extra_env={"RAMEN_DEMO_REF": "v0.8.0"})
    assert r.returncode == 0, r.stderr + r.stdout
    made = [c for c in calls if c.split(" ", 1)[0].endswith(("/api/v1/groups", "/groups/demo/environments"))]
    assert len(made) == 2 and all('"ref":"v0.8.0"' in c for c in made), made


def test_a_failed_demo_deploy_names_the_reason_and_the_way_out(tmp_path):
    import json

    err = "RuntimeError: mcp/guardrails.yaml: guardrails: no LLM provider: set one on the group or ask a super admin"
    job = json.dumps({"status": "error", "error": err})
    r, _, _ = _fake_demo_tools(tmp_path, "{}", job=job)
    assert r.returncode == 1
    assert f"deploy failed: {err}" in r.stdout, r.stdout
    assert "Config → LLM providers" in r.stdout and "RAMEN_DEMO_REF=v0.8.0" in r.stdout, r.stdout


def test_azure_operator_can_push_platform_images_in_abac_mode():
    # 1.0.0c: a fresh ABAC-mode registry answered 401 to the operator's push (Owner grants no data-plane rights there);
    # the terraform principal gets Repository Writer on the platform repositories only, never on ramen/groups/
    tf = (ROOT / "deploy" / "terraform" / "azure" / "main.tf").read_text()
    block = tf[tf.index('resource "azurerm_role_assignment" "operator_acr_push"') :]
    block = block[: block.index("\n}\n")]
    assert 'role_definition_name = "Container Registry Repository Writer"' in block
    assert "data.azurerm_client_config.me.object_id" in block
    for repo in ("ramen/console", "ramen/worker", "ramen/skillspector"):
        assert f"StringStartsWithIgnoreCase '{repo}'" in block
    assert "ramen/groups" not in block


def test_eks_installs_the_ebs_csi_driver_with_its_own_role_after_the_nodes():
    """1.0.0c: the promptfoo PVC sat Pending for hours on EKS: the only class was the in-tree `gp2`
    (kubernetes.io/aws-ebs), which modern EKS cannot provision without the EBS CSI driver. The managed addon runs as
    its own IRSA role (/ramen/ramen-ebs-csi, AWS managed AmazonEBSCSIDriverPolicy) and waits for the node group (its
    node DaemonSet needs nodes, or the addon never turns ACTIVE)."""
    tf = (ROOT / "deploy/terraform/aws/main.tf").read_text()
    addon = _block(tf, 'resource "aws_eks_addon" "ebs_csi"')
    assert 'addon_name                  = "aws-ebs-csi-driver"' in addon and "aws_eks_cluster.ramen.name" in addon
    assert "service_account_role_arn    = aws_iam_role.ebs_csi.arn" in addon
    assert "aws_eks_node_group.ramen" in addon  # after the -podnet nodes
    assert '-podnet"' in _block(tf, 'resource "aws_eks_node_group" "ramen"')  # D89 kept
    iam = (ROOT / "deploy/terraform/aws/iam.tf").read_text()
    assert 'ebs_csi    = "system:serviceaccount:kube-system:ebs-csi-controller-sa"' in iam
    role = _block(iam, 'resource "aws_iam_role" "ebs_csi"')
    assert 'name               = "ramen-ebs-csi"' in role and 'path               = "/ramen/"' in role
    assert 'irsa_trust["ebs_csi"]' in role
    att = _block(iam, 'resource "aws_iam_role_policy_attachment" "ebs_csi"')
    assert "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy" in att
    out = (ROOT / "deploy/terraform/aws/outputs.tf").read_text()
    assert "storage.className=gp3,storage.createAwsGp3=true" in out.split("engines_set =")[1]
    assert 'output "ebs_csi_role_arn"' in out
    cf = (ROOT / "deploy/cloudformation/ramen.yaml").read_text()
    a = _cfn(cf, "EbsCsi")
    assert "Type: AWS::EKS::Addon" in a and "AddonName: aws-ebs-csi-driver" in a and "DependsOn: NodeGroup" in a
    assert "ServiceAccountRoleArn: !GetAtt EbsCsiRole.Arn" in a
    r = _cfn(cf, "EbsCsiRole")
    assert "RoleName: ramen-ebs-csi" in r and "Path: /ramen/" in r
    assert "system:serviceaccount:kube-system:ebs-csi-controller-sa" in r
    assert "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy" in r
    outs = cf[cf.index("\nOutputs:") :]
    assert "  StorageSet:" in outs and "--set storage.className=gp3,storage.createAwsGp3=true" in outs
    assert "  EbsCsiRoleArn:" in outs


def test_chart_storage_class_feeds_every_pvc_and_makes_gp3_on_aws():
    """`storage.className` is the class of every PVC the chart makes (promptfoo, Phoenix) unless the component names
    its own; `storage.createAwsGp3` adds the encrypted gp3 default class (EBS CSI) on provider=aws only."""
    chart = ROOT / "deploy/helm/ramen"
    values = (chart / "values.yaml").read_text()
    assert 'storage:\n  className: ""' in values and "createAwsGp3: false" in values
    for t in ("promptfoo.yaml", "phoenix.yaml"):
        assert 'include "ramen.storageClassName"' in (chart / "templates" / t).read_text(), t
    sc = (chart / "templates/storageclass-aws.yaml").read_text()
    for s in (
        'eq .Values.provider "aws"',
        "kind: StorageClass",
        "name: gp3",
        'storageclass.kubernetes.io/is-default-class: "true"',
        "provisioner: ebs.csi.aws.com",
        "volumeBindingMode: WaitForFirstConsumer",
        "type: gp3",
        'encrypted: "true"',
    ):
        assert s in sc, s


def test_chart_renders_the_gp3_class_and_binds_the_pvcs_to_it():
    import shutil

    import pytest

    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    from tests.test_v100c_evals import by_kind, template

    aws = ("provider=aws", "aws.account=1", "region=us-east-1", "evals.engine=promptfoo", "otel.collector=phoenix")
    rc, err, docs = template(*aws, "storage.className=gp3", "storage.createAwsGp3=true")
    assert rc == 0, err
    sc = by_kind(docs, "StorageClass", "gp3")
    assert sc["provisioner"] == "ebs.csi.aws.com" and sc["parameters"] == {"type": "gp3", "encrypted": "true"}
    assert sc["volumeBindingMode"] == "WaitForFirstConsumer" and sc["allowVolumeExpansion"] is True
    for pvc in ("promptfoo", "phoenix"):
        assert by_kind(docs, "PersistentVolumeClaim", pvc)["spec"]["storageClassName"] == "gp3", pvc
    rc, err, docs = template(*aws, "storage.className=gp3", "evals.storageClassName=io2")  # the component wins
    assert rc == 0, err
    assert by_kind(docs, "PersistentVolumeClaim", "promptfoo")["spec"]["storageClassName"] == "io2"
    assert by_kind(docs, "StorageClass", "gp3") is None  # not asked for
    rc, err, docs = template("evals.engine=promptfoo", "storage.createAwsGp3=true")  # gcp: no EBS class
    assert rc == 0, err
    assert by_kind(docs, "StorageClass", "gp3") is None
    assert "storageClassName" not in by_kind(docs, "PersistentVolumeClaim", "promptfoo")["spec"]  # cluster default


# S5 / §38.2 GCP residual (sec-gaps, 2026-11-17) -----------------------------------------------------------------
def _gtf(d, f="main.tf"):
    return (ROOT / f"deploy/terraform/{d}/{f}").read_text()


def _flat_block(text, head):
    """The `resource "<type>" "<name>" { … }` block starting at `head` (brace-matched), whitespace collapsed."""
    i = text.index(head)
    depth, j = 0, text.index("{", i)
    for k in range(j, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            return " ".join(text[i : k + 1].split())  # whitespace-insensitive (terraform fmt aligns `=`)
    raise AssertionError(head)


def test_s5_gcp_removes_editor_from_the_default_compute_sa_behind_a_variable():
    tf, var = _gtf("gcp"), _gtf("gcp", "variables.tf")
    rm = _flat_block(tf, 'resource "google_project_iam_member_remove" "default_compute_editor"')
    assert "count = var.remove_default_compute_editor ? 1 : 0" in rm
    assert 'role = "roles/editor"' in rm and "local.default_compute_sa" in rm
    assert '"${data.google_project.this.number}-compute@developer.gserviceaccount.com"' in tf
    v = _flat_block(var, 'variable "remove_default_compute_editor"')
    assert "default = true" in v and "ramen-nodes" in v and "relies on" in v
    # a cluster still running as the default SA keeps what nodes need once editor is gone (granted before removal)
    keep = _flat_block(tf, 'resource "google_project_iam_member" "default_compute_node"')
    assert "local.default_sa_runs_nodes" in keep and "local.node_roles_all" in keep
    assert "google_project_iam_member.default_compute_node" in rm  # depends_on: grant first, then remove
    assert 'var.node_service_account == "default"' in tf and "var.default_compute_runs_nodes" in tf


def test_s5_gcp_satellite_nodes_run_as_ramen_nodes_not_the_default_sa():
    tf, var = _gtf("gcp-satellite"), _gtf("gcp-satellite", "variables.tf")
    assert "auto_provisioning_defaults" in tf and "service_account = local.node_service_account" in tf
    assert '"ramen-nodes@${var.project}.iam.gserviceaccount.com"' in tf
    assert 'variable "node_service_account"' in var and "default_compute_runs_nodes" in var
    assert "google_project_iam" not in tf  # still no IAM in the satellite module


def test_ar_platform_and_group_repositories_are_told_apart_by_a_resource_tag():
    tf, out = _gtf("gcp"), _gtf("gcp", "outputs.tf")
    key = _flat_block(tf, 'resource "google_tags_tag_key" "repo"')
    assert 'short_name = "ramen-repo"' in key and 'parent = "projects/${var.project}"' in key
    assert 'short_name = "platform"' in _flat_block(tf, 'resource "google_tags_tag_value" "platform"')
    assert 'short_name = "group"' in _flat_block(tf, 'resource "google_tags_tag_value" "group"')
    plat = _flat_block(tf, 'resource "google_tags_location_tag_binding" "platform_repo"')
    assert "//artifactregistry.googleapis.com/projects/" in plat
    assert "/repositories/${google_artifact_registry_repository.ramen.repository_id}" in plat
    assert "google_tags_tag_value.platform.id" in plat and "location" in plat
    # the console's repository rights only match repositories carrying ramen-repo=group
    repos = _flat_block(tf, 'resource "google_project_iam_member" "console_build_repos"')
    assert "condition {" in repos
    assert 'resource.matchTagId(\\"${google_tags_tag_key.repo.id}\\", \\"${google_tags_tag_value.group.id}\\")' in repos
    # it may attach ONLY the group value (Tag User on that value), never detach a binding anywhere
    tu = _flat_block(tf, 'resource "google_tags_tag_value_iam_member" "console_group_tag_user"')
    assert "google_tags_tag_value.group.name" in tu and 'role = "roles/resourcemanager.tagUser"' in tu
    create = _flat_block(tf, 'resource "google_project_iam_custom_role" "console_build_create"')
    assert '"artifactregistry.repositories.createTagBinding"' in create
    assert '"artifactregistry.repositories.list"' in create
    for block in (create, _flat_block(tf, 'resource "google_project_iam_custom_role" "console_build_repos"')):
        assert '"artifactregistry.repositories.deleteTagBinding"' not in block
    assert "google_tags_tag_value_iam_member" not in _flat_block(tf, 'resource "google_tags_tag_value" "platform"')
    assert tf.count('resource "google_tags_tag_value_iam_member"') == 1
    assert 'output "ar_group_tag"' in out  # the namespaced value the console binds (its default, RAMEN_AR_GROUP_TAG)
