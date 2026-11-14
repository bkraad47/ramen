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
Group environment (0.6.0): the runtime renders `<bucket>/mcp/env.yaml` (else `env.yml`, `.env`; flat `KEY: value` / `KEY=value`, `#` comments) at `runtime.load`, substituting `{{$group.VAR}}` from `RAMEN_SECRET_*`, exports every key to its own process environment (overriding), skips a reference with no secret with a warning, lists the keys as `env` in the load result and redacts rendered values from errors. Zone teardown (0.6.0): `Cloud.detach_zone(group, zone)` destroys the namespace and the zone identity; the console calls it when a zone is deleted, dropped from an environment with no other environment of the group using it, or when such an environment is deleted, then removes the worker record. HTTPS only (0.6.0): with `RAMEN_COOKIE_SECURE=1` the console answers plain http (by `X-Forwarded-Proto`, else the request scheme) with 301 to https for GET/HEAD and 403 otherwise, `/healthz` and `/readyz` excepted. Config precedence: `RAMEN_CONFIG` file (flat `KEY=value` / `key: value`) < env < `<bucket>/.ramen/env-<zone>` (or `.ramen/env`) written by the console on deploy. The deploy file may only set deploy-scoped keys (MCP keys are unioned, group/env/zone labels, verbose, timeouts); it can never change bucket, port, python path, admin key, or proxy trust. Extra env: `RAMEN_LOG_FILE` (log mirror the console Logs page tails), `RAMEN_CALL_TIMEOUT_SECS`. No `RAMEN_MCP_KEYS` and no deploy file = deny all.

