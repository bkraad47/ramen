# Ramen interface contracts (v0.3.0)
Binding for all components. Change only via a PR that updates this file and every implementer.

## 1. Group repo contract (what users write)
See `ramen-demo-mcp-group/README.md`. `mcp/{tools,resources,prompts}/<name>/<name>.{py,json}`, `utils/` importable, `mcp/requirements.txt`.
Proto JSON: `{type, name, description, callable, input:{param:{type|enum, description?}}, output:{type}, error:{type}}`.
Resources add `uri`, `mime_type`. Prompts have no `callable`; they have `skill` (SKILL.md) and `settings` (settings.json); `input` params become prompt arguments and `{{param}}` in SKILL.md is substituted.
Validation errors (folder≠name, type mismatch, bad type, missing callable) are reported per package; valid packages still load.
Secrets: any string argument containing `{{$<group>.<VAR>}}` is substituted by the runtime before the call; code may call `ramen_runtime.secrets.resolve(text)`. Values come from env vars `RAMEN_SECRET_<GROUP>__<VAR>` (upper-cased) injected by the deployer. Never logged.

## 2. Sidecar protocol: node-rs ↔ runtime-py
Transport: newline-delimited JSON-RPC 2.0 over the child's stdin/stdout. Node spawns `python -m ramen_runtime --bucket <dir>`. Stderr = runtime logs (JSON lines).
Methods (params → result):
- `runtime.load` `{bucket}` → `{tools:[{name,description,inputSchema}], resources:[{uri,name,description,mimeType}], prompts:[{name,description,arguments:[{name,description,required}]}], errors:[{package,reason}]}`
- `runtime.call_tool` `{name, arguments}` → `{content:[{type:"text",text}], isError:bool}`
- `runtime.read_resource` `{uri}` → `{contents:[{uri,mimeType,text}]}`
- `runtime.get_prompt` `{name, arguments}` → `{messages:[{role:"user",content:{type:"text",text}}]}`
- `runtime.ping` `{}` → `{ok:true}` ; `runtime.shutdown` `{}` → `{ok:true}`
Node kills the sidecar after `RAMEN_SIDECAR_IDLE_SECS` (default 300) idle and respawns on demand. inputSchema is JSON Schema derived from proto `input` (`number`→number, `string`, `integer`, `boolean`, `enum`→enum of strings; all params required).

