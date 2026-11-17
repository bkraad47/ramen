# Deploy on GCP

A regional GKE Autopilot cluster, Firestore, a bucket, Secret Manager and a global HTTPS load balancer with a
Google-managed certificate on a free `sslip.io` hostname. About 35 minutes, most of it waiting: Terraform
about ten (the Autopilot cluster), then the Google-managed certificate turns active 22–25 minutes after the apply
(measured on the 0.7.5 and 0.8.0 cloud passes). Each release's cloud pass runs this way on a throwaway project and tears it down afterwards ([the record](transport.md#what-is-verified-and-what-is-not)).

Cost while it runs: the Autopilot pods, billed by their CPU and memory requests (the console, Redis, the optional
engines and the workers), the cluster management fee once the billing account's GKE free tier is used, the load
balancer's forwarding rule, the static IP and Artifact Registry storage. Current rates:
[GKE pricing](https://cloud.google.com/kubernetes-engine/pricing) and
[network pricing](https://cloud.google.com/vpc/network-pricing); `scripts/gcp_cost_check.sh` lists what is billable.

## Prerequisites

`gcloud` logged in with billing linked, `terraform` 1.6 or newer, `helm` 3.14 or newer (`--reset-then-reuse-values`), `kubectl` with
`gke-gcloud-auth-plugin`, `docker` with buildx, `grpcurl`, `uv`, `openssl`, `python3`.

## 1. Project and credentials

```sh
export RAMEN_GCP_PROJECT=ramen-test-$(date +%y%m%d) REGION=us-central1
export RAMEN_BILLING_ACCOUNT=<your billing account id>
scripts/gcp_test_project.sh create        # skip this and set RAMEN_GCP_PROJECT to a project you already have
PROJECT=$RAMEN_GCP_PROJECT; gcloud config set project $PROJECT
gcloud auth application-default login     # Terraform; or export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
```

The script reads `RAMEN_GCP_PROJECT` and `RAMEN_BILLING_ACCOUNT`, and refuses to run without the billing account.
A deleted project keeps its id for 30 days, so a second run on the same day needs another id
(`ramen-test-$(date +%y%m%d)b`); the script refuses an id whose project is pending deletion and says so.

## 2. Infrastructure

```sh
cd deploy/terraform/gcp && cp terraform.tfvars.example terraform.tfvars   # set project and region
terraform init && terraform apply
cd ../../..
```

Terraform creates the cluster, Firestore, the Artifact Registry repo, the groups bucket, a static IP, the
certificate map, and the console's service account with the access described below — including the right to create
each group's own image repository for [image builds](worker-images.md#per-group-images-100c-d85). It takes about ten
minutes. The outputs you need later are `public_hostname`, `certificate_map`, `artifact_repo`, `builder_set`,
`evals_gsa` and `engines_set`.

## 3. Images

Every release publishes `docker.io/bkraad47/ramen-console` and `ramen-worker` (also on `ghcr.io/bkraad47/`;
linux/amd64 and arm64; tags `<version>` and `latest`). To run those, skip the build and add two values to the Helm
command in the next step:

```sh
V=$(cat VERSION)
#  --set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V \
#  --set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V
```

To build and push your own into the project's Artifact Registry instead, which is what the release verification
runs did:

```sh
make push PROJECT=$PROJECT REGION=$REGION
```

A push from a Colima or Lima Docker can fail with `broken pipe` on one layer. If it does, `docker save` the image
and push it with `crane`.

## 4. The console

```sh
gcloud container clusters get-credentials ramen --region $REGION
HOSTNAME=$(terraform -chdir=deploy/terraform/gcp output -raw public_hostname)
CERTMAP=$(terraform -chdir=deploy/terraform/gcp output -raw certificate_map)
EVALS=$(terraform -chdir=deploy/terraform/gcp output -raw evals_gsa)
kubectl create ns ramen-system
kubectl label ns ramen-system ramen.io/routes=true      # the Gateway admits routes from labelled namespaces only
helm upgrade --install ramen deploy/helm/ramen -n ramen-system \
  --set project=$PROJECT,region=$REGION \
  --set gateway.certificateMap=$CERTMAP \
  $(terraform -chdir=deploy/terraform/gcp output -raw builder_set) --set evals.gsa=$EVALS \
  $(terraform -chdir=deploy/terraform/gcp output -raw engines_set) \
  --set cluster.serviceCidr=$(gcloud container clusters describe ramen --region $REGION --format='value(servicesIpv4Cidr)') \
  --set console.env.RAMEN_PUBLIC_URL=https://$HOSTNAME \
  --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18) \
  --set console.secrets.RAMEN_FERNET_KEY=$(python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())') \
  --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)
kubectl -n ramen-system rollout status deploy/console
curl https://$HOSTNAME/readyz        # {"ok":true,"store":"firestore","version":"<version>"} once the certificate is active
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

`RAMEN_PUBLIC_URL` is the address people use and the OAuth
issuer the workers trust. `builder_set` (`--set builder.enabled=true`) is what lets a deploy build a group's worker
image from its `requirements.txt` into the group's own repository ([Worker images](worker-images.md)); leave it out
and no image is ever built, workers install requirements when they load. Add `--set image.digest=sha256:…` and
`image.workerDigest=sha256:…` to run the platform images by digest. `evals.gsa` is the identity the evals and scan Jobs run as: it can write logs and
nothing else, so a group's promptfoo config cannot reach the registry, the bucket or any secret. `cluster.serviceCidr`
is what lets the console write the `worker-egress` policy into every zone; without it there is none and the chart's
NOTES say so ([IAM and network on GCP](iam-network-gcp.md#network)). The Gateway reports `PROGRAMMED` after a few minutes; the managed certificate
turned `ACTIVE` 22–25 minutes after the Terraform apply on the measured runs, and the forwarding path answers a
little later still: poll, do not judge early.

**Optional engines.** `otel_collector = "jaeger" | "phoenix"`, `evals_engine = "promptfoo"` and
`skillspector_engine = "skillspector"` in `terraform.tfvars` (all `none` by default) become the chart's
`otel.collector`, `evals.engine` and `skillspector.engine` through `engines_set`: a try-out trace UI
([Telemetry](telemetry.md)), the promptfoo server every evals run is shared with ([Evals](evals.md)), and
SkillSpector scans of every group's skills ([SkillSpector](skillspector.md)). Each lives in `ramen-system` without an
edge route (`kubectl port-forward`), behind the install's engines sign-in (Secret `ramen-engines`, D92;
[Trust the install CA](trust-ca.md)); turning one on changes only that engine's objects.

## 5. The first zone, group and deploy

Open `https://<public_hostname>/` and sign in as `admin@ramen.local` with the password you generated. The
certificate is publicly trusted, so there is no warning.

1. **Groups → Create group**: name `demo`, repo `https://github.com/bkraad47/ramen-demo-mcp-group`, ref `main`.
2. **Environments, Zones and Workers → New environment**: name `prod`, ref `main`, tick group `demo`.
3. **New zone** on the same page: name `a`, tick environment `prod`, location `us-central1-a` (the GCP zone the
   workers pin to), **Add GCP zone**. This creates the namespace `ramen-demo-a`, its service account with Workload
   Identity, the Service with a network endpoint group, a route that matches the `ramen-group: demo` and
   `ramen-zone: a` headers, and starts the first canary deploy of `demo/prod` there (the job is on the group page;
   its log reads sync, canary, reload, smoke, stable).
4. Accept the group's LLM configs at **/groups/demo/configs** (the demo repo ships `mcp/llm-config/`; without
   that the key is refused with `403 configs not accepted`), then **open the group → MCP auth keys → Generate key**. The key is shown once; the console pushes it to the
   running workers at once (1.0.0, D92), no deploy needed.

<figure markdown>
![The group page on GCP](../img/group.png){ .ramen-shot }
<figcaption>The group page once both zones are up: the repo, the throttle, the environment and its deploy jobs.</figcaption>
</figure>

The route takes a few minutes to appear (see Gotchas). Then:

```sh
curl -s https://$HOSTNAME/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: a' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
```

The demo repo ships `mcp/llm-config/`, so the workers refuse a call that does not carry the accepted configs
version (`-32028 configs required`): add `"_meta": {"io.ramen/configs": {"version": "<v>"}}` to `params`, with `<v>` from
`/.well-known/ramen-policy?group=demo&zone=<zone>` ([Deploy your first group → Call a tool](../first-group.md#8-call-a-tool)). The bridge
does this by itself.

`RAMEN_NODE_TLS=1 scripts/cloud_smoke.sh https://$HOSTNAME admin@ramen.local '<password>' '<admin key>' $HOSTNAME:443`
runs the same steps (zone `a`, environment `dev`) and prints PASS or FAIL for each. The last argument is the gRPC
target; `RAMEN_NODE_TLS=1` makes it speak TLS to the load balancer (without it `grpcurl` dials port 443 in plaintext
and fails). The script needs `grpcurl`.

## The console's service account and access controls

Every identity and network path in one place: [IAM and network on GCP](iam-network-gcp.md).

Terraform gives the console's identity `ramen-console@<project>` the least it needs:

| It may | How |
|---|---|
| Create, delete and bind the per-zone service accounts | `roles/iam.serviceAccountCreator`, `serviceAccountDeleter`, and a custom role with `getIamPolicy`/`setIamPolicy` on service accounts (the Workload Identity binding). No `serviceAccountUser`: the console never acts as or mints tokens for another account, and IAM conditions cannot name service accounts, so the role would have covered every account in the project. The limit to `ramen-<group>-<zone>` accounts is enforced by the console, not by IAM |
| Read and write the groups bucket | `roles/storage.admin` on that one bucket; it is also what lets the console bind each worker's `roles/storage.objectViewer` under an IAM condition |
| Manage group secrets | `roles/secretmanager.admin`, used only on secrets named `ramen-<group>-*` |
| Use Firestore and read logs | `roles/datastore.user`, `roles/logging.viewer` |
| Program the load balancer and Cloud Armor | `roles/compute.loadBalancerAdmin`, `roles/compute.securityAdmin` |
| Reach the cluster API | `roles/container.developer` project-wide; inside the cluster a ClusterRole for namespaces, network policies and routes, and a RoleBinding to the ClusterRole `ramen-console-zone` in every zone it attaches |
| Call Vertex AI for LLM rails and evals (§38.1) | `roles/aiplatform.user` project-wide, Terraform variable `console_vertex` (default `true`). Rails reach a provider only through the console's judge proxy, so no worker holds an AI role unless an `ai.user` request is approved for tool code |
| Create the per-group image repositories (D85) | custom role `ramenConsoleBuildRepoCreate` (`artifactregistry.repositories.create`, `list`, `createTagBinding`, checked before a repository carries a tag) and `ramenConsoleBuildRepos` (get, update, delete, get/setIamPolicy) under an IAM condition on the resource tag `ramen-repo=group`, plus Tag User on that tag value only. Artifact Registry ignores conditions on `resource.name` but evaluates tags: Terraform tags the platform repository `ramen` with `ramen-repo=platform`, and a resource holds one value per key, so the console cannot bring `ramen` under its rights. It holds no upload right: it makes `ramen-groups-<group>`, tags it `group`, waits for IAM to see the tag (up to ~7 minutes on a group's first build) and binds `roles/artifactregistry.writer` on it to the KSA principal `ramen-system/ramen-build-<group>` |

It cannot grant project-wide roles. A permission request that needs one, such as `logs.write`, is recorded with a
note unless you set `console_project_iam = true` in Terraform.

Each zone's workers run as `ramen-<group>-<zone>@<project>` through Workload Identity. That identity reads the
group's prefix of the bucket and the group's secrets, and nothing else until an admin requests more and another
admin approves it. [Users and access](users-access.md#service-account-permissions) covers that flow. The bucket
grant is a binding on the bucket with the condition
`resource.name.startsWith("projects/_/buckets/<bucket>/objects/<group>/") || resource.name == "projects/_/buckets/<bucket>"`
(the second clause is `objects.list`, which Cloud Storage cannot narrow to a prefix: a worker can see other groups'
object names, never their contents). No worker holds a project-wide role.

The other identities Terraform creates:

| Identity | Holds |
|---|---|
| `ramen-nodes@<project>` — the GKE nodes | `roles/logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`, `stackdriver.resourceMetadata.writer`, `autoscaling.metricsWriter` (what `roles/container.defaultNodeServiceAccount` bundles) and `roles/artifactregistry.reader` project-wide (the platform repository and every group's) |
| the `ramen-system` namespace (Workload Identity Federation principal set) — image-build Jobs | `roles/artifactregistry.reader` on the `ramen` repository (the worker base image is every build's `FROM`). Each group's build KSA `ramen-build-<group>` is, besides, the only writer of `ramen-groups-<group>`, bound by the console; there is no build service account (D85) |
| `ramen-evals@<project>` — evals and scan Jobs | `roles/logging.logWriter` |

Nodes used to run as the project's default compute service account, which Google creates with `roles/editor`. A
cluster created from 1.0.0c on gets `ramen-nodes@` at creation (satellites too). GKE refuses to change the node
account of an existing Autopilot cluster (`400 Overriding Autopilot autoscaling settings is not allowed`), so a
cluster made earlier keeps the default account until it is re-created: put `node_service_account = "default"` in
`terraform.tfvars` (what the live test cluster does), or plan a re-create (`terraform destroy` of the cluster, the
groups and secrets survive in the bucket and Secret Manager, zones are re-attached from the console).

Terraform removes `roles/editor` from the default compute account (S5; `remove_default_compute_editor`, default
`true`), because a compromised console could otherwise use its service-account `setIamPolicy` to act as a project
editor. A cluster still running as that account keeps working: with `node_service_account = "default"` (or
`default_compute_runs_nodes = true` for a satellite made before this change) the account first gets the node roles
listed above. Set `remove_default_compute_editor = false` only when something else in the project relies on the
default account's `editor` grant (Compute Engine VMs, legacy Cloud Build, Cloud Functions, App Engine).
`terraform destroy` does not give `editor` back. Check after the apply:

```sh
gcloud projects get-iam-policy $PROJECT --flatten=bindings --filter="bindings.role=roles/editor" \
  --format='value(bindings.members)' | tr ';' '\n' | grep -c compute@developer || true   # 0
```

**Upgrading an install from before this change** (1.0.0c): `terraform apply` adds the `ramen-repo` tag key and its
values, tags `ramen`, conditions the console's repository role and removes `editor`; the console of the same release
tags each existing `ramen-groups-<group>` on the group's next build. Apply terraform and upgrade the console together:
an older console cannot tag, so after the apply it no longer reaches the group repositories (a build fails with
`403` until the console is upgraded). Run `terraform plan` again after the apply: it must show no changes.

## Day 2

- **Upgrading to TLS on every hop (D86) from 1.0.0b/c**: `helm upgrade … --reset-then-reuse-values`, wait for the
  console and the chart services to restart on TLS, then **redeploy every zone** — until a zone is redeployed the
  console cannot reach it and its wake, judge, telemetry and Redis calls fail, though it still serves clients. The
  exact steps: [Configuration → Upgrading the chart](configuration.md#upgrading-the-chart).
- **Scale**: the group page's *Zone actions* card. Admins set the count; super admins set the size and the sizes
  a zone allows.
- **IP rules**: a CIDR list per zone, in the group page's *Zone actions* card. It becomes the node's own
  allow-list and a Cloud Armor policy, one per **group**, attached to each of its zones' backends. Include the
  console's own range ([Groups, zones and regions](groups-zones.md#ip-rules)).
- **Logs**: Cloud Logging for the namespace, on the Logs page. External monitors read the same logs with
  `resource.labels.namespace_name="ramen-<group>-<zone>"`.
- **Upgrade the console**: `make push`, or `--set image.tag=<version>` on the public image, then
  `kubectl -n ramen-system rollout restart deploy/console`. For a minute or two after a console restart the load balancer can answer `503` with the body
  `unconditional drop overload` (no healthy backend yet); retry. `scripts/cloud_smoke.sh` rides it out.
- **Deploy from CI**: [Deploy from GitHub Actions](deploy-github-actions.md).

## Satellite clusters (other regions)

A zone must be in a region that has a cluster ([where a zone can be](groups-zones.md#where-a-zone-can-be)). For a
zone in another region, add a **satellite**: a worker-only Autopilot cluster in that region of the same project,
with its own static IP and Google-managed certificate, driven by the console you already have. It shares the
store, the bucket, the registry and the service accounts of the home apply; images are pulled from the home
repository and builds stay in the home cluster.

The console talks to workers by pod IP (per-pod smoke, metrics, reload, canary), not through the edge, so the
satellite's network is joined to the home one. On GCP both clusters sit on the default VPC and their pod ranges
route; the module adds a firewall rule letting the home cluster's pod range in on port 8080. That range is the
module's `home_pod_cidr` input; the rule is scoped to the satellite's nodes with `node_tag`, which GKE only picks
when the cluster is created, so the first apply runs without it.

```sh
SAT=australia-southeast1
gcloud container clusters describe ramen --region $REGION --format='value(clusterIpv4Cidr)'   # home_pod_cidr
cd deploy/terraform/gcp-satellite && cp terraform.tfvars.example terraform.tfvars   # project = $PROJECT, region = $SAT, home_pod_cidr
terraform init && terraform apply                  # about ten minutes
terraform output -raw node_tag_command | sh        # gke-ramen-$SAT-<hash>-node: set node_tag in terraform.tfvars and apply again
terraform output -raw kubeconfig_command | sh      # kubectl now points at the satellite
cd ../../..
kubectl create ns ramen-system
kubectl label ns ramen-system ramen.io/routes=true
helm upgrade --install ramen-satellite deploy/helm/ramen-satellite -n ramen-system \
  $(terraform -chdir=deploy/terraform/gcp-satellite output -raw helm_set)
terraform -chdir=deploy/terraform/gcp-satellite output host
```

`helm_set` expands to `--set provider=gcp,project=$PROJECT,region=$SAT --set console.identity=ramen-console@$PROJECT.iam.gserviceaccount.com --set gateway.staticIpName=ramen-$SAT,gateway.certificateMap=ramen-$SAT`.
The chart installs the Gateway, binds the ClusterRole `ramen-console` to the console GSA as a Kubernetes `User`, and
defines `ramen-console-zone`, which the console binds in each zone namespace; the console reaches the satellite's API
server with the `roles/container.developer` the home apply already gave it.
Keep one Terraform state per satellite (a copy of the directory per region). Switch `kubectl` back to the home
cluster afterwards (`gcloud container clusters get-credentials ramen --region $REGION`) and tell the home chart about
the satellite: the console's identity there goes into every zone RoleBinding, and its home pod range into every
satellite zone's worker NetworkPolicy (a home zone never admits it; in `ramen-system` it reaches the console port
only).

```sh
helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values \
  --set-json "console.k8sSubjects=[{\"kind\":\"User\",\"name\":\"ramen-console@$PROJECT.iam.gserviceaccount.com\"}]" \
  --set "console.sourceCidrs={<home_pod_cidr>}"
```

Then in the console, as a super admin: **Config → Clusters → Add satellite**: region `$SAT`,
cluster name (`terraform output cluster_name`, `ramen-$SAT` by default) and the `host` output. From now on the
location picker offers that region's zones too, a zone placed there gets its namespace, service account and route
on the satellite, and its clients call `https://<host>/mcp` with the same headers and keys; the zone pages and the
sign-in guide name the right host per zone.

Caveats:

- **The console at its public URL.** A satellite cannot resolve the home cluster's `console` Service, so a satellite
  zone's LLM judge proxy calls and a sleeping zone's wake go to the console's public URL (the zone's OAuth issuer:
  the base URI or `RAMEN_PUBLIC_URL`), verified against the public roots next to the install CA.
- **Telemetry.** The Config page's one OTLP endpoint must be reachable from every cluster. The chart's try-out
  collectors (Jaeger, Phoenix) live in the home cluster and are cluster-local, so a satellite's workers log
  `telemetry export failed` until you point the endpoint at a collector they can reach.
- **Cost.** A second Autopilot cluster (its control-plane fee after the free tier and its pods) and a second global
  load balancer forwarding rule and static IP.
- **Clean-up order.** Delete the satellite's zones in the console first (that frees their namespaces and network
  endpoint groups and lets the Clusters card drop the satellite), then `helm uninstall ramen-satellite -n ramen-system`
  on the satellite, then `terraform -chdir=deploy/terraform/gcp-satellite destroy`. The Clusters card refuses to
  remove a satellite while a zone uses its region, and destroying the cluster with zone namespaces still
  terminating leaves network endpoint groups behind (Gotchas).

## Teardown

```sh
# in the console: delete the group, which removes its namespaces, service accounts, routes, Cloud Armor policy and image repository
CTX=$(kubectl config current-context)                # check it is this install's cluster first
helm --kube-context "$CTX" uninstall ramen -n ramen-system --wait   # releases the Gateway's forwarding rule and the static IP
terraform -chdir=deploy/terraform/gcp destroy
scripts/gcp_test_project.sh delete            # deletes the whole project
```

The console keeps its own settings' secret values (identity-provider and SMTP secrets, LLM provider keys, the GitHub
App key) in Secret Manager as `ramenc-…` (label `ramen=console`), outside Terraform; deleting the project removes
them. If you keep the project, delete them after the destroy (Secret Manager has no recovery window: this is final):

```sh
gcloud secrets list --filter='labels.ramen=console' --format='value(name)' | xargs -n1 gcloud secrets delete --quiet
```

Uninstall the chart before `terraform destroy`: the Gateway's forwarding rule holds the static IP, and the destroy
stalls on it otherwise. Satellites go first (their clean-up order above). The
[`deploy-gcp` skill](skills.md) lists every leftover by name with its `scripts/leftovers.sh`: console and group
secrets, group image repositories `ramen-groups-<group>`, network endpoint groups and backend services `ramen-…`,
Cloud Armor policies, service accounts and PVC disks.

Run `terraform destroy` before deleting the project. If you skip it, the local `terraform.tfstate` still describes
the deleted project, and the next `terraform apply` in this directory plans against it: move or delete the state
file first. `scripts/gcp_cost_check.sh` lists anything billable that is left.

## Gotchas

- The Gateway programs a new route in two to seven minutes. Until then the zone's `/mcp` answers `404` from the
  console.
- A dropped zone's `/mcp` answers `503` for a short while, then `404` once its route is gone.
- HTTPRoutes you add yourself (a [path prefix](configuration.md#behind-a-reverse-proxy-or-a-path-prefix), say)
  share the Gateway with the console's. GKE rejects a `URLRewrite` of type `ReplaceFullPath`; use
  `ReplacePrefixMatch`. A rejected route stops every route on the Gateway from reconciling, the worker routes the
  console writes included, until it is fixed or deleted.
- A dropped zone's namespace would sit in `Terminating` for ten minutes or more while the Gateway controller
  garbage-collects its backend service. Since 1.0.0b the console clears the NEG finalizer itself, so the namespace
  goes at once and the same zone can be created again straight away; the zone's network endpoint groups are
  deleted in the background once the controller lets go of them (up to fifteen minutes; the console log says
  `network endpoint groups reaped`). If a backend service never clears, find it with
  `gcloud compute backend-services list --filter=ramen-<group>-<zone>` and delete it by hand.
- Deleting a cluster while group namespaces are still terminating can leave that cluster's network endpoint groups
  behind. A new cluster in the same project then cannot use the same names: the worker backend never becomes
  healthy, and the `ServiceNetworkEndpointGroup` reports `conflicting description`. Wait for the namespaces to go
  before deleting the cluster, or delete the leftover groups (`gcloud compute network-endpoint-groups list`).
- Gateway-managed backend services report "not ready" for minutes after a rollout. Rebalance and IP rules answer
  `applied: false` with a note and retry in the background.
- A client that omits the two headers gets a `404` from the Gateway, not a worker error.
