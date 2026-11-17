# IAM and network on AWS

Every identity an EKS install of Ramen uses, what each may do, and every network path, read from
`deploy/terraform/aws/{main,iam}.tf`, `deploy/terraform/aws-satellite/`, `deploy/cloudformation/ramen.yaml` (which
mirrors the Terraform module), the chart `deploy/helm/ramen` and the console's AWS adapter
(`console/src/ramen_console/cloud/aws*.py`). Terraform creates nothing per group. The install itself:
[Deploy on AWS](deploy-aws.md).

## Identities at a glance

| Identity | Kind | Created by | Used by |
|---|---|---|---|
| `/ramen/ramen-console` | IAM role, IRSA for `system:serviceaccount:ramen-system:console` | Terraform / CloudFormation | the console pod |
| `ramen-eks-cluster` | IAM role for the EKS service | Terraform | the control plane |
| `ramen-eks-node` | IAM role for EC2 | Terraform | the managed node group |
| `/ramen/ramen-<group>-<zone>` | IAM role, IRSA for `ramen-<group>-<zone>/worker`, boundary `ramen-worker-boundary` | the console, when a zone is bound | that zone's worker pods |
| `/ramen/build/ramen-build-<group>` | IAM role, IRSA for `ramen-system/ramen-build-<group>`, boundary `ramen-build-boundary` | the console, on the group's first build | the group's image-build Job |
| `/ramen/ramen-evals` | IAM role, IRSA for `ramen-system/ramen-evals` | Terraform | the evals and SkillSpector scan Jobs |
| `/ramen/ramen-alb-controller`, `/ramen/ramen-fluent-bit` | IAM roles, IRSA in `kube-system` | Terraform | the AWS Load Balancer Controller, Fluent Bit |
| `/ramen/ramen-ebs-csi` | IAM role, IRSA for `kube-system/ebs-csi-controller-sa` | Terraform / CloudFormation | the `aws-ebs-csi-driver` addon's controller (the chart's PVCs) |

