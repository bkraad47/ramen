# Deploy on AWS

EKS, DynamoDB, S3, Secrets Manager, ECR and an application load balancer with gRPC target groups. Each release's cloud
pass applies this on a real account and tears it down ([the record](transport.md#what-is-verified-and-what-is-not)). Least exercised so far: WAF blocking under repeated rule changes, rebalance weights under real load and
Fluent Bit at volume. If a step breaks on your account, open an issue with the step and the error.

Cost while it runs (us-east-1 on-demand list prices, 2026-11): the EKS control plane ($0.10 an hour while its
Kubernetes version is in standard support, $0.60 in extended support), four `t3.small` nodes (about $0.08 an hour
together), the application load balancer ($0.0225 an hour plus its LCUs) and CloudWatch logs. Current rates:
[EKS](https://aws.amazon.com/eks/pricing/), [EC2](https://aws.amazon.com/ec2/pricing/on-demand/),
[Elastic Load Balancing](https://aws.amazon.com/elasticloadbalancing/pricing/).

## Prerequisites

`aws` CLI logged in with admin on the target account, `terraform` 1.6 or newer, `helm` 3.12 or newer, `kubectl`,
`docker` with buildx, `grpcurl`, `uv`, `openssl`, `python3`. SSO sessions expire mid-apply; `terraform apply`
resumes.

If the account is not your default profile, export it once so that `aws`, Terraform, the Helm provider's
`aws eks get-token` and `kubectl` all use it, and check it before each step:

```sh
aws login --profile ramen               # or: aws sso login --profile ramen
export AWS_PROFILE=ramen AWS_REGION=us-east-1
aws sts get-caller-identity --query Account --output text
```

A profile made by `aws login` holds a `login_session`, which the Terraform AWS provider pinned here (5.x) cannot
read: `terraform plan` fails with "No valid credential sources found". Give Terraform a profile that wraps it; it
refreshes on its own, so a long apply does not outlive exported keys:

```ini
# ~/.aws/config
[profile ramen-tf]
region = us-east-1
credential_process = aws configure export-credentials --profile ramen --format process
```

Then `export AWS_PROFILE=ramen-tf` for the steps below. `aws eks update-kubeconfig` writes the profile in force
into the kubeconfig, so pass `--profile` there if you switch.

## 1. Infrastructure

```sh
cd deploy/terraform/aws && cp terraform.tfvars.example terraform.tfvars   # region, node size
terraform init && terraform apply        # most of the wait is the EKS control plane and node group
cd ../../..
```

Terraform creates the cluster (Kubernetes 1.35 by default, in standard support until 2027-03-27; an older version
in extended support bills the control plane at $0.60 an hour instead of $0.10) and node group, the DynamoDB table, the bucket, ECR repos, the console's IAM role (which may create each group's own ECR repository and build role, D85),
permissions boundaries for the zone roles and the build roles, a self-signed certificate imported into ACM, the
managed `vpc-cni` and `aws-ebs-csi-driver` addons (the EBS CSI driver runs as its own role `ramen-ebs-csi`), the AWS
Load Balancer Controller and Fluent Bit. A CloudFormation template with the same base resources is in `deploy/cloudformation`.

If the apply fails before its Helm releases, re-run it. Without the load-balancer controller the console never
gets an address.

## 2. Images

Every release publishes `docker.io/bkraad47/ramen-console` and `ramen-worker` (also on `ghcr.io/bkraad47/`). To run
those, skip this step and add `--set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$(cat VERSION)
--set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$(cat VERSION)` to the Helm command in the next
step. To build and push your own into ECR, which is what the release verification runs did:

```sh
REGION=us-east-1; ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
EC=$(terraform -chdir=deploy/terraform/aws output -raw ecr_console); EW=$(terraform -chdir=deploy/terraform/aws output -raw ecr_worker)
docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=$(cat VERSION) -t $EC:$(cat VERSION) --push console
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$(cat VERSION) -t $EW:$(cat VERSION) --push .
```

A push from a Colima or Lima Docker to ECR often fails with `broken pipe`, and retrying rarely helps. Build
locally and push with `crane`:

```sh
aws ecr get-login-password --region $REGION | crane auth login $ACCOUNT.dkr.ecr.$REGION.amazonaws.com -u AWS --password-stdin
docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$(cat VERSION) -t $EW:$(cat VERSION) --load .
docker save $EW:$(cat VERSION) -o worker.tar && crane push worker.tar $EW:$(cat VERSION)   # same for the console
```

Nodes cache an image by tag. If you push the same tag again, pods keep the copy they already have: record the
new build with its digest (`crane digest $EW:$(cat VERSION)`) on the group page's **Worker image** card and deploy,
or push a new tag.

## 3. The console

```sh
REGION=$(terraform -chdir=deploy/terraform/aws output -raw region); ACCOUNT=$(terraform -chdir=deploy/terraform/aws output -raw account)
aws eks update-kubeconfig --name ramen --region $REGION
CERT=$(terraform -chdir=deploy/terraform/aws output -raw certificate_arn)
TABLE=$(terraform -chdir=deploy/terraform/aws output -raw dynamodb_table)
EVALS=$(terraform -chdir=deploy/terraform/aws output -raw evals_role_arn)
helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace \
  --set provider=aws,region=$REGION --set-string aws.account=$ACCOUNT --set aws.cluster=ramen --set aws.table=$TABLE \
  --set aws.certificateArn=$CERT \
  $(terraform -chdir=deploy/terraform/aws output -raw builder_set) --set evals.roleArn=$EVALS \
  $(terraform -chdir=deploy/terraform/aws output -raw engines_set) \
  $(terraform -chdir=deploy/terraform/aws output -raw network_set) \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
kubectl -n ramen-system get ingress console          # ADDRESS is the load balancer's DNS name after a few minutes
aws acm get-certificate --certificate-arn $CERT --region $REGION --query Certificate --output text > ramen-lb.pem
ALB=<that address>
curl --cacert ramen-lb.pem https://$ALB/readyz      # {"ok":true,"store":"dynamodb","version":"<version>"}
```

The three values are generated on the command line and never printed. Read them back from the chart's Secret
when you need them (the sign-in below, `cloud_smoke.sh`):

```sh
kubectl -n ramen-system get secret ramen-console -o jsonpath='{.data.RAMEN_ADMIN_PASSWORD}' | base64 -d; echo
kubectl -n ramen-system get secret ramen-console -o jsonpath='{.data.RAMEN_ADMIN_KEY}' | base64 -d; echo
```

The console re-applies `RAMEN_ADMIN_PASSWORD` to `admin@ramen.local` on every start, so the Secret stays the
password of record: change it with `helm upgrade … --reuse-values --set console.secrets.RAMEN_ADMIN_PASSWORD=…`
(a password changed in the console is reset at the next start). Keep `RAMEN_FERNET_KEY`: it encrypts the store ([rotate-keys skill](skills.md) to rotate it).

Then set the public address, which is the OAuth issuer the workers trust, and upgrade again:

```sh
helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set console.env.RAMEN_PUBLIC_URL=https://$ALB
```

`builder_set` (`--set builder.enabled=true`) is what lets a deploy build a group's worker image from its
`requirements.txt` into the group's own repository `ramen/groups/<group>/worker`, as the group's own role
([Worker images](worker-images.md#per-group-images-100c-d85)). Leave it out and no image is ever built: workers
install requirements when they load. Add `--set image.digest=sha256:…,image.workerDigest=sha256:…` to run the platform
images by digest. `evals.roleArn` is the `ramen-evals` role the evals and scan Jobs assume: it
may write its own CloudWatch log streams and nothing else, so a group's promptfoo config cannot reach ECR, the
bucket or any secret.

`network_set` (`--set networkPolicy.awsAlbCidrs={10.42.0.0/24,10.42.1.0/24}`, the two public subnets) tells the
NetworkPolicies where the ALB comes from: the chart's `ramen-system` policies and, through `RAMEN_AWS_ALB_CIDRS`,
the console's per-zone `worker` policies. Pods take their addresses from two separate pod subnets (VPC CNI custom
networking, D89), so admitting the ALB's subnets does not admit other pods. Terraform turns on the VPC CNI's
network-policy agent, so these policies are enforced; without the flag the chart's default `10.0.0.0/16` misses
the VPC and the ALB is cut off ([IAM and network on AWS](iam-network-aws.md)).

**Optional engines.** `otel_collector = "jaeger" | "phoenix"`, `evals_engine = "promptfoo"` and
`skillspector_engine = "skillspector"` in `terraform.tfvars` (all `none` by default) become the chart's
`otel.collector`, `evals.engine` and `skillspector.engine` through `engines_set`: a try-out trace UI
([Telemetry](telemetry.md)), the promptfoo server every evals run is shared with ([Evals](evals.md)), and
SkillSpector scans of every group's skills ([SkillSpector](skillspector.md)). Each lives in `ramen-system` without
auth or an edge route (`kubectl port-forward`); turning one on changes only that engine's objects.

`engines_set` also carries `storage.className=gp3,storage.createAwsGp3=true`: the chart adds an encrypted `gp3`
StorageClass (EBS CSI, `WaitForFirstConsumer`, the cluster default) and binds its PVCs (promptfoo, Phoenix) to it.
EKS itself ships only the in-tree `gp2` class, which provisions nothing, so without these a PVC stays `Pending`.
If the cluster already has a class named `gp3`, set `storage.createAwsGp3=false` and keep `storage.className`.
CloudFormation: output `StorageSet`.

The certificate is self-signed. Its name covers `*.<region>.elb.amazonaws.com`, so the exported PEM verifies the
load balancer for `curl --cacert` and the bridge's `--ca`.

## 4. The first zone, group and deploy

Open `https://<alb>/`, accept the certificate, and sign in as `admin@ramen.local`. The steps are the same as on
GCP: a zone named `a` with provider `aws` and region `us-east-1a`, the `demo` group from the demo repo, an
environment on zone `a`, a key, a canary deploy. Creating the environment makes the namespace `ramen-demo-a`, an
IAM role `ramen-demo-a` bound to the zone's service account through IRSA, and two zone Ingresses on the same load
balancer, both matching the two headers: `worker` sends the gRPC paths to a `GRPC` target group, and `worker-http`
sends `/mcp` and `/.well-known/oauth-protected-resource` to an `HTTP1` target group (a gRPC target group answers
`464` to anything else). Both target groups speak HTTPS to the pods and health-check over HTTPS (D86); the load
balancer encrypts but does not verify the worker's certificate. The load balancer needs a few minutes to program the
listener rules.

```sh
curl --cacert ramen-lb.pem https://$ALB/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: a' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
ramen-mcp-bridge --target $ALB:443 --tls --ca ramen-lb.pem --key "$RAMEN_MCP_KEY" --group demo --zone a
```

The demo repo ships `mcp/llm-config/`, so the workers refuse a call that does not carry the accepted configs
version (`-32028 configs required`): add `"_meta": {"io.ramen/configs": {"version": "<v>"}}` to `params`, with `<v>` from
`/.well-known/ramen-policy?group=demo&zone=<zone>` ([Deploy your first group → Call a tool](../first-group.md#8-call-a-tool)). The bridge
does this by itself.

Signing a person in through the bridge uses the same PEM: from bridge 0.2.2, `--ca` also verifies the console
during `--oauth` (`--oauth https://$ALB --client-id <id>`); older bridges fail there with
`CERTIFICATE_VERIFY_FAILED` unless `SSL_CERT_FILE=ramen-lb.pem` is set.

## The console's role and access controls

Every identity and network path in one place: [IAM and network on AWS](iam-network-aws.md).

| It may | How |
|---|---|
| Create and delete the per-zone IAM roles | Only under the path `/ramen/`, and only within the permissions boundary Terraform created. No `iam:PassRole` at all: IRSA binds a role to a service account by annotation, nothing is ever passed to a service |
| Call Bedrock for LLM rails (§38.1) | Inline policy `ramen-console-bedrock`, Terraform variable `console_bedrock` (default `true`): `bedrock:InvokeModel` and `InvokeModelWithResponseStream` on foundation models and the account's inference profiles, `bedrock:CallWithBearerToken`. Rails reach a provider only through the console's judge proxy; the worker boundary keeps `bedrock:InvokeModel` only for an approved `ai.user` request on tool code |
| Create and delete the per-group build roles and repositories (D85) | Roles `/ramen/build/ramen-build-<group>` only with the `ramen-build-boundary`, which allows the registry login, pull on `ramen/*` and push only on `ramen/groups/${aws:PrincipalTag/ramen-build}/*`; ECR `CreateRepository`, `DescribeRepositories`, `TagResource`, `ListImages`, `BatchDeleteImage` and `DeleteRepository` on `ramen/groups/*` only. No push action: the platform repositories `ramen/console`, `ramen/worker`, `ramen/skillspector` are out of its reach and every build role's |
| Read and write the groups bucket | `GetObject`, `PutObject` and `DeleteObject` on its objects; `ListBucket` and `GetBucketLocation` on the bucket |
| Manage group secrets | Secrets Manager under `ramen/<group>/` |
| Use the state table and read logs | The `ramen` DynamoDB table, CloudWatch Logs Insights |
| Program edge IP rules | `wafv2` actions scoped to the web ACL `ramen` and its IP sets, plus the read-only `wafv2:GetWebACLForResource` on `*` (the call names no web ACL, so a scoped grant fails with a `502` on the first IP rule) |
| Manage the zone namespaces | The same ClusterRole and per-zone Role as on GCP |

Each zone's workers assume `ramen-<group>-<zone>` through IRSA. The role reads the group's prefix of the bucket
(`s3:ListBucket` under an `s3:prefix` condition, `s3:GetObject` on `arn:aws:s3:::<bucket>/<group>/*`) and the
group's secrets. Requested permissions add actions to that role's inline policy, named resources only when
a scope was given. [Users and access](users-access.md#service-account-permissions) covers the approval flow. The
permissions boundary `ramen-worker-boundary` caps all of it a second time, keyed on the role's `group` tag: the
bucket statements allow only `${aws:PrincipalTag/group}/` and the secret statement only
`ramen/${aws:PrincipalTag/group}/*`, so no inline policy, however written, reaches another group's prefix.

The other roles Terraform creates: `ramen-eks-node` (the EKS managed policies: worker node, CNI, ECR read-only —
AWS manages the node identity, nothing was changed there), `ramen-evals` (`logs:CreateLogStream` and `logs:PutLogEvents` on the Container Insights log group), the Load
Balancer Controller and Fluent Bit roles with their vendored policies. There is no shared builder role any more
(D85): each group's build role is the console's, made on the group's first build, and it pushes to that group's
repository only (inline policy `ramen-build`: push and pull on `ramen/groups/<group>/worker`, pull on the worker base
repository, the registry login).

## Day 2

- **Upgrading to TLS on every hop (D86) from 1.0.0b/c**: `helm upgrade … --reset-then-reuse-values`, wait for the
  console and the chart services to restart on TLS, then **redeploy every zone** — until a zone is redeployed the
  console cannot reach it and its wake, judge, telemetry and Redis calls fail, though it still serves clients. The
  exact steps: [Configuration → Upgrading the chart](configuration.md#upgrading-the-chart).
- **Scale, IP rules, logs** work as on GCP ([Groups, zones and regions](groups-zones.md#ip-rules)). Here an IP
  rule becomes a WAF IP set per group and a rule in the web ACL that blocks requests carrying
  `ramen-group: <group>` from any other source. WAF IP sets take `/1` to `/32`, so `0.0.0.0/0` is written as
  `0.0.0.0/1` and `128.0.0.0/1`; the node keeps the list as given.
- **Canary and the load balancer**: the zone's two tracks share its traffic by weighted target groups. Before
  any canary pod changes, the console moves the canary's weight to the stable track and waits
  `RAMEN_ALB_SPLIT_DRAIN_SECS` (15 seconds; the load balancer took about 7) so no request lands on a pod that is
  going away; the canary gets its share back once it has passed.
- **Node sizing**: worker pods are pinned to their zone's availability zone, and the node group spreads over the
  two subnets' zones. The defaults are `node_count = 4`, `node_max = 5`: two `t3.small` per availability zone. With
  one per zone, the first canary deploy stays `Pending` ("1 Insufficient memory"), because the node in `us-east-1a`
  also carries the console and CoreDNS. Scale later with `aws eks update-nodegroup-config`. A stuck rollout names
  the pending pod and the scheduler's reason in the deploy log. With pod subnets (D89) a node's first network
  interface holds no pods, so EKS caps a `t3.small` at 8 pods (it was 11); three of them are `aws-node`,
  `kube-proxy` and Fluent Bit. `kubectl get nodes -o custom-columns=NAME:.metadata.name,PODS:.status.allocatable.pods`
  shows the cap; a pod stuck `Pending` with "Too many pods" needs another node.
- **Deploy from CI**: [Deploy from GitHub Actions](deploy-github-actions.md).
- **Logs**: Fluent Bit ships every pod to the CloudWatch log group `/aws/containerinsights/ramen/application`.
  External monitors filter on `kubernetes.namespace_name = "ramen-<group>-<zone>"`.

## Upgrading an install from before 1.0.0b: the public route table

The home module's public route table used to declare its internet route inline; since 1.0.0b it is a standalone
`aws_route.internet`, because an inline route table wipes every standalone route on each apply — the satellite
modules add their peering routes to this table. On the first apply after upgrading, import the existing route once
or terraform will report `RouteAlreadyExists`:

```sh
terraform import aws_route.internet $(terraform output -json route_table_ids | tr -d '[]" ')_0.0.0.0/0
```

## Upgrading an install from before 1.0.0c: NetworkPolicy enforcement

Since 1.0.0c the modules install the managed `vpc-cni` addon with `enableNetworkPolicy`, so the policies Ramen
writes start being enforced (the addon adopts the existing `aws-node` DaemonSet; nothing is replaced). Before that
apply, give the chart the ALB's subnets, or the newly enforced policies cut the ALB off the console and the
workers. The same apply also moves the pods to their own subnets (next section).

## Upgrading an install from before 1.0.0c: PVCs stuck Pending (EBS CSI)

Before 1.0.0c nothing installed the EBS CSI driver, so the promptfoo PVC (and Phoenix's) stayed `Pending` and so
did its pod. The next `terraform apply` adds the `aws-ebs-csi-driver` addon and role `ramen-ebs-csi` (after the
node group); then upgrade the chart with the class:

```sh
helm upgrade ramen deploy/helm/ramen -n ramen-system --reset-then-reuse-values \
  --set storage.className=gp3,storage.createAwsGp3=true
kubectl get storageclass                                   # gp3 (default), next to the unused gp2
kubectl -n ramen-system get pvc                            # Bound once the pod is scheduled
```

A `Pending` PVC that names no class takes `gp3` in place (Kubernetes lets an unset class be set once, and assigns
the default to such PVCs on its own). A PVC's class is otherwise immutable: if the upgrade fails on a PVC that
already names another class (for example `gp2`), delete it — `kubectl -n ramen-system delete pvc promptfoo` (or
`phoenix`) — and run the upgrade again so the chart recreates it on `gp3`; a PVC that never bound holds no data.
Teardown already uninstalls the chart before `terraform destroy`, while the driver still runs to delete the
volumes; a volume left behind is tagged `app=ramen` (`aws ec2 describe-volumes --filters Name=tag:app,Values=ramen`).
A satellite needs no driver: it runs workers only, and no worker has a PVC.

## Upgrading an install from before D89: pod subnets

Since D89 the modules add two private pod subnets (`10.42.64.0/20`, `10.42.80.0/20` for the default VPC) and turn
on VPC CNI custom networking: nodes and the ALB stay in the public `/24`s, pods take addresses from the pod subnets,
and the policies admit the public subnets only. Before, nodes, ALB and pods shared the public subnets and the
policies admitted the whole VPC, so a pod in any namespace could call a worker pod directly. Nodes that started
before custom networking keep their pods in the public subnets, so the node group is replaced once: it is renamed
`ramen-podnet`, the new nodes come up first (for a moment twice the nodes: 16 `t3.small` = 32 vCPUs with
`node_count = 8`, check the account's EC2 vCPU quota), then the old group drains and goes. In this order (a console
image from this release reads `RAMEN_AWS_ALB_CIDRS`; an older one reads only `RAMEN_AWS_VPC_CIDR`):

```sh
helm upgrade ramen deploy/helm/ramen -n ramen-system --reset-then-reuse-values --set image.tag=<this release> \
  --set 'networkPolicy.awsAlbCidrs={10.42.0.0/24,10.42.1.0/24}'      # the public subnets (output network_set after the apply)
terraform -chdir=deploy/terraform/aws apply                          # pod subnets, aws-node rolled, nodes replaced
kubectl get eniconfigs; kubectl get pods -A -o wide                  # pod IPs now in 10.42.64.0/20 and 10.42.80.0/20
```

Until the apply the pods still sit in the public subnets, so nothing loses access in between. Then redeploy each
zone: its `worker` policy carries the old whole-VPC range until the console rewrites it. With satellites, apply
each satellite module the same way and list its subnets too (see below). CloudFormation: update the stack with
the new template (it replaces the node group the same way) and use output `NetworkSet`.

## Satellite clusters (other regions)

A zone must be in a region that has a cluster ([where a zone can be](groups-zones.md#where-a-zone-can-be)). For a
zone in another region, add a **satellite**: a worker-only EKS cluster in that region of the same account, with its
own load balancer and self-signed certificate, driven by the console you already have. It shares the table, the
bucket, ECR and the IAM roles of the home apply; nodes pull images from the home region's ECR and builds stay in the
home cluster. The module adds two things to the home account: an EKS access entry mapping the console's role to the
Kubernetes group `ramen-console` on the new cluster, and an inline policy `ramen-satellite-<region>` on that role
for the new region's EKS, WAF and load-balancer ARNs (the home policy is scoped to the home region).

The console talks to workers by pod IP (per-pod smoke, metrics, reload, canary), not through the load balancer, so
the satellite's VPC is peered with the home one: the module creates the cross-region peering and accepts it in the
home region, adds routes to both VPCs' public route tables, and lets the home VPC's CIDR in on port 8080 at the
satellite's cluster security group. It needs the home VPC from the home apply's outputs: `home_vpc_id`, and unless
the defaults hold `home_vpc_cidr` (10.42.0.0/16) and `home_route_table_ids`; the satellite's own `vpc_cidr`
(10.43.0.0/16) must not overlap.

```sh
SAT=eu-west-1
terraform -chdir=deploy/terraform/aws output -raw vpc_id                            # home_vpc_id
cd deploy/terraform/aws-satellite && cp terraform.tfvars.example terraform.tfvars   # region = $SAT, home_region, home_vpc_id; same AWS_PROFILE as the home apply
terraform init && terraform apply                  # most of the wait is the EKS control plane and node group
$(terraform output -raw kubeconfig_command)        # kubectl now points at the satellite
cd ../../..
kubectl create ns ramen-system
kubectl label ns ramen-system ramen.io/routes=true
helm upgrade --install ramen-satellite deploy/helm/ramen-satellite -n ramen-system \
  $(terraform -chdir=deploy/terraform/aws-satellite output -raw helm_set)
kubectl -n ramen-system get ingress edge           # ADDRESS is the satellite host after a few minutes
SATCERT=$(terraform -chdir=deploy/terraform/aws-satellite output -raw certificate_arn)
aws acm get-certificate --certificate-arn $SATCERT --region $SAT --query Certificate --output text > ramen-$SAT.pem
```

`helm_set` expands to `--set provider=aws,region=$SAT --set aws.certificateArn=<arn>,aws.albGroup=ramen,aws.consoleGroup=ramen-console`.
The chart installs a catch-all Ingress named `edge` (a fixed `404`, so the load balancer and its hostname exist
before the first zone), creates the console's two ClusterRoles and binds `ramen-console` to the group the access
entry names (`ramen-console-zone` is bound per zone namespace by the console). The
certificate covers `*.$SAT.elb.amazonaws.com`; clients of the satellite's zones pass its PEM with `--ca` /
`--cacert`, as they do the home one. Keep one Terraform state per satellite (a copy of the directory per region).
Switch `kubectl` back to the home cluster afterwards (`aws eks update-kubeconfig --name ramen --region $REGION`).

On the home cluster, hand the console chart that group, so the RoleBinding the console writes in each satellite zone
namespace names it too; without it every satellite deploy is a `403` inside the zone namespace:

```sh
helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values \
  --set-json 'console.k8sSubjects=[{"kind":"Group","name":"ramen-console"}]'
```

Then in the console, as a super admin: **Config → Clusters → Add satellite** (the provider is the console's cloud, `aws`): region `$SAT`,
cluster name (`terraform output cluster_name`, `ramen-$SAT` by default) and the Ingress address as host. From now
on the location picker offers that region's availability zones too, a zone placed there gets its namespace, IAM
role and Ingresses on the satellite, and its clients call `https://<host>/mcp` with the same headers and keys; the
zone pages and the sign-in guide name the right host per zone.

Caveats:

- **NetworkPolicies.** The satellite module turns on the same network-policy agent and the same pod subnets
  (D89), and a satellite zone's `worker` policy admits the subnets in the home console's `RAMEN_AWS_ALB_CIDRS`.
  The satellite's own load balancer comes from the satellite's public subnets, so list them after the home ones
  and redeploy the satellite's zones:
  `helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set console.env.RAMEN_AWS_ALB_CIDRS=10.42.0.0/24\,10.42.1.0/24\,10.43.0.0/24\,10.43.1.0/24`
  (the home `alb_cidrs` output, then each satellite's). The home console's calls to a satellite worker leave the
  home VPC, so the VPC CNI rewrites their source to the home node's address, which is in the home public subnets.
  A pre-D89 `console.env.RAMEN_AWS_VPC_CIDR` still works but admits whole VPCs.
- **The console at its public URL.** A satellite cannot resolve the home cluster's `console` Service, so a satellite
  zone's LLM judge proxy calls and a sleeping zone's wake go to the console's public URL (the zone's OAuth issuer:
  the base URI or `RAMEN_PUBLIC_URL`), verified against the public roots next to the install CA. The default
  self-signed ALB certificate is in neither, so on AWS that needs a publicly trusted certificate on the console's
  load balancer (source reading; not run live).
- **Telemetry.** The Config page's one OTLP endpoint must be reachable from every cluster. The chart's try-out
  collectors (Jaeger, Phoenix) live in the home cluster and are cluster-local, so a satellite's workers log
  `telemetry export failed` until you point the endpoint at a collector they can reach.
- **Cost.** A second EKS control plane ($0.10 an hour), its nodes (`node_count = 2` `t3.small` by default,
  one per availability zone; a zone with its canary needs two in its zone), a second load balancer, its
  CloudWatch log group `/aws/containerinsights/ramen-<region>/application`, and inter-region data transfer over
  the peering for the console's calls (cents).
- **Clean-up order.** Delete the satellite's zones in the console first (that frees their namespaces, roles and
  Ingresses and lets the Clusters card drop the satellite), then `helm uninstall ramen-satellite -n ramen-system`
  on the satellite (releases the load balancer), then `terraform -chdir=deploy/terraform/aws-satellite destroy`
  (it runs the same ENI and security-group sweeps as the home destroy). The web ACL `ramen` the console creates in
  the satellite region is not Terraform's; delete it as in Teardown.

## Teardown

```sh
# in the console: delete the group first, which removes its namespaces, IAM roles and WAF IP sets
helm uninstall ramen -n ramen-system                 # releases the load balancer and the PVCs' EBS volumes
terraform -chdir=deploy/terraform/aws destroy
# the console's own secrets (config values, including copies of LLM provider keys) are not Terraform's:
for id in $(aws secretsmanager list-secrets --region $REGION --filters Key=name,Values=ramen-console/ \
              --query 'SecretList[].Name' --output text); do
  aws secretsmanager delete-secret --region $REGION --secret-id "$id"   # 30-day recovery window, restorable
done
```

The console writes its own settings' secret values (identity-provider and SMTP secrets, LLM provider keys, the
GitHub App key, the install CA's key) to Secrets Manager as `ramen-console/console-…`, outside Terraform, so `terraform destroy` leaves
them. The loop deletes them with the default recovery window: they stop billing at once and can be restored
(`aws secretsmanager restore-secret`) for 30 days. It matches the name prefix only, so run it only when no other
Ramen install shares the account and region.

`terraform destroy` also cleans up two things EKS can leave behind (0.7.5). The first is a terminated node's network
interface (`aws-K8S-i-…`), which blocks deleting the subnets. The second is the `eks-cluster-sg-ramen-…` security
group, which blocks deleting the VPC. `deploy/terraform/aws/scripts/eni_sweep.sh` runs at the right point in the
destroy and needs the `aws` CLI, as the Helm provider already does. An install applied before 0.7.5 gets these steps on
its next `terraform apply`. If a destroy still stops on `DependencyViolation`, run the script by hand and destroy again:

```sh
bash deploy/terraform/aws/scripts/eni_sweep.sh <vpc id> us-east-1                    # node interfaces
bash deploy/terraform/aws/scripts/eni_sweep.sh <vpc id> us-east-1 cluster-sg ramen   # cluster security group
```

Check for leftovers: load balancers and target groups, EBS volumes tagged `app=ramen`, the WAF web ACL and IP sets, CloudWatch log groups, the
ECR images, the ACM certificate, IAM roles under `/ramen/` and Secrets Manager entries under `ramen/` (group secrets; a deleted group removes its own) and `ramen-console/`. If the
console was down when a group was deleted, its per-zone roles survive: `aws iam list-roles --path-prefix /ramen/`
lists them and `aws iam delete-role` removes each one once its inline policy is gone. The web ACL `ramen` is created by the console, not
by Terraform, and is billed monthly:

```sh
aws wafv2 delete-web-acl --scope REGIONAL --name ramen --id <id> --lock-token <token>
```

## Gotchas

- `curl -k` skips hostname verification; a real gRPC client does not. Export the certificate from ACM as above
  and pass it with `--ca`.
- The console's own range must be inside any IP lock, because a deploy smoke-tests the worker as an ordinary
  caller.
- An IP lock blocks health probes from outside the range at the WAF too: `grpc.health.v1.Health` from your laptop
  fails while the workers are healthy.
- A WAF block looks like the node's own denial: gRPC `PERMISSION_DENIED` with "permission denied". If the worker's
  access log has no `denied` line for the call, the edge blocked it; the web ACL's sampled requests show it.
- The `Admin` gRPC service is not routed through the load balancer by design. A smoke test that tries it sees the
  load balancer's `464`.