## 3. Node surface (node-rs) — v0.3.1: gRPC, see §11 (the HTTP endpoints below were removed)
Port `RAMEN_NODE_PORT` (default 8080). Since v0.3.1 the port serves gRPC (§11: `ramen.v1.Mcp/Call`, `ramen.v1.Admin/{Reload,Metrics}`, `grpc.health.v1.Health`); the method list, metrics fields, reload semantics, auth/CIDR/config rules below still apply, carried in gRPC metadata instead of HTTP headers.
- `POST /mcp` → §11 `Mcp/Call` (one JSON-RPC 2.0 message per call). Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`. JSON responses only (no SSE) in v0.1.0. Protocol version `2025-06-18`.
- `GET /healthz` / `GET /readyz` → §11 `grpc.health.v1.Health/Check` (`""`/`ramen.v1.Mcp` SERVING once `runtime.load` succeeded; `ramen.v1.Admin` = process alive).
- `GET /metrics` → §11 `Admin/Metrics`, JSON `{inflight, total, errors, load: "low|even|high", sidecar_alive, loaded_at, packages:{tools,resources,prompts,errors}}`. load = low <30% of `RAMEN_MAX_INFLIGHT`, high >80%.
- `POST /admin/reload` → §11 `Admin/Reload` (metadata `x-ramen-admin-key`) → re-run pip install for `requirements.txt` + `runtime.load`. Returns the load result.
Auth: metadata `authorization: Bearer <key>` (constant-time compare); keys in `RAMEN_MCP_KEYS` (comma list) reloaded on `Admin/Reload`; unauth → UNAUTHENTICATED (§11). `RAMEN_ALLOWED_CIDRS` (comma list, default 0.0.0.0/0) → PERMISSION_DENIED outside, applies to `Mcp/*` only. `Admin/*` is gated by `RAMEN_ADMIN_KEY` plus the separate `RAMEN_ADMIN_CIDRS` (default any) so an MCP IP lock can never block the console's deploys. `RAMEN_VERBOSE=1` logs full request/response bodies; otherwise one JSON line per call: `ts, ip, group, method, name, status, ms, key_id`. Group name from `RAMEN_GROUP`, zone from `RAMEN_ZONE`, environment from `RAMEN_ENV`.
Config precedence: `RAMEN_CONFIG` file (flat `KEY=value` / `key: value`) < env < `<bucket>/.ramen/env-<zone>` (or `.ramen/env`) written by the console on deploy. The deploy file may only set deploy-scoped keys (MCP keys are unioned, group/env/zone labels, verbose, timeouts); it can never change bucket, port, python path, admin key, or proxy trust. Extra env: `RAMEN_LOG_FILE` (log mirror the console Logs page tails), `RAMEN_CALL_TIMEOUT_SECS`. No `RAMEN_MCP_KEYS` and no deploy file = deny all.

## 4. Console (console/)
Port `RAMEN_CONSOLE_PORT` (default 8000; TLS terminated by ingress/LB; local compose serves https on 8443 with a self-signed cert).
Storage interface `ramen_console.storage.base.Store` (async): `get/put/delete/list(collection, filters)` + `transaction`. Collections: `users, groups, environments, zones, workers, secrets, api_keys, audit, activity, config, backups`. Adapters: `memory` (tests), `firestore` (honors `FIRESTORE_EMULATOR_HOST`), `dynamodb` (tests use moto). Selected by `RAMEN_STORE=memory|firestore|dynamodb`. Fields marked sensitive (password hashes, secret values, API key hashes, github tokens) are Fernet-encrypted with `RAMEN_FERNET_KEY` before write.
Cloud interface `ramen_console.cloud.base.Cloud`: `sync_repo(group, repo_url, ref, token) -> bucket_uri`, `deploy(group, env, zone, canary=True)`, `rebalance(group, zone)`, `workers(group, zone) -> [{id, load, metrics}]`, `logs(group, zone, worker=None, tail=500)`, `set_ip_rules(group, zone, cidrs)`, `create_service_account(group, zone)`, `refresh()`. Adapters: `local` (filesystem bucket at `RAMEN_BUCKET_ROOT/<group>`, deploy = write env file + `Admin/Reload` on each worker via `ramen_console.grpcclient` (§11), logs = worker container stdout file), `gcp` (§7), `aws` (§8, untested on a real account). Selected by `RAMEN_CLOUD=local|gcp|aws`.
Roles: `super_admin`, `group_admin` (per group), `viewer` (per group), `mcp_user` (per group; 0.5.92, D39: may only sign in and approve an OAuth client for a zone of their groups — no console page, no API; `/api/v1/me` and `/oauth/authorize` are the only things that answer them). Bootstrap super admin from `RAMEN_ADMIN_EMAIL`/`RAMEN_ADMIN_PASSWORD` on first start. Sessions: signed cookie. Passwords: argon2. API keys: `rmn_<id>_<secret>`, stored hashed, scoped to role+groups, header `X-Ramen-Api-Key` on `/api/v1/*`.
Pages (left sidebar): Dashboard (zone→group load map, blue/green/red), Groups, Environments, Zones/Workers, Secrets, Users, API Keys, Logs, Audit, Backups, Config. Theme: coral `#F26B3A` on `#F4F1EC`, logo at `/static/logo.png`. HTMX for partial refresh; no JS build step.
Every mutating request writes `audit` `{ts, user, ip, action, target, ok, tags}`.
Probes: `GET /healthz` 200 always; `GET /readyz` 200 once the store answers and a super admin exists, else 503.
Additive details settled in v0.1.0: `Cloud.deploy(..., config: dict)` carries the RAMEN_* / RAMEN_SECRET_* vars; local adapter writes `<bucket>/.ramen/env` and `.ramen/env-<zone>`; local workers are addressed via `RAMEN_LOCAL_WORKERS="group/zone=host:port|...,..."` with `RAMEN_WORKER_URL` fallback (default `worker:8080`; a scheme is accepted and stripped, `https://` = TLS); permission requests live in `activity`.

### 4a. Console routes (binding; mirrored by tests/src/ramen_tests/console.py)
| key | route | notes |
|---|---|---|
| login/logout | `POST /login` form {email,password} → 303 + `ramen_session` cookie; `/logout` | 401 on bad password |
| me | `GET /api/v1/me` | |
| users | `/api/v1/users`, `/api/v1/users/{id}` | POST {email,password,role,groups} → 201 |
| groups | `/api/v1/groups`, `/api/v1/groups/{group}` | POST {name,repo_url,ref} → 201 |
| zones | `/api/v1/zones`, `/api/v1/zones/{zone}` (GET/DELETE) | POST {name,provider,region}, super admin |
| environments | `/api/v1/groups/{group}/environments[/{env}]` (GET/PUT/DELETE on the item), `/api/v1/environments?group=` | POST {name,ref,zones:[...]} |
| deploy | `POST /api/v1/groups/{group}/environments/{env}/deploy` {canary,zone?} → 202 {id,status} | poll `GET /api/v1/jobs/{id}` |
| workers | `GET /api/v1/groups/{group}/zones/{zone}/workers` → {live:[{load}],count,size} | scale count via PUT (size super admin only) |
| rebalance / ip-rules | `POST .../zones/{zone}/rebalance`, `PUT .../zones/{zone}/ip-rules` | |
| secrets | `/api/v1/groups/{group}/secrets[/{id}]` | POST {name,value,env?,zone?} → 201 {id,name}; value never returned |
| mcp-keys | `/api/v1/groups/{group}/mcp-keys[/{id}]` | POST {name} → 201 {id,key:"rmk_..."}; written to workers on deploy |
| api-keys | `/api/v1/api-keys[/{id}]` | POST {name,role?,groups?} → 201 {id,key:"rmn_..."} |
| audit / logs / dashboard | `GET /api/v1/audit`, `/api/v1/logs`, `/api/v1/dashboard` | |
| backups | `GET /api/v1/backups`; `POST /api/v1/backups` {target} → 201 {id,release_version}; `GET /api/v1/backups/{id}/download` | super admin |
| config / refresh | `/api/v1/config` (GET), `POST /api/v1/config/reload`, `GET|PUT /api/v1/config/sa-rules`, `POST /api/v1/refresh` | super admin |
| service account | `POST /api/v1/groups/{group}/zones/{zone}/service-account` → {name,…} | super admin |
| sa-restrictions | `PUT /api/v1/groups/{group}/sa-restrictions` {rules} | 409 on clash with super-admin rules |
| verbose | `POST /api/v1/groups/{group}/environments/{env}/verbose` {verbose} | |
| requests | `POST|GET /api/v1/requests`, `POST /api/v1/requests/{id}/approve` | permission requests (F4.2) |
| restore / password | `POST /api/v1/backups/{id}/restore` {dry_run,prune,reconcile,force} (§13.1), `POST /api/v1/users/{id}/password` | |
| revocation (v0.4.1) | `POST /api/v1/requests/{id}/deny`, `POST /api/v1/requests/{id}/revoke`, `DELETE /api/v1/groups/{group}/zones/{zone}/permissions/{permission}` | super admin (§13.2) |
| worker images (v0.4.1) | `GET|POST /api/v1/groups/{group}/images`, `PUT|DELETE /api/v1/groups/{group}/images/current` | GET: viewer of the group; writes: super admin (§13.3) |
| logs | `GET /api/v1/logs?group&zone&worker?&tail&download=1` → text/plain (+Content-Disposition attachment) | |
| requests (v0.3.0) | `POST /api/v1/requests` {group,zone,permission} → 201 {id,type:"permission",status}; approve → {status:"approved",applied:{ok,permissions,…}} | group admin of the group; 422 unknown permission, 409 denied by super-admin/group rules (audited) |
| policy | `GET /api/v1/policy/permissions` → [{permission,desc,gcp:[roles],aws:[actions]}] | catalogue from `policy/permissions.py` |
| blocked | `PUT /api/v1/groups/{group}/environments/{env}/blocked` {blocked:[names]} → env doc | admin; applied by the next deploy as `RAMEN_BLOCKED` |
| config/auth | `GET|PUT /api/v1/config/auth` {password_login?,magic_link?} → {password_login,magic_link,break_glass,providers,mail} | super admin; 422 when disabling passwords would lock everyone out |
| auth forms | `GET|POST /auth/reset` {email} (200 always), `GET|POST /auth/reset/{token}` {password} → 303 /login; `POST /auth/magic` {email}, `GET /auth/magic/{token}` → 303 + session; `GET /auth/{name}/login` → IdP, `GET /auth/{name}/callback` | tokens single use (reset 24 h, magic 15 min); `/auth/oauth/{name}/…` kept as aliases |
| csrf | `ramen_csrf` cookie (set on login and HTML pages); cookie-authenticated `/api/*` mutations need `X-Ramen-CSRF`, HTML forms the hidden `csrf_token` field | 403 `CSRF token missing or invalid`; `X-Ramen-Api-Key` requests exempt |
| zone blocked (v0.4.0) | `PUT /api/v1/groups/{group}/environments/{env}/zones/{zone}/blocked` {blocked:[names]} → env doc with `blocked_zones` | U5; group admin; 422 when the zone is not attached to the environment; the next deploy writes `blocked` ∪ `blocked_zones[zone]` into that zone's `RAMEN_BLOCKED` |
| api-keys client type (v0.4.0) | `POST /api/v1/api-keys` {name,role?,groups?,client_type?} → 201 {id,name,client_type,key}; `GET /api/v1/api-keys` rows carry `client_type` | D21; `client_type` is `devops` (default, `rmn_`) or `agent` (`rmk_`); an agent key must name ≥1 group (422) and is mirrored as those groups' MCP keys so deploy hands it to workers; revoking the key removes the mirror |
| agent key refused (v0.4.0) | any console route with an `agent` key → 403 `{"detail":"Agent key cannot call the console API"}` | D21; a `devops` key is likewise never written to a worker |
| mcp-keys client type (v0.4.0) | `POST /api/v1/groups/{group}/mcp-keys` → 201 now also carries `client_type:"agent"` | the group page's key form is the agent control |
Response shapes on gcp: rebalance `{ok, load, capacity_scaler, backend_service|null, applied, note?, scaled_to?}`; ip-rules `{ok, policy, cidrs, backend_service|null, attached, note?}`; workers `live[]` items carry `track` (stable|canary), `ip`, `phase`.

## 5. Local stack (deploy/local/docker-compose.yml)
Services: `firestore` (emulator, 8081), `console` (8443), `worker` (node-rs + runtime-py in one image, 8080 gRPC h2c, §11), shared volume `buckets`. `make demo` = up, wait ready, create group `demo` pointing at the demo repo, deploy, call `demo_calculator_tool` via an MCP client (the stdio bridge, §11), print result.

## 6. Versioning
`ramen/VERSION` is the single source; Cargo.toml and both pyproject versions must equal it (CI checks).

## 7. GCP deployment (v0.2.0, binding)
Decisions: D6 one zone first but multi-zone capable, D16 throwaway project, D17 static IP + self-signed cert.
- **Terraform (`deploy/terraform/gcp`)** creates: regional GKE Autopilot cluster `ramen` (var `region`, default `us-central1`), Firestore Native DB `(default)`, Artifact Registry docker repo `ramen`, global static IP `ramen-console`, master GSA `ramen-console@<project>` with roles `storage.admin` (on the groups bucket only, SEC-08 — a 0.5.93 scoped permission naming another bucket needs the same grant there), `secretmanager.admin`, `container.developer`, `logging.viewer`, `iam.serviceAccountAdmin`, `iam.serviceAccountUser`, `compute.securityAdmin` (Cloud Armor), `compute.loadBalancerAdmin`, `datastore.user` (Firestore), `resourcemanager.projectIamAdmin` (bind conditioned roles to per-group GSAs), and a Workload Identity binding (depends on the cluster so the WI pool exists) to KSA `ramen-system/console`. Outputs: `cluster_name`, `region`, `console_ip`, `artifact_repo`, `console_gsa`. Nothing per-group is created by Terraform.
- **Images**: `<region>-docker.pkg.dev/<project>/ramen/console:<VERSION>` and `.../ramen/worker:<VERSION>`, linux/amd64, pushed by `make push PROJECT=… REGION=…`.
- **Helm (`deploy/helm/ramen`)**: namespace `ramen-system` holds the console Deployment (KSA `console`, WI-annotated), Service (NEG), the GKE Gateway described under *Worker exposure* (replaces Ingress/BackendConfig), HealthCheckPolicy `/healthz`, and the console ClusterRole. `deploy/helm/ramen-worker` renders one zone namespace (also rendered in Python by the console). Values: `project`, `region`, `image.*`, `console.env`.
- **Zone = k8s namespace** `ramen-<group>-<zone>` created by the console when a zone is attached to a group. It holds Deployments `worker` and `worker-canary` (same image, `nodeSelector: topology.kubernetes.io/zone=<gcp-zone>` from the zone record's `region` field), Service `worker` (port 8080, NEG annotation, selects both), KSA `worker` bound to the group+zone GSA, Secret `ramen-deploy` (deploy env: RAMEN_* + RAMEN_SECRET_*). Worker pods get `RAMEN_BUCKET_URI=gs://<bucket>/<group>` and `RAMEN_BUCKET=/data/bucket`.
- **Bucket sync**: on `runtime.load` (and thus on `Admin/Reload`, §11) the runtime syncs `RAMEN_BUCKET_URI` → `RAMEN_BUCKET` (google-cloud-storage, ADC/Workload Identity) before loading when the URI is set. Sync is content-hash based and deletes stale files.
- **Console GCP adapter (`ramen_console.cloud.gcp`)**: in-cluster kube config (fallback `KUBECONFIG`). `sync_repo` → git clone/fetch to temp then upload to `gs://ramen-<project>-groups/<group>/` (bucket created by Terraform var `groups_bucket`, one bucket, per-group prefix; IAM per group via prefix conditions). `deploy(canary=True)`: write Secret `ramen-deploy` (secret values fetched from Secret Manager), `kubectl rollout restart worker-canary` → wait ready → `Admin/Reload` on the canary pod IP:8080 → `Mcp/Call tools/list` smoke (`Health/Check` SERVING when the group has no MCP keys; §11) → then restart `worker` → wait ready; on any failure scale canary to 0 and return the error; job log lines are streamed to the console job record. `workers()` = pods of `worker`+`worker-canary` + each pod's `Admin/Metrics` (direct pod IP; the former `RAMEN_GCP_POD_PROXY` mode is gone, §11). `logs()` = Cloud Logging `resource.type="k8s_container" AND resource.labels.namespace_name="ramen-<group>-<zone>"` (+ `pod_name` for one worker), newest N, downloadable. `rebalance(group, zone)` = set the backend service capacity scaler for that zone's NEG backend (0.5 when `high`, 1.0 otherwise) via compute API, then trigger an HPA-friendly `scale` if count differs. If the Gateway has not programmed a backend yet it returns 200 with `applied:false` and a `note`, never an error; if the backend service is busy (Gateway reconciling after a rollout) the capacity change is retried in the background and the response says `applied:false` with a pending note. `set_ip_rules` = Cloud Armor policy `ramen-<group>` (allow listed CIDRs, deny rest) attached to the worker backend service; also patched into `RAMEN_ALLOWED_CIDRS` of the deploy Secret. Without a programmed backend it returns 200 with `attached:false` and a `note`. Changing IP rules rolls the zone's worker pods (env comes from the Secret at pod start) so the node enforces immediately; the Cloud Armor attach retries in the background when the backend service is busy (`attached:false` + note). `attach_zone` ensures the zone identity before the first deploy (v0.3.2: it creates the GSA, its baseline grants and the WI binding when the zone KSA carries no GSA annotation — workers otherwise start with no identity and every bucket read fails 403). `create_service_account(group, zone)` = the same GSA `ramen-<group>-<zone>@<project>` with `storage.objectViewer` bound on the groups bucket (conditioned to the group prefix) + `secretmanager.secretAccessor` bound on each existing `ramen-<group>-*` secret (new secrets are bound at creation by the `gcp` secrets backend; v0.3.1 §11: no project-level bindings, the console GSA has no `projectIamAdmin`; project-wide roles from approved permissions need Terraform `console_project_iam=true`, otherwise they are reported as `skipped`) + WI binding to KSA `ramen-<group>-<zone>/worker`; extra roles only via super-admin rules (F4.2). `refresh()` = list namespaces/deployments/GSAs and reconcile the store. `detach_group(group)` (called by group delete, F4.1) deletes every namespace labelled `ramen.io/group=<group>` and every GSA `ramen-<group>-*`; failures abort the delete. A failed deploy always scales `worker-canary` to 0, including a canary left by an earlier deploy.
- **Secrets backend**: `RAMEN_SECRETS_BACKEND=store|gcp`. `gcp` stores values in Secret Manager as `ramen-<group>-<env>-<zone>-<NAME>` (labels group/env/zone) and the store keeps only the name + resource ref. Viewers/admins never read values back through the console.
- **Console runtime env on GCP**: `RAMEN_STORE=firestore`, `RAMEN_CLOUD=gcp`, `RAMEN_SECRETS_BACKEND=gcp`, `RAMEN_GCP_PROJECT`, `RAMEN_GCP_REGION`, `RAMEN_GROUPS_BUCKET`, `RAMEN_IMAGE_WORKER`.
- **Worker exposure (added during 0.2.0; v0.3.1 routing per §11)**: MCP clients reach workers through the same global LB at `https://<console_ip>` with gRPC metadata `ramen-group`/`ramen-zone`. GKE Gateway API: Gateway `ramen` in `ramen-system` (class `gke-l7-global-external-managed`, static IP, self-signed cert, `allowedRoutes` namespaces selector `ramen.io/routes=true`); console HTTPRoute for `/`; each worker namespace (labelled `ramen.io/routes=true`) gets HTTPRoute `worker` matching headers `ramen-group=<group>`, `ramen-zone=<zone>` + one PathPrefix rule per service in `route.paths` (default `/ramen.v1.Mcp`, `/grpc.health.v1.Health` and both reflection services; `/ramen.v1.Admin` stays cluster-internal; no rewrite) — routing Health and reflection matters because anything unmatched falls through to the console route → Service `worker:8080` (`appProtocol: kubernetes.io/h2c`), plus HealthCheckPolicy `worker` of type GRPC. The LB backend service is auto-named by GKE; the console discovers it by NEG name `ramen-<group>-<zone>` for rebalance and Cloud Armor attachment (fallback `ramen-<group>`).
- **Console RBAC (v0.3.1, §11)**: KSA `ramen-system/console` has ClusterRole `ramen-console` for namespaces, networkpolicies, httproutes, healthcheckpolicies/gcpbackendpolicies, rolebindings and `bind` on ClusterRole `ramen-console-zone` only; `ramen-console-zone` (serviceaccounts, secrets, services, deployments, HPAs, pods read) is unbound cluster-wide and bound by the console through RoleBinding `ramen-console` in every `ramen-<group>-<zone>` namespace it attaches (a namespaced Role would be refused by RBAC escalation prevention, hence the RoleBinding→ClusterRole form). A `GCPBackendPolicy` sets the console backend timeout to 300s because IAM/compute operations exceed the LB's 30s default.
- **Sizes**: worker size presets `s` (250m/512Mi), `m` (500m/1Gi), `l` (1/2Gi); super admin sets allowed sizes per group+zone, admins set count (HPA min/max).

## 8. AWS deployment (v0.3.0, binding, UNTESTED — no AWS account; D18)
Mirror of §7 with AWS primitives. Everything is built and unit-tested with moto/fakes; nothing is applied to a real account and the docs must say so.
- **Terraform (`deploy/terraform/aws`)**: EKS cluster `ramen` (one small managed node group, var `region` default `us-east-1`), DynamoDB table `ramen` (pk/sk single-table, on-demand; matches the existing `dynamodb` store adapter), S3 groups bucket `ramen-<account>-groups`, Secrets Manager, ECR repos `ramen/console` + `ramen/worker`, console IAM role (IRSA) with S3/SecretsManager/DynamoDB/EKS-describe/IAM-create-role (scoped by path `/ramen/`)/WAF/ELB permissions, AWS Load Balancer Controller + Fluent Bit → CloudWatch Logs (Container Insights) installed via Helm from Terraform, a self-signed cert imported into ACM (no domain), outputs `cluster_name`, `region`, `console_url`, `ecr_console`, `ecr_worker`, `console_role_arn`.
- **CloudFormation (`deploy/cloudformation/ramen.yaml`)**: the same base resources (EKS, node group, DynamoDB, S3, Secrets Manager, ECR, IAM roles, OIDC provider) as one template with parameters, for teams that cannot run Terraform. Helm steps are documented, not templated.
- **Helm**: `deploy/helm/ramen` gains `provider: gcp|aws`. On AWS: console Ingress class `alb` with annotations `group.name: ramen`, `scheme: internet-facing`, `certificate-arn`, `listen-ports [{"HTTPS":443}]`; each worker namespace gets an Ingress in the same ALB group with header conditions `ramen-group`/`ramen-zone` + the `route.paths` set (Mcp, Health, reflection; Admin internal), `backend-protocol-version: GRPC` target groups and a gRPC health check (success code 0) — v0.3.1 §11; `RAMEN_MCP_PATH_PREFIX` is gone; worker KSA annotated with the group+zone IAM role (IRSA); pods get `RAMEN_BUCKET_URI=s3://<bucket>/<group>`.
- **Runtime**: `ramen_runtime.bucket.sync` handles `s3://` (boto3) as it does `gs://`.
- **Console AWS adapter (`ramen_console.cloud.aws`)**: `sync_repo` → S3 prefix; zone attach = namespace + manifests + Ingress; deploy = same canary flow as GCP; `workers()` = pods + `Admin/Metrics` (§11); `logs()` = CloudWatch Logs Insights query on the Container Insights log group filtered by namespace/pod, downloadable; `rebalance()` = weighted target groups (stable/canary) via the Ingress `actions` annotation; `set_ip_rules()` = WAFv2 IPSet + web ACL associated to the ALB (allow list, default block) + `RAMEN_ALLOWED_CIDRS` in the deploy Secret + worker roll; `create_service_account()` = IAM role `ramen-<group>-<zone>` trusting the cluster OIDC provider for KSA `ramen-<group>-<zone>/worker`, policies scoped to the group's S3 prefix and secrets path; `detach_group` deletes namespaces + roles; `refresh` reconciles. Secrets backend `aws`: Secrets Manager names `ramen/<group>/<env|all>/<zone|all>/<NAME>` with tags; store keeps `sm://`-style ref `asm://`.
- **Console env on AWS**: `RAMEN_STORE=dynamodb`, `RAMEN_CLOUD=aws`, `RAMEN_SECRETS_BACKEND=aws`, `RAMEN_AWS_REGION`, `RAMEN_GROUPS_BUCKET`, `RAMEN_IMAGE_WORKER`, `RAMEN_EKS_CLUSTER`, `RAMEN_ALB_GROUP=ramen`, optional `RAMEN_AWS_PERMISSIONS_BOUNDARY` (v0.3.1: worker roles are created with the `ramen-worker-boundary` managed policy from Terraform/CloudFormation; the console role may only create `/ramen/` roles carrying it; `-` disables).
- Apply the §7 lessons: retry/background on ELB/WAF propagation, per-call boto3 clients (thread-safe by design), throttling backoff.
- Settled during the build (deviations from the wording above): the web ACL `ramen` defaults to **allow** because the console shares the ALB; each group's IP rules add a rule that blocks the group's traffic (matched on the `ramen-group` header since v0.3.1) unless the source is in the group's IP set. `rebalance()` on AWS shifts the stable↔canary target-group weights on the zone's Ingress (there is no cross-zone capacity scaler on an ALB path rule) and returns `applied:false` + note until the controller has created the ALB. The worker image installs the runtime with the `gcp` and `aws` extras. `apply_sa_permissions` on AWS writes an inline policy `ramen-sa-permissions` on the worker role.

## 9. Auth, policy, tool blocking (v0.3.0, binding)
- **OAuth/OIDC (F6.1)**: providers from yaml/env (`RAMEN_OAUTH_<NAME>_{ISSUER,CLIENT_ID,CLIENT_SECRET,SCOPES}`); login page shows a button per provider; callback `/auth/<name>/callback` links or creates the user by verified email; role mapping `auth.oauth.<name>.role_claim` + `role_map` (default viewer, no groups) — since 0.5.92 (D38) also editable by a super admin at `PUT /api/v1/config/auth/role-map/{provider}` (`{claim}` / `{value, role, groups}` / `{remove}`, stored in `config/auth`, merged over env with the store winning per value) and, once a claim is named for a provider, **authoritative on every login**: role and groups are replaced from the rules at each OIDC callback, no match → viewer with no groups, the bootstrap super admin exempt; `auth.password_login: false` (super admin toggle at `PUT /api/v1/config/auth`) disables email/password login except for the bootstrap super admin via `RAMEN_ADMIN_FORCE_PASSWORD=1` (break-glass). Audited.
- **Email auth (G12)**: SMTP from yaml/env (`RAMEN_SMTP_{HOST,PORT,USER,PASSWORD,FROM,TLS}`); invite email on user create, password reset (`POST /auth/reset`, `POST /auth/reset/{token}`), optional magic-link login (`auth.magic_link: true`). Dev backend `RAMEN_SMTP_HOST=file://<dir>` writes .eml files (tests use it). Never log message bodies.
- **SA policy engine (F4.2, F6.5)**: super-admin rules `[{effect: allow|deny, permission: <glob>}]` (existing) gate what group admins may request. `POST /api/v1/requests {group, zone, permission}` → super admin `approve` → `Cloud.apply_sa_permissions(group, zone, permissions)` binds the mapped cloud roles (gcp: IAM roles/conditions on the GSA; aws: IAM policy on the role; local: recorded only). Mapping table `ramen_console/policy/permissions.py` (e.g. `bucket.read` → `roles/storage.objectViewer` / `s3:GetObject`). Denied-by-rule requests are rejected with 409 and audited.
- **Tool blocking (F5.6)**: per environment `blocked: [names]` (`PUT /api/v1/groups/{g}/environments/{e}/blocked`); deploy writes `RAMEN_BLOCKED=<comma list>` into the deploy config; the node removes blocked names from `tools/list`, `resources/list`, `prompts/list` and answers `-32601` for calls to them; the UI packages page has a block/unblock toggle per item (admins).
- **Hardening (added after the 0.3.0 security audit)**: signing secret = `RAMEN_SESSION_SECRET` → `RAMEN_FERNET_KEY` → a random per-process secret (never a constant; set the env in production); security headers on every response (CSP self+inline, nosniff, DENY framing, referrer same-origin; HSTS when `RAMEN_COOKIE_SECURE=1`); per-IP rate limit on `POST /login`, `/auth/reset*`, `/auth/magic*` (`RAMEN_LOGIN_RATE_LIMIT`, default 20/min → 429); reset/magic tokens redacted from the access log; `next` after login must be a same-origin path; OIDC requires `email_verified: true` unless `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED=1`; CSV exports neutralise formula cells; log queries accept only valid namespace/pod names (422 otherwise). Worker pods: `allowPrivilegeEscalation: false`, all capabilities dropped, seccomp RuntimeDefault, and a per-namespace NetworkPolicy allowing ingress only from `ramen-system` and the load-balancer ranges (GCP `35.191.0.0/16`, `130.211.0.0/22`; AWS the VPC CIDR).
- **CSRF**: HTML forms carry a per-session token (`ramen_csrf` cookie + hidden field / `X-Ramen-CSRF` header); the JSON API with an API key is exempt.

## 10. Docs site & release material (v0.3.0, binding)
- MkDocs Material in `ramen/docs/` (`mkdocs.yml` at repo root, `.github/workflows/pages.yml` deploys on push to main and on tags to GitHub Pages). Pages: landing (logo, value proposition, quickstart, badges), how-it-works, architecture (`architecture/v0.1.0.md`, `v0.2.0.md`, `v0.3.0.md` + current), how-tos (local quickstart, GCP verbose, AWS verbose *untested*, security, secrets, devops with the API/keys), wiki (concepts: group/environment/zone/worker, protos, canary, rebalance), version tracker (`versions.md` generated from CHANGELOG), `llms.txt` and JSON-LD on the landing page for discoverability.
- README: logo, badges, 5-command local quickstart that ends with a visible PASS (fixes fresh-user U1–U6: console URL/login, minting an `rmk_` key vs `rmn_` API keys, idempotent `make demo` with a success line), screenshots of login, dashboard, group, secrets, logs, deploy job (captured from the compose stack with Playwright into `docs/img/`), links to how-tos and releases.
- `ramen/skills/` cloud-ops agent skills (agentskills style): deploy-gcp, deploy-aws, rotate-keys, backup-restore, scale-zone, with a validation sub-agent note.
- `reports/launch-posts-v0.3.0.md`: drafts for reddit, forums, agent portals, GitHub discussions, LinkedIn with hashtags; repo topics list. Nothing is posted by agents.

## 11. gRPC transport (v0.3.1, binding; supersedes §3 `/mcp` and §5/§7/§8 where they mention HTTP endpoints)
Decision D19. Protos: `proto/ramen/v1/mcp.proto`, `proto/ramen/v1/admin.proto` (single source; Rust via tonic-build, Python via grpcio-tools into `ramen_proto` packages vendored in console, runtime-py bridge and tests).
- **Node (`ramen-node`)** serves one h2c port `RAMEN_NODE_PORT` (default 8080) with services `ramen.v1.Mcp`, `ramen.v1.Admin`, `grpc.health.v1.Health` (SERVING once `runtime.load` succeeded; NOT_SERVING before). The axum HTTP server, `/mcp`, `/healthz`, `/readyz`, `/metrics`, `/admin/reload` are removed. Optional node TLS: `RAMEN_TLS_CERT` + `RAMEN_TLS_KEY` (PEM) switches the port to TLS (h2); otherwise the LB terminates TLS.
- **Security parity**: `Mcp/Call` requires metadata `authorization: Bearer <key>` validated with a constant-time compare against `RAMEN_MCP_KEYS` (UNAUTHENTICATED otherwise; empty key set = deny all); peer address (or the client hop of `x-forwarded-for`, see the v0.4.0 amendment below) must match `RAMEN_ALLOWED_CIDRS` (PERMISSION_DENIED); `Admin/*` requires `x-ramen-admin-key` and `RAMEN_ADMIN_CIDRS`; blocked names (`RAMEN_BLOCKED`) are filtered from `*/list` and answered with JSON-RPC `-32601`; message size limit 4 MiB (tonic codec → OUT_OF_RANGE); the initial `runtime.load` retries every `RAMEN_LOAD_RETRY_SECS` (default 5, 0 disables) until it succeeds, so a worker whose cloud identity is still propagating becomes ready without an admin reload; concurrency `RAMEN_MAX_INFLIGHT` → RESOURCE_EXHAUSTED; admin: bad key → UNAUTHENTICATED, outside admin CIDRs → PERMISSION_DENIED, reload failure → INTERNAL; health: service "" and `ramen.v1.Mcp` = readiness, `ramen.v1.Admin` always SERVING (liveness); server reflection enabled by default and off-able (`RAMEN_REFLECTION`, amendment below); JSON-RPC protocol errors are returned as gRPC OK with a JSON-RPC error body; denied calls are access-logged with `status: denied`; one JSON access-log line per call with the same fields as before plus `grpc_code`. Health is unauthenticated.
- **Routing**: clients (and the bridge) send metadata `ramen-group` and `ramen-zone`. GKE Gateway: worker Service `appProtocol: kubernetes.io/h2c` (fallback: node TLS + `HTTP2` if the Gateway rejects h2c), HTTPRoute matches `headers: [ramen-group=<g>, ramen-zone=<z>]` and one PathPrefix per exposed service (`route.paths`: Mcp, Health, both reflection services; Admin stays cluster-internal), no path rewrite; HealthCheckPolicy type GRPC. Verified live on GKE in v0.3.2 over h2c — the TLS fallback was not needed. AWS ALB: target group `backend-protocol-version: GRPC`, listener rule on the two headers, health check gRPC code 0. Cloud Armor / WAF unchanged. The console targets pods directly (pod IP:8080) as before.
- **Console**: `ramen_console.grpcclient` (grpcio, per-call channel or cached per target, 10s deadline) replaces every HTTP call to workers: `_reload_and_smoke` → `Admin/Reload` + `Mcp/Call tools/list`; `workers()` → `Admin/Metrics`; readiness → `Health/Check`. Local adapter identical against `worker:8080`.
- **Bridge (`ramen-mcp-bridge`, Python, its own package/repo since v0.5.7 (github.com/bkraad47/ramen-mcp-bridge), also installed in the worker image)**: a stdio MCP server for Claude Desktop / Cursor / the mcp SDK: `ramen-mcp-bridge --target <host:port> --key <rmk_…> --group <g> --zone <z> [--tls|--insecure] [--ca <pem>]`. It forwards each stdio JSON-RPC message to `Mcp/Call` and writes the response back; notifications are forwarded and produce nothing. Documented as the way to connect standard clients.
- **Local stack**: compose worker exposes 8080 h2c; `demo.sh` and the harness call gRPC (grpcio); `mcp_call.py` uses the bridge. `deploy/local/mcp-client-config.example.json` becomes a bridge stdio config.
- **Harness**: `ramen_tests.mcp_client` speaks gRPC; conformance covers UNAUTHENTICATED, PERMISSION_DENIED (CIDR), blocked `-32601`, size limit, health states, and the bridge end to end via the mcp SDK stdio client.
- **Carried security mediums fixed in the same release**: GCP console GSA drops `resourcemanager.projectIamAdmin` in favour of `iam.serviceAccountAdmin` + a custom role limited to `setIamPolicy` on `ramen-*` service accounts and bucket/secret-level bindings (project-level bindings replaced by resource-level IAM on the groups bucket and secrets); AWS console role: `wafv2:*` narrowed to the `ramen` web ACL/IP sets by ARN pattern and `iam:PutRolePolicy` limited to `/ramen/` roles; console ClusterRole loses cluster-wide `secrets`/`serviceaccounts` verbs — the console creates a namespaced Role + RoleBinding for its KSA on every zone namespace it attaches (ClusterRole keeps only namespaces, networkpolicies, httproutes and read verbs); local `sync_repo` passes the token via `http.extraheader`/`GIT_ASKPASS`, never in the remote URL, and strips credentials from `.git/config`; OIDC uses PKCE (S256) and `nonce`; node key compares are constant-time.
- **Logo**: `images/logo_v2.png` replaces v1 in console static, docs (`docs/img/logo.png`, favicon), README, launch drafts; theme colours re-derived from v2.
- **Version**: 0.3.1 (user's choice). CHANGELOG marks the transport change as breaking for HTTP MCP clients (use the bridge).

### 11a. v0.4.0 amendment — proxy trust, reflection, metrics (binding)
Source: `reports/claims-review-v0.4.0.md` rows 14/25/36 and the `metrics_json` defect. Supersedes the `x-forwarded-for`
and reflection clauses of §11 above.
- **`x-forwarded-for` is counted from the right.** Proxies append, so the left-hand entries are caller-supplied: taking
  the left-most hop let a client choose the address `RAMEN_ALLOWED_CIDRS` was checked against. `RAMEN_TRUST_PROXY_HOPS`
  (integer, default `0` = header ignored) says how many hops at the right-hand end are trusted proxies; the client
  address is the **Nth entry counted from the right** and everything to its left is ignored. `RAMEN_TRUST_PROXY=1`
  still means one hop; an explicit `RAMEN_TRUST_PROXY_HOPS` wins over it. Neither can be set from the bucket deploy
  file (not in `DEPLOY_KEYS`), so a group cannot widen its own trust.
- **Per-provider values, set where the deployment knows the topology**: GCP's external Application Load Balancer
  appends `<client>, <lb>` → `2`; an AWS ALB appends `<client>` → `1`. `deploy/helm/ramen-worker` (`trustProxyHops: ""`
  → per `provider`, overridable) and both console renderers (`cloud/gcp_k8s.py` `_deployment(trust_proxy_hops=2)`,
  `cloud/aws_k8s.py` passing `1`) write it into the worker env.
- **A wrong count fails closed, never open.** Fewer than N entries, or an unparseable entry at that position, uses the
  **peer address** — behind a load balancer that is the proxy, so a client-range allowlist denies the call instead of
  admitting a spoofed one. The address actually used is the `ip` field of the access log. The node list is a real gate
  on the client address only when the hop count matches the deployment; otherwise treat the NetworkPolicy and the
  bearer key as what stops a misconfigured edge.
- **`RAMEN_REFLECTION`** (default `1`): `grpc.reflection.v1[alpha]` is registered only when it is on. Reflection, like
  `grpc.health.v1.Health`, runs ahead of every guard — no key, no CIDR check — and `route.paths` publishes it through
  the load balancer, so anyone who can reach the edge can list the services and describe `ramen.v1.Admin`. The schema
  is public, so this is disclosure rather than a breach; it is now a choice. Default on for local development,
  **off on deployed workers**: the chart sets `reflection: false` and both renderers write `RAMEN_REFLECTION=0`, and
  the LB paths stay as they are and answer `UNIMPLEMENTED`. Read once at startup, so `Admin/Reload` does not change it.
- **`Admin/Metrics`**: `inflight` is measured against the semaphore's startup capacity with a saturating subtraction.
  The semaphore is still sized once (§11 row: a `RAMEN_MAX_INFLIGHT` change needs a pod restart), so a reload that
  lowers the limit used to underflow `max - available_permits`; `max` keeps reporting the configured value.

## 12. v0.4.0 — console UI, docs, and the verification the user asked for (binding)
Source: `instructions/v0.4.md`, normalised as U1–U26 in `facts/v0.4.md`. Decisions D21–D23.

### 12.1 Console UI
- **Naming (U1, U10)**: the product name appears once, in the logo image. The sidebar wordmark, the `<title>` suffix and the login heading drop it. Every button, label, heading, flash and error message is sentence case with a capital first letter ("Generate key", "Create service account", "Scale workers"); no all-caps words and no abbreviations in user-visible text — "service account", never "SA" (U2).
- **Sidebar (U6)**: under the logo, one row holds the signed-in email and the role side by side. The role reads `Super Admin`, `Group Admin` or `Viewer` and carries a per-role colour (three distinct tokens on `:root`). `Log out` sits below that row in the error colour. Version stays last.
- **Group page actions (U2)**: every per-zone action (scale workers, IP rules, create service account, rebalance, view logs) lives in one `Actions` section per zone, rendered as a button row with a single shared width class, in that order.
- **Logs (U4)**: two panes. Left lists entries newest first, the newest highlighted and selected by default, the newest 15 rendered eagerly and the remainder inside a scrollable frame; a worker selector above it filters the list (`all` by default). Right shows the selected entry's body. Every row shows consumer (the `key_id`, resolved to the key's name when the console knows it), timestamp, the method and tool name, and outcome as success or failure. Download keeps its current behaviour.
- **Per-zone packages (U5, U22)**: the group page lists tools, resources and prompts grouped by zone, each with an enable/disable toggle for that zone. Storage: `environments[].blocked` stays the environment-wide list (§9); a new per-zone map `environments[].blocked_zones = {zone: [names]}` adds to it. Deploy writes the union of both into that zone's `RAMEN_BLOCKED`. Route: `PUT /api/v1/groups/{group}/environments/{env}/zones/{zone}/blocked {blocked:[...]}`. A language model connected to a zone therefore sees only that zone's enabled packages.
- **Passwords and keys (U3)**: generated passwords and keys are at least 12 characters and contain upper, lower, digit and special characters; `ramen_console.security.generate_password()` and the key minters produce them, and setting or changing a password rejects anything weaker with a 422 naming the rule. `RAMEN_MIN_PASSWORD_LEN` (default 12) may raise but not lower it.
- **API keys (U7, U8, U9 / D21)**: the page says `Generate key`. Groups are picked from a multi-select of the groups the caller may grant, and are displayed as names. Every key carries `client_type`:
  - `devops` — prefix `rmn_`, accepted by `/api/v1/*` exactly as today; rejected by workers.
  - `agent` — prefix `rmk_`, accepted by a worker's `ramen.v1.Mcp` for the groups and zones it names; rejected by `/api/v1/*` with 403 `{"detail":"Agent key cannot call the console API"}`. Generating one writes it into the deploy config of the zones it covers on the next deploy, exactly as the group MCP key does now; the group page's key form is the same control with `client_type` fixed to `agent`.
  Keys minted before 0.4.0 default to `devops` if they start `rmn_`, `agent` otherwise.
- **Dashboard (U11)**: the load legend and cell text name the load (`low`, `even`, `high`, `down`), never the colour. Colour stays the visual signal and every cell keeps a text label, so the grid is readable without colour.
- **Sessions (V1.4, folded in for U25)**: each user doc carries `session_epoch`; sessions embed it and are rejected when it differs. It is bumped on password change, role change, group change, delete, and on any change to `config/auth`.

### 12.2 Docs and repository
- U12 badges: one row, one size, aligned (a single badge table or a flex row with fixed height).
- U13: README and the wiki explain the transport plainly — JSON-RPC 2.0 over gRPC, why the node is Rust, and what secures each hop (bearer key with constant-time compare, CIDR allowlist, TLS at the edge and optionally at the node, blocked-name filtering, per-zone identity). Claims are limited to what `reports/` shows; the AWS path is still described as untested.
- U14: repository description and topics set from `reports/launch-posts-v0.3.1.md`.
- U15 remove `content.action.edit` and `edit_uri`. U16 keep `docs/llms.txt` served, remove its nav entry. U18 dark only: one palette, no toggle. U19 `toc.permalink: false` so headings stop rendering a leading `§`.
- U17: `docs/img/architecture.svg`, hand-written inline SVG, dark-mode legible, showing the real path — MCP client → bridge (stdio) → gRPC through the load balancer with `ramen-group`/`ramen-zone` metadata → Rust node → Python runtime sidecar → the group's bucket — with the console alongside writing deploy secrets and reading metrics. It replaces the ASCII shape on the how-it-works page.

### 12.3 Verification (U20–U26)
- **kind (U20, U21 / D22)**: `deploy/kind/` brings up a local cluster with metrics-server, the console chart and two worker namespaces (`demo/a`, `demo/b`). `make kind-up`, `make kind-test`, `make kind-down`. Proves: two zones serving independently; an HPA scaling a zone's workers up under generated load and back down; `rebalance` changing the split; per-zone package lists differing (U22).
- **GKE (U21 second half)**: the same suite once on a throwaway project, then deleted, reported in `reports/cloud-v0.4.0.md`.
- **Transport re-check (U23)**: state, with evidence, what each hop is and how it is protected, including the bridge. Any hop that is plaintext by default says so.
- **Independent clients (U24 / D23)**: Claude Code configured against the bridge, screenshotted calling the demo tool; a Cursor config published for the user to capture. Screenshots land in `docs/img/clients/`.
- **Security assumptions (U25)**: a matrix test over roles × groups × both key types, asserting every cross-group and cross-role action is refused, and that revocation takes effect immediately.
- **Independent review (U26)**: a reviewer that did not write the text checks the U13 claims against the code and the reports, and files `reports/claims-review-v0.4.0.md`.

## 13. v0.4.1 — the three half-finished promises (binding)
Source: `facts/v0.4.md` V5.1–V5.3 (F7.2, F4.2, F9.3), confirmed by the human on 2026-09-29. Decisions D24–D26.

### 13.1 Backup restore (V5.1 / F7.2)
`POST /api/v1/backups/{bid}/restore` takes `{dry_run, prune, reconcile, force}`, all default `false`, super admin only.
- **Version gate**: the backup's `release_version` is compared with the console's. Newer than the console → `409`
  naming both versions, unless `force: true` (the audit entry carries `force:true`).
- **Plan**: per collection (`groups`, `zones`, `environments`, `workers`, `users`, `config`) the restore computes
  `created` (in the backup, absent from the store), `updated` (present and different), `unchanged`, and
  `extra` (in the store, absent from the backup). `dry_run: true` returns that plan and writes nothing.
- **Write**: each restored doc is merged field-wise over the existing one, so the fields a backup strips
  (`password_hash`, secret `value`, `secret_hash`, `github_token` — `storage.encrypted.SENSITIVE`) survive a restore.
  A user the store does not have is created with `login_disabled: true` and named in `warnings`: the backup carries no
  hash, so the account must be re-invited rather than silently left password-less.
- **Sessions**: every restored user's `session_epoch` is bumped (§12.1, V1.4). A role or group lowered by a restore
  therefore cannot be outlived by an open session.
- **Prune** (`prune: true`): `extra` docs are deleted from `groups`, `zones`, `environments`, `workers` and `users`.
  `config` is never pruned. The acting principal's own user doc is never pruned, and says so in `warnings`.
- **Reconcile** (`reconcile: true`): after the write, every restored `(group, zone)` whose zone still exists is
  re-applied through `cloud.attach_zone(group, zone, zone_spec(...))`, so live zones match the restored count, size
  and image pin. Namespaces the cloud has that the backup does not are reported as `orphans` and never deleted —
  destroying infrastructure is `DELETE /groups/{g}`, not a restore.
- **Response**: `{release_version, dry_run, restored:{col:{created,updated,unchanged}}, extra, pruned, reconciled,
  orphans, warnings}`. Audit action `backup.restore`, tags carry the flags.

### 13.2 Permission revocation (V5.2 / F4.2)
Granting stays as §9. Revocation is the missing half.
- `DELETE /api/v1/groups/{g}/zones/{z}/permissions/{permission}` (super admin): drops the permission from
  `workers[].sa_permissions` and calls `cloud.apply_sa_permissions(g, z, remaining)`. `404` when it was never granted.
- `POST /api/v1/requests/{rid}/deny` (super admin): a `pending` request becomes `denied`; nothing is applied.
  `409` when the request is already handled.
- `POST /api/v1/requests/{rid}/revoke` (super admin): an `approved` request becomes `revoked`.
  - `type: permission` → the same effect as the `DELETE` above.
  - `type: role` → the user's role returns to the `prior_role` the approval recorded and the group the approval added
    is removed again; the user's `session_epoch` is bumped, so revocation takes effect on the next request (U25).
    `approve_request` therefore records `prior_role` and `granted_group` on the request doc.
- **`apply_sa_permissions` is a set operation, not an add**: called with a shorter list it unbinds the cloud roles that
  no remaining permission maps to. GCP removes the member from the bucket, secret and project bindings
  (`gcp_api.Iam.revoke_*`); AWS rewrites the inline policy `ramen-sa-permissions` and deletes it when the list is
  empty; the local adapter rewrites its recorded JSON. The zone identity's baseline roles
  (`roles/storage.objectViewer` on the group prefix, the group secret accessor) are never unbound — they are what
  makes the zone's service account work, not a granted permission.
- Audit actions `permission.revoke` and `permission.deny`. The group page shows `Revoke` beside each granted
  permission, and `Deny` / `Revoke` on the requests table.

### 13.3 Per-group worker images (V5.3 / F9.3)
The console records and recalls image references; it never builds. Builds come from `make build-worker` or the release
workflow and are pushed to the registry, so the console needs no build credentials and no new IAM.
- Collection `images`: `{id, group, ref, tag, digest, note, created, created_by}`. `ref` is what the manifests use:
  `tag`, or `tag@digest` when a digest is recorded. The group doc carries `image = {id, ref}` — the current pin.
- `POST /api/v1/groups/{g}/images {tag, digest?, note?}` (super admin) records a build and pins it. `201`.
- `GET /api/v1/groups/{g}/images` (viewer of the group) — history newest first, the pinned one flagged `current: true`.
- `PUT /api/v1/groups/{g}/images/current {id}` (super admin) — recall an earlier record (F9.3 "recalled").
- `DELETE /api/v1/groups/{g}/images/current` (super admin) — drop the pin; the group returns to the release image.
- Validation (`util.parse_image`): the reference splits at its last `:` into a repository and a tag. The repository
  matches `^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(:\d{2,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$` (lowercase path, optional
  registry port), the tag `^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$`, and a recorded digest `^sha256:[a-f0-9]{64}$`. A
  reference with neither a tag nor a digest is `422`: an implicit `latest` would make "recall this build" meaningless.
- `zone_spec()` gains `"image"`, the group's pinned `ref` or `None`. `GcpCloud._render` and `AwsCloud.deploy`/`_attach`
  use `spec.get("image") or self.image`, so a group that pins nothing keeps running `RAMEN_IMAGE_WORKER` exactly as
  before. Recalling a tag and redeploying the environment is what puts a group back on an older image.
- `make build-worker GROUP=<g> TAG=<t>` tags per group; `make push-worker GROUP=<g>` pushes it. Recording it in the
  console is a separate, deliberate API call.
- UI: a `Worker image` section on the group page — the current pin, the history with `Recall`, and a form to record a
  tag. Sentence case, no colour words (§12.1).

## 14. v0.4.2 — console usability and docs (binding)
Source: the user's `v0.4.2` instructions, normalised as W1–W10 in `manifests/iterate.md` and
`facts/current_state.facts.md`. UI rules from §12.1 still hold: sentence case, no abbreviations, no colour words.

- **W1 group picker (API keys)**: the multi-select list box is replaced by a dropdown of the groups the caller may
  grant plus an `Add` button; chosen groups render as removable chips and post as repeated `groups` fields, so a key
  can still name several groups (D21 mirrors agent keys to each). Choosing none still means "the caller's own groups".
- **W2 dashboard**: the grid refreshes on load and **every 60 seconds**, gated by a filter the toggle button controls
  (`Auto refresh: on|off`). Toggling off stops the polling; the button never hides the manual refresh.
- **W3 audit**: the page renders the newest **100** rows (`/audit?limit=` up to 500), inside a scrollable frame. A
  search box filters across every column and an outcome filter narrows to succeeded or failed; both act on the
  rendered rows, so filtering never re-queries.
- **W4 backups**: `Download`, `Preview restore`, `Restore` and `Restore and prune` share one width class and sit in an
  evenly spaced row.
- **W5 service-account rules**: the super admin adds a rule from an effect dropdown (`allow`/`deny`) and a permission
  dropdown (the catalogue, plus a glob field), and removes one per row. The raw JSON stays as a read-only view of
  what will be sent to `PUT /api/v1/config/sa-rules`, which is unchanged.
- **W6 environments**: `last_deploy` is flattened into `Last deploy` (outcome), `When` and `Error` columns — never a
  JSON blob — and the row actions use the shared width class.
- **W7 users**: the row's `Save` and `Delete user` sit in one actions row, delete to the right, labelled `Delete user`.
- **W8 favicon**: `console/src/ramen_console/static/favicon.png` (the docs site's favicon) is the icon for every
  console page, the sign-in page included, and the docs site keeps `docs/img/favicon.png`.
- **W9 version**: `ramen_console.__version__` and `ramen_runtime.__version__` are read from installed package
  metadata, falling back to the repo `VERSION`; nothing hard-codes a version string. `scripts/check_versions.py`
  checks both packages, so drift fails the gate instead of reaching the sidebar.
- **W10 docs**: the site builds `--strict` with no dead links, `version_current` tracks `VERSION`, the console page
  documents the behaviour above, and the release notes carry 0.4.1 and 0.4.2.

## 15. v0.4.3 — screenshots that cannot go stale (binding)
The 0.4.0 screenshots survived two UI releases in `docs/img/`, so `api-keys.png` advertised a control that had been
replaced. Nothing in the repo noticed. From 0.4.3:

- `scripts/shots.py` (`make shots`) captures every page of a throwaway console — memory store, local adapter, two
  in-process fake gRPC workers, fictional groups, users, keys, secrets, an image pin, a backup, a deploy and a
  worker log — with Playwright at 1440x900, device scale 2. It seeds and tears down everything it uses; it never
  touches a cloud, a real key or a real repository.
- `docs/img/shots.json` records, for every screenshot, which page it shows and which release it was captured for
  (`captured_for`), plus `ui_contract`: the release whose console UI the shots must match.
- `console/tests/test_docs_images.py` fails when a docs-referenced image is missing, when a screenshot has no entry,
  or when any `captured_for` is older than `ui_contract`. **A round that changes the console raises `ui_contract`,
  and the gate then demands fresh captures before the release can go out.**
- `make shots OUT=reports/ui-<version>` writes a set elsewhere, which is how a before-and-after review set is made.

## 16. v0.5.0 — Streamable HTTP at the edge, gRPC inside (binding; supersedes §11 where they differ)
Source: `instructions/v0.5.0.md`. Decision D31: **Streamable HTTP is the front door; gRPC stays internal and as a
documented option.** gRPC needs a locally installed bridge, which rules out phones, browsers and hosted agent
platforms; Streamable HTTP is a URL and a header and is what the MCP spec defines. D19's removal of the HTTP MCP
endpoint is reversed by the user's own decision. Sub-decisions D32–D35 below.

### 16.1 Transport (X1, X3) — D32: the handler lives in the node, on the same port
- `ramen-node` serves `POST /mcp`, `GET /mcp` and `DELETE /mcp` on `RAMEN_NODE_PORT` **alongside** the gRPC
  services, on one listener (hyper speaks HTTP/1.1 and h2/h2c; the LB routes `/mcp` by path, gRPC by service path).
- **Shared guards, not reimplemented.** The HTTP handler builds a `MetadataMap` from the request headers and calls
  the same `auth::check_key`, `auth::client_ip` + CIDR check, the same inflight semaphore and the same JSON-RPC
  dispatch (`mcp::*`) that `ramen.v1.Mcp/Call` calls. There is one code path per check; the transports differ only
  in how a denial is spelled: gRPC `UNAUTHENTICATED` ↔ HTTP `401` (+ `WWW-Authenticate: Bearer`), `PERMISSION_DENIED`
  ↔ `403`, `RESOURCE_EXHAUSTED` ↔ `429`, `OUT_OF_RANGE` (4 MiB) ↔ `413`, `UNAVAILABLE` (not loaded) ↔ `503`.
- **Streamable HTTP semantics** (MCP spec 2025-06-18): `POST /mcp` with `Content-Type: application/json` carries one
  JSON-RPC message; a request gets `200 application/json` with the JSON-RPC response; a notification gets `202` with
  no body; `Accept` must include `application/json` (`406` otherwise). `GET /mcp` answers `405` — the node has no
  server-initiated messages, and saying so is spec-legal. Header `MCP-Protocol-Version` is echoed; an unsupported
  value is `400`.
- **Origin validation (DNS rebinding).** When the request carries `Origin`, it must match `RAMEN_ALLOWED_ORIGINS`
  (comma list of origins, `*` allowed for local dev only; default empty = **every browser origin refused with 403**)
  or, when the list is empty, be absent. Non-browser clients send no `Origin` and are unaffected. The chart and both
  renderers leave it empty on deployed workers unless the group sets it; it is a `DEPLOY_KEYS` entry so a group can
  allow its own web client.
- **Routing metadata** stays `ramen-group` / `ramen-zone`, as HTTP headers. The LB rules already match on them.
- **Access log**: the same one-line JSON per call, `transport: "http"|"grpc"` added, `http_status` beside `grpc_code`.

### 16.2 Sessions (X3) — D33: stateless, signed, bound to the credential
- `initialize` returns `Mcp-Session-Id: <nonce>.<expiry>.<mac>` where `mac = HMAC-SHA256(RAMEN_SESSION_SECRET,
  nonce ‖ expiry ‖ key_id)`; nonce 128 bits random, expiry = now + `RAMEN_SESSION_TTL_SECS` (default 1800).
- Any pod in the zone verifies it without state, so no affinity, no store, and a rollout keeps sessions alive.
- A session id presented with a different credential, past expiry, or with a bad MAC is `404` — the spec's signal to
  re-initialize — never `401`, so a stolen id alone reveals nothing and cannot be replayed under another key.
- `DELETE /mcp` with a valid session is `204`; ids are not stored, so "ending" a session is the client forgetting
  it; the TTL bounds the window. Requests without a session header are still served (sessions are optional in the
  spec); a session header on ANY request, `initialize` included, must validate or the call is `404` — the spec's
  instruction to the client is then to start over with a new `initialize` that carries no id, and the node never
  lets an unverifiable id ride along on the request that would mint its replacement.
- `RAMEN_SESSION_SECRET` is written into the deploy Secret by the console per zone (32 random bytes, base64,
  generated once per group and kept in the store like the MCP keys); absent → the node generates one at startup and
  logs that sessions will not survive a rollout. Not a `DEPLOY_KEYS` entry — a group cannot set its own.

### 16.3 OAuth for end users (X5) — D34: the console is the authorization server; pre-registered clients; PKCE
- Discovery: the **worker** answers `GET /.well-known/oauth-protected-resource` (RFC 9728) naming the console as
  its authorization server; the **console** answers `GET /.well-known/oauth-authorization-server` (RFC 8414) with
  `authorization_endpoint`, `token_endpoint`, `code_challenge_methods_supported: ["S256"]` — and no
  `scopes_supported`, which would list every group and zone to anyone who asks (review 0.5.0 L7). The worker's
  `WWW-Authenticate` names `resource_metadata` as an absolute URL when the deploy set `RAMEN_PUBLIC_URL` on it,
  relative otherwise (a pod cannot know its public address on its own).
  The metadata's `resource` is the worker's MCP URL (`<RAMEN_PUBLIC_URL>/mcp`, else from the request's
  `X-Forwarded-Proto`/`Host`) — RFC 9728 clients compare it with the URL they were given (0.5.92, found with Claude
  Code); `scopes_supported` is `[mcp:<group>:<zone>]`. The authorize request's `resource` (RFC 8707) may be that
  URL or the scope string; the token's `aud` is always the scope.
- `GET /oauth/authorize?response_type=code&client_id&redirect_uri&scope&state&code_challenge&code_challenge_method=S256
  &resource`: the user signs in to the console (existing password/OIDC/magic link; the login redirect carries the
  whole query, so the request survives sign-in) and sees a consent page naming the client, the group and the zone
  (`scope = mcp:<group>:<zone>`, exactly one, for a zone the group has deployed to, and the user must have at least
  viewer access to the group); on approval a single-use code (10 min) is bound to the client, redirect URI, PKCE
  challenge, user, scope and the user's session epoch. Every validation failure is an error page — never a
  redirect, because the redirect URI is only trusted once it has matched the registered client (exactly, except
  that a registered plain-http loopback URI matches any port, RFC 8252 §7.3). A denied consent redirects with
  `error=access_denied`, appended to whatever query the URI already carries. Only a signed-in user may authorize:
  an `rmn_` API key on this endpoint is `403`.
- `POST /oauth/token` (`grant_type=authorization_code` + `code_verifier`, or `refresh_token`): returns a JWT access
  token (HS256; key = `HMAC-SHA256(RAMEN_SESSION_SECRET, "oauth")`, the derivation `node-rs/src/session.rs` and
  `console/oauth_server.py` share; claims `iss` console URL, `sub` user id, `email`, `aud` = **`mcp:<group>:<zone>`**
  — the resource identifier the worker publishes, not a URL, because a pod behind a load balancer cannot know its
  public address — `scope` (the same string), `group`, `zone`, `iat`, `exp` = `iat` + 1 h, `jti`, `client_id`), and
  an opaque refresh token (30 days, stored under its SHA-256, rotated on use, bound to its client, revoked with the
  user's session epoch). A rotated-out refresh token presented again is reuse: every refresh token of that grant
  is revoked (RFC 9700 §4.14.2). At every mint — code exchange and refresh alike — the console re-checks that the
  user exists, may log in, still has viewer access to the group and has the epoch the grant was made under; the code
  or token is burned in the same store transaction as it is read. The issuer is `RAMEN_PUBLIC_URL`; a deploy without it leaves `RAMEN_OAUTH_ISSUER` unset on the worker, so
  tokens are simply not accepted there and the job log says so.
- Clients are **pre-registered** by a super admin: `POST /api/v1/oauth/clients {name, redirect_uris}` → `client_id`
  (public client, no secret; PKCE is the proof). Listed and deleted on the Config page (API keys page before 0.5.92). No dynamic registration
  (RFC 7591) in this release — it is an unauthenticated write endpoint and is deferred with that reason stated.
- The node accepts **either** an `rmk_` key **or** a JWT in `Authorization: Bearer`: a token is verified (signature,
  `exp`, `aud` matches this worker's group/zone, `scope` names this group/zone); a valid token's `sub` is what the
  access log records as the consumer (`key_id` stays for keys). A token for another group/zone is `401` — it is
  not a credential for this worker at all, and the challenge tells the client where to get one that is.
- Revocation: the console's session epoch (V1.4) is embedded in refresh tokens; bumping it (password/role change,
  delete, restore) invalidates refresh. Access tokens live ≤ 1 h and are not revocable individually — stated in the
  security how-to.

### 16.4 Defaults and clients (X2, X6, X7)
- Quickstart, `make demo`, `deploy/local/mcp-client-config.example.json`, the README and the how-tos land a new user
  on `http://localhost:8080/mcp` with `Authorization: Bearer <rmk_>` — no bridge to install. The bridge is documented
  once, under "stdio-only clients", as the compatibility path; it keeps working unchanged.
- `RAMEN_MCP_KEY` is read by the bridge (`--key` wins) and by every example config, so a key never has to sit in a
  config file. The docs say so on the first page a new user reads.
- Windows: the client flow (bridge over stdio with the key from the environment, and a Streamable HTTP call with the
  `mcp` SDK) runs in CI on `windows-latest` against an in-process fake worker; path handling in the bridge and the
  runtime is checked there.

### 16.5 Verification (X4, X8)
- `tests/conformance/test_mcp_node_local.py` and the harness `mcp_client` are parametrised over `grpc` and `http`:
  every guard case (no key, wrong key, CIDR, size, blocked names, inflight, health/readiness) runs on both, plus the
  HTTP-only cases (Origin, session id reuse under another key, expired session, 405 on GET, 202 on notification,
  406 on a bad Accept). `mcp>=2`'s Streamable HTTP client drives the SDK-level case end to end.
- CI: `node-conformance` runs both transports; a `client-windows` job runs the Windows client flow.
- GKE: both transports through the same Gateway (`/mcp` added to `route.paths` for both providers; the ALB rule gains
  the path), the security matrix over HTTP, and the OAuth flow against a real console URL. Reported in
  `reports/cloud-v0.5.0.md`. The AWS path stays untested: no account (F10.3).

### 16.6 Security (X9) and positioning — D35
- An independent subagent audits the new surface (HTTP handler, sessions, Origin, OAuth endpoints, token
  verification, the Windows path) against the code and files `reports/security-v0.5.0.md`; the coordinator fixes
  what it finds before the release is tagged.
- `docs/threat-model.md` is published: what Ramen defends against, what it does not, and what is unverified.
- README leads with what is verified today. CHEAPER carries a number (one shared runtime per zone versus a
  container per MCP server, with the pod count for a thirty-tool team). EASIER ("git push to a fleet") is claimed
  only once HTTP is the front door — which this release makes true, so it may be claimed for HTTP clients and no
  further. SECURE is one sentence: user code never runs in the process that holds keys and auth. The unverified
  list stays in the README as a feature of the pitch, not a footnote.

## 17. v0.5.1 — the end-to-end guide, its screenshots, and the GKE proof of 0.5.0 (binding)
- 17.1 One how-to, `docs/how-tos/end-to-end.md`, walks the whole path **console → worker → AI client** twice: on the
  local compose stack and on GKE. Same numbered steps in both columns of the story (sign in, zone, group +
  environment, key, deploy, connect a client over Streamable HTTP, per-user access through OAuth, what the audit
  and access log then show), so a reader can do it on a laptop first and repeat it in a cloud with the same
  muscle memory. Every console step has a screenshot; the client side is shown as the exact config a client
  needs plus the real transcript of the call. Screenshots of Claude Desktop and Cursor are the maintainer's
  (they cannot be generated) and are referenced only once they exist.
- 17.2 Screenshots are generated by `scripts/shots.py` as before (§15) and the rig grows two captures — the OAuth
  consent page and the one-time key display — and a **live mode** (`--base URL --email --password`, capture only,
  no seeding, no server) that takes the same set from a real console, which is how the GKE pictures are made.
  Live captures go to `docs/img/gke/` and are recorded in `shots.json` under `live` with the cluster date; the
  staleness gate (§15) applies to the seeded set only — a cloud shot is redone when a cloud run is.
  `ui_contract` becomes `0.5.1` (the API keys page gained the OAuth clients section in 0.5.0).
- 17.3 The GKE run of 0.5.0 (X8): throwaway project, `deploy/README.md` §GCP bring-up with
  `console.env.RAMEN_PUBLIC_URL`, then — in this order, each recorded in `reports/cloud-v0.5.0.md` — the console
  journey (17.1) by hand through the UI, `scripts/cloud_smoke.sh` with `RAMEN_SMOKE_CA`, the cloud suites
  (`tests/cloud`, including 0.4.1's permissions and backup-restore tests), a Streamable HTTP call and an OAuth
  round trip (register client, authorize, exchange, call with the token) through the load balancer, then
  `terraform destroy` and project deletion with `gcp_cost_check.sh --expect-empty`. What passes moves from "not
  yet run in a cloud" to "verified" in the README, the transport wiki and the threat model; what fails is fixed
  in this release or stated.
- 17.4 Version 0.5.1 (patch): no protocol or API change; the changelog names the proof, the guide and any fix
  the run forced.