Every IRSA role trusts the cluster's OIDC provider with an exact `sub` (namespace and service account) and `aud =
sts.amazonaws.com`.

## The console

Inline policy `ramen-console` on `/ramen/ramen-console`:

| Actions | Resources |
|---|---|
| `s3:ListBucket`, `s3:GetBucketLocation`; `s3:GetObject`, `PutObject`, `DeleteObject` | the groups bucket; its objects |
| `secretsmanager:CreateSecret`, `PutSecretValue`, `GetSecretValue`, `DescribeSecret`, `DeleteSecret`, `RestoreSecret`, `TagResource`, `UpdateSecret` | `secret:ramen/*` (group secrets) and `secret:ramen-console/*` (its own) |
| `secretsmanager:ListSecrets`, `dynamodb:ListTables`, `iam:ListRoles`, `wafv2:ListWebACLs`, `ListIPSets`, `wafv2:GetWebACLForResource`, `elasticloadbalancing:DescribeLoadBalancers`, `DescribeTags`, `SetWebAcl`, `logs:StartQuery`, `GetQueryResults`, `StopQuery`, `DescribeLogGroups` | `*` (not resource-scopable) |
| DynamoDB item and table actions | the state table and its indexes |
| `eks:DescribeCluster` | the cluster (the OIDC issuer for worker roles) |
| `iam:DeleteRole`, `GetRole`, `UpdateAssumeRolePolicy`, `TagRole`, `DeleteRolePolicy`, `GetRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `DetachRolePolicy` | `role/ramen/*` |
| `iam:CreateRole`, `PutRolePolicy`, `AttachRolePolicy` | `role/ramen/*`, **only** with `iam:PermissionsBoundary` = `ramen-worker-boundary` or `ramen-build-boundary` |
| `ecr:CreateRepository`, `DescribeRepositories`, `TagResource`, `DeleteRepository`, `BatchDeleteImage`, `ListImages` | `repository/ramen/groups/*` only — no push action anywhere |
| `wafv2:` create/get/update/delete IP sets and the web ACL, `TagResource`; `AssociateWebACL`, `DisassociateWebACL` | the web ACL `ramen`, IP sets `ramen-*`, the IngressGroup's ALBs |

No `iam:PassRole`: IRSA binds a role to a service account by annotation, nothing is passed to a service. With
`console_bedrock = true` (the default; CloudFormation parameter `ConsoleBedrock`) a second inline policy
`ramen-console-bedrock` allows `bedrock:InvokeModel` and `InvokeModelWithResponseStream` on foundation models and the
account's inference profiles, and `bedrock:CallWithBearerToken`: the judge proxy calls Bedrock for LLM rails.

Inside the cluster the console has the same ClusterRole `ramen-console`, per-zone `ramen-console-zone` RoleBinding and
`ramen-console-build` Role as on GCP ([IAM and network on GCP](iam-network-gcp.md#the-console)), the
`ramen-console-tls` Role in `ramen-system` (D86: the CA ConfigMap `ramen-ca`, the chart services' `ramen-tls-<service>`
Secrets, restarts of those Deployments), plus Ingress rights (the ALB routes are Ingresses on AWS).

## Worker roles

`/ramen/ramen-<group>-<zone>`, tagged `ramen=worker`, `group=<group>`, `zone=<zone>`. Baseline inline policy:
`s3:ListBucket` with `s3:prefix` `<group>/` or `<group>/*`, `s3:GetObject` on `<bucket>/<group>/*`,
`secretsmanager:GetSecretValue` on `secret:ramen/<group>/*`. Approved permission requests add actions to the inline
policy ([service-account permissions](users-access.md#service-account-permissions)).

The permissions boundary `ramen-worker-boundary` caps all of it, keyed on the role's tag:

- `s3:ListBucket` only with `s3:prefix` `${aws:PrincipalTag/group}/…`; object actions only on
  `<bucket>/${aws:PrincipalTag/group}/*`;
- `secretsmanager:GetSecretValue`, `DescribeSecret` only on `secret:ramen/${aws:PrincipalTag/group}/*`;
- the catalogue's account-wide actions (logs, CloudWatch metrics, SNS publish, SQS consume, DynamoDB items,
  `kms:Decrypt`, `bedrock:InvokeModel`) — reachable only when a request for them was approved.

So no inline policy, however it is written, reaches another group's prefix or secrets.

## Image builds, evals and scans

- **Builds (D85).** The console creates ECR `ramen/groups/<group>/worker` and role `/ramen/build/ramen-build-<group>`
  (tag `ramen-build=<group>`) with inline policy `ramen-build`: `ecr:GetAuthorizationToken`, pull and push on that
  repository, pull on the worker base repository. The boundary `ramen-build-boundary` allows the registry login, pull
  on `repository/ramen/*` and push only on `repository/ramen/groups/${aws:PrincipalTag/ramen-build}/*`. The platform
  repositories `ramen/console`, `ramen/worker` and `ramen/skillspector` are out of every build role's reach, and out of
  the console's (it holds no push action).
- **Evals and scans.** `/ramen/ramen-evals` may `logs:CreateLogStream` and `logs:PutLogEvents` on the Container
  Insights application log group, nothing else. The KSA mounts no API token and is bound to no Role.

## Cluster, nodes and add-ons

`ramen-eks-cluster` holds `AmazonEKSClusterPolicy`. `ramen-eks-node` holds `AmazonEKSWorkerNodePolicy`,
`AmazonEKS_CNI_Policy` and `AmazonEC2ContainerRegistryReadOnly` (AWS-managed; pulls every repository of the account).
The Load Balancer Controller role carries the controller's official policy (vendored, v2.13.0); Fluent Bit's carries
`CloudWatchAgentServerPolicy`. `ramen-ebs-csi` carries `AmazonEBSCSIDriverPolicy` (AWS-managed: create, attach,
snapshot and delete EBS volumes, the create calls limited to volumes the driver tags), for the managed
`aws-ebs-csi-driver` addon that provisions the chart's PVCs (promptfoo, Phoenix) on the chart's encrypted `gp3` class;
satellites run no PVCs and get no driver. The cluster's access mode is `API_AND_CONFIG_MAP` with the creator as admin.

## Network

| Path | From → to | Port | Control |
|---|---|---|---|
| Clients → edge | internet → internet-facing ALB (IngressGroup `ramen`, `target-type: ip`) | 443 only | ACM certificate (self-signed import, SAN `*.<region>.elb.amazonaws.com`); WAF web ACL `ramen` |
| Edge → console | ALB → console pods | 8000 | Ingress `console` on `/`; HTTPS to the pod and HTTPS health check on `/healthz` (D86; the ALB does not verify the certificate) |
| Edge → workers | ALB → worker pods | 8080 | per-zone Ingresses `worker` (gRPC paths, `GRPC` target group) and `worker-http` (`/mcp`, OAuth metadata, `HTTP1` target group), both matching `ramen-group` + `ramen-zone`, both `backend-protocol: HTTPS` with HTTPS health checks (D86); IP rules = a WAF IP set per group and a web ACL rule blocking that group's header from other sources |
| Console → workers | console pod → worker pod IPs (pod subnets, D89) | 8080 | gRPC over TLS verified against the install CA as `worker.ramen.internal` (D86); admin key; NetworkPolicy `worker` (see below) |
| Workers → console, Redis, collector, internet | as on GCP | 8000, 6379, 4318, any | TLS on each (D86); `worker-egress` (needs `cluster.serviceCidr`: `aws eks describe-cluster --name ramen --query cluster.kubernetesNetworkConfig.serviceIpv4Cidr --output text`; EKS picks `172.20.0.0/16` when the VPC is in `10.0.0.0/8`, as the default `10.42.0.0/16` is, else `10.100.0.0/16`). The install command in [Deploy on AWS](deploy-aws.md#3-the-console) does not set it, so there is no `worker-egress` policy until you add `--set cluster.serviceCidr=…` |
| Home → satellite | home VPC (as the home node's address) → satellite worker pods | 8080 | VPC peering with routes both ways; ingress rule on the satellite's cluster security group (which the pod interfaces carry) from `home_vpc_cidr`; the satellite zone's `worker` policy admits the home public subnets |

- **VPC.** `vpc_cidr` (default `10.42.0.0/16`), an internet gateway, no NAT, one route table for all four subnets,
  and the EKS API endpoint public and private. Subnets (D89; the satellite's are the same in its `10.43.0.0/16`):

  | Subnet | Default CIDR | Holds | Public IP on launch | ALB may use it |
  |---|---|---|---|---|
  | `ramen-public-0`, `-1` | `10.42.0.0/24`, `10.42.1.0/24` | nodes (their primary addresses), the ALB's interfaces, the EKS control plane's interfaces | yes | yes (`kubernetes.io/role/elb`) |
  | `ramen-pods-0`, `-1` | `10.42.64.0/20`, `10.42.80.0/20` | every pod that is not `hostNetwork` | no | no (untagged) |

  Pods take addresses from the pod subnet of their node's availability zone through VPC CNI custom networking: the
  managed `vpc-cni` addon sets `AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG` and `ENI_CONFIG_LABEL_DEF =
  topology.kubernetes.io/zone` and creates one `ENIConfig` per zone (pod subnet + the cluster security group).
  Pod traffic leaving the VPC is rewritten to the node's address (`AWS_VPC_K8S_CNI_EXTERNALSNAT=false`), so there is
  no NAT gateway. A node's first interface holds no pods: a `t3.small` takes 8 pods (was 11).
- **Security groups** are EKS's cluster security group and the ALB's, which the Load Balancer Controller manages
  (443 from anywhere, then to the pods). Terraform adds none on the home cluster.
- **NetworkPolicies.** The chart and the console write the same policies as on GCP. Two things differ on EKS:
  1. A policy is only enforced when the cluster runs a policy engine. The Amazon VPC CNI enforces NetworkPolicies
     only with its network-policy agent on (`enableNetworkPolicy`); since 1.0.0c Terraform (home and satellite) and
     the CloudFormation template install the managed `vpc-cni` addon with it on. Checked live on 1.0.0b35: an
     unrelated pod is dropped at Redis ([Threat model](../threat-model.md#findings-and-their-status), S6b).
  2. The ALB reaches pods from its interfaces in the public subnets, so the "edge" peer is a CIDR list: chart
     `networkPolicy.awsAlbCidrs` for the `ramen-system` policies, which the chart also hands the console as
     `RAMEN_AWS_ALB_CIDRS` for each zone's `worker` policy. Terraform sets it: `terraform output -raw network_set`
     (CloudFormation: output `NetworkSet`) is `--set networkPolicy.awsAlbCidrs={10.42.0.0/24,10.42.1.0/24}`, part of
     the install command in [Deploy on AWS](deploy-aws.md#3-the-console). With satellites, the console's list also
     needs each satellite's public subnets (`console.env.RAMEN_AWS_ALB_CIDRS`, comma separated; output `alb_cidrs`).
     The pre-D89 `networkPolicy.awsVpcCidr` / `RAMEN_AWS_VPC_CIDR` (a whole VPC) still work when the new value is
     unset, but a VPC range holds the pod subnets too and admits every pod.

  Who can reach a worker pod on 8080, then: the ALB (public subnets), the console (namespace `ramen-system`), the
  zone's own pods, and anything using a node's address — kubelet probes, `hostNetwork` pods, and on a satellite the
  home cluster's pods, whose cross-VPC traffic carries the home node's address. A pod in any other namespace has a
  pod-subnet address and is dropped ([Threat model](../threat-model.md#findings-and-their-status), S6b). The
  network-policy agent lets the kubelet's probes through as well (Redis, whose policy has no `ipBlock`, passes its
  `tcpSocket` probe on EKS).
- **IP rules** are enforced by the node (`RAMEN_ALLOWED_CIDRS`) and by WAF. WAF IP sets take `/1`–`/32`, so
  `0.0.0.0/0` is written as two `/1`s.

## Satellites

`deploy/terraform/aws-satellite` adds an EKS cluster in another region of the same account, a VPC (default
`10.43.0.0/16`) peered with the home one, routes both ways and the 8080 rule above, an EKS access entry mapping the
console role to the Kubernetes group `ramen-console` (bound to the ClusterRole `ramen-console` by the satellite chart,
and in each zone namespace through the home chart's `console.k8sSubjects`), and
a separate inline policy on the console role for the satellite's EKS, WAF and ALB actions. ECR is regional: satellite
nodes pull from the home region's repositories. [Deploy on AWS → Satellite clusters](deploy-aws.md#satellite-clusters-other-regions).

## Switches that change IAM

| Terraform variable / CloudFormation parameter | Default | Effect |
|---|---|---|
| `console_bedrock` / `ConsoleBedrock` | `true` / `"true"` | the console's Bedrock policy for the judge proxy |
| `otel_collector`, `evals_engine`, `skillspector_engine` | `none` | the optional engines (no IAM of their own); CloudFormation has no parameter for them — add the chart's `--set otel.collector=…,evals.engine=…,skillspector.engine=…` yourself |
