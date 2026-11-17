# IAM and network on GCP

Every identity a GKE install of Ramen uses, what each may do, and every network path, read from
`deploy/terraform/gcp/main.tf`, `deploy/terraform/gcp-satellite/`, the chart `deploy/helm/ramen` and the console's GCP
adapter (`console/src/ramen_console/cloud/gcp*.py`), which creates the per-group and per-zone pieces at run time.
Terraform creates nothing per group. The install itself: [Deploy on GCP](deploy-gcp.md).

## Identities at a glance

| Identity | Kind | Created by | Used by |
|---|---|---|---|
| `ramen-console@<project>` | Google service account (GSA), Workload Identity for KSA `ramen-system/console` | Terraform | the console pod |
| `ramen-nodes@<project>` | GSA, the Autopilot node identity | Terraform (honoured at cluster creation) | GKE nodes |
| `ramen-<group>-<zone>@<project>` (`ramen-<24 hex of its sha1>@` when the name exceeds GCP's 30 characters) | GSA, Workload Identity for KSA `ramen-<group>-<zone>/worker` | the console, when a zone is bound to a group | that zone's worker pods |
| KSA `ramen-system/ramen-build-<group>` | Kubernetes SA used directly as a Workload Identity Federation principal (no GSA) | the console, on the group's first image build | the group's image-build Job |
| `ramen-evals@<project>` | GSA, Workload Identity for KSA `ramen-system/ramen-evals` | Terraform | the evals and SkillSpector scan Jobs |

## The console

Project roles on `ramen-console@`:

| Role | Why |
|---|---|
| `roles/secretmanager.admin` | create `ramen-<group>-*` secrets and the console's own `ramenc-…` secrets, and set per-secret accessor bindings |
| `roles/container.developer` | reach the cluster API (and a satellite cluster's) |
| `roles/logging.viewer` | the Logs page reads Cloud Logging |
| `roles/iam.serviceAccountCreator`, `roles/iam.serviceAccountDeleter` | the per-zone worker GSAs |
| `roles/compute.securityAdmin`, `roles/compute.loadBalancerAdmin` | Cloud Armor policies for IP rules, backend services for rebalance |
| `roles/datastore.user` | Firestore, the console's state |
| custom `ramenConsoleSaIam` | `iam.serviceAccounts.getIamPolicy`, `setIamPolicy`, `get`, `list`, `resourcemanager.projects.get`: the Workload Identity binding on a worker GSA |
| custom `ramenConsoleBuildRepoCreate` | `artifactregistry.repositories.create`: a group's image repository |
| custom `ramenConsoleBuildRepos` | `artifactregistry.repositories.get`, `update`, `delete`, `getIamPolicy`, `setIamPolicy` on the group repositories |
| `roles/aiplatform.user` | only with `console_vertex = true` (the default): the judge proxy calls Vertex for LLM rails and evals |
| `roles/resourcemanager.projectIamAdmin` | only with `console_project_iam = true` (default off): bind project-wide roles from approved permission requests |

On one resource: `roles/storage.admin` on the groups bucket `ramen-<project>-groups` (sync the repos, and bind each
worker's prefix-conditioned read). The console holds **no** `iam.serviceAccountUser` (it never acts as another
account; IAM conditions cannot name service accounts, so the role would cover every account in the project) and no
Artifact Registry upload permission.

Two limits are enforced by the console, not by IAM, and are worth knowing:

- `ramenConsoleSaIam` is project-wide: the console only ever addresses `ramen-<group>-<zone>@` accounts, but IAM
  would let it set the policy of any service account in the project.
- The two Artifact Registry roles are project-wide too. Artifact Registry ignores IAM Conditions on `resource.name`
  (a `ramen-groups-*` condition never matched on GKE), so a compromised console could grant itself writer on the
  platform repository `ramen`. Platform images can be pinned by digest (`image.digest`, `image.workerDigest`), so a
  re-tag there does not reach a pod until an operator changes the digest.

Inside the cluster (chart `rbac.create`):

| Object | Rights |
|---|---|
| ClusterRole `ramen-console` (bound to `ramen-system/console`) | namespaces, NetworkPolicies, HTTPRoutes, GKE `HealthCheckPolicy` / `GCPBackendPolicy`: get, list, create, patch, delete; `ServiceNetworkEndpointGroup`: get, list, patch (clear the NEG finalizer of a deleted zone); RoleBindings; `bind` on the ClusterRole `ramen-console-zone` only |
| ClusterRole `ramen-console-zone` (bound by the console in each `ramen-<group>-<zone>` namespace) | ServiceAccounts, Secrets, Services, ConfigMaps (the CA bundle `ramen-ca`, D86), Deployments (+ scale), HorizontalPodAutoscalers: full; Pods, `pods/log`, Endpoints: read |
| Role `ramen-console-build` in `ramen-system` | ConfigMaps get/create/patch/delete; Secrets create/delete (never read); Jobs get/list/create/delete; Pods and `pods/log` read; ServiceAccounts create/patch/delete (the build KSAs) |
| Role `ramen-console-tls` in `ramen-system` (D86) | Secrets `ramen-tls-{redis,phoenix,jaeger,promptfoo}` and ConfigMap `ramen-ca`: get/patch/update; Secrets and ConfigMaps: create (RBAC cannot narrow `create` by name); Deployments `redis`, `phoenix`, `jaeger`, `promptfoo`: get/patch (restart after a renewal) |

## Worker identities

Each zone's workers run as `ramen-<group>-<zone>@`. The console grants:

- `roles/storage.objectViewer` on the groups bucket under the condition
  `resource.name.startsWith("projects/_/buckets/<bucket>/objects/<group>/") || resource.name == "projects/_/buckets/<bucket>"`.
  The second clause is listing, which Cloud Storage cannot narrow to a prefix: a worker can see other groups'
  object **names**, never their contents.
- `roles/secretmanager.secretAccessor` on each of the group's secrets (`ramen-<group>-…`), never on the project.
- `roles/iam.workloadIdentityUser` on the GSA for `serviceAccount:<project>.svc.id.goog[ramen-<group>-<zone>/worker]`.

Nothing else until a group admin requests a permission and another admin approves it
([service-account permissions](users-access.md#service-account-permissions)); project-wide ones need
`console_project_iam`. No worker holds an AI role for guardrails: LLM rails reach a provider only through the
console's judge proxy.

## Image builds, evals and scans

- **Builds (D85).** Each group gets an Artifact Registry repository `ramen-groups-<group>`. Its only writer is the
  principal `principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<project>.svc.id.goog/subject/ns/ramen-system/sa/ramen-build-<group>`,
  bound by the console. Terraform gives the whole `ramen-system` namespace (principal set) `roles/artifactregistry.reader`
  on the platform repository `ramen` (every build's `FROM` is the worker base image). No build identity can write
  `ramen`, so no group's build can overwrite the console, the worker base, SkillSpector or another group's image. The
  build KSA mounts no API token.
- **Evals and scans.** `ramen-evals@` holds `roles/logging.logWriter` and nothing else; the KSA mounts no API token
  and is bound to no Role. A group's promptfoo config runs here, so it cannot reach the registry, the bucket or any
  secret. Provider keys reach the Job only as its own env for that run.

## Nodes

`ramen-nodes@` holds `roles/logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`,
`stackdriver.resourceMetadata.writer`, `autoscaling.metricsWriter` (what `roles/container.defaultNodeServiceAccount`
bundles) and `roles/artifactregistry.reader` project-wide (platform and group repositories). Autopilot takes the node
account only at cluster creation; a cluster made before 1.0.0c keeps the default compute account (which Google
creates with `roles/editor`) until re-created — set `node_service_account = "default"` for it, and remove `editor`
from that account once the nodes run as `ramen-nodes@` ([Deploy on GCP](deploy-gcp.md#the-consoles-service-account-and-access-controls)).

## Network

| Path | From → to | Port | Control |
|---|---|---|---|
| Clients → edge | internet → global external HTTPS load balancer (GKE Gateway `gke-l7-global-external-managed`) | 443 only | Google-managed certificate on `<ip>.sslip.io` (or your hostname); plain http is not served |
| Edge → console | Google front ends `35.191.0.0/16`, `130.211.0.0/22` → console pods | 8000 | TLS (D86): Service `console` port 443 `appProtocol: HTTPS` → pod 8000, HealthCheckPolicy `HTTPS` on `/healthz` (the load balancer encrypts but does not verify the backend certificate); NetworkPolicy `console` |
| Edge → workers | the same ranges → worker pods (NEG) | 8080 | TLS: Service `worker` `appProtocol: HTTP2`, HealthCheckPolicy `HTTP2` on `/readyz`; per-zone HTTPRoute matching `ramen-group` + `ramen-zone`; Cloud Armor policy `ramen-<group>` (IP rules); NetworkPolicy `worker` |
| Console → workers | `ramen-system` → worker pod IPs | 8080 | gRPC over TLS, verified against the install CA as `worker.ramen.internal`; NetworkPolicy `worker` admits the `ramen-system` namespace; admin key |
| Workers → console | worker pods → `https://console.ramen-system.svc.cluster.local:443` → console pods | 8000 | TLS (install CA); `worker-egress`: `/otlp`, wake, `/llm/v1` judge proxy |
| Workers → Redis | → `ramen-system` Redis (`redis.enabled`) | 6379 | TLS only (`rediss://`); `worker-egress`; NetworkPolicy `redis` (console + workers); password |
| Workers → collector | → Phoenix / Jaeger | 4318 | OTLP over TLS; `worker-egress`; collector policy (console, workers, Jobs) |
| Workers → internet | → anything but the cluster's Service range | any | `worker-egress` `0.0.0.0/0 except cluster.serviceCidr` |
| Activator → workers | same zone | 8080 | NetworkPolicy `worker`, second rule |
| Home → satellite | home pod range → satellite nodes | 8080 | firewall `ramen-home-to-satellite-<region>` (`home_pod_cidr`, target tag `node_tag`) |

- **ramen-system policies** (`networkPolicy.system`, default on): the console accepts its port from the edge, the
  worker namespaces and the evals/scan Job pods; the collector takes 4318 from the console, workers and Jobs and its
  UI only from the edge ranges (it has no route, so in practice a port-forward); promptfoo takes its TLS front on pod
  port 3443 (Service port 3000) from the edge, the console and the Jobs; Redis takes 6379 from the console and workers.
- **Worker policies** are written by the console into each zone namespace: `worker` (ingress on 8080 from
  `ramen-system`, the load balancer ranges, `console.sourceCidrs`, and the zone's own pods) and `worker-egress`
  (`networkPolicy.workerEgress`, default on; needs `cluster.serviceCidr` = `gcloud container clusters describe … --format='value(servicesIpv4Cidr)'`,
  without it there is no egress policy and the chart's NOTES say so).
- GKE Autopilot enforces NetworkPolicies (Dataplane V2). It does not admit pod sources through `ipBlock` rules, which
  is why the console's pod range in `console.sourceCidrs` does not open the `ramen-system` pods to every pod here; on a
  CNI that does match pod IPs by `ipBlock` it would ([Threat model](../threat-model.md#findings-and-their-status)).
- **IP rules** per zone are enforced twice: by the node (`RAMEN_ALLOWED_CIDRS`, checked against the address the load
  balancer appended) and by Cloud Armor at the edge (one policy per group on its zones' backend services). Include the
  console's own range or a redeploy's canary smoke is refused.
- The cluster's control-plane endpoint is public (Terraform sets no authorized networks); restrict it for production.
- **Satellites** sit on the same default VPC; pod ranges route, and the firewall rule above lets the console reach
  satellite workers by pod IP. The satellite chart binds the ClusterRole `ramen-console` to the console GSA as a
  Kubernetes `User`; the console binds `ramen-console-zone` per zone namespace to the subjects in the home chart's
  `console.k8sSubjects`, and every worker NetworkPolicy admits the home pod range from `console.sourceCidrs`. Telemetry from a satellite reaches the chart's collector only if you expose one
  ([Deploy on GCP → Satellite clusters](deploy-gcp.md#satellite-clusters-other-regions)).

## Terraform switches that change IAM

| Variable | Default | Effect |
|---|---|---|
| `console_project_iam` | `false` | grants the console `roles/resourcemanager.projectIamAdmin` so approved project-wide permissions can be bound |
| `console_vertex` | `true` | grants the console `roles/aiplatform.user` for the judge proxy |
| `node_service_account` | `""` (= `ramen-nodes@`) | `"default"` keeps the default compute account on a pre-1.0.0c cluster |
| `otel_collector`, `evals_engine`, `skillspector_engine` | `none` | the optional engines (no IAM of their own) |