## 4. Console (console/)
Port `RAMEN_CONSOLE_PORT` (default 8000; TLS terminated by ingress/LB; local compose serves https on 8443 with a self-signed cert).
Storage interface `ramen_console.storage.base.Store` (async): `get/put/delete/list(collection, filters)` + `transaction`. Collections: `users, groups, environments, zones, workers, secrets, api_keys, audit, activity, config, backups`. Adapters: `memory` (tests), `firestore` (honors `FIRESTORE_EMULATOR_HOST`), `dynamodb` (tests use moto). Selected by `RAMEN_STORE=memory|firestore|dynamodb`. Fields marked sensitive (password hashes, secret values, API key hashes, github tokens) are Fernet-encrypted with `RAMEN_FERNET_KEY` before write.
Cloud interface `ramen_console.cloud.base.Cloud`: `sync_repo(group, repo_url, ref, token) -> bucket_uri`, `deploy(group, env, zone, canary=True)`, `rebalance(group, zone)`, `workers(group, zone) -> [{id, load, metrics}]`, `logs(group, zone, worker=None, tail=500)`, `set_ip_rules(group, zone, cidrs)`, `create_service_account(group, zone)`, `refresh()`. Adapters: `local` (filesystem bucket at `RAMEN_BUCKET_ROOT/<group>`, deploy = write env file + `Admin/Reload` on each worker via `ramen_console.grpcclient` (§11), logs = worker container stdout file), `gcp` (§7), `aws` (§8, untested on a real account). Selected by `RAMEN_CLOUD=local|gcp|aws`.
Roles (0.5.95, D41: **per group** — `user.memberships: {group: role}`; `role`/`groups` on a user are derived summaries; one person may hold a different role in each group; `rbac.can(p, role, group)` is the one rule, `require()` the one gate for pages and API; Config, API keys, Backups, Audit and group deletion are super-admin only, secrets admin-only, service-account requests are decided by another admin of the group or a super admin): `super_admin` (global), `group_admin` (per group), `viewer` (per group), `mcp_user` (per group; 0.5.92, D39: may only sign in and approve an OAuth client for a zone of their groups — no console page, no API; `/api/v1/me` and `/oauth/authorize` are the only things that answer them). Bootstrap super admin from `RAMEN_ADMIN_EMAIL`/`RAMEN_ADMIN_PASSWORD` on first start. Sessions: signed cookie. Passwords: argon2. API keys: `rmn_<id>_<secret>`, stored hashed (0.7.5: §21.8), scoped to role+groups, header `X-Ramen-Api-Key` on `/api/v1/*`.
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
| config / refresh | `/api/v1/config` (GET), `POST /api/v1/config/reload`, `GET\|PUT /api/v1/config/sa-rules`, `POST /api/v1/refresh` | super admin |
| base URI (v0.6.1) | `GET\|PUT /api/v1/config/base-uri` {base_uri} → {base_uri} | super admin; §18; 422 on an invalid value; `""` unsets |
| service account | `POST /api/v1/groups/{group}/zones/{zone}/service-account` → {name,…} | super admin |
| sa-restrictions | `PUT /api/v1/groups/{group}/sa-restrictions` {rules} | 409 on clash with super-admin rules |
| verbose | `POST /api/v1/groups/{group}/environments/{env}/verbose` {verbose} | |
| requests | `POST\|GET /api/v1/requests`, `POST /api/v1/requests/{id}/approve` | permission requests (F4.2) |
| restore / password | `POST /api/v1/backups/{id}/restore` {dry_run,prune,reconcile,force} (§13.1), `POST /api/v1/users/{id}/password` | |
| revocation (v0.4.1) | `POST /api/v1/requests/{id}/deny`, `POST /api/v1/requests/{id}/revoke`, `DELETE /api/v1/groups/{group}/zones/{zone}/permissions/{permission}` | super admin (§13.2) |
| worker images (v0.4.1) | `GET\|POST /api/v1/groups/{group}/images`, `PUT\|DELETE /api/v1/groups/{group}/images/current` | GET: viewer of the group; writes: super admin (§13.3) |
| logs | `GET /api/v1/logs?group&zone&worker?&tail&download=1` → text/plain (+Content-Disposition attachment) | |
| requests (v0.3.0) | `POST /api/v1/requests` {group,zone,permission} → 201 {id,type:"permission",status}; approve → {status:"approved",applied:{ok,permissions,…}} | group admin of the group; 422 unknown permission, 409 denied by super-admin/group rules (audited) |
| policy | `GET /api/v1/policy/permissions` → [{permission,desc,gcp:[roles],aws:[actions]}] | catalogue from `policy/permissions.py` |
| blocked | `PUT /api/v1/groups/{group}/environments/{env}/blocked` {blocked:[names]} → env doc | admin; applied by the next deploy as `RAMEN_BLOCKED` |
| config/auth | `GET\|PUT /api/v1/config/auth` {password_login?,magic_link?} → {password_login,magic_link,break_glass,providers,mail} | super admin; 422 when disabling passwords would lock everyone out |
| auth forms | `GET\|POST /auth/reset` {email} (200 always), `GET\|POST /auth/reset/{token}` {password} → 303 /login; `POST /auth/magic` {email}, `GET /auth/magic/{token}` → 303 + session; `GET /auth/{name}/login` → IdP, `GET /auth/{name}/callback` | tokens single use (reset 24 h, magic 15 min); `/auth/oauth/{name}/…` kept as aliases |
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
- **Console GCP adapter (`ramen_console.cloud.gcp`)**: in-cluster kube config (fallback `KUBECONFIG`). `sync_repo` → git clone/fetch to temp then upload to `gs://ramen-<project>-groups/<group>/` (bucket created by Terraform var `groups_bucket`, one bucket, per-group prefix; IAM per group via prefix conditions). `deploy(canary=True)`: write Secret `ramen-deploy` (secret values fetched from Secret Manager), `kubectl rollout restart worker-canary` → wait ready → `Admin/Reload` on the canary pod IP:8080 → `Mcp/Call tools/list` smoke (`Health/Check` SERVING when the group has no MCP keys; §11) → then restart `worker` → wait ready (since 0.6.1 a canary deploy leaves the stable Deployment's pod template — image, size — as it was until the canary passed, then applies it in that same roll; `worker-canary` rolls in place, `maxSurge 0`); on any failure scale canary to 0 and return the error; job log lines are streamed to the console job record. `workers()` = pods of `worker`+`worker-canary` + each pod's `Admin/Metrics` (direct pod IP; the former `RAMEN_GCP_POD_PROXY` mode is gone, §11). `logs()` = Cloud Logging `resource.type="k8s_container" AND resource.labels.namespace_name="ramen-<group>-<zone>"` (+ `pod_name` for one worker), newest N, downloadable. `rebalance(group, zone)` = set the backend service capacity scaler for that zone's NEG backend (0.5 when `high`, 1.0 otherwise) via compute API, then trigger an HPA-friendly `scale` if count differs. If the Gateway has not programmed a backend yet it returns 200 with `applied:false` and a `note`, never an error; if the backend service is busy (Gateway reconciling after a rollout) the capacity change is retried in the background and the response says `applied:false` with a pending note. `set_ip_rules` = Cloud Armor policy `ramen-<group>` (allow listed CIDRs, deny rest) attached to the worker backend service; also patched into `RAMEN_ALLOWED_CIDRS` of the deploy Secret. Without a programmed backend it returns 200 with `attached:false` and a `note`. Changing IP rules rolls the zone's worker pods (env comes from the Secret at pod start) so the node enforces immediately; the Cloud Armor attach retries in the background when the backend service is busy (`attached:false` + note). `attach_zone` ensures the zone identity before the first deploy (v0.3.2: it creates the GSA, its baseline grants and the WI binding when the zone KSA carries no GSA annotation — workers otherwise start with no identity and every bucket read fails 403). `create_service_account(group, zone)` = the same GSA `ramen-<group>-<zone>@<project>` with `storage.objectViewer` bound on the groups bucket (conditioned to the group prefix) + `secretmanager.secretAccessor` bound on each existing `ramen-<group>-*` secret (new secrets are bound at creation by the `gcp` secrets backend; v0.3.1 §11: no project-level bindings, the console GSA has no `projectIamAdmin`; project-wide roles from approved permissions need Terraform `console_project_iam=true`, otherwise they are reported as `skipped`) + WI binding to KSA `ramen-<group>-<zone>/worker`; extra roles only via super-admin rules (F4.2). `refresh()` = list namespaces/deployments/GSAs and reconcile the store. `detach_group(group)` (called by group delete, F4.1) deletes every namespace labelled `ramen.io/group=<group>`, every GSA `ramen-<group>-*` and the Cloud Armor policy `ramen-<group>` (cleared from any backend service still using it first); failures abort the delete. A failed deploy always scales `worker-canary` to 0, including a canary left by an earlier deploy.
- **Secrets backend**: `RAMEN_SECRETS_BACKEND=store|gcp`. `gcp` stores values in Secret Manager as `ramen-<group>-<env>-<zone>-<NAME>` (labels group/env/zone) and the store keeps only the name + resource ref. Viewers/admins never read values back through the console.
- **Console runtime env on GCP**: `RAMEN_STORE=firestore`, `RAMEN_CLOUD=gcp`, `RAMEN_SECRETS_BACKEND=gcp`, `RAMEN_GCP_PROJECT`, `RAMEN_GCP_REGION`, `RAMEN_GROUPS_BUCKET`, `RAMEN_IMAGE_WORKER`.
- **Worker exposure (added during 0.2.0; v0.3.1 routing per §11)**: MCP clients reach workers through the same global LB at `https://<console_ip>` with gRPC metadata `ramen-group`/`ramen-zone`. GKE Gateway API: Gateway `ramen` in `ramen-system` (class `gke-l7-global-external-managed`, static IP, self-signed cert, `allowedRoutes` namespaces selector `ramen.io/routes=true`); console HTTPRoute for `/`; each worker namespace (labelled `ramen.io/routes=true`) gets HTTPRoute `worker` matching headers `ramen-group=<group>`, `ramen-zone=<zone>` + one PathPrefix rule per service in `route.paths` (default `/ramen.v1.Mcp`, `/grpc.health.v1.Health` and both reflection services; `/ramen.v1.Admin` stays cluster-internal; no rewrite) — routing Health and reflection matters because anything unmatched falls through to the console route → Service `worker:8080` (`appProtocol: kubernetes.io/h2c`), plus HealthCheckPolicy `worker` of type GRPC. The LB backend service is auto-named by GKE; the console discovers it by NEG name `ramen-<group>-<zone>` for rebalance and Cloud Armor attachment (fallback `ramen-<group>`).
- **Console RBAC (v0.3.1, §11)**: KSA `ramen-system/console` has ClusterRole `ramen-console` for namespaces, networkpolicies, httproutes, healthcheckpolicies/gcpbackendpolicies, rolebindings and `bind` on ClusterRole `ramen-console-zone` only; `ramen-console-zone` (serviceaccounts, secrets, services, deployments, HPAs, pods read) is unbound cluster-wide and bound by the console through RoleBinding `ramen-console` in every `ramen-<group>-<zone>` namespace it attaches (a namespaced Role would be refused by RBAC escalation prevention, hence the RoleBinding→ClusterRole form). A `GCPBackendPolicy` sets the console backend timeout to 300s because IAM/compute operations exceed the LB's 30s default.
- **Sizes**: worker size presets `s` (250m/512Mi), `m` (500m/1Gi), `l` (1/2Gi); super admin sets allowed sizes per group+zone, admins set count (HPA min/max).

## 8. AWS deployment (v0.3.0, binding; applied to a real account since 0.5.6, so D18's "untested" no longer holds)
Mirror of §7 with AWS primitives. Everything is built and unit-tested with moto/fakes; nothing is applied to a real account and the docs must say so.
- **Terraform (`deploy/terraform/aws`)**: EKS cluster `ramen` (one small managed node group, var `region` default `us-east-1`), DynamoDB table `ramen` (pk/sk single-table, on-demand; matches the existing `dynamodb` store adapter), S3 groups bucket `ramen-<account>-groups`, Secrets Manager, ECR repos `ramen/console` + `ramen/worker`, console IAM role (IRSA) with S3/SecretsManager/DynamoDB/EKS-describe/IAM-create-role (scoped by path `/ramen/`)/WAF/ELB permissions, AWS Load Balancer Controller + Fluent Bit → CloudWatch Logs (Container Insights) installed via Helm from Terraform, a self-signed cert imported into ACM (no domain), outputs `cluster_name`, `region`, `console_url`, `ecr_console`, `ecr_worker`, `console_role_arn`.
- **CloudFormation (`deploy/cloudformation/ramen.yaml`)**: the same base resources (EKS, node group, DynamoDB, S3, Secrets Manager, ECR, IAM roles, OIDC provider) as one template with parameters, for teams that cannot run Terraform. Helm steps are documented, not templated.
- **Helm**: `deploy/helm/ramen` gains `provider: gcp|aws`. On AWS: console Ingress class `alb` with annotations `group.name: ramen`, `scheme: internet-facing`, `certificate-arn`, `listen-ports [{"HTTPS":443}]`; each worker namespace gets an Ingress in the same ALB group with header conditions `ramen-group`/`ramen-zone` + the gRPC paths of `route.paths` (Mcp, Health, reflection; Admin internal), `backend-protocol-version: GRPC` target groups and a gRPC health check (success code 0) — v0.3.1 §11; since 0.6.1 a second Ingress `worker-http` (same group, conditions and weighted split) carries `/mcp` and `/.well-known/oauth-protected-resource` on `HTTP1` target groups health-checked on the metadata path (`200-499`), because a GRPC target group answers `464` to non-gRPC requests; `RAMEN_MCP_PATH_PREFIX` is gone; worker KSA annotated with the group+zone IAM role (IRSA); pods get `RAMEN_BUCKET_URI=s3://<bucket>/<group>`.
- **Runtime**: `ramen_runtime.bucket.sync` handles `s3://` (boto3) as it does `gs://`.
- **Console AWS adapter (`ramen_console.cloud.aws`)**: `sync_repo` → S3 prefix; zone attach = namespace + manifests + Ingress; deploy = same canary flow as GCP; `workers()` = pods + `Admin/Metrics` (§11); `logs()` = CloudWatch Logs Insights query on the Container Insights log group filtered by namespace/pod, downloadable; `rebalance()` = weighted target groups (stable/canary) via the Ingress `actions` annotation; `set_ip_rules()` = WAFv2 IPSet + web ACL associated to the ALB (allow list, default block) + `RAMEN_ALLOWED_CIDRS` in the deploy Secret + worker roll; `create_service_account()` = IAM role `ramen-<group>-<zone>` trusting the cluster OIDC provider for KSA `ramen-<group>-<zone>/worker`, policies scoped to the group's S3 prefix and secrets path; `detach_group` deletes namespaces + roles + the group's WAF IP sets; `refresh` reconciles. Since 0.6.1: before any canary pod change the adapter sets the stable/canary split to 100/0 and waits `RAMEN_ALB_SPLIT_DRAIN_SECS` (default 15), and gives the canary its share back only after it passed; a match-all CIDR (`0.0.0.0/0`, `::/0`) goes to the WAF IP set as its two `/1` halves (IP sets take /1–/32); `wafv2:GetWebACLForResource` is granted on `*` (it names no web ACL), associate/disassociate stay scoped. Secrets backend `aws`: Secrets Manager names `ramen/<group>/<env|all>/<zone|all>/<NAME>` with tags; store keeps `sm://`-style ref `asm://`.
- **Console env on AWS**: `RAMEN_STORE=dynamodb`, `RAMEN_CLOUD=aws`, `RAMEN_SECRETS_BACKEND=aws`, `RAMEN_AWS_REGION`, `RAMEN_GROUPS_BUCKET`, `RAMEN_IMAGE_WORKER`, `RAMEN_EKS_CLUSTER`, `RAMEN_ALB_GROUP=ramen`, optional `RAMEN_AWS_PERMISSIONS_BOUNDARY` (v0.3.1: worker roles are created with the `ramen-worker-boundary` managed policy from Terraform/CloudFormation; the console role may only create `/ramen/` roles carrying it; `-` disables).
- Apply the §7 lessons: retry/background on ELB/WAF propagation, per-call boto3 clients (thread-safe by design), throttling backoff.
- Settled during the build (deviations from the wording above): the web ACL `ramen` defaults to **allow** because the console shares the ALB; each group's IP rules add a rule that blocks the group's traffic (matched on the `ramen-group` header since v0.3.1) unless the source is in the group's IP set. `rebalance()` on AWS shifts the stable↔canary target-group weights on the zone's Ingress (there is no cross-zone capacity scaler on an ALB path rule) and returns `applied:false` + note until the controller has created the ALB. The worker image installs the runtime with the `gcp` and `aws` extras. `apply_sa_permissions` on AWS writes an inline policy `ramen-sa-permissions` on the worker role.

## 9. Auth, policy, tool blocking (v0.3.0, binding)
- **OAuth/OIDC (F6.1)**: providers from yaml/env (`RAMEN_OAUTH_<NAME>_{ISSUER,CLIENT_ID,CLIENT_SECRET,SCOPES}`); login page shows a button per provider; callback `/auth/<name>/callback` links or creates the user by verified email; role mapping `auth.oauth.<name>.role_claim` + `role_map` (default viewer, no groups) — since 0.5.92 (D38) also editable by a super admin at `PUT /api/v1/config/auth/role-map/{provider}` (`{claim}` / `{value, role, groups}` / `{remove}`, stored in `config/auth`, merged over env with the store winning per value) and, once a claim is named for a provider, **authoritative on every login**: role and groups are replaced from the rules at each OIDC callback, no match → viewer with no groups, the bootstrap super admin exempt; `auth.password_login: false` (super admin toggle at `PUT /api/v1/config/auth`) disables email/password login except for the bootstrap super admin via `RAMEN_ADMIN_FORCE_PASSWORD=1` (break-glass). Audited.
- **Email auth (G12)**: SMTP from yaml/env (`RAMEN_SMTP_{HOST,PORT,USER,PASSWORD,FROM,TLS}`); invite email on user create, password reset (`POST /auth/reset`, `POST /auth/reset/{token}`), optional magic-link login (`auth.magic_link: true`). Dev backend `RAMEN_SMTP_HOST=file://<dir>` writes .eml files (tests use it). Never log message bodies.
- **SA policy engine (F4.2, F6.5)**: super-admin rules `[{effect: allow|deny, permission: <glob>}]` (existing) gate what group admins may request. `POST /api/v1/requests {group, zone, permission}` → super admin `approve` → `Cloud.apply_sa_permissions(group, zone, permissions)` binds the mapped cloud roles (gcp: IAM roles/conditions on the GSA; aws: IAM policy on the role; local: recorded only). Mapping table `ramen_console/policy/permissions.py` (e.g. `bucket.read` → `roles/storage.objectViewer` / `s3:GetObject`). Denied-by-rule requests are rejected with 409 and audited.
- **Tool blocking (F5.6)**: per environment `blocked: [names]` (`PUT /api/v1/groups/{g}/environments/{e}/blocked`); deploy writes `RAMEN_BLOCKED=<comma list>` into the deploy config; the node removes blocked names from `tools/list`, `resources/list`, `prompts/list` and answers `-32601` for calls to them; the UI packages page has a block/unblock toggle per item (admins).
- **Hardening (added after the 0.3.0 security audit)**: signing secret = `RAMEN_SESSION_SECRET` → `RAMEN_FERNET_KEY` → a random per-process secret (never a constant; set the env in production); security headers on every response (CSP self+inline, nosniff, DENY framing, referrer same-origin; HSTS when `RAMEN_COOKIE_SECURE=1`); per-IP rate limit on `POST /login`, `/auth/reset*`, `/auth/magic*` (`RAMEN_LOGIN_RATE_LIMIT`, default 20/min → 429); reset/magic tokens redacted from the access log; `next` after login must be a same-origin path; OIDC requires `email_verified: true` unless `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED=1` (superseded in 0.7.5 by §21.3/D51: no `email_verified` requirement); CSV exports neutralise formula cells; log queries accept only valid namespace/pod names (422 otherwise). Worker pods: `allowPrivilegeEscalation: false`, all capabilities dropped, seccomp RuntimeDefault, and a per-namespace NetworkPolicy allowing ingress only from `ramen-system` and the load-balancer ranges (GCP `35.191.0.0/16`, `130.211.0.0/22`; AWS the VPC CIDR).
- **CSRF**: HTML forms carry a per-session token (`ramen_csrf` cookie + hidden field / `X-Ramen-CSRF` header); the JSON API with an API key is exempt.

## 10. Docs site & release material (v0.3.0, binding)
- MkDocs Material in `ramen/docs/` (`mkdocs.yml` at repo root, `.github/workflows/pages.yml` deploys on push to main and on tags to GitHub Pages). Pages, as reorganized in 0.6.0: Home (logo, badges in one row, the three repositories, why), Get started (local run, connect an MCP repo, connect a client, a picture per step), How it works (architecture and every feature with where it is managed), Wiki (`deploy-gcp`, `deploy-aws`, `groups-zones`, `mcp-repo`, `secrets`, `users-access`, `connect-oauth`, `connect-password`, `api-keys`, `throttling`, `sso`, `backups`, `configuration`), Changelog, Release, and Reference (transport, threat model, cloud-ops skills, these contracts, contributing, the generated version tracker, architecture current and per release). Plus `llms.txt` and JSON-LD on the home page for discoverability.
- README: logo, badges, 5-command local quickstart that ends with a visible PASS (fixes fresh-user U1–U6: console URL/login, minting an `rmk_` key vs `rmn_` API keys, idempotent `make demo` with a success line), screenshots of login, dashboard, group, secrets, logs, deploy job (captured from the compose stack with Playwright into `docs/img/`), links to the guides and releases.
- `ramen/skills/` cloud-ops agent skills (agentskills style): deploy-gcp, deploy-aws, rotate-keys, backup-restore, scale-zone, with a validation sub-agent note.
- `reports/launch-posts-v0.3.0.md`: drafts for reddit, forums, agent portals, GitHub discussions, LinkedIn with hashtags; repo topics list. Nothing is posted by agents.

## 11. gRPC transport (v0.3.1, binding; supersedes §3 `/mcp` and §5/§7/§8 where they mention HTTP endpoints)
Decision D19. Protos: `proto/ramen/v1/mcp.proto`, `proto/ramen/v1/admin.proto` (single source; Rust via tonic-build, Python via grpcio-tools into `ramen_proto` packages vendored in console, runtime-py bridge and tests).
- **Node (`ramen-node`)** serves one h2c port `RAMEN_NODE_PORT` (default 8080) with services `ramen.v1.Mcp`, `ramen.v1.Admin`, `grpc.health.v1.Health` (SERVING once `runtime.load` succeeded; NOT_SERVING before). The axum HTTP server, `/mcp`, `/healthz`, `/readyz`, `/metrics`, `/admin/reload` are removed. Optional node TLS: `RAMEN_TLS_CERT` + `RAMEN_TLS_KEY` (PEM) switches the port to TLS (h2); otherwise the LB terminates TLS.
- **Security parity**: `Mcp/Call` requires metadata `authorization: Bearer <key>` validated with a constant-time compare against `RAMEN_MCP_KEYS` (UNAUTHENTICATED otherwise; empty key set = deny all); peer address (or the client hop of `x-forwarded-for`, see the v0.4.0 amendment below) must match `RAMEN_ALLOWED_CIDRS` (PERMISSION_DENIED); `Admin/*` requires `x-ramen-admin-key` and `RAMEN_ADMIN_CIDRS`; blocked names (`RAMEN_BLOCKED`) are filtered from `*/list` and answered with JSON-RPC `-32601`; message size limit 4 MiB (tonic codec → OUT_OF_RANGE); the initial `runtime.load` retries every `RAMEN_LOAD_RETRY_SECS` (default 5, 0 disables) until it succeeds, so a worker whose cloud identity is still propagating becomes ready without an admin reload; concurrency `RAMEN_MAX_INFLIGHT` → RESOURCE_EXHAUSTED; admin: bad key → UNAUTHENTICATED, outside admin CIDRs → PERMISSION_DENIED, reload failure → INTERNAL; health: service "" and `ramen.v1.Mcp` = readiness, `ramen.v1.Admin` always SERVING (liveness); server reflection enabled by default and off-able (`RAMEN_REFLECTION`, amendment below); JSON-RPC protocol errors are returned as gRPC OK with a JSON-RPC error body; denied calls are access-logged with `status: denied`; one JSON access-log line per call with the same fields as before plus `grpc_code`. Health is unauthenticated.
- **Routing**: clients (and the bridge) send metadata `ramen-group` and `ramen-zone`. GKE Gateway: worker Service `appProtocol: kubernetes.io/h2c` (fallback: node TLS + `HTTP2` if the Gateway rejects h2c), HTTPRoute matches `headers: [ramen-group=<g>, ramen-zone=<z>]` and one PathPrefix per exposed service (`route.paths`: Mcp, Health, both reflection services; Admin stays cluster-internal), no path rewrite; HealthCheckPolicy type GRPC. Verified live on GKE in v0.3.2 over h2c — the TLS fallback was not needed. AWS ALB: target group `backend-protocol-version: GRPC`, listener rule on the two headers, health check gRPC code 0; the Streamable HTTP paths on `HTTP1` target groups (0.6.1). Cloud Armor / WAF unchanged. The console targets pods directly (pod IP:8080) as before.
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
  relative otherwise (a pod cannot know its public address on its own). Since 0.6.1 the console's base URI (§18)
  is what the deploy sets, else its own `RAMEN_PUBLIC_URL`; a base URI with a path puts the metadata, the issuer
  and the resource under that path.
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
  or token is burned in the same store transaction as it is read. The issuer is the base URI (§18), else `RAMEN_PUBLIC_URL`; a deploy without either leaves `RAMEN_OAUTH_ISSUER`
  unset on the worker, so tokens are simply not accepted there and the job log says so. Workers take a changed
  issuer on their next deploy.
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
  `reports/cloud-v0.5.0.md`. The AWS path was first applied to a real account in 0.5.6.

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
- 17.1 One how-to, `docs/how-tos/end-to-end.md` (folded into `docs/get-started.md` by the 0.6.0 docs reorganization), walks the whole path **console → worker → AI client** twice: on the
  local compose stack and on GKE. Same numbered steps in both columns of the story (sign in, zone, group +
  environment, key, deploy, connect a client over Streamable HTTP, per-user access through OAuth, what the audit
  and access log then show), so a reader can do it on a laptop first and repeat it in a cloud with the same
  muscle memory. Every console step has a screenshot; the client side is shown as the exact config a client
  needs plus the real transcript of the call. Screenshots of Claude Desktop and Cursor are the maintainer's
  (they cannot be generated) and are referenced only once they exist.
- 17.2 Screenshots are generated by `scripts/shots.py` as before (§15) and the rig grows two captures — the OAuth
  consent page and the one-time key display — and a **live mode** (`--base URL --email --password`, capture only,
  no seeding, no server) that takes the same set from a real console, which is how the GKE pictures are made.
  Since the 0.6.0 docs reorganization **every screenshot in the docs is a live capture** from a real cloud
  deployment of the release: the set lives in `docs/img/`, is recorded in `shots.json` under `screenshots` with
  the release it was captured for and a `captured_from` line naming the run, and the staleness gate (§15) applies
  to all of it. Before that, live captures were a second set under `docs/img/gke/` recorded under a `live` key,
  with the gate applied to the seeded set only.
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

## 18. v0.6.1 — the console's public address (base URI) (binding)
- Stored as `config/base_uri` `{"base_uri"}`; `GET|PUT /api/v1/config/base-uri`, super admin, audited `config.base_uri`
  (tag `base_uri:<value>|-`, `error:invalid` on a refusal). Value: `""` (unset) or an absolute `http(s)` URL with an
  optional path, no query/fragment/credentials/whitespace; trailing slash stripped; else `422`. Edited on the
  Config page's *Public address (base URI)* card.
- Read per request by the outermost middleware, cached 5 s per replica (a save applies at once on the saving
  replica; a store error keeps the last value). Precedence everywhere: base URI → `RAMEN_PUBLIC_URL` →
  the request's own base URL.
- When set, every generated link goes through it: template links, forms, `hx-*`, static assets, redirects (login,
  `next=` — kept as an internal path and prefixed only when redirecting — logout, reset, https-only), mail links,
  RFC 8414 metadata and issuer, the upstream OIDC callback, client snippets, and the deploy's `RAMEN_PUBLIC_URL` /
  `RAMEN_OAUTH_ISSUER` (workers follow on their next deploy). Unset, output is byte-identical to 0.6.0.
- Requests still arrive at the console's root: a proxy serving a path prefix strips it. Cookies keep `Path=/`.
  The Swagger UI (`/api/docs`) loads `/openapi.json` from the root and does not follow a prefix.
- Recovery from a wrong value: `PUT /api/v1/config/base-uri {"base_uri": ""}` at the console's root.
- Prefix proxy recipe (verified on GKE with HTTPRoute `URLRewrite ReplacePrefixMatch`): `<prefix>/*` → console,
  stripped; `<prefix>/*` with `ramen-group`/`ramen-zone` → that zone's workers, stripped;
  `/.well-known/oauth-authorization-server<prefix>` → the console's `/.well-known/oauth-authorization-server`.
  Documented in `docs/wiki/configuration.md`.

## 19. v0.7.0 — gates, manifests, regions, output schemas (binding)
- **Manifest hash.** The runtime's load result and `Admin/Reload` carry `hash` = sha256 of the canonical JSON
  (`sort_keys`, no spaces) of `{"tools", "resources", "prompts"}` with each prompt's `_meta` removed; `errors`/`env`
  are not hashed. `Admin/Metrics` reports `manifest_hash` (the last load's `hash`, `""` before it). The console stores
  `env.last_deploy.manifest[<zone> <pod>] = {hash, tools, resources, prompts}` and `last_deploy.stable[zone]` (the
  stable track's key), shows the hash per zone and per worker, and flags workers of one zone that disagree.
- **Output schema.** A tool proto's `output` is a JSON Schema (invalid → that package's load error). `describe()`
  emits it as `outputSchema`; a non-object schema is wrapped as `{"type":"object","properties":{"result":<it>},
  "required":["result"]}` and the value as `structuredContent: {"result": v}`; an object schema and dict are used as
  declared. The runtime validates and answers `isError: true`, text `invalid output: <path>: <message>` on a
  mismatch, without `structuredContent`. Tools without `output` are unchanged (no `outputSchema`, no
  `structuredContent`).
- **Compatibility gate.** `Cloud.deploy(..., gate=None)`: adapters await `gate(canary_result, call)` after the
  canary's reload and smoke; a `GateError` takes the existing failure path (canary to 0, stable untouched). The
  console's gate diffs the canary manifest against `last_deploy.stable[zone]`'s manifest: removed tool/resource/
  prompt, removed property, new required property, changed `type`, narrowed `enum` (recursive; prompt arguments and
  `outputSchema` included) ⇒ breaking ⇒ refused unless `DeployIn.breaking` is true; descriptions and `_meta` are
  ignored; `job.result[zone].compat = {breaking, additive, against, forced}`. The local adapter runs the gate on
  every reload.
- **Golden cases.** `mcp/tests.yaml` `{cases: [{name?, tool, args, expect: {text_contains|subset|exact}}]}`, read
  through `Cloud.read_file(group, "mcp/tests.yaml")` (local clone, GCS blob, S3 object). Each case is a `tools/call`
  on the canary with the group's first MCP key; `{{$group.VAR}}` in `args` is resolved from the deploy's resolved
  config; a JSON-RPC error or a mismatch fails the case; any failure is a `GateError` naming the case;
  `job.result[zone].golden = {passed, failed}` or `{"skipped": "no MCP key"}`. A malformed file fails the job before
  any zone changes.
- **Blocked regions.** `config/regions` `{"blocked": {"gcp": [...], "aws": [...]}}`, `GET|PUT /api/v1/config/regions`
  (super admin, audited `config.regions`, unknown region → 422), names from `regions.json`. A zone's `region` is
  matched to a region by prefix on the provider's list (`us-west1-a` → `us-west1`; `us-west1` never blocks
  `us-west12`). Blocked ⇒ `POST /api/v1/zones` 403 `Region <r> is blocked by a super admin`; a deploy marks that zone
  `{"ok": false, "error": <same>, "workers": []}`, logs a `ramen.deploy` WARNING and continues with the other zones.
- **Denial reasons.** Every denied call's access-log line carries `reason` ∈ `no_key`, `bad_key`, `token_expired`,
  `token_invalid`, `cidr`, `blocked_name`, `throttled`, `origin`, `too_large`, `inflight`, `session_expired`,
  `session_invalid`; the credential is never logged; a blocked-name call logs `denied` (warn), wire `-32601` as
  before. The console audits `oauth.token`, `oauth.refresh`, `oauth.refresh_reuse`, `oauth.denied` (+ `reason:`).
- **Token lifetimes.** `RAMEN_OAUTH_ACCESS_TTL` (default 3600 s) and `RAMEN_OAUTH_REFRESH_TTL` (default 30 d), read at
  every mint, invalid values fall back with a warning; neither is an OAuth provider name.
- **Sessions and expiry (verified, no change).** Session ids are bound to `user:<sub>` for tokens, so a refreshed
  token keeps the session; an expired token gets the no-token `401` challenge byte for byte.
- **Images.** The release publishes `ramen-worker` and `ramen-console` for `linux/amd64,linux/arm64`, tags
  `<version>` and `latest`, to `ghcr.io/bkraad47/` always and `docker.io/bkraad47/` when the Docker Hub secrets exist.
- **Process (D42).** Versions are built on branch `v<version>`, merged to `main` by pull request, then tagged; the
  demo repo carries `VERSION` and the same tag; `check_versions.py` compares it when present.

## 20. v0.7.2 — per-tool access, rollout awareness, drift (binding)
- **Tool access (A8).** `environment.tool_access = {"<tool>": {"list": [kind…], "call": [kind…]}}`, kinds `key`,
  `group_admin`, `viewer`, `mcp_user`; `super_admin` always allowed; a tool absent from the map is unrestricted;
  `call ⊆ list`. `PUT /api/v1/groups/{g}/environments/{e}/tool-access` (group admin+, whole map, unknown kind → 422).
  Deploy hands `RAMEN_TOOL_ACCESS` (compact JSON) to every zone. Tokens carry `role` (the person's role in the scope's
  group; `super_admin`); a key is kind `key`; a token without `role` is `mcp_user`. Node: `tools/list` omits tools the
  kind may not list; `tools/call` → `-32601 tool not found` when not listable (log reason `tool_hidden`), `-32003
  forbidden: <tool> is not callable for <kind>` when listable only (reason `tool_denied`). Tools only.
- **Rollout awareness (B1-lite).** Session ids are `<nonce>.<expiry>.<hash12>.<mac>` (`hash12` = the manifest hash's
  first 12 hex at `initialize`, `""` before a load; 3-part 0.7.1 ids still verify as `hash12 = ""`). When a request's
  session `hash12` differs from the node's current hash and `Accept` includes `text/event-stream`, the node answers
  `text/event-stream` with `notifications/tools/list_changed` then the response; otherwise JSON. Once per session nonce
  per pod, per manifest version (a second rollout is announced again; 4096-entry set). `initialize` declares
  `tools.listChanged = true`. Nothing is pinned: the pod serves the current version at once; the notification only
  prompts a re-list. Behind a load balancer a session may be told once per pod. During a 0.7.1 → 0.7.2 rolling upgrade a
  0.7.1 pod answers a 4-part id `404` and the client re-initializes. `initialize` and notifications are never answered as
  SSE; an unknown `role` matches no list (fails closed).
- **Drift (C2).** `tools/call` log lines carry `args` = sha256 hex[:12] of the canonical arguments (never the values).
  `drift.repeat_rate(lines, window_s=60)` → `{tool: {calls, repeats, rate}}`, a repeat being the same `key_id` + `name`
  + `args` as another call within the window. `GET /api/v1/groups/{g}/zones/{z}/drift?tail=500` (viewer+) returns the
  map + `threshold`; `config/drift {"warn_pct": 30}`, `GET|PUT /api/v1/config/drift` (super admin); a rate ≥ threshold
  logs a WARNING `ramen.drift` (digest) and shows red on the group page's Drift card. No automatic rollback.
- **Not built (D44).** Per-user upstream credentials (tools acting as the person against Google/Microsoft) need a token
  broker and IdP consent in the console; deferred to a minor release.

## 21. v0.7.5 — guardrails in the runtime, identity federation, custom roles (binding)
Ramen's own design (D45–D48). Nothing here is derived from another product. Every item ships with positive,
negative and combination tests (the brief's acceptance bar); the combinations are listed at the end.

### 21.1 Runtime on Python 3.12 (D45)
- `runtime-py` `requires-python = ">=3.12,<3.13"` — 3.12 only, because the flagship guardrails engine (NeMo
  Guardrails) does not run on 3.14. Worker image and both Glama images build `FROM python:3.12-slim`; CI runtime jobs
  (`runtime`, `node-conformance`, compose, kind) install 3.12. The console stays on 3.14. `scripts/check_versions.py`
  fails when a runtime pin and the Dockerfiles disagree.

### 21.2 Guardrails (D46) — `mcp/guardrails.yaml` in the group repo
```yaml
engine: nemo            # nemo | policy | none (default none: the file may be absent)
config: mcp/guardrails  # nemo: a NeMo rails config dir (config.yml, *.co, actions.py); policy: dir holding policy.py
fail: closed            # closed (default) | open — what a hook error/timeout means
timeout_s: 10           # per hook call, default 10, max 120
tools:                  # per-tool opt-in; a tool not listed is never checked
  word_count: {pre: true, post: true}
  unit_convert: {pre: true}        # post defaults false
```
- Hook points, both inside `Executor.call_tool`: **pre** runs after argument validation and secret substitution and
  before the tool function (payload: tool name + canonical JSON of the *redacted* arguments — `{{$group.VAR}}`
  references stay as written, the secret value never reaches an engine); **post** runs after output validation
  (payload: tool name, the same as-written arguments, and the result text or the canonical JSON of
  `structuredContent` when present, with every resolved secret value replaced by `***` — the client's copy is untouched).
- Engines. `nemo`: `ramen_runtime.guardrails.nemo` loads `RailsConfig.from_path(config)` once per `runtime.load` and
  calls `generate(messages, options={"rails": ["input"]})` for pre (user message = payload, `name` = tool) and
  `options={"rails": ["output"]}` for post (user = pre payload, assistant = result). Blocked ⇔ an activated rail
  stopped; the engine's message is the bot message. Importing `nemoguardrails` is the `[nemo]` extra
  (`ramen-runtime[nemo]`), installed in the worker image; `engine: nemo` without it, or a config that fails to load,
  is reported in the load result (`errors: [{"package": "guardrails", "reason": "..."}]`), the tools still load, and
  every opted-in tool answers blocked (`guardrail unavailable: ...`) until a load succeeds — a broken engine never
  means an unguarded tool, whatever `fail` says (`fail` governs call-time errors only). `policy`: `policy.py` in
  `config` exports `pre(tool: str, arguments: dict) -> None | str` and/or `post(tool, arguments, result) -> None | str`;
  a returned string blocks with that message; `None` allows; an exception = hook error (→ `fail`).
- Verdict. Allowed → the call proceeds unchanged (engines never rewrite). Blocked → the tool result
  `{"content": [{"type": "text", "text": "guardrail blocked: <pre|post>: <message>"}], "isError": true,
  "_meta": {"ramen": {"guardrail": {"stage": "pre"|"post", "engine": "nemo"|"policy"}}}}` — a tool-call outcome
  (§2), not a JSON-RPC error; a post block never returns the tool's output. Hook error/timeout with `fail: closed` →
  blocked with message `guardrail unavailable: <reason>`; with `fail: open` → the call proceeds and the runtime logs
  `warn guardrail_error`. Runtime log line per hook: `guardrail tool=<t> stage=<s> verdict=allow|block|error ms=<n>`
  — never the payload.
- Node: a result carrying `_meta.ramen.guardrail` is logged with reason `guardrail_pre` / `guardrail_post` (access
  log, both transports); `_meta` stays on the result for the client. Order on `tools/call`: tool access (C10) first,
  then node-side argument validation, then the runtime (pre → tool → output schema → post).
- `runtime.load` result and `Admin/Reload` carry `guardrails: {"engine": "<e>", "fail": "...", "tools": {<tool>: ["pre","post"]}}`
  (`{"engine": "none", "tools": {}}` when absent); it is NOT part of the manifest hash (C1). The group page's Manifest
  section shows "Guardrails: nemo · word_count (pre, post), unit_convert (pre)" per zone. Golden cases (C3) run
  through the hooks like any call: a case whose tool is blocked by a rail fails the gate with the rail's message
  (that is the point of a test case).
- LLM-backed rails (`models:` in NeMo's config.yml) take their API keys from `mcp/env.yaml` (0.6.0: exported into the
  runtime process before load). The demo repo ships deterministic rails (no model, no cost).

### 21.3 Identity federation (D47)
- **Group source per provider.** `RAMEN_OAUTH_<NAME>_GROUPS = claim` (default, today's behaviour: the role claim is
  read from the ID token/userinfo) or `lookup`. With `lookup` the callback fetches the person's groups with the
  access token and injects them as claim `groups` (overriding any token `groups`): provider `entra` → Microsoft Graph
  `GET https://graph.microsoft.com/v1.0/me/memberOf?$select=id,displayName` (values: every `id`, plus `displayName`
  when Graph returns it — with `User.Read` alone it returns ids only, verified live 2026-10-10, so rules should name
  ids; follows `@odata.nextLink`; scope `User.Read` added when absent; add `GroupMember.Read.All` to
  `RAMEN_OAUTH_ENTRA_SCOPES` to map on display names); provider `google` → Cloud
  Identity `GET https://cloudidentity.googleapis.com/v1/groups/-/memberships:searchDirectGroups?query=member_key_id=='<email>'`
  (values: each `groupKey.id` (the group email) and `group` name; scope
  `https://www.googleapis.com/auth/cloud-identity.groups.readonly` added when absent). Other providers: `lookup` is a
  startup error. The lookup URL base is overridable (`RAMEN_OAUTH_<NAME>_GROUPS_URL`) so tests use a fake. A lookup
  failure (non-2xx, timeout 10 s) is a 502 `could not read your groups from <provider>` — nobody is signed in with
  stale groups. Config page: "Group source" select per provider (store doc `config/auth.groups_source[<provider>]`,
  env wins only when the doc has no value).
- **Providers are managed on the Config page (the user, 2026-10-10; D50).** A super admin adds, edits and removes
  identity providers in the browser: store doc `config/oauth_providers = {"<name>": {issuer, client_id, client_secret,
  scopes, groups_source}}`, the secret encrypted at rest like SMTP's and never read back;
  `GET /api/v1/config/oauth-providers` (masked), `PUT /api/v1/config/oauth-providers/{name}` (omit `client_secret` to
  keep it), `DELETE`; audited `config.oauth_provider`. `RAMEN_OAUTH_<NAME>_*` still works and is shown as source `env`;
  the store doc wins per provider name. The registry and the login page follow a change at once (no restart). Config
  page card "Identity providers (Entra ID, Google Workspace)", with the group-source select and the role-mapping rules
  next to it, so everything about a provider is in one place.
- **The provider's email is the account (the user, 2026-10-10; D51).** No `email_verified` requirement: the account is
  the `email` claim (or a `preferred_username` containing `@`); with no email the account is keyed on the provider's
  stable id (Entra `oid`+`tid`, else `sub`) as `provider_key = "<provider>:<id>"`, shown in place of the email until one
  arrives; neither → 403. `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED` is read nowhere. The provider callback is
  `public_url(request) + /auth/<name>/callback` (base URI → `RAMEN_PUBLIC_URL` → request), never the request scheme alone.
- **Membership from either side.** A user document carries `idp_memberships` (`{group: role}`, rewritten on every SSO
  login when the provider has a mapping) and `manual_memberships` (set on the Users page / `PUT|DELETE
  /groups/{g}/members/{uid}`). Effective `memberships` = `idp_memberships` overlaid by `manual_memberships` (an admin's
  entry for a group wins, whatever the IdP says; an admin entry absent → the IdP role stands). Super admin via the
  mapping is `idp_super`; set by hand stays `role: super_admin`. Users page shows the source per membership
  (`via entra`, `set here`). Documents from before 0.7.5: `memberships` → `manual_memberships`. Negative: a person
  removed from the IdP group loses that membership at the next login (sessions: epoch bump as today) unless an admin
  entry exists.
- **IdP and cloud independent.** Any configured provider works on any adapter; nothing in `cloud/` reads provider
  settings (test: both providers configured on the local adapter; GCP/AWS adapters' env unchanged).

### 21.4 Custom roles (D48)
- `config/roles` store doc: `{"<name>": {"base": "group_admin"|"viewer"|"mcp_user", "label": "...", "oauth_only": bool}}`;
  `GET /api/v1/config/roles`, `PUT /api/v1/config/roles/{name}` (body `{base, label?, oauth_only?}`), `DELETE`
  (super admin; audited `config.roles`). Name `^[a-z][a-z0-9_]{1,31}$`, not a built-in. Delete → 409 while any user
  membership, role-mapping rule or tool-access entry uses the name.
- A custom role is usable wherever a group role is: `memberships` (both sides), role-mapping rules, tool-access kinds.
  Console authorization uses the **base** (`rbac.can`); the token's `role` claim is the custom name; the worker gets
  `RAMEN_ROLES` (compact JSON `{name: base}`, deploy-time, every zone). Node: a caller's kind matches a tool-access
  list when the list names the kind **or its base**; an unknown name with no `RAMEN_ROLES` entry matches nothing
  (fails closed, as 0.7.2).
- **Per-role OAuth enforcement.** `oauth_only: true` → a person who holds that role in any group (either side) cannot
  sign in with a password or a magic link: `/login` answers 403 `This account signs in with <provider list>`; the
  OAuth AS login step (A2 password MCP users) is the same endpoint, so MCP clients land on the provider too. The
  break-glass bootstrap admin is exempt (as for mappings). The built-in roles cannot be made `oauth_only`.
- Config page: a "Roles" card (list, add, remove); the Users page, role-mapping and tool-access selects list custom
  roles after the built-ins.

### 21.5 Acceptance (combinations, besides each item's positive and negative tests)
1. NeMo pre rail + tool access on the same tool: a kind that may not call gets `-32003` (access first, no hook run);
   an allowed kind with a blocked payload gets the guardrail `isError` result; log reasons `tool_denied` vs
   `guardrail_pre`; both transports.
2. IdP mapping (fake provider, `lookup` against a fake Graph/Cloud Identity server) → custom role `analyst`
   (`oauth_only`) → the person's token carries `role: analyst`; the worker lists/calls per a tool-access entry naming
   `analyst`; the same person's password login is 403; over Streamable HTTP and over the stdio bridge (`--oauth`).
3. Removed from the IdP group → next login: membership gone, token no longer carries the role, worker denies; an admin
   `manual_memberships` entry keeps it.
4. `fail: closed` with the engine broken (`engine: nemo` config dir missing a flow) → every opted-in tool blocked,
   unlisted tools unaffected; `fail: open` → calls proceed, `guardrail_error` logged.
5. Golden case on a guarded tool with a payload the rail blocks → deploy gate fails with the rail's message.

### 21.6 Azure (AKS) as a third deploy target (D54 — the user, 2026-10-11, overrides the AKS part of D49)
Same shape as GCP and AWS: one cluster per install, a namespace + identity per group/zone, workers load the group's code
from object storage, the console manages everything through the cloud's APIs and Kubernetes. Cheap first.
- **Selection.** `RAMEN_CLOUD=azure`; zone provider `azure`; zones named `<region>-<n>` (`eastus-1`, availability zones
  1–3); `regions.json` gains an `azure` list; Config "Blocked regions" and the Zones page list Azure like the others.
- **Infrastructure** (`deploy/terraform/azure`, azurerm + helm providers): resource group `ramen-<suffix>`, AKS (free
  tier control plane, one system pool `Standard_B2s_v2` × 2, autoscale ≤ 3, OIDC issuer + Workload Identity on, Azure CNI
  overlay), ACR Basic, a storage account with blob container `groups` and Table Storage table `ramen` (the store), Key
  Vault (RBAC mode) for secrets, a static public IP + DNS label for the edge, the console's user-assigned managed
  identity with a federated credential for `ramen-system/console`, role assignments scoped to the resource group only.
  Outputs mirror GCP/AWS (`acr`, `public_hostname` = `<ip-with-dashes>.sslip.io`, `storage_account`, `key_vault`, …).
- **Edge.** Gateway API through **Envoy Gateway** installed by Terraform (Helm), one `Gateway` on the static IP, the same
  header-matched `HTTPRoute`s the GKE adapter writes (`ramen-group`/`ramen-zone` → zone service; console default;
  `/mcp`, `/.well-known/oauth-protected-resource`, gRPC paths) — reuse the GKE route builder. TLS: cert-manager +
  Let's Encrypt HTTP-01 for the sslip.io name (port 80 answers only ACME and redirects); `RAMEN_AZURE_TLS=selfsigned`
  falls back to a self-signed cert like AWS. Per-zone IP rules: enforced by the node (authoritative, as everywhere) and
  mirrored at the edge with an Envoy Gateway `SecurityPolicy` (client-CIDR authorization) per zone route.
- **Store.** `RAMEN_STORE=azuretables` — one Table Storage table, partition = collection, row = id, the document as
  encrypted JSON (same `encrypted.py` wrapper); list/query/transaction semantics as `dynamodb.py`.
- **Secrets.** `RAMEN_SECRETS_BACKEND=azure` — Key Vault secrets named `ramen--<group>--<VAR>` (`_` → `-` with a
  reversible escape), values never returned to the browser; the zone identity gets *Key Vault Secrets User* on exactly
  the group's secrets (per-secret role assignment).
- **Code bucket.** `az://<account>/<container>/<group>` URIs; the runtime's bucket sync (`bucket.py`) supports `az://`
  with `azure-storage-blob` + `DefaultAzureCredential` (workload identity) — `[azure]` extra in the worker image.
- **Zone identity.** User-assigned managed identity `ramen-<group>-<zone>` with a federated credential for the zone
  namespace's service account, *Storage Blob Data Reader* on the container (Azure cannot scope to a blob prefix — the
  group prefix is enforced by the console's sync and documented), secrets as above. Permission catalogue: the same names
  with Azure roles (`bucket.write` → Storage Blob Data Contributor, `logs.write` → Monitoring Metrics Publisher, …).
  `detach_zone` deletes the namespace, the identity and its role assignments.
- **Logs.** Worker access logs read from the Kubernetes API (pod logs, per zone) — no Log Analytics dependency.
- **Helm.** `provider=azure` values: workload-identity annotations/labels, the Envoy `Gateway`/`HTTPRoute` for the console,
  cert-manager `Certificate`/`ClusterIssuer`.
- **Docs.** `wiki/deploy-azure.md` step by step (mirror of deploy-gcp.md), README/home "GKE, EKS and AKS".
- **Acceptance.** Unit tests with fakes for every Azure API call (as `fakes_gcp.py`/`fakes_aws.py`), the 33-check
  `verify075.py` run on a live AKS deploy with Entra sign-in, then teardown.

### 21.7 Autoscaling by Ramen, not by the cloud (D55 — the user, 2026-10-11)
Workers are scaled by the console's rebalance, the same on GKE, EKS and AKS; no cloud autoscaler is involved.
- **Bounds.** Per group × zone: `min_count` is always 1; `max_count` (≥ 1, default = current `count`) is set by a super
  admin or a group admin of that group — `PUT /api/v1/groups/{g}/zones/{z}/workers {"max_count": n}` (viewers and MCP
  users get 403 and never see the control). `count` stays the current target, always clamped to [1, `max_count`]; a
  manual `count` outside the bounds is a 422. Audited `workers.max_count`.
- **No HPA.** The adapters stop writing `HorizontalPodAutoscaler` objects and delete an existing `worker` HPA on the next
  apply/scale; the worker Helm chart's `hpa` is off by default. The stable Deployment's `replicas` = `count`; the canary
  stays at 1.
- **Decision** (`autoscale.decide(workers, state, w, now)`, pure, unit-tested): per worker the node's metrics give
  `inflight` and the `max_inflight` it runs with (the snapshot adds `max_inflight`), plus `total` calls; the console keeps
  the previous `total` per pod and the time in `workers.autoscale_state` to derive calls per second.
  - **Up** by one when any ready worker is `high` (inflight > 80 % of its limit), the zone's mean inflight ≥ 50 %, or the
    calls per second per worker reach `RAMEN_AUTOSCALE_TARGET_RPS` (default 50 — fast tools rarely raise inflight), and
    `count < max_count`.
  - **Down** by one when every ready worker is `low` (< 30 %) for 3 consecutive rebalances and the zone's call rate per
    worker after removing one would stay below the up threshold, and `count > 1`.
  - **Cooldown**: up after 30 s (`RAMEN_AUTOSCALE_UP_COOLDOWN_SECS`), down after 120 s (`RAMEN_AUTOSCALE_COOLDOWN_SECS`);
    a worker that is `down` (not answering) never counts as low. The up step is proportional —
    `ceil(ready × max(mean/50 %, rate per worker/target))`, at least +1, at most `max_count` — down is always −1 (load
    tests on GKE: one-at-a-time steps with one 120 s cooldown took 309 s to reach 3 while calls were refused).
  - **Capacity counts every ready pod**, the canary included, so a zone at `count` 1 measures load across two pods; the
    proportional step is therefore slightly conservative (observed on AKS: a saturated 1+canary zone asks for 4, clamped
    to `max_count`).
  - **Probes**: worker readiness and liveness use a 5 s timeout and liveness tolerates 6 misses — the 1 s default made
    Kubernetes restart CPU-saturated workers mid-load on GKE.
  - The result `{action: up|down|hold, from, to, reason}` is returned by rebalance, written to the worker doc, audited
    `workers.autoscale` (user `scheduler` or the person), and shown on the group page.
- **Who runs it.** Manual: the Rebalance button / `POST …/rebalance` (group admin+). Scheduled: the Config page's
  scheduler, which now rebalances every group × zone each interval (not only the skewed ones), so it scales up and back
  down on its own. The load-balancer leg of rebalance (GCP capacity scaler, AWS/Azure weights where present) is unchanged.
- **UI.** Group page (zone actions) and the Zones and workers page: "Workers: <count> (min 1, max [n])" with an input and
  Save for super admins and group admins only, the last autoscale decision and its reason; read-only count for others.
- **Audit and logs (the user).** Every action is audited with the actor (a person, an API key, or `scheduler`), the
  group/zone and tags: `workers.max_count` (`from:`, `to:`), `workers.count` (manual), `workers.rebalance` (who pressed
  it), `workers.autoscale` for every up/down (`action:`, `from:`, `to:`, `reason:`), refused changes with
  `error:forbidden|invalid`. A `hold` is not audited (it would flood the log) but is kept as the last decision on the
  worker doc. Each scale change also logs one console line `ramen.autoscale` (INFO; WARNING when a zone sits at
  `max_count` under high load, which the warning digest carries).
- **Load tests** (`scripts/load_test.py`): drive concurrent `tools/call` against one zone through the edge until the zone
  reaches `max_count`, then stop and confirm it scales down to 1; plus bursts, a max of 1, a group admin changing the max
  mid-test, and a viewer refused. Run on GKE, EKS and AKS; results in `reports/cloud-v0.7.5.md`.

### 21.8 Salted hashes at rest (0.7.5)
- API and MCP key secrets: `secret_hash = "s1$<salt hex>$<HMAC-SHA256(salt, secret) hex>"`, a random 16-byte salt per
  key. A hash without the `s1$` prefix is a pre-0.7.5 bare SHA-256: it still verifies, and is rewritten salted on the
  key's next successful use.
- OAuth authorization codes and refresh tokens: stored under `HMAC-SHA256(token_salt, raw)`, where `token_salt` is one
  random 32-byte value per install in the store doc `config/hashing` (Fernet-encrypted like the other sensitive
  fields). The salt is per install, not per token, because the hash is the lookup id. A lookup tries the salted id,
  then the bare SHA-256 every pre-0.7.5 token used.
- Passwords stay argon2 (salted by construction).

## 22. v0.8.0 — the security and concurrency review's fixes (binding)
Source: `reports/security-concurrency-v0.8.0.md` (private workspace). Each item has a test named in the report.
- 22.1 OAuth redirect URIs (§16.3): a URI's netloc is exactly `host` or `host:port` (no userinfo, nothing `urlparse`
  reads past); the loopback leniency (any port, any of `localhost` / `127.0.0.1` / `::1`) applies only to such a
  netloc. The consent hand-off page for a loopback target encodes it per context: HTML-escaped in the `href`, a JSON
  string literal in the script with `<`, `>` and `&` as `\u` escapes.
- 22.2 `repo_url` is an `https://`, `http://`, `ssh://` or `git://` URL with a host, validated on create and update
  (`422` otherwise): no `ext::`, scp-like `user@host:path`, leading `-` or whitespace anywhere; a filesystem path or
  `file://` URL only when `RAMEN_CLOUD=local` (the compose stack, a laptop), never on a cloud console. Every git invocation
  runs with `GIT_ALLOW_PROTOCOL=https:http:ssh:git:file` (never a remote helper) and `--` before the URL.
- 22.3 The deploy file is the console's alone. A checkout's `.ramen/` is never uploaded to the bucket (and a tracked
  `.ramen/*` is removed from the local adapter's checkout); the runtime's bucket sync never downloads `.ramen/*`.
  `config.rs` reads `.ramen/env-<zone>` as before, so on the cloud adapters it comes only from the console's Secret.
- 22.4 `Services.autoscale` holds a per-`group/zone` lock around read-decide-write; a concurrent call waits and then
  sees the new state (a `hold` on cooldown).
- 22.5 `POST …/environments/{env}/deploy` answers `409` naming the running job while a deploy of that environment is
  `running`.
- 22.6 The console's caller address (`audit.ip`, the login rate-limit key) is the `x-forwarded-for` entry
  `RAMEN_TRUST_PROXY_HOPS` from the right (default 1; 0 = the peer), as the node does (§11); fewer entries than hops →
  the peer. The chart sets 2 on GCP and 1 on AWS and Azure for the console as for the workers.
- 22.7 The Google group lookup refuses an email containing a quote, a backslash or whitespace (`LookupError`, no
  session), since the email is a query filter value.
- 22.8 Known and documented, not changed: `Store.transaction()` is a per-process lock, so the console runs as one
  replica (chart default `console.replicas: 1`); a guardrail hook that overruns its timeout leaves its thread running
  after the fail-closed answer.

## 23. v1.0.0 — MCP 2026-07-28: stateless core, standard headers, MRTR, tasks (binding)
Spec revision 2026-07-28 (SEPs 2575, 2567, 2243, 2322, 2549, 2577; tasks extension `io.modelcontextprotocol/tasks`).
The node is **dual-era**: 2025-06-18 clients are served exactly as in 0.8.0 (§16, §20); nothing below changes a legacy
answer. `MODERN = "2026-07-28"`; `LEGACY = ["2025-06-18", "2025-03-26", "2024-11-05"]`.

### 23.1 Era selection and the modern envelope
- A message is **modern** when the HTTP header `MCP-Protocol-Version` is not a legacy version, or the body has
  `params._meta["io.modelcontextprotocol/protocolVersion"]`. Everything else is legacy (a bare request with neither
  included). On gRPC (`Mcp/Call`) only the body decides, and the header rules of §23.2 do not apply.
- Order on `POST /mcp`: Accept/Content-Type → address → Origin → credential (§16.1, unchanged) → body → era → modern
  checks → throttle/inflight → dispatch. Modern checks, in this order, each answered as a JSON-RPC error carrying the
  request `id`:
  1. `_meta` missing, or without `protocolVersion` or `clientCapabilities` (an object) → `-32602`, HTTP 400.
     `clientInfo` is optional.
  2. header `MCP-Protocol-Version` missing or ≠ the body version → `-32020`, HTTP 400.
  3. version ≠ `MODERN` → `-32022`, HTTP 400, `data: {"supported": [MODERN, ...LEGACY], "requested": "<v>"}`.
  4. standard headers (§23.2) → `-32020`, HTTP 400.
  5. `initialize`, `ping`, `logging/setLevel`, `resources/subscribe`, `resources/unsubscribe` and any unknown method →
     `-32601`, HTTP 404. `notifications/*` → 202, ignored.
- HTTP status of a modern JSON-RPC error: 400 for `-32020/-32021/-32022` and the `_meta` `-32602`; 404 for `-32601`;
  200 for every other JSON-RPC error. Transport denials keep §16.1 (401/403/413/429/503).
- Modern requests ignore `Mcp-Session-Id` and never get one. `GET /mcp` stays 405; `DELETE /mcp` stays legacy-only.
- Every modern result carries `resultType` (`complete` | `input_required` | `task`) and
  `_meta["io.modelcontextprotocol/serverInfo"] = {"name": "ramen-node", "version": <node version>}` (merged into an
  existing `_meta`). `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`, `resources/read` and
  `server/discover` add `ttlMs` (`RAMEN_LIST_TTL_MS`, default 60000; `resources/read` always 0) and
  `cacheScope: "private"` (lists vary by caller).
- Modern error codes — nothing from `-32000..-32019`: unknown, blocked or hidden tool → `-32602 Unknown tool: <name>`;
  listable but not callable → `-32602 forbidden: <name> is not callable for <kind>`; unknown/blocked prompt → `-32602`;
  resource not found or blocked → `-32602 Resource not found`, `data: {"uri": ...}`; runtime not loaded → `-32603`.
  Access-log reasons (`blocked_name`, `tool_hidden`, `tool_denied`) are unchanged.
- `server/discover` → `{"resultType": "complete", "supportedVersions": [MODERN, ...LEGACY], "capabilities": {"tools":
  {"listChanged": true}, "prompts": {"listChanged": true}, "resources": {"listChanged": true}, "completions": {},
  "extensions": {"io.modelcontextprotocol/tasks": {}}}, "ttlMs", "cacheScope"}`; `extensions` only when a loaded tool
  has `task: true`. No `logging`, no `resources.subscribe`. Legacy `initialize` is unchanged except that it accepts a
  requested `2025-11-25` by answering `2025-06-18` (as it already does for any version).
- `completion/complete` (both eras): values = the `enum` entries of the named prompt argument / template variable that
  start with `argument.value` (max 100), else `[]`; `{"completion": {"values": [...], "total": n, "hasMore": false}}`.
- Access log: every `mcp` line gains `protocol` (the version served). `Admin/Metrics` gains
  `calls_by_protocol: {"<version>": n}`; `subscriptions` (open streams) and `tasks` (live) counts.

### 23.2 Standard headers (HTTP, modern only)
- `Mcp-Method` is required and must equal `method`. `Mcp-Name` is required on `tools/call`, `prompts/get` (=
  `params.name`), `resources/read` (= `params.uri`) and `tasks/get|update|cancel` (= `params.taskId`). A value of the
  form `=?base64?<b64>?=` is decoded (UTF-8) before comparing. Missing, mismatching or undecodable → `-32020`.
- `Mcp-Param-<Name>`: a tool parameter declares `"header": "<Name>"` in its proto `input` entry (types `string`,
  `integer`, `boolean` and `enum` only; `<Name>` an RFC 9110 token, case-insensitively unique per tool — otherwise the
  package fails to load). The MCP `inputSchema` property then carries `"x-mcp-header": "<Name>"`. On `tools/call`: the
  argument present and non-null in the body ⇒ the header is required and must match (integers numerically, booleans
  `true`/`false`, base64 sentinel decoded); argument absent or null ⇒ header not expected; unknown `Mcp-Param-*` ignored.
- The body stays authoritative; a mismatch is refused and logged with reason `header_mismatch`, never guessed.
- `traceparent`, `tracestate`, `baggage` (HTTP header, else the `_meta` key) are copied onto the access-log line as
  `trace: {"traceparent": ..., ...}` when `traceparent` matches the W3C shape; never forwarded to tools.
- CORS (§16.1) allows and exposes the new header names. Compatibility gate (§19): adding, removing or renaming a
  parameter's `header` is a breaking change unless `breaking: true`.

### 23.3 Group contract additions (runtime-py; all optional, 0.8 packages load unchanged with the same manifest hash)
`from ramen_runtime import ...` inside tool/resource/prompt code:
- Content: `text(s)`, `image(data: bytes, mime)`, `audio(data: bytes, mime)`, `resource(uri, text=None | blob=None,
  mime="text/plain")`. A tool may return one block, a list of blocks, or (as before) any JSON value. Blocks go into
  `content` as given; no `structuredContent` for a block result.
- Resources: the callable may return `bytes` (→ `blob`, base64). A `uri` containing `{var}` is a template: listed by
  `resources/templates/list` as `uriTemplate` (not by `resources/list`), matched on read (`{var}` = one path segment),
  variables passed to the callable as keyword strings and declared in `input`.
- Prompts: `"callable": "<fn>"` (in `<name>.py`) instead of `"skill"`: returns a string, a block, or a list of
  `{"role", "content"}` messages / blocks (role `user`). Arguments as keywords.
- `InputRequired(requests: dict[str, dict], state=None)` — raise it from a tool or callable prompt. Builders:
  `elicit(message, schema)` (form), `elicit_url(message, url)`, `sample(messages | text, max_tokens=100, **params)`,
  `list_roots()`. On the retry `input_responses()` returns the client's answers (dict, `{}` on the first call),
  `request_state()` the verified `state` (or `None`), `client_capabilities()` the declared capabilities.
  A form-mode schema with a property named like a credential (`password|passwd|secret|token|api[_-]?key|credential|
  card|cvv|ssn`, case-insensitive) is refused: the call answers `isError` `input request refused: <key>.<field> looks
  like a secret; use elicit_url` — so a golden case catches it at the canary.
- `MissingCapability(required: dict)` — raise when the tool cannot run for this client → `-32021`.
- `progress(progress, total=None, message=None)` — no-op unless the request carried `_meta.progressToken`.
- Tool proto `"task": true` — see §23.5.

### 23.4 MRTR (multi round-trip requests)
- Sidecar protocol (§2): `runtime.call_tool` / `runtime.get_prompt` params gain `inputResponses` (object, optional),
  `state` (any JSON, optional — already verified by the node), `client: {"capabilities": {...}}`, `progress: bool`.
  The result is the 0.8 shape, or `{"resultType": "input_required", "inputRequests": {...}, "state": <json>?}`, or
  `{"resultType": "missing_capability", "requiredCapabilities": {...}}`. While a call runs the runtime may write
  `{"jsonrpc": "2.0", "method": "runtime.progress", "params": {"id": <call id>, "progress", "total"?, "message"?}}`.
- Node, modern era: `input_required` → result `{"resultType": "input_required", "inputRequests", "requestState"?}`;
  `requestState` only when the tool gave `state`. An input request whose method the client did not declare
  (`elicitation/create` → `elicitation`, with `mode: "url"` → `elicitation.url`; `sampling/createMessage` → `sampling`;
  `roots/list` → `roots`), and `missing_capability`, → `-32021` with `data.requiredCapabilities` (HTTP 400).
- `requestState = b64url(payload) "." b64url(HMAC-SHA256(key, payload))`, key derived from the group session secret
  (label `mrtr`), `payload = {"v": 1, "s": state, "b": caller binding (§16.2), "g": group, "z": zone, "m": method,
  "n": name, "a": args digest (C12), "e": expiry}`; TTL `RAMEN_MRTR_TTL_SECS` (default 600). A presented state that is
  malformed, fails the MAC, is expired, or was issued for another caller, group, zone, method, name or arguments →
  `-32602 requestState integrity check failed` (log reason `state_invalid`). Not single-use.
- `inputResponses` must be an object of objects when present → else `-32602`. Unknown keys are the tool's to ignore.
- Guardrails (§21.2): **pre runs on every round** — the first call and each retry; on a retry the engine sees the
  arguments plus `"$inputResponses": {...}` — and before the tool body runs again. **Post runs on the final
  (`complete`) result only.** A blocked round is the usual `isError` result with `_meta.ramen.guardrail`.
- Legacy era: an `input_required` outcome becomes `isError` `this tool needs more input (<keys>); it can only ask a
  client that speaks MCP 2026-07-28` (prompts: `-32602` with the same text). `missing_capability` → the same style.
- `progress`: when the request has `_meta.progressToken` and `Accept` includes `text/event-stream` (either era), the
  POST is answered as SSE: one `notifications/progress` (`progressToken`, `progress`, `total`?, `message`?) per runtime
  line, then the response. Otherwise progress lines are dropped.
- Golden cases (§19): a case may carry `respond: {<key>: <response>}`; the console then calls in the modern era
  declaring elicitation (form + url), sampling and roots, and on `input_required` retries once with those responses
  and the `requestState`; `expect` applies to the final result. `expect: {input_required: [<keys>]}` asserts the
  first round instead.

### 23.5 Tasks (`io.modelcontextprotocol/tasks`, modern only)
- A tool with `"task": true` called by a client that declared the extension on that request returns
  `{"resultType": "task", "taskId", "status": "working", "createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs"}`;
  any other caller (legacy, or no declaration) gets the synchronous call as before (bounded by the call timeout).
- `tasks/get {taskId}` → the task fields + `inputRequests` (`input_required`) | `result` (`completed`, the tool result
  incl. `isError`) | `error` (`failed`); `tasks/update {taskId, inputResponses}` and `tasks/cancel {taskId}` →
  `{"resultType": "complete"}`. Called without the extension declared → `-32021`. Unknown, expired, or another
  caller's task → `-32602 Unknown task` (a task is bound to the caller binding of §16.2 and to group + zone).
- `taskId`: 128 random bits, hex. `ttlMs` = `RAMEN_TASK_TTL_SECS` (default 3600) × 1000; `pollIntervalMs` 2000;
  at most `RAMEN_MAX_TASKS` (default 4) running per pod — beyond it the call is refused like inflight (429).
- Runtime: `runtime.task_start {taskId, name, arguments, client}` returns at once and runs the tool on a thread;
  `runtime.task_poll {taskId}` → `{"status", "result"?, "error"?, "inputRequests"?}`; `runtime.task_update {taskId,
  inputResponses}` (the tool body runs again with them); `runtime.task_cancel {taskId}` (cooperative: the result is
  discarded). Guardrails: pre before the start and before every re-run, post on the final result; a block completes
  the task with the `isError` result.
- Node: the owning pod polls the runtime (500 ms) and writes the record to the task store: the scope Redis
  (`RAMEN_REDIS_SCOPE_URL`), else the item Redis, else process memory (single pod; documented). Any pod of the zone
  answers `tasks/get` from the store; `update`/`cancel` reaching another pod are queued in the store for the owner.
  **In a pod without a Redis store no tasks are handed out**: a zone normally runs two pods (stable and
  canary) and a poll would reach the wrong one, so `task: true` tools answer synchronously and the extension is not
  advertised (`RAMEN_TASKS=1|0` forces either way).
  A `working` task whose owner stopped heart-beating (30 s) is `failed` (`-32603 worker lost`). The sidecar is not
  idle-killed while a task runs. Access log: `tasks/*` lines carry `name` = the tool and `task` = the id.

### 23.6 Subscriptions (`subscriptions/listen`, HTTP, modern only)
- A POST answered as `text/event-stream` (`X-Accel-Buffering: no`): first the ack
  `notifications/subscriptions/acknowledged` with `params._meta["io.modelcontextprotocol/subscriptionId"]` = the
  request id and `params.notifications` = the honoured subset of `toolsListChanged`, `promptsListChanged`,
  `resourcesListChanged` (+ `taskIds` when the extension is declared — asking for `taskIds` without declaring it is
  `-32021`, as the extension requires; `resourceSubscriptions` is never honoured);
  then, after each load whose manifest hash differs, one `notifications/{tools,prompts,resources}/list_changed` per
  requested kind, each tagged with the subscription id; `notifications/tasks` for watched tasks; an SSE comment every
  15 s. On shutdown/drain the node sends `{"id", "result": {"resultType": "complete", "_meta": {subscriptionId}}}`.
- A stream holds no inflight permit; at most `RAMEN_MAX_SUBSCRIPTIONS` (default 64) per pod, then 429. It is
  per-pod: a stream learns of the reload of the pod it is connected to. Over gRPC the method is `-32601`.
- Supersedes the "no push channel" note of §20 for modern clients; the legacy SSE answer of C11 is unchanged.

### 23.7 Verification
- Golden regression: every 0.8 tool/resource/prompt of the fixture and demo groups answers byte-identically (minus
  `ms`) on the legacy era, and identically plus the modern envelope on the modern era, on both transports.
- Official suite (`@modelcontextprotocol/conformance@alpha`, pinned version) against a node serving
  `tests/fixtures/conformance_group` through a local proxy that adds the credential (the suite cannot send one):
  `--requirements 2026-07-28` and `--spec-version 2025-06-18`, each with a checked-in expected-failures baseline.
  A baseline entry needs a one-line reason in the file. Known for the legacy wire: server-initiated sampling,
  elicitation and logging, and `resources/subscribe` are not offered to 2025-06-18 clients.

## 24. v1.0.0 — sleeping workers, images built on deploy, OpenTelemetry with consent (binding)
Asked for by the user on 2026-11-13 for 1.0. Supersedes §21.7 "min 1 per zone" (a sleeping zone has 0 workers) and
§13.3 "the console never builds" (it starts a build Job; it still holds no registry credential itself).

### 24.1 Sleeping workers
- Setting per group × zone: `sleep_after_secs` (0 = never, the default; otherwise ≥ 60), set by a super admin or an
  admin of the group — `PUT /api/v1/groups/{g}/zones/{z}/workers {"sleep_after_secs": n}`, group page → Zone actions.
  Others never see the control. Zone state: `awake` | `asleep` | `waking`, shown on the workers table.
- **Idle** = every worker pod of the zone reports (in `Admin/Metrics`) `last_call_at` older than `sleep_after_secs`,
  `inflight` 0, `tasks` 0 and `subscriptions` 0, and the zone was woken or deployed longer ago than that too.
  `last_call_at` (epoch seconds, 0 = never) counts authenticated MCP calls only — health, reflection, `Admin/*` and
  denied calls do not keep a zone awake. The console scheduler checks on its tick; `POST …/zones/{z}/sleep` and
  `POST …/zones/{z}/wake` (same roles) do it by hand. Every transition is audited (`workers.sleep`, `workers.wake`,
  with `trigger: idle|manual|call|deploy`).
- **Asleep** = the zone's `worker` and `worker-canary` Deployments are at 0 replicas and an **activator** runs in
  the zone namespace: one small pod (`Deployment/worker-activator`, the worker image started with
  `RAMEN_ACTIVATOR=1`, labels `app: worker`, `ramen.io/role: activator`, requests 10m / 32Mi, no Python runtime
  started). It is selected by the zone's existing `worker` Service, so every edge (GKE Gateway NEG, ALB, Envoy
  Gateway, kind NodePort) keeps its route and its health check untouched. Real worker pods carry
  `ramen.io/role: worker`; `Service/worker-direct` selects `app: worker, ramen.io/role: worker`.
- Activator behaviour (node-rs): the SAME guards as a worker (`grpc::guard`: address, Origin, credential, throttle)
  with the zone's own config — an unauthenticated call never wakes a zone. `grpc.health.v1.Health` answers SERVING.
  `Admin/Metrics` answers `{"asleep": true, "last_call_at": …, "woken": n}`; `Admin/Reload` → `FAILED_PRECONDITION`.
  An accepted MCP call (HTTP or gRPC): `POST {RAMEN_CONSOLE_INTERNAL_URL}/api/v1/internal/zones/{g}/{z}/wake` with
  header `x-ramen-admin-key` (once per wake, concurrent calls share it), poll `worker-direct:8080` gRPC health until
  SERVING, then forward the request unchanged (HTTP: method, path, headers, body, streaming the answer; gRPC:
  `Mcp/Call` bytes and metadata) and relay the answer. Not SERVING within `RAMEN_WAKE_TIMEOUT_SECS` (default 25, under every edge's default backend
  timeout; the wake goes on, so the retry finds the zone awake) →
  HTTP 503 with `Retry-After: 10` / gRPC `UNAVAILABLE`, access-log reason `waking`. Its log lines carry
  `activator: true`.
- Console: **sleep** = apply the activator (replicas 1), wait Ready **and listed as a ready address in the Endpoints
  of `Service/worker`** (1.0.0a: until then kube-proxy still routes to the terminating-but-serving workers, and a
  draining worker never asks for a wake; a zone role that cannot read Endpoints skips the wait with a warning; not
  listed within the wait → 502), scale `worker` and `worker-canary` to 0 (the workers' SIGTERM drain serves what is in
  flight), state `asleep`. **wake** = state `waking`, scale `worker` to
  the zone's count before it slept (≥ 1) and `worker-canary` to what it was, wait Ready, state `awake`, then scale
  the activator to 0 — while both exist the activator forwards, so no call is refused. A deploy, a rebalance or a
  count change on a sleeping zone wakes it first. The autoscaler (§21.7) skips a zone that is not `awake`.
  `Cloud` gains `sleep_zone(group, zone) -> dict`, `wake_zone(group, zone, count: int, canary: int) -> dict` and
  `zone_runtime(group, zone) -> {"workers": n, "canary": n, "activator": n}`; local adapter: no-ops that only flip
  the state (compose has one fixed worker). `/api/v1/internal/*` is reachable in-cluster only (not routed by any
  edge; the chart's routes list `/api/v1/internal` nowhere and the console refuses it when `X-Forwarded-For` is set).

### 24.2 Worker images built on deploy
- Per group: `build_image` (bool; default true on gcp/aws/azure when the deployment has a builder, false otherwise),
  group page → Worker image card, group admins. When true and the checkout has a non-empty `mcp/requirements.txt`,
  a deploy first ensures the group's image (a file of comments and blank lines counts as empty): tag `b-<group>-<sha256(base image ref + "\n" + requirements.txt)[:16]>`
  in the cloud's own registry next to the base image — `<region>-docker.pkg.dev/<project>/ramen/worker`, 
  `<account>.dkr.ecr.<region>.amazonaws.com/ramen/worker`, `<acr>/ramen/worker` (one repository, no per-group
  repository to create). An `images` record with that tag and `source: "build"`, `status: "ok"` means it exists:
  nothing is built. Otherwise the console starts `Job/ramen-build-<hash>` in `ramen-system`
  (`serviceAccountName: ramen-builder`, image `RAMEN_BUILDER_IMAGE`, default a pinned kaniko executor) whose context
  is a ConfigMap holding `requirements.txt` and

      FROM <base worker image>
      COPY requirements.txt /opt/ramen/requirements.txt
      RUN /opt/venv/bin/pip install --no-cache-dir -r /opt/ramen/requirements.txt
      ENV RAMEN_DEPS_BAKED=<sha256 of requirements.txt>

  waits up to `RAMEN_BUILD_TIMEOUT_SECS` (default 900), records the image (history as §13.3) and uses it for this
  deploy. A failed or timed-out build fails the deploy before the canary, with the last 40 log lines of the Job in
  the deploy log; the stable track is untouched. Job and ConfigMap are deleted afterwards (TTL 600 s on the Job).
- Precedence for the zone's image: an admin's pinned image (§13.3 `images/current` with `source: "manual"`) → the
  built image → the base image. Runtime: when `RAMEN_DEPS_BAKED` equals the sha256 of the bucket's
  `mcp/requirements.txt`, `deps.install` is skipped (log `deps baked`); any other value installs as before.
- The builder identity is the deployment's, never the console's: Terraform creates `ramen-builder` (GCP: GSA with
  `roles/artifactregistry.writer` on the `ramen` repository, workload identity for `ramen-system/ramen-builder`; AWS:
  IRSA role with ECR push on `ramen/worker`; Azure: user-assigned identity with `AcrPush`, federated to the KSA) and
  the chart creates the KSA and sets `RAMEN_BUILDER=1` + `RAMEN_BUILD_REGISTRY` on the console. Without it
  `build_image` cannot be turned on (422 naming the missing builder). `Cloud` gains
  `build_image(group, tag, dockerfile, files: dict[str, str]) -> {"image": ref, "log": str}`.
- kind: `deploy/kind/up.sh` runs an in-cluster registry and sets the same two variables, so the path is testable.

### 24.3 OpenTelemetry
- Settings (super admin, Config page → Telemetry; `GET|PUT /api/v1/config/otel`): `enabled`, `endpoint` (OTLP/HTTP
  base URL, e.g. `http://jaeger.ramen-system:4318`), `headers` (what Ramen sends; encrypted, write-only),
  `client_endpoint` + `client_headers` (what connected agents are told to use; default: `endpoint`, no headers —
  never Ramen's own `headers`). Stored in `config/otel`; not in backups' secrets-free rule exception: the two header
  maps are stripped from backups like every secret. Workers get `RAMEN_OTEL_ENDPOINT` / `RAMEN_OTEL_HEADERS` (JSON)
  on their next deploy.
- Export. Node: one span per MCP call (`SERVER`, name `mcp <method>`; attributes `mcp.method`, `mcp.name`,
  `mcp.protocol`, `ramen.group|zone|env`, `ramen.transport`, `ramen.status`, `ramen.reason`, `ramen.key_id`,
  `ramen.guardrail.stage|engine` when a rail blocked), a child span `runtime <call>` around the sidecar round trip,
  parent taken from the request's `traceparent` (§23.2); counters `ramen.mcp.calls` and `ramen.mcp.errors` by
  method/name/status. Never arguments, results or credentials. OTLP/HTTP JSON (`/v1/traces`, `/v1/metrics`),
  batched off the call path; an unreachable collector drops data and logs one warning a minute, never fails a call.
  Console: one span per deploy job (`ramen.deploy`, attributes group/env/zones, outcome, `ramen.golden.passed|failed`,
  `ramen.compat`, `ramen.image`). `service.name` `ramen-node` / `ramen-console`, `service.version`.
- Telling the client. `POST /oauth/token` answers gain `telemetry: {"otlp_endpoint", "headers", "resource_attributes":
  {"ramen.group", "ramen.zone"}}` when enabled; `server/discover` carries the same object under
  `_meta["io.ramen/telemetry"]` for any authenticated modern caller.
- **Consent.** Per group: `require_telemetry_consent` (group admin; only settable while telemetry is enabled).
  Workers get `RAMEN_TELEMETRY_CONSENT=1` and `RAMEN_TELEMETRY_NOTICE` (the endpoint's host) on deploy. When on,
  `tools/call`, `prompts/get` and `resources/read` are served only to a caller who accepted:
  1. A console token carries `tel: true` when the person approved the OAuth consent page, which then shows the
     telemetry notice and a required checkbox; approving without it is impossible, declining ends the flow as today.
     Such a caller is never asked again.
  2. Otherwise, modern era + the client declared `elicitation`: the node answers `input_required` with
     `inputRequests: {"ramen_telemetry_consent": elicitation/create (form) message "This Ramen group records
     telemetry about your calls (tool name, timing, outcome — never arguments or results) at <host> and asks your
     agent to send its telemetry there. Accept to continue.", requestedSchema {accept: boolean}}` and a signed
     `requestState` (§23.4, bound as usual). A retry with `accept: true` records the consent for that caller
     binding (store: scope Redis → item Redis → pod memory; 30 days) and runs the call — the tool's own input
     requests follow in later rounds. `decline`, `cancel` or `accept: false` → `-32602 telemetry consent declined`.
  3. Modern era without `elicitation` → `-32021` (`requiredCapabilities: {"elicitation": {}}`). Legacy era →
     `-32003 forbidden: this group requires telemetry consent; use a client that speaks MCP 2026-07-28 or sign in
     through OAuth`.
  Lists, `server/discover` and `completion/complete` are not gated (MRTR is not allowed on them). The console's own
  calls (golden cases, package listing) present the worker admin key and skip the gate. Access-log reasons
  `consent_required`, `consent_declined`; a recorded consent logs `consent: "granted"`.
- Chart: `otel.collector: none | jaeger | phoenix` (default none) adds a Jaeger all-in-one (OTLP in, query API) or
  Arize Phoenix (OTLP/HTTP + UI on 6006, OTLP/gRPC 4317) to `ramen-system` for trying it out and for the test runs;
  not for production.

### 24.4 Verification
Unit tests per component; kind: a zone sleeps on idle, a call through the edge wakes it and is answered, a deploy
builds and uses a group image, spans of a call and a deploy arrive in Jaeger, consent by prompt / by token / refused.
Cloud (GKE, EKS, AKS): the same four, plus every sign-in path: password, API key, MCP key, person's OAuth token over
HTTP and through the bridge, Entra with group lookup; Google Workspace against a fake unless a live client is given.

### 24.5 The console's own secrets
On a cloud (`RAMEN_SECRETS_BACKEND` ≠ `store`) every reversible secret the console stores (`storage/encrypted.py`
SENSITIVE minus hashes and group secrets, plus a provider's `client_secret`) is written to the cloud's secret manager
as its own secret and the doc keeps `vault:<ref>`; a changed value is a new secret, the old one deleted; refs are
cached. Names: GCP/Azure `ramenc-…`, AWS `ramen-console/…` — outside every worker grant and boundary. Plaintext
values found at start are moved. Locally nothing changes (Fernet in the store). The Config page lists each section's
settings; a secret shows only as set/not set. Every secret read or written is scrubbed (`***`) from every log handler,
deploy job logs and backups; `scheme://user:pass@` is scrubbed always. Backups keep no nested provider secret and a
restore merges per entry so the live one survives.

## 25. v1.0.0 — the zones a group may use (superseded by §26.1 in v1.0.0a: zones are bound to groups, `allowed_zones` is gone)
Asked for by the user on the live GKE console: a group must not reach every zone.
- `groups[].allowed_zones: list[str]` — the zones the group's environments may use. `GET /api/v1/groups/{g}/zones` →
  `{"allowed": [...]}` (viewers of the group); `PUT` the same body (super admin only; every zone must exist, 422
  otherwise; a zone an environment of the group still uses cannot be removed, 409 naming the environment and zone).
  Audited `group.zones` with a `zone:<z>` tag per zone.
- A group admin's `POST …/environments` or `PUT …/environments/{env}` naming a zone outside the list → 403 naming the
  zones. A super admin's environment adds the zones it names to the list (the super admin is the one who decides).
- New groups start with `[]`. At startup a group without the field gets the zones its environments use, once; a list
  a super admin set (even empty) is never widened.
- Group page: the zone tables, zone actions and the environment zone picker show only allowed zones; a super admin
  edits the list in the "Zones and workers" card, everyone else sees it read-only.

## 26. v1.0.0a — zones bound to groups, persisted deploy jobs, telemetry through Ramen (binding)
Asked for by the user (D60–D63, 2026-11-14). Supersedes §25.

### 26.1 Zones bound to groups
- `zones[].groups: list[str]` — the groups a zone serves. Super admin only: `POST /api/v1/zones` takes `groups`
  (default `[]`), `PUT /api/v1/zones/{zone}/groups {"groups": [...]}` replaces the list (every group must exist, 422;
  a group whose environment still uses the zone cannot be removed, 409 naming environment and zone). Audited
  `zone.groups` with a `group:<g>` tag per group.
- A group's zones are the zones listing it: `GET /api/v1/groups/{g}/zones` → `{"zones": [...]}` (read-only, viewers of
  the group); `PUT` on it is gone (404). `groups[].allowed_zones` is gone; at startup a zone gets the groups whose
  `allowed_zones` named it or whose environments use it, once (`zones[].groups` present → left alone).
- An environment (`POST`/`PUT …/environments`) may name only the group's zones — 403 naming the zones, for every role
  (a super admin binds the zone to the group first). A worker record (`workers/<g>:<z>`) exists only for a bound
  pair: binding creates it (defaults as today), unbinding detaches the zone for that group (namespace, workers, zone
  identity, record) after the 409 check; `worker_config(g, z)` for an unbound pair → 404.
- Zones page: the zone table gains a `Groups` column with a checkbox dropdown + "Save groups" per row (super admin);
  "Workers per group" lists bound pairs only. Group page: the "Zones and workers" and "Zone actions" cards are gone;
  one **Environments** table (environment, ref, verbose, last deploy, actions) with, under each environment row, one
  row per zone of it: zone (namespace), workers (live cell), size, sleep state, IP allow-list, service account, and
  the zone's actions (scale, sleep, IP rules, throttle, service account, rebalance, logs) in a `details` disclosure
  — every zone action keeps its route. The group's zones not used by any environment are listed once below the table
  ("Bound, unused: …"). The environment zone picker offers the group's zones.

### 26.2 Deploy jobs persisted
- `jobs` collection: `{id, kind, target, status, started, finished, result, error, log: [...]}`, written at creation
  and at every log line / end (the log is scrubbed, §24.5). `GET /api/v1/jobs/{id}` and the group page read the store,
  so a job is visible after a console restart and from every replica. A job still `running` after
  `RAMEN_JOB_STALE_SECS` (default 1800) without a change is shown and answered as `error: "lost (console restarted)"`.
  Kept: the last 50 per target (older deleted at creation). Backups do not include jobs.

### 26.3 Telemetry through Ramen
- **Config version.** `telemetry_version` = first 12 hex of sha256 over the canonical JSON of
  `{enabled, endpoint, client_endpoint, client_headers}` of `config/otel`. `GET /api/v1/config/otel` (public form) and
  the Config page show it. The token answer's `telemetry` object (§24.3) gains `version` and `required` (the group's
  consent switch); `server/discover`'s `_meta["io.ramen/telemetry"]` likewise. Workers get it in `RAMEN_OTEL_CLIENT`
  (`version` key) and in `RAMEN_TELEMETRY_VERSION`.
- **Push on update.** Saving `config/otel` rewrites every deployed zone's worker config (the same `cfg` the deploy
  writes) and sends `Admin/Reload` to each zone's workers, so the new endpoint/headers/version are live without a
  deploy; the reload's outcome per zone is in the `PUT` answer (`pushed: {"<g>/<z>": "ok" | "<error>"}`) and audited
  `telemetry.push`. A worker that cannot be reached keeps the old config until its next deploy.
  On Kubernetes the worker pods read the deploy Secret as env, so the push rewrites the Secret, reloads every pod and
  rolling-restarts the stable track (a 20 s drain per pod, §24 — calls are not dropped); locally the env files are
  rewritten and the workers reloaded.
- **Report on every call.** When the group requires telemetry (`RAMEN_TELEMETRY_CONSENT=1`), a gated call
  (`tools/call`, `prompts/get`, `resources/read`) from anyone but the console must carry
  `params._meta["io.ramen/telemetry"]`:
  `{"version": "<telemetry_version>", "seq": <int, +1 per report>, "events": [{"ts": "<RFC 3339>", "kind":
  "mcp" | "llm" | "tool" | "other", "name": "<method or model or tool>", "ms": <int>, "ok": <bool>, "reasoning":
  "<text, optional, ≤ 4 KiB kept>"}, …]}` — everything the client did since its last report (its MCP calls to any
  server, its model calls with the reasoning, its other tool calls); `events: []` is a valid report. The same object is
  accepted on `telemetry/report` (modern, authenticated; answer `{"accepted": <n>, "version": "<current>"}`) for
  reporting between calls. Limits: ≤ 200 events and ≤ 64 KiB per report (`-32602` beyond).
  - Missing on a gated call → `-32024 telemetry report required` (HTTP 428), `data: {"version": "<current>"}`;
    a 2025-06-18 client without the report → `-32003 forbidden: this group requires telemetry; use a client that
    reports it (MCP 2026-07-28 or the Ramen bridge)`. A 2025-06-18 body that carries the report (the bridge in front
    of a legacy host) is accepted and forwarded like a modern one — the report is what matters, not the era; a
    stale version on a legacy body → `-32025` too (HTTP 200 on the legacy era, §23.1). Access-log reason
    `telemetry_required`.
  - `version` ≠ the worker's → `-32025 telemetry config changed: sign in again` (HTTP 428), `data: {"version":
    "<current>"}`; reason `telemetry_stale`. Nothing of the stale report is forwarded.
  - The gate runs after the consent gate (§24.3) and before the runtime; a call refused here is not dispatched.
  - A worker not yet told a version (`RAMEN_TELEMETRY_VERSION` empty, a deploy from before this console) accepts
    any version; a malformed report (shape, limits) is `-32602 invalid telemetry report: <what>` with no reason.
- **Forwarding.** Each event becomes a span (kind `CLIENT`, name `client <kind> <name>`, start `ts`, duration `ms`,
  error `!ok`) under the call's own span (§24.3), attributes `ramen.client.ip`, `ramen.user` (the token's subject /
  email, else the key id), `ramen.key_id`, `ramen.group`, `ramen.zone`, `ramen.env`, `ramen.worker` (the pod's
  hostname), `ramen.event.kind`, `ramen.event.seq`, `ramen.reasoning` (the text, ≤ 4 KiB), `ramen.transport`;
  counter `ramen.client.events` by kind/ok. Reasoning and events never reach the access log or the console; they go
  to the collector only. A collector that is down drops them (one warning a minute, §24.3) — the call still runs.
- **Client side (the bridge).** At sign-in the bridge keeps the token answer's `telemetry` object encrypted in memory
  (a per-process random key, never on disk); it records every call it forwards and offers the host a tool
  `ramen_report(kind, name, ms, ok, reasoning)` (listed with instructions that the model must report its model and
  non-MCP tool calls with its reasoning before the next Ramen call); every gated call it forwards carries the report
  since the last one; `-32025` → it signs in again (refresh grant, else the interactive flow) and retries once;
  `-32024` from a server it has no config for → it signs in, then retries. Not consenting at sign-in ends the flow
  as today (§24.3).
- Chart: `otel.collector: jaeger` or `phoenix` is set for the test deploys (`cloud2.sh`); the Config page's
  Telemetry section lists the trace UI address when the chart's collector is on (`RAMEN_OTEL_UI_URL`).

## 27. v1.0.0b — environments across groups, zones attached to environments, one admin page (binding)
Asked for by the user (`instructions/v1.0.0b.md`, 2026-11-14): *a group has many environments, an environment has
many zones, a zone has workers*; an environment applies to the groups it names, a zone to the environments it names.
Supersedes §26.1's binding by group: a zone's groups are now **derived** — the union of the groups of its environments.
Agent-settled details are marked D64–D66 (the user can overrule).

### 27.1 Model
- **Environment** = a name shared across groups. Storage stays `environments/<group>:<name>` (every per-group state —
  `blocked`, `blocked_zones`, `tool_access`, `verbose`, `last_deploy`, `zones` — lives there, unchanged); the
  environment the user sees is the set of docs with that `name` (D64). Its `ref` is one value: a change writes every
  doc; on migration docs that differ keep theirs and the page shows the first with a "differs per group" note.
  Names are unique console-wide: `POST` of a name that exists → 409 (add the group with `PUT` instead). An
  environment names at least one group (422 otherwise). Only groups the caller administers may be named (403).
- **Zone** = `{name, provider, region, environments: [names], groups: [derived], created}`. `groups` is kept in the
  doc (so §26.1 readers — `GET /groups/{g}/zones`, worker records, the group page — work unchanged) and is
  recomputed on every zone or environment write. Each environment doc's `zones` is kept in sync with the zones
  naming it (write-through), so deploys, secrets scoping, keys and the group page keep working.
- **Worker record** `workers/<g>:<z>` exists for every pair the derivation yields (as §26.1): a pair that appears is
  bound (record created, `cloud.attach_zone`, then a deploy, 27.3); a pair that disappears is detached (namespace,
  workers, identity, record).
- **Who may edit (D65).** Super admins: everything. A group admin may create an environment for groups they
  administer, edit/delete an environment whose groups are all theirs, create a zone whose environments are all
  theirs, and edit/delete a zone whose groups (derived) are all theirs. Everything else 403. Viewers and MCP users
  get 403 on the page and on every write; the reads in 27.2 stay viewer-scoped as before.

### 27.2 API (`/api/v1`)
- `GET /environments` unchanged (per-group docs, csv/json). New `GET /environments/by-name` →
  `[{name, ref, ref_differs, groups: [...], zones: [...], last_deploy: {"<g>": {...}}}]`, viewer-scoped to the
  caller's groups.
- `POST /environments {name, ref?, groups: [..]}` → 201, the by-name object. Creates `<g>:<name>` for every group
  (ref defaults to each group's `ref`); zones `[]`.
- `PUT /environments/{name} {ref?, groups?}` → 200. `ref` rewrites every doc. `groups` replaces the set: a new group
  gets a doc (same ref, the environment's zones → attach + deploy per zone); a removed group loses its doc and is
  detached from every zone no other environment of that group uses (today's §26.1 rule).
- `DELETE /environments/{name}` → 204. Removes every doc; a zone whose only environment it was is **deleted** (the
  zone record too — the user's rule: "deleting an environment deletes the zones and workers related to it that are
  not related to other environments"); a zone other environments still use is only detached for the groups that
  lost it.
- `POST /zones {name, provider, region, environments: [..]}` (`groups` is gone → 422 if sent) → 201
  `{...zone, "jobs": [ids]}`: every environment must exist (422); region checked against the Config block list
  (403); each derived pair is bound and **deployed** (one canary deploy job per `<g>/<env>` scoped to the zone, the
  §26.2 job; `jobs` lists them; a pair that cannot be deployed records the error on the job, the zone stays).
- `PUT /zones/{zone} {region?, environments?}` → 200 `{...zone, "jobs": [ids]}`. `environments` replaces the set
  (bind + deploy the pairs that appear, detach the pairs that disappear). `region` change: every bound pair is
  re-attached in place with the new location (the Deployments' node selector) and deployed, so the rollout destroys
  the old workers and starts new ones there (D66: the zone keeps its name, namespace and worker records — count,
  size, sleep, IP rules, throttle, service-account grants; the namespace is never deleted — on GKE a deleted one
  sits Terminating on the NEG finalizer and a re-create is refused).
  Provider cannot change (422). Audited `zone.update` with `region:` / `env:` tags.
- `PUT /zones/{zone}/groups` is gone (404). `DELETE /zones/{zone}` as today (detaches every pair, removes the zone
  from every environment doc), under 27.1's edit rule.
- `POST /groups/{g}/environments {name, ref?, zones?}` stays as the per-group shortcut: it adds `g` to the
  environment `name` (creating it when new) and, when `zones` are given, attaches the environment to those zones —
  so the caller must be allowed to edit those zones (27.1), the old "zone not bound" 403 keeps its wording for an
  unbound zone the caller may not edit. `PUT /groups/{g}/environments/{env}` keeps `ref`/`verbose`/`blocked`; its
  `zones` field is accepted with the same meaning (attach/detach this environment on those zones) for the harness.
  `DELETE /groups/{g}/environments/{env}` removes only that group's doc (= `PUT /environments/{name}` without `g`).

### 27.3 Deploy on bind
A pair that appears (new zone, zone gains an environment, environment gains a group, region change) is deployed at
once: canary deploy of `<g>/<env>` scoped to that zone, a job per pair, started from the write (background), so the
worker runs the group's code without a click. A running deploy of the same target → the write still succeeds, the
pair's job is skipped with `error: "a deploy is already running"`. The group page's Deploy button stays for re-deploys.

### 27.4 Pages
- `/environments` is **Environments, Zones and Workers** (nav entry replaces both old ones; `/zones` → 301 there);
  `require("group_admin")`. Sections in order: **New environment** (name, ref, groups checkbox dropdown of the
  admin's groups); **Environments** table (name, ref input, groups dropdown, zones, last deploy per group, Save /
  Delete with confirm); **New zone** (name, environments dropdown, location = the console cloud's regions not
  blocked, as the old Zones page; local: free text); **Zones** table (name, provider, location select, environments
  dropdown, derived groups, Save / Delete zone); **Workers per group** (group, zone, environments, namespace, live
  cell every 15 s) — the old card plus the environments column. Rows the caller may not edit are read-only.
- Group page: the "Add environment" form and its zone picker go; the Environments table keeps every row and action
  (Deploy, Verbose, Delete = this group's doc) and links to the new page; "Bound, unused" stays.
- Migration at start (once, idempotent): every zone gets `environments` = the names of the environment docs listing
  it; `groups` is left as stored (a superset is harmless — recomputed on the next write). `zones.png` is replaced by
  `environments.png` of the new page in `shots.json`.
