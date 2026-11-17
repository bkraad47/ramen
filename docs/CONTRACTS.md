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
| restore / password | `POST /api/v1/backups/{id}/restore` {dry_run,prune,reconcile,force} (§13.1), `POST /api/v1/users/{id}/password`, `POST /api/v1/users/{id|me}/sign-out` (D92, §16.7) | |
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
- **Base images (1.0.0, Phase 9).** Every Ramen-built image (worker, console, SkillSpector, both Glama files) names its
  base by language minor + `-slim` only — `python:3.12-slim` for everything that installs the runtime and for
  SkillSpector, `python:3.14-slim` for the console, `rust:<minor of rust-toolchain.toml>-slim` for the build stages —
  never a patch version, a Debian codename or a digest: upstream re-publishes these tags with security fixes and the
  next build takes them (Debian 13 trixie as of 1.0.0). Third-party images in the chart and compose carry plain version
  tags (the chart and compose must agree, §34.1). `.github/dependabot.yml` proposes updates weekly with a 14-day
  cooldown; a Python or Rust minor is bumped by hand. Ramen's own images may still be pinned per install with the
  chart's opt-in `image.digest` / `image.workerDigest` (D85).

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
- **Terraform (`deploy/terraform/aws`)**: EKS cluster `ramen` (one small managed node group, var `region` default `us-east-1`), DynamoDB table `ramen` (pk/sk single-table, on-demand; matches the existing `dynamodb` store adapter), S3 groups bucket `ramen-<account>-groups`, Secrets Manager, ECR repos `ramen/console` + `ramen/worker`, console IAM role (IRSA) with S3/SecretsManager/DynamoDB/EKS-describe/IAM-create-role (scoped by path `/ramen/`)/WAF/ELB permissions, AWS Load Balancer Controller + Fluent Bit → CloudWatch Logs (Container Insights) installed via Helm from Terraform, a self-signed cert imported into ACM (no domain), outputs `cluster_name`, `region`, `console_url`, `ecr_console`, `ecr_worker`, `console_role_arn`. Since 1.0.0c the managed `aws-ebs-csi-driver` addon (after the node group; IRSA role `/ramen/ramen-ebs-csi` for `kube-system/ebs-csi-controller-sa`, AWS-managed `AmazonEBSCSIDriverPolicy`; one controller replica, volumes tagged `app=ramen`; output `ebs_csi_role_arn`) provisions the chart's PVCs: EKS ships only the in-tree `gp2` class, which provisions nothing. The class is the chart's (no kubernetes provider in the module): `storage.createAwsGp3` (provider=aws only) adds StorageClass `gp3` (`ebs.csi.aws.com`, `type: gp3`, `encrypted: "true"`, `WaitForFirstConsumer`, expansion on, the cluster default) and `storage.className` is every chart PVC's class unless `evals.storageClassName` / `otel.phoenix.persistence.storageClass` names one (empty = the cluster default); the AWS `engines_set` carries `storage.className=gp3,storage.createAwsGp3=true`. Satellites run no PVCs and get no driver.
- **CloudFormation (`deploy/cloudformation/ramen.yaml`)**: the same base resources (EKS, node group, DynamoDB, S3, Secrets Manager, ECR, IAM roles, OIDC provider; since 1.0.0c the `EbsCsi` addon with `EbsCsiRole` and output `StorageSet` for the chart's `gp3` class) as one template with parameters, for teams that cannot run Terraform. Helm steps are documented, not templated.
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
  delete, restore) invalidates refresh. Access tokens live ≤ 1 h (`RAMEN_OAUTH_ACCESS_TTL`) and, since 1.0.0 (D92),
  are revocable before that through the pushed denylist of §16.7.

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

### 16.7 Revocation that takes effect at once (D92, 1.0.0, binding)
Keys and access tokens are checked offline by the node; the console tells the node what is revoked.
- **The list.** `RAMEN_REVOKED` = compact JSON `{"keys": [<lowercase hex SHA-256 of an rmk_ key>], "jti": [<token id>],
  "sub": {<user id>: <unix seconds>}}`, per group, in every zone's deploy config (`deploy.zone_config`, so every deploy
  and every §26.3 push carries it). The node merges it from every source — env, deploy file, and the overlay below —
  as a union (it never shrinks a list it holds); a listed key is dropped from `RAMEN_MCP_KEYS` (the call is
  `UNAUTHENTICATED` / HTTP 401 like any unknown key); a token whose `jti` is listed, or whose `sub` is listed with
  `iat` ≤ that time (or no `iat`), is refused the same way with access-log reason `token_revoked`. A malformed list is a
  config error (fatal at start, "keeping current" at a reload).
- **At once.** `ramen.v1.Admin/Reload` takes `ReloadRequest.revoked` (bytes, the same JSON); the node merges it over the
  config it re-reads before anything else and keeps it (in memory) for every later reload until the pod restarts, so a
  pushed revocation holds on a pod whose env (the deploy Secret) still names the key; malformed → `INVALID_ARGUMENT`,
  nothing changes. Every adapter's `push_config` sends the zone's `RAMEN_REVOKED` on each reload, then rolls the pods
  as before (the restart makes it the env).
- **Store.** `revocations` docs `{kind: key|jti|sub, group, value, before?, exp, reason, created}`; `exp` = a key's
  delete + 24 h, a token id's token `exp`, a person's cut-off + `RAMEN_OAUTH_ACCESS_TTL` — never longer than something
  it covers can be presented; expired entries are pruned when the list is built. Refresh records carry the `jti` and
  `access_exp` of the access token minted with them; a person's entry is written for every group in which they hold a
  live refresh record (where a live token can be), and none when they hold none.
- **Triggers.** Key: `DELETE /api/v1/groups/{g}/mcp-keys/{id}` and `DELETE /api/v1/api-keys/{id}` (an agent key, in
  every group it was mirrored to); minting a key pushes too, so a rotation (mint, then revoke) needs no deploy. Person:
  every session-epoch bump — membership removed or changed, role changed, user deleted, password set or reset, an
  approved request revoked, an IdP mapping that changes the effective set — and `POST /api/v1/users/{uid}/sign-out`
  (`me`, or anyone whose account the caller may manage; ends the console sessions too; answers `revoked_groups`).
  Token: RFC 7009 `POST /oauth/revoke` (`token`, `token_type_hint`, `client_id`; public clients, no secret): an access
  token's `jti` until it expires, a refresh token's whole grant with every access token it minted; unknown, malformed
  or expired tokens answer `200 {}`, another client's token `400 invalid_request`, an unknown client `401
  invalid_client`; the metadata names `revocation_endpoint`. Refresh-token reuse and OAuth client deletion deny every
  live access token of the grants they end.
- **Push and retry.** `revocation.Pusher.push(group)` = `deploy.push_config(svc, warnings, group)` (only that group's
  zones), one at a time per group, a request that arrives during a push answered by the next one. Key routes push
  synchronously and answer `pushed`, plus `warning` (`"<g>/<z>": "<error>; … retried in the background"`) and
  `pending: ["<g>/<z>"]` for a zone not reached; person and token triggers push in the background. A zone not reached
  is recorded in `config/revocation_pending` (`{groups: {g: {since, zones, tries}}}`), shown on the group page above
  the MCP keys, and retried every `RAMEN_REVOKE_RETRY_SECS` (default 30) until it lands; a sleeping zone's Secret is
  rewritten by the push, so it wakes with the list.
- **TTL.** `RAMEN_OAUTH_ACCESS_TTL` (seconds, default 3600) is the access token's life and the longest a person's entry
  is kept. Shorter = a smaller window if the console cannot reach a zone and a shorter list, at the cost of a refresh
  per TTL per client. TLS and every enforcement gate are unchanged.

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
  Guardrails) does not run on 3.14. Worker image and both Glama images build `FROM python:3.12-slim` (§6); CI runtime jobs
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
- LLM-backed rails get their model and key from Ramen only (§36.1 named rails, §36.2 scoped providers). Deprecated
  since 1.0.0c: a non-empty `models:` in the repo's NeMo config (still handed to the repo's own flows, with
  `warnings: ["guardrails: models: in config.yml is deprecated since 1.0.0c; set the provider on the group or the
  org"]` in the `runtime.load` result, logged by the deploy, never a refusal) and LLM keys via `mcp/env.yaml` (the
  rails still load after it). The demo repo keeps `models: []`.

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
  batched off the call path, never failing a call. A batch an unreachable or failing (5xx, 408, 429) collector did not
  take is kept (≤ 8192 spans, ≤ 120 s, oldest dropped first) and sent once it answers; a batch it refuses (other 4xx)
  is dropped; counters are cumulative and re-sent (§39.5a, b35). One warning a minute while it fails.
  Console: one span per deploy job (`ramen.deploy`, attributes group/env/zones, outcome, `ramen.golden.passed|failed`,
  `ramen.compat`, `ramen.image`), tried again after 5/10/20/30/30 s on no answer, 5xx, 408 or 429. `service.name` `ramen-node` / `ramen-console`, `service.version`.
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
  Arize Phoenix (UI 6006, behind an OTel Collector sidecar on 4318 that turns Ramen's OTLP/JSON into protobuf and
  drops metrics) to `ramen-system` for trying it out and for the test runs;
  not for production. Both UIs want a sign-in since D92 (§39.7). Phoenix's SQLite lives on a PVC (`otel.phoenix.persistence`, default on, 5Gi, maxSurge 0);
  Jaeger keeps traces in memory (lost at a restart).

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
  rolling-restarts every track that runs — the stable `worker` and a `worker-canary` left by a canary deploy, which
  serves ~half the zone from the same Secret (a 20 s drain per pod, §24 — calls are not dropped; on AWS the canary
  leaves the ALB split while it rolls and gets its share back once Ready); a canary that could not be reloaded or
  rolled leaves the zone `ok` and is named in the answer's `warning: {"<g>/<z>": "<pod>: <code>"}` and in the audit
  (1.0.0c, P4-B1); locally the env files are rewritten and the workers reloaded.
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
- **Forwarding.** Each event becomes a span (kind `CLIENT`, named by the event's `name` since 1.0.0c (§32.1; was
  `client <kind> <name>`), attributes `ramen.event.kind`, `ramen.event.ok`, `openinference.span.kind`, start `ts`, duration `ms`,
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
- D70: a zone of the console's own cloud (`RAMEN_CLOUD`) must be in its cluster's region (`Cloud.home_region()`:
  GCP region, AWS region, Azure location): `POST /zones` and a `PUT` region change answer 422 naming the zones that
  region has, and the page's location picker offers only them. Other providers' zones are records and unchecked.
  A zone in another region needs a cluster there — not built; one console, one cluster.
- `PUT /zones/{zone}/groups` is gone (404). `DELETE /zones/{zone}` as today (detaches every pair, removes the zone
  from every environment doc), under 27.1's edit rule. On GKE a detach finishes the Gateway controller's cleanup
  (D69): the `networking.gke.io/neg-finalizer` is cleared so the namespace terminates now, the zone's network
  endpoint groups are deleted in the background once the controller releases them, and an attach that meets a
  namespace still terminating waits for it (409 after the deploy deadline) instead of answering 403.
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

## 28. v1.0.0b — satellite clusters: zones in other regions, one console (binding, D71)
The user: a zone in `australia-southeast1-b` on a `us-central1` cluster never got a node (28.0, D70); "create a
duplicate for other regions, managed from the same Ramen". One console, many clusters: the **home** cluster the
console runs in, plus **satellite** clusters — worker-only clusters in other regions the console drives directly.
Records (groups, environments, zones, users, keys, secrets, jobs) stay in the one console store.

### 28.0 Interim rule (D70, kept as the fallback)
A zone of the console's own cloud must be in a region that has a cluster: the home region (`Cloud.home_region()`)
or a satellite's. Otherwise `POST /zones` / a `PUT` region change answer 422 naming the regions that have a cluster
and pointing at Config → Regions; the page's location picker offers only those regions' zones. Other providers'
zones are records only and stay unchecked.

### 28.1 Model
- `config/regions` grows `clusters`: a list of satellites `{provider, region, name, host, project?}` — `host` is the
  satellite edge hostname (its own static address + managed certificate; sslip.io by default, like the console's).
  The home cluster is implicit (`home_region()`, host = the console's public URL). Super admins manage the list on
  **Config → Regions → Clusters** (add: provider, region, cluster name, host; remove: refused with 409 while a zone
  uses that region). `GET/PUT /api/v1/config/regions` carry `clusters`; audited `config.clusters`.
- A zone belongs to the cluster of its region: `regions.cluster_for(provider, region, clusters, home)` → `"home"` or
  a satellite entry; `GET /api/v1/zones` and the zone pages show each zone's `cluster` (home/satellite name) and
  `host`. A region change across clusters is a detach from the old cluster and an attach + deploy on the new one
  (D66's in-place rule holds within one cluster; D69 makes the re-create safe).
- Clients reach a zone at **its cluster's host**: the sign-in guide boxes, `mcp_user` page and the group page name
  the zone's host (`<host>/mcp` + headers, `--target <host>:443`); tokens and scopes are unchanged
  (`mcp:<group>:<zone>`, issuer = the console). Satellite workers get `RAMEN_PUBLIC_URL` = the satellite host (the
  RFC 9728 `resource_metadata` base); `RAMEN_OAUTH_ISSUER` stays the console. The console's own calls to satellite
  workers (smoke, golden cases, packages, metrics, reload) go through the satellite host like any client, as they
  do through the home edge today — no: the console keeps addressing workers by pod IP (per-pod smoke, metrics,
  reload, canary), so a satellite's network is joined to the home network: GCP — same VPC (VPC-native pod ranges are
  routable) plus a firewall rule home-pod-CIDR → satellite nodes tcp:8080; AWS — VPC peering home↔satellite with
  routes both ways and a node security-group rule from the home VPC CIDR on 8080; Azure — VNet peering both ways
  (non-overlapping address spaces) and an NSG rule. The satellite terraform modules create this from the home
  module's outputs (network/VPC id + CIDR); the console-to-worker path never crosses the public edge.
- Telemetry: the one OTLP endpoint must be reachable from every cluster; the chart's try-out collectors (Jaeger,
  Phoenix) live in the home cluster and are cluster-local, so with satellites either expose a collector or accept
  that satellite workers log `telemetry export failed` (documented, not papered over).

### 28.2 Adapters (all three clouds; the user: "this should also work for azure and aws")
- `GcpCloud` keeps one `Kube` per cluster: the home one as today (in-cluster config) and, per satellite, clients
  built from the GKE API (`container.projects.locations.clusters.get` → endpoint + CA) with the console GSA's
  Google credentials (google-auth ADC in the pod; `GOOGLE_OAUTH_ACCESS_TOKEN` locally). Every kube-touching
  operation resolves the zone's cluster first (`attach`, `deploy`, `push_config`, `workers`, `logs`, `scale`,
  `sleep`/`wake`, `set_ip_rules`, `rebalance`, `detach_zone`, `refresh` across all clusters, zone cleanup D69).
  Compute objects (backend services, NEGs, Cloud Armor) are project-global and keyed by namespace name, which is
  unique per zone — unchanged. The per-zone GSA, bucket and Secret Manager are project-level — unchanged.
- Satellite cluster RBAC: the console authenticates to a satellite API server as its GSA email; the satellite chart
  binds the console's ClusterRoles to that Google identity (`kind: User, name: <gsa email>`), plus the zone
  ClusterRole bound per namespace by the console as today (SEC-09) — and since the console is not the home
  ServiceAccount there, the per-namespace RoleBinding also carries the subjects the console chart is given
  (`console.k8sSubjects` → `RAMEN_CONSOLE_SUBJECTS`: GKE `User <gsa>`, EKS `Group ramen-console`, AKS `User <object
  id>`). Found live: on GKE `roles/container.developer` masks a missing binding, on EKS it is a 403 inside the
  namespace. Likewise every worker NetworkPolicy admits the console's home networks (`console.sourceCidrs` →
  `RAMEN_CONSOLE_SOURCE_CIDRS`: the home pod range / VPC / VNet) next to its own `ramen-system` namespace — found
  live: a satellite worker dropped the console's reload (DEADLINE_EXCEEDED) until it did. (Amended by S6, §33.4a:
  only a satellite zone's worker policy admits them; a home zone never does.)
- Images: Artifact Registry is regional but world-readable to the project's identities; satellite nodes pull from
  the home repo. Builds (kaniko Jobs) stay in the home cluster.
- AWS: a satellite is an EKS cluster in another region (same account); the console builds its kube client from
  `eks.describe_cluster` (endpoint, CA) and an STS-presigned token (the aws-iam-authenticator scheme) under its own
  pod role, which the satellite's access entry / aws-auth maps to the console's ClusterRoles. The satellite's edge
  is its own ALB IngressGroup (the chart's Ingress shape) with its own certificate; ECR is regional — the satellite
  pulls the home region's repo (cross-region pull is allowed in one account). AWS compute objects (WAF IP sets,
  target groups) are regional: IP rules and rebalance act in the zone's region.
- Azure: a satellite is an AKS cluster in another location (same subscription/resource group or its own); the
  console builds its kube client from ARM `managedClusters.listClusterUserCredential` (kubeconfig) with its
  workload identity, the satellite's Envoy Gateway + public IP + certificate make its edge; the ACR is global.
- Blocked regions (C7) win over everything: a region a super admin blocked cannot host a zone even when a satellite
  covers it (403 `region … is blocked by a super admin` as today, checked before the cluster rule).

### 28.3 Infra
- `deploy/terraform/{gcp,aws,azure}-satellite/` (new), one per cloud, mirroring each home module's cluster + edge
  pieces only. GCP: inputs `project`, `region`, `name` (default `ramen-<region>`),
  `public_hostname` (optional), `console_gsa` (email, for the output only). Creates: regional Autopilot cluster,
  global static address, Certificate Manager cert + map for the satellite host. Outputs `cluster_name`, `region`,
  `host`, `certificate_map`, `helm_set`. No bucket, Firestore, registry, console GSA — shared from the home apply.
- `deploy/helm/ramen-satellite/` (new chart, `provider: gcp|aws|azure` like the home chart): `ramen-system`
  namespace label for routes, the edge (GKE Gateway + certmap / AWS ALB Ingress group / Azure Envoy Gateway, the
  home chart's shapes), the console identity's ClusterRoleBindings (console + zone ClusterRoles; GCP: the GSA as a
  User, AWS: the console pod role via an access entry or aws-auth, Azure: the console's workload identity object
  id), no console/builder/collector. Values: provider, region, `console.identity`, `gateway.*`/`aws.*`/`azure.*`.
- `scripts/cloud_smoke.sh` and `cloud2.sh` learn `RAMEN_SATELLITE_REGION`/`RAMEN_SATELLITE_HOST`: the §28 proof
  creates a zone in the satellite region, waits for the attach job, calls `2+3=5` through the satellite host,
  deletes the zone (D69 cleanup on the satellite cluster too).

### 28.4 Verification
Console: `regions.cluster_for`, the 422s (no cluster / AWS / Azure), the Clusters card round trip and its 409, the
zone's `cluster`/`host` in API and pages, the sign-in guide naming the satellite host, `GcpCloud` routing every
kube call to the zone's cluster (fakes: two clusters), cross-cluster region change = detach + attach. Cloud, on all three (GKE home us-central1 + satellite australia-southeast1; EKS home us-east-1 + satellite us-west-2;
AKS home westus2 + satellite eastus): item list in 28.3, plus deleting the satellite zone frees its
namespace and NEGs, and the Config card refuses to drop the satellite while the zone exists.

## 29. v1.0.0c Phase 1 — promptfoo evals, LLM providers, client policy (binding, D72)
`instructions/v1.0.0c.md` Phase 1, decided with the user on 2026-11-16: Phase 1 only; evals **report first, gate per
group**; LLM providers **Anthropic, OpenAI, Azure OpenAI**; client enforcement = **bridge-held policy bundle AND
client config files**. Phases 2–5 are later iterations.

### 29.1 Config → LLM providers (super admin)
`config/llm_providers` = `{anthropic: {api_key}, openai: {api_key, base_url?}, azure_openai: {api_key, host}}`, each
optional; keys are console secrets (§24.5 vault, shown as set/not set). `GET/PUT /api/v1/config/llm-providers`
(PUT merges; an empty string clears a key), audited `config.llm_providers` with `provider:` tags only. A card on the
Config page. The providers feed evals as env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` + `OPENAI_BASE_URL`,
`AZURE_API_KEY` + `AZURE_API_HOST`) — never written to a log, a job result or a client.

### 29.2 Evals from the group repo, run on deploy
- The group repo may carry `mcp/evals/promptfooconfig.yaml` (promptfoo-native; `providers` may be the `mcp`
  provider pointed at `{{env.RAMEN_MCP_URL}}` with bearer `{{env.RAMEN_MCP_KEY}}`, or an LLM provider with
  `config.mcp` tools; tests assert as promptfoo does). Ramen injects env `RAMEN_MCP_URL` (the canary's in-cluster
  Streamable HTTP URL), `RAMEN_MCP_KEY` (the deploy's `RAMEN_EVALS_KEY`, §31.4a; a group key before D76),
  `RAMEN_GROUP`, `RAMEN_ENV`, `RAMEN_ZONE` and the 29.1 provider env. The console renders `{{env.RAMEN_*}}` in the
  config text before the run (promptfoo does not render env refs inside a provider's `server.url` / headers); the
  provider keys stay env vars promptfoo reads itself. A run whose results file is missing reports promptfoo's last
  log lines in `evals.error`.
- Group settings `evals: {gate: false, required: false}` — `PUT /api/v1/groups/{g}/evals` (group admin), shown and
  edited on the group page.
- Deploy (after the golden cases of a zone, when the file exists and `RAMEN_EVALS_IMAGE` is set): a Job
  `ramen-evals-<group>-<zone>-<rand>` in `ramen-system` (the builder's ServiceAccount pattern, §24.2) runs
  `promptfoo eval -c <config> -o /out/results.json --no-cache --no-progress-bar --no-table` (plus `--share` with
  `PROMPTFOO_REMOTE_API_BASE_URL`/`PROMPTFOO_REMOTE_APP_BASE_URL` = the chart's promptfoo server when it runs, so
  the run appears in its UI) with the config mounted from a ConfigMap built from the synced repo file; the console
  reads `results.json` from the pod log (`cat` after the eval; the builder's log pattern), parses
  `stats.{successes,failures,errors}` and the failed tests' `description`/`failureReason` (≤ 20 lines into the job
  log), and records `job.result[zone].evals = {passed, failed, errors, url, run?}` — `url` = the console's report page
  `/groups/<g>/evals/<job>/<zone>`, `run` = promptfoo's eval id — and `job.reports[zone].evals = {passed, failed,
  errors, id?, cases: [{name, status: pass|fail|error, input?, output?, reason?, latency_ms?}], truncated?}` (≤ 200
  cases, each text ≤ 500 chars, scrubbed). promptfoo's `shareableUrl` is not kept (it names the in-cluster server).
  Exit 100 = failures, 1 = error, timeout `RAMEN_EVALS_TIMEOUT_SECS` (600). With `gate` on, failures or an error
  fail the deploy exactly like a failed golden case (canary scaled down, stable untouched); with `gate` off they are
  a warning line and a red `evals: <n> failed` badge on the group page's last deploy. No file → nothing runs and the
  zone's result carries no `evals` key (the harness reads that as "absent"). No `RAMEN_EVALS_IMAGE` → `evals: {skipped: "no evals engine"}` in the result, once per deploy in
  the log.
- Chart `evals.engine: none | promptfoo` (default none): `promptfoo` adds a Deployment + Service `promptfoo`
  (`ghcr.io/promptfoo/promptfoo:<pinned>`, port 3000, a PVC for `/home/promptfoo/.promptfoo`, single replica, a
  credential on its TLS front since D92 (§39.7), no edge route — try-out and test runs, like Jaeger/Phoenix) and hands the console `RAMEN_EVALS_IMAGE`,
  `RAMEN_EVALS_API_URL` (`https://promptfoo.<ns>:3000`, §39) and `RAMEN_EVALS_UI_URL` = `evals.uiUrl` (default
  `https://localhost:3443`: where a person opens promptfoo's UI, through its TLS front after `kubectl -n ramen-system
  port-forward deploy/promptfoo 3443:3443`; also the Job's and the server's `PROMPTFOO_REMOTE_APP_BASE_URL`; promptfoo
  itself listens on 127.0.0.1:3000 only, pinned by a chart-mounted Node preload). `evals.image` may be
  set without the server (Jobs only, `--no-write`).
- **The run page** `GET /groups/{g}/evals/{job}/{zone}` (group member, `viewer`+, like §30.2's scan report; 404 for a
  job of another group or a zone without a run): the counts, every case of `job.reports[zone].evals`, and — when the
  chart runs the server — the run's address in promptfoo's UI (`<RAMEN_EVALS_UI_URL>/eval/<run>`) with the
  port-forward command. The group page's evals badge links it. promptfoo's UI itself is **not** proxied through the
  console (1.0.0c, found on GKE): its single-page app hard-codes `/assets/…`, `/api/…` and a root router (no base
  path), has no sign-in of its own and lists every group's runs, so it stays a port-forward-only operator tool behind
  the operator's credential (D92, §39.7).

### 29.3 Policy to clients: versions, the worker gate, the bridge, client config files
- The token answer (§24.3/§26.3) gains `evals: {version, required, config}` when the group repo carries the file:
  `config` is the file's text (≤ 64 KiB), `version` = first 12 hex of sha256 over `config` + `required`,
  `required` = the group's `evals.required`. Workers learn `RAMEN_EVALS_VERSION` / `RAMEN_EVALS_REQUIRED` in the
  deploy secret and by `push_config` when either changes (like telemetry). `GET /.well-known/ramen-policy?
  group=&zone=` (public, no secrets) answers `{telemetry: {version, required}, evals: {version, required}}` for the
  bridge's offline check.
- Worker gate (node): a modern call carries `_meta["io.ramen/evals"] = {"version": "<v>"}` (a legacy body via the
  bridge: `params.evals`). When the group `required`s evals: missing → `-32026 evals config required` (HTTP 428,
  `data: {version}`), stale → `-32026 evals config changed: sign in again`; the gate runs after the telemetry gate,
  before the runtime; a worker told no version accepts any. Access-log reason `evals_required` / `evals_stale`.
- The bridge (0.5.0): the token answer's `telemetry` + `evals` form the **policy bundle**, kept at
  `~/.ramen/policy/<host>/<group>-<zone>.json` (0600; telemetry headers stay in memory only, as §26.3) and the evals
  config at `~/.ramen/evals/<host>/<group>/promptfooconfig.yaml`; every forwarded call carries the evals version;
  `-32026` → sign in again once (as `-32025`); at start without a bundle and unable to sign in → exit non-zero with
  `Ramen policy missing: reconnect (sign in) — Ramen cannot be used without it`. **Client config files** (default on,
  `--no-client-config`): at sign-in the bridge merges, idempotently and with a `ramen` marker, (1) Claude Code
  `~/.claude/settings.json` → `hooks.SessionStart` running `ramen-mcp-bridge policy check --target <host> --group
  <g> --zone <z>` (non-zero = stale/missing, with the reconnect message), (2) Cursor
  `~/.cursor/rules/ramen-<g>-<z>.mdc` stating the policy (telemetry/evals required, versions, how to re-sign-in),
  (3) VS Code user `settings.json` → `mcp.servers.ramen-<g>-<z>` for the bridge; a ledger `~/.ramen/clients.json`
  records every write; `ramen-mcp-bridge policy {show,check,remove}` show the bundle, compare it with
  `/.well-known/ramen-policy`, or revert every write. An LLM that never uses the bridge or Ramen's workers cannot be
  forced — documented limit. The bridge is one client of the policy, not the only enforcement point: every sign-in
  path into Ramen (console password / magic link, OAuth providers, a group key) answers the same policy, and the
  workers gate every call on every required version whatever client sent it (D73).

### 29.4 Verification
Console: providers card round trip (set/not set, clear, audit tags, never in logs), group `evals` settings, deploy
with a fake Job (passed / failed-gate / failed-report / error / absent / no engine), results parsing, token answer
`evals`, `/.well-known/ramen-policy`, pages, the run page (cases, member 200 / outsider 403 / other job 404, the
port-forward address only with a server), `evals` offered in the tool-access lists and refused as a custom role name.
Node: the `-32026` gate (required/missing/stale/none-known); the evals key is kind `evals` — hidden by a rule without
it, admitted by a rule naming it — and a token with role `evals` is refused. Bridge:
bundle persistence, version on calls, re-sign-in on `-32026`, start without a bundle, client files written /
checked / removed in a temp HOME (every editor file merged, never clobbered). Cloud (GKE, latest): chart with
`evals.engine: promptfoo`; the demo repo's `mcp/evals/promptfooconfig.yaml` (mcp provider, no LLM key needed) → a
deploy reports `evals: {passed: 6, failed: 0, errors: 0}` with demo/dev's tool access narrowing `word_count` to
group_admin, analyst and `evals`, and an analyst opens the run page from the badge; gate behaviour proven on the
local stack where the test owns the repo file.

## 30. v1.0.0c Phase 2 — NVIDIA SkillSpector scans, more LLM providers (binding, D73)

SkillSpector (github.com/NVIDIA/SkillSpector, Apache-2.0, pinned tag **v2.13.4**) is a CLI scanner for agent skills:
static rules (prompt injection, exfiltration, supply chain, MCP tool poisoning, least privilege) plus optional LLM
semantic analysis; 0–100 risk score → `SAFE` (≤ 20) / `CAUTION` (≤ 50) / `DO_NOT_INSTALL`; JSON and SARIF output;
`--recursive` scans the immediate subdirectories holding a `SKILL.md`, at most 32 (aggregate `{max_risk_score,
risk_severity, risk_recommendation, analysis_completeness, skills_scanned, skills: [{name, path, risk_score,
risk_severity, finding_count, issues: [{id, severity, location: {file, start_line}, explanation, …}], …}]}`); exit
0/1 (score > 50 or a strict gate)/2 (error). No PyPI package, no image, no UI.

### 30.0 Shipping it
- `deploy/docker/skillspector/Dockerfile` builds **`ghcr.io/bkraad47/ramen-skillspector:2.13.4`** (clone of the
  pinned tag, the upstream multi-stage build, non-root `USER`, `WORKDIR /scan`, `ENTRYPOINT ["skillspector"]`);
  the release workflow publishes it next to console / worker; `scripts/check_versions.py` pins the tag in one place.
- Chart `skillspector.engine: none | skillspector` (default `none`), `skillspector.image`, `skillspector.cpu/memory`;
  `skillspector` → console env `RAMEN_SKILLSPECTOR_IMAGE` (+ `RAMEN_SKILLSPECTOR_TIMEOUT_SECS`, default 600). The
  Config page shows the engine and the image under "Evals & scans". Nothing else runs: SkillSpector has no server.

### 30.1 Config → LLM providers, part 2 (super admin)
`config/llm_providers` (§29.1, same vault, same `PUT /api/v1/config/llm-providers`, same audit tags) gains
`vertex: {project, location}` (ADC of the Job's ServiceAccount — Workload Identity on GKE, no key),
`bedrock: {region}` (the Job's cluster identity — IRSA on EKS, no key), `foundry: {endpoint, api_key,
deployment}` (Azure AI Foundry / Azure OpenAI), `openai_compatible: {base_url, api_key, headers}` (local or any
OpenAI-compatible server; `headers` a flat JSON object). A second card, "More providers", on the Config page; the
answer tells `set`/`not set` only. Env handed to the evals Job (§29.2) and the scan Job (30.2):
`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `AWS_REGION`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_DEPLOYMENT`, `SKILLSPECTOR_COMPAT_BASE_URL`, `SKILLSPECTOR_COMPAT_API_KEY`,
`SKILLSPECTOR_COMPAT_HEADERS` (promptfoo gets `OPENAI_BASE_URL` for the compatible server as before). Only
providers that are set are exported. The Job's ServiceAccount is the builder's (§24.2); the cloud adapters grant it
nothing new in Phase 2 — Vertex / Bedrock analysis needs the operator to grant `roles/aiplatform.user` /
`bedrock:InvokeModel` to that identity (documented, not automated; D73).

### 30.2 Scan on deploy
- Group settings `skillspector: {gate: false, required: false, llm: ""}` — `PUT /api/v1/groups/{g}/skillspector`
  (group admin; `llm` is one of the configured providers' names or `""` = static only `--no-llm`; `vertex` is
  refused for scans — v2.13.4 has no ADC Vertex provider; `foundry` maps to upstream `azure_openai`); shown and
  edited on the group page next to evals.
- Targets, per zone after the evals of §29.2 (when `RAMEN_SKILLSPECTOR_IMAGE` is set): a scan directory holding
  (1) every immediate subdirectory of the repo's `mcp/skills/` that has a `SKILL.md` (the group repo's skills,
  from the same synced copy the image build uses), and (2) a synthesized skill `ramen-served-<zone>/SKILL.md`
  written by the console from the canary's `tools/list` and `prompts/list` (name, title, description, input schema
  as fenced JSON) so the instruction-text rules run against what the zone really serves. The repo's optional
  `mcp/skillspector-baseline.yaml` is applied to single-skill scans only (upstream rejects it with `--recursive`;
  Ramen then scans skill by skill with the baseline and merges into the recursive shape). `skillspector scan /scan
  --recursive --format json -o /out/report.json [--no-llm | SKILLSPECTOR_PROVIDER=<llm>]`; the Job is
  `ramen-scan-<group>-<zone>-<rand>` in ramen-system, builder ServiceAccount, the directory handed as one ConfigMap
  (`files.json` = {path: base64}, unpacked by a helper the image's python runs; ≤ 900 KiB packed — a larger
  `mcp/skills` tree is an error of the run naming the size), report echoed between `RAMEN_SCAN_RESULTS_BEGIN/END`
  like §29.2. The console env is set only when `skillspector.engine: skillspector` (the image alone does nothing).
- `job.result[zone].skillspector = {score, severity, recommendation, skills, findings, url}`; `findings` = count;
  the full report (≤ 256 KiB, else truncated with `truncated: true`) is kept on the job record under
  `reports[zone].skillspector` and shown at `GET /groups/{g}/scans/{job}/{zone}` (group member; a table per skill
  with each finding's rule, severity, file:line, message) — that page is the "link for prompts" of the brief, since
  the tool has no UI; the group page's Prompts/Packages section and the per-zone badge link to the latest one.
- `gate` on: `DO_NOT_INSTALL` (or a run error) fails the deploy like a golden case (`GateError`); `CAUTION` is a
  warning line. `gate` off: report only. Env `RAMEN_SCAN_*` mirror §29.2's `RAMEN_EVALS_*` names.

### 30.3 Policy to clients
- Token answer gains `skillspector: {version, required, llm, baseline}` when the chart runs the engine or the group
  sets `required`: `version` = first 12 hex of sha256 over `required` + `llm` + the baseline text (`""` without
  one), `baseline` = `mcp/skillspector-baseline.yaml` text (≤ 64 KiB). `/.well-known/ramen-policy` answers
  `skillspector: {version, required}` too. Workers learn `RAMEN_SKILLSPECTOR_VERSION` / `_REQUIRED` with the deploy
  secret and `push_config`.
- Worker gate (node): `_meta["io.ramen/skillspector"] = {"version"}` (legacy `params.skillspector`); when required:
  missing → `-32027 skillspector policy required` (HTTP 428, `data.version`), stale → `-32027 skillspector policy
  changed: sign in again`; after the evals gate, before the runtime; reasons `skillspector_required` / `_stale`.
  Every sign-in path hands out the same policy; the gate does not care which client calls (D73).
- Bridge (0.6.0): the bundle carries `skillspector`; every call carries its version; `-32027` → one re-sign-in.
  `policy check` (the Claude Code `SessionStart` hook, the Cursor rule text, `policy show`) additionally runs, when
  `required`: `skillspector scan <dir> --recursive --no-llm --format json` over the client's skill directories
  (`~/.claude/skills`, `<cwd>/.claude/skills`, `~/.codex/skills` when present; the baseline when the bundle has one
  and the directory holds one skill), with the local `skillspector` (`uv tool install
  'git+https://github.com/NVIDIA/SkillSpector.git@v2.13.4'` is the hint when missing). `DO_NOT_INSTALL` for any
  skill, or no `skillspector` binary → exit 3 with the skills named and `Ramen policy: fix or remove these skills
  — Ramen cannot be used with them`; `CAUTION` is printed. Results are cached per directory content hash in
  `~/.ramen/scans/<host>/` for 24 h so hooks stay fast. `--no-client-scan` turns the local scan off (the bundle
  and the version contract stay).

### 30.4 Verification
Console: providers part 2 round trip (set/not set, env export, never in logs), group `skillspector` settings,
deploy with a fake Job (SAFE / CAUTION / DO_NOT_INSTALL gated and reported / error / no skills and no served
tools / no engine), synthesized `SKILL.md`, report page and RBAC, token answer, policy endpoint. Node: the
`-32027` gate. Bridge: bundle, version on calls, local scan with a fake `skillspector` on PATH (clean / caution /
block / missing), cache, `--no-client-scan`. Cloud (GKE, latest, the real image): the demo repo's
`mcp/skills/*` + the served tools scan → a deploy reports `skillspector: {score, recommendation: SAFE|CAUTION}`
and the report page renders; `gate` proven on the local stack with a planted malicious `SKILL.md`.

## 31. v1.0.0c Phase 3 — initial LLM configs from the group repo, accepted per user, enforced everywhere (binding, D74)

User's rule (D73/D74): every LLM interface that signs in to Ramen gets the group's configs and telemetry settings;
enforcement = the client config files the bridge writes + the worker gate on every call from any client. Honest
limit (documented, unchanged): a client that reads none of the files and never calls Ramen's workers cannot be forced.

### 31.0 Repo layout — `mcp/llm-config/`
- `manifest.yaml`: `version: 1`, `description`, `targets: [{client, file, path, format, merge}]`. `client` is a free
  label (`claude-code`, `cursor`, `vscode`, `codex`, `gemini-cli`, `env`, anything); `file` the fragment in the same
  directory; `path` the file on the user's machine (`~`, `{group}`, `{zone}`, `{host}` expanded); `format`
  `json | toml | yaml | text | env`; `merge` `deep` (json/toml/yaml: Ramen's keys win, others untouched), `block`
  (text: one `# ramen:<host>/<group>` … `# /ramen` block replaced in place — `<!-- … -->` in Markdown; the 0.6.0-era
  `ramen:<group>` form is migrated on the next write), `replace` (whole file, ramen-owned), `keys`
  (env: `KEY=value` lines replaced by key). The fragment may use `${RAMEN_OTLP_ENDPOINT}`, `${RAMEN_OTLP_HEADERS}`,
  `${RAMEN_USER}`, `${RAMEN_GROUP}`, `${RAMEN_HOST}` — filled in by the bridge at write time (the token never
  reaches the repo or the console's answer in clear; the bridge holds it).
- Allowed content is checked by the console at sync (a violation fails the deploy with the key named): a Claude Code
  `settings.json` fragment may set `hooks`, `env`, `permissions`, `model`, `otelHeadersHelper`-free telemetry env;
  never `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `forceLoginMethod`, or any hook command outside
  `ramen-mcp-bridge …`; a Codex/Gemini/other fragment may not name `*_API_KEY*`, `*_TOKEN*` keys or paths outside
  `~`; `text` fragments ≤ 32 KiB; the whole directory ≤ 256 KiB. `version` = first 12 hex of sha256 over every file
  (sorted) + the manifest. Absent directory → no `configs` key anywhere, nothing gated (Phase 3 is not optional for
  the product, it is optional for a repo).

### 31.1 Acceptance (console)
- Token answer (§24.3) and `/.well-known/ramen-policy` gain `configs: {version, required: true, accepted: <bool>,
  manifest, files: {<file>: <text>}}` (the policy endpoint: `{version, required}` only). Acceptance is per user and
  version: `users/<id>.accepted_configs = {<group>: {version, at}}`; `POST /api/v1/groups/{g}/configs/accept
  {version}` (any member; audited `configs.accept`); `GET /groups/{g}/configs` (console page) and the OAuth consent
  page show the manifest and every file verbatim with an Accept control; both are reached on every sign-in path —
  the OAuth flow redirects to consent as today, a password / magic-link sign-in that lands on a group with
  unaccepted configs is redirected to `/groups/{g}/configs` before any token or key is handed out; the user token (`POST /oauth/token`, code exchange — a refresh grant is never gated: it answers `configs.accepted:
  false` so a waiting bridge sees acceptance land) and MCP-key minting for a user answer `403 configs not accepted`
  (`data: {version, url}`) until then; the accept API answers 200, 409 on a stale version. A group key counts as
  accepted by the admin who minted it (`mcp_keys[].accepted_configs = version`, recorded; a stale key version is shown
  on the group page). A new version invalidates acceptance → re-accept at the next sign-in (the worker gate makes
  "next sign-in" now). The group page lists acceptance per member (admin).

### 31.2 Worker gate (node)
`_meta["io.ramen/configs"] = {"version"}` (legacy `params.configs`); env `RAMEN_CONFIGS_VERSION` / `_REQUIRED`
(`1` whenever the repo has the directory) in the deploy secret and `push_config`; missing → `-32028 configs
required` (HTTP 428, `data.version`), stale → `-32028 configs changed: sign in again`; after the skillspector gate.
Reasons `configs_required` / `configs_stale`. The gate holds for every client (D73).

### 31.3 Bridge (0.6.0) — apply, verify, OTel
- Sign-in: if `configs.accepted` is false the bridge opens the consent URL (`/groups/{g}/configs?next=…`) and waits
  (as the OAuth flow); then writes every target per `merge`, records each in `~/.ramen/clients.json` (path, format,
  sha256 of the ramen block / keys, version), carries the version on every call, `-32028` → one re-sign-in,
  `policy remove` reverts every write, `policy show` lists targets and their state.
- **Telemetry targets** (built in, not from the repo; written when the group requires telemetry, §26.3): for Claude
  Code `env: {CLAUDE_CODE_ENABLE_TELEMETRY: "1", OTEL_METRICS_EXPORTER: "otlp", OTEL_LOGS_EXPORTER: "otlp",
  OTEL_TRACES_EXPORTER: "otlp", OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf", OTEL_EXPORTER_OTLP_ENDPOINT:
  "<console>/otlp", OTEL_EXPORTER_OTLP_HEADERS: "Authorization=Bearer <user token>", OTEL_RESOURCE_ATTRIBUTES:
  "ramen.group=<g>,ramen.user=<email>,ramen.host=<host>"}` in `~/.claude/settings.json`; the same `OTEL_*` lines in
  `~/.ramen/env.sh` (`format: env`) for any tool that honours the OTel SDK environment (Codex, Gemini CLI, Python /
  Node SDKs, harnesses), plus a one-line `source ~/.ramen/env.sh` block in `~/.zshrc` / `~/.bashrc` (`merge: block`,
  only when the user did not pass `--no-shell-rc`). Codex `~/.codex/config.toml` gets Codex's own shape,
  `otel.exporter = { otlp-http = { endpoint = "<console>/otlp/v1/logs", protocol = "binary", headers = {…} } }`,
  when `~/.codex` exists. Files that carry the token are 0600 and rewritten on every token refresh; the ledger keeps
  hashes of Ramen's part only, never the token. `policy remove` restores byte-for-byte when nothing else in the file
  changed since the write, else removes only Ramen's block/keys and keeps the user's later edits.
- The bridge keeps one ledger entry per (file, source) so a repo fragment and the built-in telemetry target on the same
  file restore independently; Claude Code `hooks.*` lists merge by hook command (the bridge's own `policy check` hook is
  never clobbered); Cursor/VS Code entries get a `-<host>` suffix when another host already owns the plain name;
  `policy remove --target <host>` touches that host's entries only and never leaves a file empty. Started from a
  `--token-file` with telemetry required, the bridge fetches a telemetry object before its first gated call, and treats a
  worker's `-32003 … requires telemetry` like `-32024/-32025`: one re-sign-in, then the retry (P4-B2…B5).
- `policy check` (the SessionStart hook, Cursor rule, VS Code) verifies every recorded target still holds its block /
  keys (hash compare) and that the telemetry env is present where written; anything missing → exit 3 `Ramen configs
  removed or changed: reconnect (sign in) — Ramen cannot be used without them`; the bridge refuses to forward calls
  while the check fails (same path as a missing bundle).

### 31.4 OTLP ingest on the console
`POST /otlp/v1/traces | /otlp/v1/metrics | /otlp/v1/logs` (OTLP/HTTP, protobuf or JSON, ≤ 4 MiB, gzip accepted),
bearer = a user's token (OAuth access token, console session token or MCP key); the console stamps resource attributes
`ramen.group`, `ramen.user` (email or `key:<name>`), `ramen.zone` when known, `openinference.project.name = <group>`
(Phoenix: one project per group) and `service.namespace = ramen/<group>` and forwards to `config/otel.endpoint`
with `config/otel.headers` (one upstream call per request, 10 s, errors answered as `502` with the upstream status;
`204` when telemetry is off). Rate limit per token: 60 requests/min, else `429`. Nothing is stored on the console;
an audit line per 1000 requests per token (`telemetry.ingest`). The Config page shows the ingest URL. This is the
endpoint every pushed telemetry setting points at; §26.3's `client_endpoint` stays for clients that already export
directly.

### 31.4a Ramen's own clients and the gates (D76)
The gates of §26.3/§29.3/§30.3/§31.2 are for the people's clients. Ramen's own calls are exempt: the console's golden
cases (admin key, as before) and the evals Job (§29.2), which authenticates with **`RAMEN_EVALS_KEY`** — a random key
the deploy generates per run, written into the zone's deploy Secret (so the canary accepts it) and into the Job's
Secret as `RAMEN_MCP_KEY`; workers accept it as a bearer for MCP methods only (never `Admin/*`), skip every policy
gate for it (access-log `user = evals`), and forget it with the next deploy (rotated each run). Its caller kind is
**`evals`** (user decision 2026-11-17, after GKE: demo/dev narrowed `word_count` to group_admin + analyst and the eval
errored `Tool word_count not found`; as a `key` no rule could admit the Job without every group key):
`RAMEN_TOOL_ACCESS` applies to it like to any kind — a rule that does not name `evals` hides the tool from the Job (its
case errors "not found", documented), a rule that names it admits it; `RAMEN_BLOCKED` hides names as for everyone. The
console offers `evals` in the tool-access lists ("Evals Job", hint "the evals Job before a deploy goes live") and
reserves the name (no custom role `evals`, like `key`); a worker refuses a token whose `role` is `evals`
(`token_invalid`). Bare test clients
(`scripts/cloud_smoke.sh`, the harness) are not exempt: they read `/.well-known/ramen-policy` and carry the required
`_meta` versions like any client.

### 31.5 Verification
Console: manifest validation (allowed/forbidden keys, sizes), version, acceptance round trip and RBAC, redirects on
every sign-in path, 403 on token/key until accepted, group key acceptance, token answer/policy endpoint, consent
page and configs page render files verbatim, OTLP ingest (auth, stamping, forwarding to a fake collector, size and
rate limits, off → 204). Node: `-32028` gate. Bridge: every `format`/`merge` in a temp HOME (idempotent, never
clobbers the user's own keys), telemetry targets, env.sh + rc block, `policy check` detects a removed block, `policy
remove` restores files byte-for-byte to the pre-ramen state where Ramen owned a block. Cloud (GKE, latest): the demo
repo's `mcp/llm-config/` accepted in the browser (user), pushed by the bridge on the operator's machine, a Claude
Code session's native telemetry visible in Phoenix under project `demo` with `ramen.user` set; a stale version
gated `-32028` live.

## 32. v1.0.0c Phases 4–5 — telemetry by group and user, the independent verifier (binding, D75)

### 32.1 Phase 5 — every span names the group and the user
- Worker (node) OTel resource gains `openinference.project.name = <group>` and `service.namespace = ramen/<group>`
  (Phoenix shows one project per group); every call span (tools/call, prompts/get, resources/read, tasks) carries
  `ramen.user` (the token's email, or `key:<key id>` for a group key — workers know keys by their 8-hex access-log id,
  the console's ingest and key list name them), `ramen.group`, `ramen.zone`, `ramen.env`, and
  the client-reported events of §26.3 become child spans (`kind` as `ramen.event.kind`, `name`, `ms`, `ok`, the
  reasoning text truncated to 4 KiB as `ramen.reasoning`) under the call span, so a user's LLM and tool activity is
  one trace per call. The console's deploy spans gain `openinference.project.name = <group>` too.
- The OTLP ingest of §31.4 is how everything a user's client does outside a Ramen call reaches the same project: the
  bridge's telemetry targets (§31.3) point Claude Code's native telemetry and the OTel SDK env at it; the console stamps
  `ramen.user` / `ramen.group` on every resource. With that, "all user LLM activity after sign-in" = Ramen call spans
  (worker) + client-native spans/metrics/logs (ingest), both under the group's project, both naming the user.
- Config page: a "Telemetry by group" row with the ingest URL and the Phoenix project naming; `telemetry.md` says what
  lands where.

### 32.2 Phase 4 — the independent verifier
Not a product feature: a sub-agent that was not part of the build reads §26.3, §29–§31 and this section and proves on
the live stack (GKE, latest) that (1) a call without each required `_meta` version is refused with its code
(`-32024/-32025` telemetry, `-32026` evals, `-32027` skillspector, `-32028` configs) through the edge over HTTP and
gRPC, by a key and by an OAuth token; (2) changing each policy (config/otel save, evals `required`, skillspector
`required`, a new `mcp/llm-config` version) flips the live workers without a deploy (push_config) and the bridge
re-signs-in once then carries the new version; (3) the bridge's `policy check` exits 3 when a client file is removed,
and the SessionStart hook blocks a Claude Code session until re-sign-in; (4) a Claude Code session's native telemetry
and a Ramen call appear in Phoenix under project `demo` with `ramen.user`; (5) `policy remove` restores the files.
Its findings go to `reports/test-status-v1.0.0c.md` (Phase 4 section) with the exact requests; anything it cannot
prove is listed as such, never papered over.

## 33. v1.0.0c Phase 7 — MCP Apps, Skills over MCP, the Iris example, the security pass (binding, D77)

### 33.1 MCP Apps (runtime, `io.modelcontextprotocol/ui`, modern only)
- A group repo may carry `mcp/apps/<name>/index.html` (+ `app.yaml`: `title`, `description`, optional `csp`
  allowlist). The runtime serves each as a resource `ui://<group>/<name>` (`resources/list` entry with
  `mimeType: text/html;profile=mcp-app`, `_meta["ui/csp"]` when set) — `resources/read` returns the HTML text
  (≤ 512 KiB, read like other resources, access-gated like a resource). A tool package may declare `"ui": "<name>"`
  in its proto JSON (`<name>.json`; the app must be loaded, else the tool fails to load); the tool then carries
  `_meta["ui/resourceUri"] = "ui://<group>/<name>"` in `tools/list` and in every tool result (`_meta` on the result,
  not on a §23.4 round). The load result carries `apps` (name, uri, title, description) and the manifest hash covers
  them. `server/discover` lists the extension when any app is loaded. The
  console's Packages card lists apps per zone; the group page links each `ui://` resource to a sandboxed preview
  (`/groups/{g}/apps/{zone}/{name}`: `<iframe sandbox="allow-scripts" srcdoc=…>` with the app's CSP, no credentials).
  The bridge passes the resource and `_meta` through unchanged. SkillSpector scans `mcp/apps/*` HTML as part of the
  synthesized skill text (script-bearing HTML is called out as CAUTION by its rules).
### 33.2 Skills over MCP (`io.modelcontextprotocol/skills`, modern only)
- `skills/list` → `{skills: [{name, description, version, files: [<relative path>…]}]}` from `mcp/skills/*/SKILL.md`
  (frontmatter `name` = the folder, `description`; `version` = sha12 over the folder's files, sorted, paths and
  contents); `resources/read` of `skill://<name>/SKILL.md` and `skill://<name>/<file>` (every file ≤ 256 KiB,
  checked at load naming the file and again at read; UTF-8 → `text`, else base64 `blob`; a path outside `files`
  is not found, `..` or a leading `/` is refused with `-32602`), both gated like tools (tool access `skills:<name>`,
  the blocked list too; the node applies it); the load result carries `skills` (the `skills/list` entries) and the
  manifest hash covers them; listed in `server/discover` extensions. The bridge gains `ramen-mcp-bridge skills install <name>`
  (writes `~/.claude/skills/<name>/…` after a SkillSpector scan when the group requires it, recorded in the ledger,
  removed by `policy remove`) and `skills list`. The console's Packages card lists skills per zone with the scan
  recommendation from the last deploy.
### 33.3 The Iris example (demo repo)
`mcp/resources/iris_csv` (the 150-row public-domain Iris data, `text/csv`), tools `iris_describe` (per-column
count/mean/std/min/max via pandas), `iris_outliers {column, z: 2.5}` (rows whose z-score exceeds `z`, ≤ 50 rows),
`iris_group_means {by: species}`; prompt `iris_analysis` (SKILL.md package guiding the agent through describe → outliers →
group means, boundaries: never fabricate numbers); app `iris_chart` (an inline SVG histogram built from the tool result
via MCP Apps); skill `iris-analyst` (Skills over MCP); guardrail: `iris_outliers` post-rail blocks results above 50 rows
and any output containing a non-numeric cell dump (NeMo rail in `mcp/guardrails.yaml`); evals: three promptfoo tests
(describe counts 150, outliers for `sepal_width` with z 2.5 ≤ 5 rows, group means for 3 species); SkillSpector
baseline `mcp/skillspector-baseline.yaml` accepting the app's inline script rule; `mcp/env.yaml` gets
`IRIS_MAX_ROWS={{$group.IRIS_MAX_ROWS}}` with a group secret so the environment-secrets path is re-proven.
`mcp/requirements.txt` adds `pandas`; the worker image is built on deploy (§24.2).
### 33.4 Security pass and the full retest
A sub-agent that did not build 1.0.0c probes the live GKE stack read-only (auth matrix for every role incl. keys with
and without acceptance, every gate code, headers, `/otlp` abuse: spoofed resource attributes, oversized and gzip-bomb
bodies, rate limit; configs page/consent XSS with a hostile fragment; report page with a hostile finding text; the
scan ConfigMap size bomb; IAM of the builder SA; NetworkPolicies; Secrets at rest) and writes findings with options to
`reports/test-status-v1.0.0c.md` (Phase 7); clearly safe fixes (headers, escaping, limits, RBAC narrowing) are applied,
anything else goes to the user (D77). Then the full retest: local (`make up`), GCP, AWS, Azure — `cloud_smoke.sh`,
`verify` with every `RAMEN_*=1` block, the access matrix, auth + pushed configs through the bridge on the operator's
machine, Phoenix showing the Iris calls under project `demo` with `ramen.user`; AWS and Azure torn down after, GCP kept.
### 33.5 Verification
Runtime: apps/skills listing, read, gating, size limits, `_meta` on tool results, discover extensions; console: cards,
preview page sandbox and CSP, RBAC; bridge: `skills install/list`, scan-before-install, ledger/remove; demo: the Iris
tools' unit tests in the repo, evals green on deploy, guardrail block proven (a tool call that returns 150 rows is
refused), env secret resolved; cloud: as 33.4.

## 34. v1.0.0c Phase 8 — optional engines in the installers, the docs rebuilt (binding, D78)

### 34.1 Installers
Every install path offers the three optional engines with one switch each: terraform variables `otel_collector`
(`none|jaeger|phoenix`, default `none`), `evals_engine` (`none|promptfoo`), `skillspector_engine`
(`none|skillspector`) on `deploy/terraform/{gcp,aws,azure}` (passed to the chart where terraform runs helm; emitted into
the generated `values-<cloud>.yaml` / the documented `helm install` command where the operator runs helm), the same
three keys in `deploy/local/docker-compose.yml` profiles (`--profile phoenix`, `--profile promptfoo`; SkillSpector runs
as a Job image, so compose only needs the image variable), and the kind harness. Defaults stay off; turning one on
changes only that engine's objects. `terraform validate`/`fmt` in CI; `scripts/check_versions.py` pins the three
images in one place.
### 34.2 README and GitHub Pages
README rewritten from the running 1.0.0c: what Ramen is (one paragraph), the key features list (MCP 2026-07-28, Tasks,
MCP Apps, Skills over MCP, Streamable HTTP + gRPC, guardrails, evals, SkillSpector, telemetry by group/user, LLM
configs enforced at sign-in, satellites, three clouds), the roles table (super admin / group admin / analyst / viewer /
mcp_user / group key — what each can do), architecture (console, node, runtime, environments → zones → workers, home
and satellite clusters, the bridge), how enforcement works (the gate codes, the policy bundle, acceptance), deploy
quick start per cloud with the optional engines, screenshots (`make shots`: group page with evals/scan badges, scan
report, configs page, Phoenix project `demo`, promptfoo run, app preview), the honest limit. GitHub Pages (mkdocs)
gain: `features.md` refreshed, `architecture/` refreshed with environments/zones/workers and satellites, a roles page,
"Deploy your first group" walkthrough with screenshots, per-cloud **IAM and network** pages (every role/permission the
console GSA / IAM role / managed identity needs, the builder SA, Workload Identity/IRSA, firewall / SG / NSG rules,
CIDRs, the edge), a **Threat model, security and isolation** page (trust boundaries, what a group can and cannot reach,
secrets at rest, gates, the §33.4 findings and their status), and an **MCP features** page (every MCP capability Ramen
serves and how a group repo codes it, with examples from the demo repo: tools, resources, templates, prompts as
skills, tasks, MRTR, subscriptions, apps, skills, guardrails, evals). Images regenerated with the engines on.
### 34.3 Independent verification
A sub-agent that did not write the docs checks every claim on the running GKE console (D-docs rule: verify against the
console, not the code), every command in the deploy pages against the terraform/helm sources, every link
(`check_docs_links.py`), and fixes what is wrong or marks it as unverified; its list goes to the report. Then AWS and
Azure are torn down, GCP is kept on the latest build for the user's manual check, and every item of
`instructions/v1.0.0c.md` is ticked in the report before the user is told Phase 8 is done.

### 33.4a Hardening after the security pass (D79)
- **Job identity.** The evals and scan Jobs run as ServiceAccount `ramen-evals` (chart `evals.serviceAccount`,
  default `ramen-evals`): no registry, no bucket, no Secret Manager rights — Workload Identity / IRSA / workload identity
  federation to a cloud identity `ramen-evals@…` that holds nothing beyond logging; the builder keeps `ramen-builder`.
  Terraform creates the identity on the three clouds (`evals_gsa` / `evals_role_arn` / `evals_client_id` outputs → chart).
- **Provider allowlist.** At sync the console parses `mcp/evals/promptfooconfig.yaml`: every `providers[]` id must be
  `mcp`, `http`/`https`, `websocket`, or one of the configured LLM provider ids (`anthropic:*`, `openai:*`, `azure:*`,
  `vertex:*`, `bedrock:*`, the compatible server's `openai:` with the configured base URL); `exec:`, `python:`,
  `javascript:`/`file://`, `golang:`, `webhook:` and any `transform`/`extensions` script fail the deploy at the sync step
  naming the entry. Assertions of type `javascript`/`python` with inline code are allowed (they run in the Job as the
  group's own tests do) — the identity makes them harmless.
- **Scrubbing.** LLM provider key values join `remember_secret` so they never appear in logs, job records or pages.
- **IAM (terraform).** GCP: worker GSAs already read the groups bucket under the console's IAM condition
  `resource.name.startsWith("projects/_/buckets/<bucket>/objects/<group>/") || resource.name == <bucket>` (the second
  clause is what GCS needs for listing; object names of other groups stay visible, contents do not — the residual of
  S4); a dedicated node SA `ramen-nodes@` (`logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`,
  `stackdriver.resourceMetadata.writer`, `autoscaling.metricsWriter`, `artifactregistry.reader` on the repo) set as the
  Autopilot node identity for NEW installs — Autopilot refuses the change on a live cluster, so the test cluster keeps
  the default compute SA until re-created (tfvars `node_service_account = "default"`; S5, sec-gaps: Terraform removes
  `roles/editor` from the default compute SA — `google_project_iam_member_remove`, variable
  `remove_default_compute_editor`, default true — after granting it the node roles while a cluster still runs as it,
  and `gcp-satellite` sets `ramen-nodes@` as its node identity); the console GSA's
  `iam.serviceAccountUser` is REMOVED (IAM conditions cannot name service accounts, and the console never impersonates).
  AWS: the worker permissions boundary is keyed on the role tag `group=<group>` (`s3:prefix`, object ARNs and secrets
  under `<group>/`), so a mis-written inline policy cannot cross groups; the console policy carries no `iam:PassRole`.
  Azure: ABAC on the worker `Storage Blob Data Reader` assignment (`blobs:path StringStartsWith '<group>/'` and the
  matching list `prefix` clause) — set by the console's `grant()` with `conditionVersion 2.0`; the storage account is
  pinned `StorageV2`.
- **Network.** NetworkPolicies in `ramen-system`: Phoenix, promptfoo, Redis and the console accept ingress only from
  the console, the collector, the Jobs' pods (`ramen-evals`) and the edge; worker namespaces get an egress policy to
  the console API (`/otlp` ingest + `Admin/*` callbacks), Redis, DNS and the internet — never to ramen-system's
  ClusterIP services directly. Redis runs with `requirepass` (chart generates the password into a Secret; workers and
  the console get it from the deploy/console Secrets).
- **Preview credential (S8).** The app preview reads `ui://` resources with the deploy's evals key as bearer plus the
  admin key (the node accepts no bearer-less call), never a stored MCP key. The evals key is therefore derived —
  `rek_` + HMAC(group session secret, deploy job id)[:32] — rotated per deploy, never stored, recomputable by the console.
- **Workers and the collector.** Workers export OTel straight to the chart's collector (4318) and reach Redis; those two
  ports admit the worker namespaces in the ramen-system policies, everything else in ramen-system (Phoenix UI, promptfoo,
  the console except its own port) is closed to workers. Egress policies need `cluster.serviceCidr`; `redis.enabled`
  defaults off (the test cluster keeps its hand-applied Redis until re-installed).
- **Enforcement (S6b amendment).** The IaC enables an engine: EKS managed `vpc-cni` addon with `enableNetworkPolicy`
  (Terraform home/satellite, CloudFormation), AKS `network_policy = "calico"` (home overlay, satellite node subnet);
  chart `networkPolicy.awsVpcCidr` also sets the console's `RAMEN_AWS_VPC_CIDR` (comma list), Terraform `network_set`.
- **EKS pod subnets (D89, amends S6b).** Nodes, ALB interfaces and pods shared the public /24s, so the edge `ipBlock`
  (the whole VPC) admitted every pod (live, 1.0.0b35: `default` → worker pod 8080 = 200). Terraform home/satellite and
  CloudFormation add one private pod subnet per AZ (`cidrsubnet(vpc, 4, 4 + i)`: 10.42.64.0/20, 10.42.80.0/20; no public
  IP, the public route table, no `kubernetes.io/role/elb` tag, no NAT — egress SNATed to the node) and set the
  `vpc-cni` addon's `env` (`AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true`, `ENI_CONFIG_LABEL_DEF=topology.kubernetes.io/zone`)
  and `eniConfig` (one ENIConfig per AZ: pod subnet + cluster security group) before the node group. The node group is
  `<name>-podnet` with `create_before_destroy` (replaced once on an existing cluster); max pods per t3.small 8 (EKS
  computes it, no launch template). Chart `networkPolicy.awsAlbCidrs` (list) is the AWS edge and the console's
  `RAMEN_AWS_ALB_CIDRS` (comma list); `network_set` / `NetworkSet` emit the public subnet CIDRs, outputs `alb_cidrs`
  (home, satellite). Deprecated, rendered as before when `awsAlbCidrs` is empty: `networkPolicy.awsVpcCidr`,
  `console.env.RAMEN_AWS_VPC_CIDR`; the console reads `RAMEN_AWS_ALB_CIDRS`, else `RAMEN_AWS_VPC_CIDR`, else
  10.0.0.0/16. Still admitted on 8080: node addresses (probes, `hostNetwork`, a home pod's SNATed call to a satellite).
- **Console source networks (S6, sec-gaps).** `console.sourceCidrs` (`RAMEN_CONSOLE_SOURCE_CIDRS`) is admitted only by
  the chart's `console` policy (its own port) — never Phoenix / Jaeger, promptfoo or Redis — and, in zone namespaces,
  only by a satellite zone's `worker` policy, as a separate ingress rule on 8080 (`gcp_k8s.with_console_sources`, applied
  by the adapter's attach for a zone whose cluster is not home); a home zone admits the console as the `ramen-system`
  namespace peer only. On EKS / AKS (`ipBlock` matches pod addresses) the value must list node subnets, never a pod
  range; the chart's NOTES warn when it is set there. Residual: a satellite admits every home pod (GKE pod range;
  EKS/AKS home node subnets after SNAT) — the console has no fixed address.
- **Managed Redis.** Chart `redis.externalUrl` (with `redis.enabled: false`) writes a managed instance's URL into Secret
  `ramen-redis` (`url`, `password`) and runs no Redis; `rediss://` is TLS (the node's client carries rustls + webpki
  roots — managed instances are TLS-only). The Azure Terraform creates an Azure Managed Redis (`managed_redis`,
  Balanced_B0, port 10000, access key) and `helm_set` / `redis_url` (sensitive) carry the URL.
- Verified by: console tests (allowlist accept/refuse matrix, scrub), chart lint + kind (policies applied, Redis auth),
  terraform validate + a live `terraform apply` on the GKE project followed by the smoke and `verify`.

## 35. v1.0.0c — load tests on the three clouds before the Phase 9 spot check (binding, D80)

- **Harness.** `tests/load/` (pytest, opt-in with `RAMEN_LOAD=1`, same env as the cloud tests): an asyncio load
  generator over the edge — Streamable HTTP and gRPC, one group key plus one OAuth token, every call carrying the policy
  `_meta` (telemetry report with 2 events, evals/skillspector/configs versions) — with scenarios `steady` (C=32
  concurrent clients, 120 s, `tools/call demo_calculator_tool`), `mixed` (tools/list, prompts/get, resources/read
  `iris_csv`, tools/call iris_describe, tasks round trip, 90 s), `burst` (C=256 for 30 s against a zone whose scope
  throttle is set to 1000/min — proves 429s, not 5xx), and `scale` (steady ramp 8→64 clients over 4 min on a zone
  with min 1 / max 4 workers — proves the autoscaler adds workers, p95 stays under the bound, and scaling down after
  idle). Each scenario records requests, errors by code, p50/p95/p99 latency, worker count before/after
  (`GET /groups/{g}/zones/{z}/workers`), the access-log reason counts and the Phoenix span count delta for project
  `demo`; thresholds (fail the test): error rate < 0.5 % excluding expected 429s, p95 < 1.5 s on steady/mixed over
  the public edge, no 5xx under burst, every call span present in Phoenix (sampled check). Output: a markdown table per
  cloud appended to `reports/test-status-v1.0.0c.md` ("Load tests") by `scripts/cloud_load.sh <cloud>`.
- **Where.** GCP (the kept cluster), Azure (Managed Redis, D80) and AWS (rebuilt once for the run, torn down after);
  satellites are not load-tested in 1.0.0c. Costs: the runs use the group key path only (no LLM spend) and the
  cheapest worker sizes already deployed.
- **Done when** the three tables are in the report with every threshold met, or each miss has a filed finding with
  the evidence and an option list; only then does the Phase 9 spot-check message go to the user.

## 36. v1.0.0c Phase 2.5 — LLM checks in NeMo Guardrails, scoped LLM providers, guardrails as a client policy (binding, D81)

### 36.1 Rails
- `mcp/guardrails.yaml` (engine `nemo`) may name LLM-backed rails per tool and stage: `self_check_input`,
  `self_check_output`, `jailbreak_detection`, and the NeMo library rails `fact_checking`, `hallucination`,
  `topical` (`topics: [...]`), `sensitive_data` (any rail NeMo 0.24 ships; the runtime maps names to NeMo flows and
  copies the library prompts). The repo's `config.yml` keeps `models: []`; the runtime fills `models:` at load from the
  credential Ramen hands the worker (36.2) — `engine: openai|azure|anthropic|vertex|bedrock|openai_compatible`,
  `model: <allowed model>` — and refuses to load (fail closed, clear error on the deploy job) when a rail needs a model
  and none is available. Rails keep the per-tool `pre`/`post` semantics of §22; LLM rails add latency, so the manifest
  lists them: `guardrails.tools[<tool>] = {"pre": [...], "post": [...], "llm": [...]}` (static-only tools keep the
  `["pre","post"]` list) and `guardrails.llm = {provider, model}`. As built (1.0.0c): NeMo 0.24 ships neither the
  heuristics model for jailbreak detection nor a topic-control model in the worker image, so `jailbreak_detection` and
  `topical` are self-check variants on the main model with generated prompts (a repo `prompts.yml` task wins);
  `hallucination` is one deterministic grounded judge call (temperature 0) over the tool's structured result plus the
  arguments (prose string leaves of the result are the statement, never evidence); `fact_checking` the same evidence
  with NeMo's entailment prompt; NeMo's sampling check stays available as `self_consistency` (three calls,
  non-deterministic, not used by the demo); every judge call runs at temperature 0; `sensitive_data` needs
  presidio (not in the image; fails closed naming it); providers are reached through their OpenAI-compatible endpoints
  (`RAMEN_LLM_API_VERSION` for Azure). Blocked: `isError`, text `guardrail blocked: <stage>: <message>`,
  `_meta.ramen.guardrail = {stage, engine: nemo, rail}`.
### 36.2 Scoped LLM providers and allowed models
- Config → LLM providers (org scope, super admin, §29.1/§30.1) gains **`allowed_models`** per provider (a list of model
  ids; empty = the provider's default only). A group gains **its own providers** (`PUT /api/v1/groups/{g}/llm-providers`,
  group admin, same shape, vaulted under `groups/<g>/llm_providers`, same `allowed_models`): the group scope is used for
  the group's guardrails, SkillSpector scans and promptfoo evals when it names a provider; else the org scope when it
  does; else the run fails with `no LLM provider: set one on the group or ask a super admin` — never silently skipped.
  Group settings `guardrails: {llm: "<provider>", model: "<id>", required: false}` (`model` must be in that provider's
  `allowed_models`; `PUT /api/v1/groups/{g}/guardrails`). The deploy writes the chosen provider's key into the zone's
  deploy Secret under group-scoped names (`RAMEN_LLM_PROVIDER`, `RAMEN_LLM_MODEL`, `RAMEN_LLM_API_KEY`,
  `RAMEN_LLM_BASE_URL`/`_ENDPOINT`/`_DEPLOYMENT` as the provider needs) — only when a rail needs it; `mcp/env.yaml` is
  not used for this. Keys are `remember_secret`-scrubbed everywhere.
### 36.3 Guardrails as a client policy
- Token answer and `/.well-known/ramen-policy` gain `guardrails {version, required, config}` when the repo has
  `mcp/guardrails.yaml` (+ the `mcp/guardrails/` dir): `config` = the yaml + the rails dir packed (≤ 64 KiB, base64
  gzip tar with entries `guardrails.yaml`, `guardrails/<rel>`, deterministic headers; `error` instead when larger),
  `version` = sha12 over the pack + `required` + the group's `guardrails.llm/model`. `client_llm` carries `{provider,
  model, api_key?, base_url?, endpoint?, deployment?}` (Azure OpenAI and Foundry both as provider `azure`) and is
  absent, never a sign-in failure, when no provider resolves. Workers: `RAMEN_GUARDRAILS_VERSION`
  / `_REQUIRED`; gate `-32029 guardrails policy required|changed: sign in again` (HTTP 428, after the configs gate,
  `_meta["io.ramen/guardrails"]`). Bridge 0.6.0: bundle + version on calls, re-sign-in on `-32029`, client files state the
  rails in force, `policy check` fails when removed — **and** the bridge runs the same rails locally
  (`ramen-mcp-bridge[nemo]` extra; `policy rails on|off`, on by default when the bundle carries a config): the input rail
  on every outgoing gated call, the output rail on every tool result, using the LLM credential the token answer hands it
  for that purpose (`guardrails.client_llm {provider, model, api_key}` — the group's or the org's, scrubbed from logs);
  a blocked call/result is answered to the host like the worker does (`isError`, `_meta.ramen.guardrail.stage`,
  `engine: nemo`, `where: client`). Without the nemo extra installed the bridge warns once and relies on the worker
  (documented limit).
- Not bridge-only (user, 2026-11-16, as D73): the guardrails policy is handed out on EVERY sign-in path — group key
  (the key's deploy carries the rails and the worker enforces them on each call), console password / magic link and
  OAuth tokens (the token answer carries `guardrails`) — and the worker gate `-32029` applies to every caller; the
  rails themselves run in the worker for every client, the bridge adds the local run on top.
### 36.4 Demo and verification
Demo repo: `iris_outliers` gains `self_check_output`; a new tool `iris_report {question}` (pandas summary as prose)
gets `self_check_input`, `jailbreak_detection`, `topical (iris, statistics)` and `hallucination`; golden cases for the
static rails only; evals add a `llm-rubric` test when a provider is set. Local proof with a fake OpenAI-compatible
server (deterministic answers) for every rail; clouds (GCP, then AWS and Azure rebuilt for the run) with the real
OpenAI provider at `gpt-4o-mini`-class under the $15 budget: a blocked jailbreak prompt, a passed normal call, the
`-32029` gate through the edge, the bridge's local rail blocking the same prompt, Phoenix spans for the rail calls.

## 37. v1.0.0c — one LLM providers card with live model lists, every region in the zone picker (binding, D82)
- Config → **LLM providers** is one card (the §29.1 and §30.1 cards merged): one row per provider (Anthropic, OpenAI,
  Azure OpenAI, Vertex AI, Bedrock, Azure AI Foundry, OpenAI-compatible) with its `set`/`not set` state, that provider's
  fields inline, an **Allowed models** control and one "Save changes"; the group page's card (§36.2) uses the same macro.
- `GET /api/v1/config/llm-providers/{p}/models` (super admin) and `GET /api/v1/groups/{g}/llm-providers/{p}/models`
  (group admin) answer `{models: [ids], source: live|cached|static|unavailable, error?}` from the provider's own listing
  (OpenAI/compatible `/models`, Anthropic `/v1/models`, Azure OpenAI/Foundry deployments, Gemini when the console has
  a Google API key, Bedrock `ListFoundationModels` when it has AWS credentials; else a static list), 5 s timeout, 10 min
  cache per scope, keys never logged. The control is a multi-select over that list plus a free-text field; the group
  Guardrails `model` is a select over the resolved scope's allowed models; a model outside the list is refused (422)
  and would fail at the worker.
- The zone picker lists **every** non-blocked region of the console's cloud grouped by continent (`regions.json`: GCP
  42, AWS 34, Azure 44 — Azure regions without availability zones as one logical zone `<region>-1`); regions with a
  cluster (home, satellites) are selectable, the others are shown disabled with "no cluster yet — register a satellite
  cluster on the Config page" (D71 unchanged). The Clusters card's region select is grouped the same way.

### 35.1 Capacity findings and D83
Measured (Azure and GCP, 1.0.0b24): burst passes (≥ 336 rps at the edge, only 429s, no 5xx), the autoscaler adds and
removes workers (1→3→1, 1→4→1), but one worker serves ~25 rps of a rail-guarded trivial tool: the worker pod of size
`s` has CPU requests = limits = 250m, the Python runtime serves one request at a time, and a static NeMo rail costs
~11 ms CPU per call. Runtime fix (1.0.0c): hooks run on one long-lived thread and event loop; tools that name no LLM
rail run the repo's own rails only (the §36.1 wrapper flows cost ~2 ms per input flow per call under Colang 1.0) —
rail cost 9.3 → 3.6 ms. D83: worker CPU limits stay at 250m; the autoscaler scales up when the zone's p50 over the
last minute exceeds `RAMEN_AUTOSCALE_P50_MS` (default 500) or rps/worker exceeds the target, and down only when both
are low; the runtime executes up to `RAMEN_RUNTIME_CONCURRENCY` (default 4) tool calls at once on a thread pool
(load, reload and shutdown stay exclusive; tenant tool code must tolerate concurrent calls — documented in the group
repo contract). The §35 steady/mixed p95 bound stays 1.5 s and is re-measured after D83 lands.

**Per-call cost pass (1.0.0, as built; D80 misses on 1.0.0b34–b36: GKE `scale` p95 1872 ms, EKS `steady` 1927 ms
at 27 rps, EKS `scale` 2116 ms).** Worker CPU stays 250m (D83). Root causes, measured on the demo's
`demo_calculator_tool` (static NeMo pre rail) in-process: 4.2 ms CPU per call = NeMo Colang 2.8 + JSON Schema 1.2 +
the rest; the node is ~0.35 ms (`burst` serves 550–660 rps of node-only 429s on one worker).
- **Runtime — JSON Schema.** `jsonschema.validate` checked the input and the output schema against the metaschema on
  every call. Each tool now compiles its validators once per load (`Package.input_validator` / `output_validator`,
  same draft choice and best-match error text): a rail-free tool 0.89 → 0.04 ms.
- **Runtime — NeMo library flows.** NeMo appends its whole Colang 1.0 library to every config and tries to start each
  top-level flow on every event. A top-level flow whose first step runs an action that no kept flow runs, and that no
  flow calls, can never start and is dropped at load (`guardrails_nemo.drop_idle_flows`, to a fixpoint; a flow that
  starts on `bot ...` is never dropped; a library flow whose action an LLM rail wrapper runs stays, as before). −0.65 ms.
- **Runtime — Colang expressions.** Colang's `eval_expression` built a new simpleeval evaluator and re-parsed the text
  ~90 times per rail call. `guardrails_nemo.cached_simple_eval` replaces the `simple_eval` NeMo looks up per call: one
  evaluator per thread, parsed trees cached (≤ 4096); any other call shape, or a nested call, goes to simpleeval
  unchanged. nemoguardrails stays pinned `<0.25`; the swap is skipped if NeMo stops using simpleeval there. −0.7 ms.
- **Runtime — NeMo usage stats.** NeMo 0.24 posts an anonymous usage event to NVIDIA from a thread at every rails
  config (two per load) plus a 10-min heartbeat; the engine sets `NEMO_GUARDRAILS_NO_USAGE_STATS=1` unless the deploy
  set it.
- **Node — load timeout.** A cold `runtime.load` at 250m (`import nemoguardrails` alone ≈ 8 CPU-s, pandas ≈ 3) can pass
  the 120 s call timeout; the node killed it and loaded again from scratch, doubling a new worker's time to SERVING.
  `runtime.load` now waits `max(RAMEN_CALL_TIMEOUT_SECS, 600)` s (`sidecar::LOAD_TIMEOUT_SECS`); other calls keep
  the call timeout.
- **Console — latency trigger.** `RAMEN_AUTOSCALE_P50_MS` defaults to 250 (was 500); the rest of the rule is unchanged
  (down below half the bound, `MIN_RPS` 2). The p50 is a one-minute window and a new worker needs one to two minutes
  or more to serve behind a cloud LB, so at 500 ms the `scale` ramp asked for help when the first worker was already at
  its limit.

Local result (Docker `--cpus 0.25`, arm64 worker 1.0.0 + the demo with pandas baked, 32 clients half Streamable HTTP
h2 / half gRPC, calc, 40 s, Docker HEALTHCHECK off as on Kubernetes): before 29.8 rps, p50/p95/p99 808/2111/7398 ms,
8.5 ms CPU per call; after 69.3 rps, 404/786/1000 ms, 3.7 ms. Expected on the clouds (to be re-measured, §35): one
worker serves about 2.3× the calls per second it did on b34–b36 — `steady` (32 clients, one worker) ≈ 60 rps with p95
≈ 0.8 s on EKS t3.small (was 27 rps / 1927 ms) and lower on GKE and AKS; `mixed` and `burst` unchanged or better;
`scale` asks for the second worker at about half the load and each worker carries twice the calls, so a fluid model
fitted to the b34–b36 misses puts its p95 at 0.8–1.2 s on all three (was 1.1–2.1 s).

## 38. v1.0.0c — closing the final security pass (binding, D85)

### 38.1 LLM judge proxy (F1)
Provider keys never leave the console. `POST /llm/v1/chat/completions` (OpenAI-compatible request/response, streaming
off) on the console: bearer = a per-zone **judge token** (`rjt_…`, minted per deploy like the evals key, in the zone's
deploy Secret as `RAMEN_LLM_API_KEY`, bound to `<group>/<zone>`) or a user's token / MCP key of the group; the console
resolves the group's provider (§36.2 group → org), checks the requested model against `allowed_models` (else 403),
adds the provider credential, forwards (Azure/Anthropic/Vertex/Bedrock mapped as §36.1 does today), enforces a per-group
rate limit (`RAMEN_LLM_PROXY_PER_MIN`, default 120) and a per-request token cap, audits `llm.proxy` per 100 calls, and
answers 502 with the upstream status on failure. Workers get `RAMEN_LLM_PROVIDER=openai_compatible`,
`RAMEN_LLM_BASE_URL=<console>/llm/v1`, the judge token and the model; the token answer's `client_llm` becomes
`{provider: openai_compatible, base_url: <console>/llm/v1, model}` and the bridge uses the user's own token as the
bearer. `RAMEN_LLM_*` names in a repo's `mcp/env.yaml` are refused at sync. Evals/scan Jobs keep provider env (they run
as `ramen-evals` in ramen-system, not tenant code) but promptfoo inline assertions may not read env — the allowlist
refuses `process.env`/`os.environ` in inline assertion code.
### 38.2 Per-group image builds (F2)
Each group's worker images go to `<registry>/ramen/groups/<group>/worker` (AR / ECR / ACR repository per group, created
by the console on first build with the group's labels); the build Job runs as a per-group identity
(`ramen-build-<group>`: GSA / IAM role / managed identity) that may push only to that repository; console, worker base
and SkillSpector images live in `<registry>/ramen/platform/*`, which no build identity can write; the chart references
platform images by digest (`image.digest` beside `image.tag`), and deploys pin the group's worker image by digest.
Terraform creates the platform repository and the permission for the console to create per-group repos/identities;
`requirements.txt` additionally refuses option lines (`-e`, `--index-url`, `--extra-index-url`, `-f`) and VCS/URL
requirements unless the group admin sets `build.allow_urls: true`.
- **As built (§38.2).** GCP: one Artifact Registry repository per group, `ramen-groups-<group>` (AR grants only per
  repository and repository names cannot hold `/`), image `…/ramen-groups-<group>/worker`; the build identity is the
  Kubernetes ServiceAccount `ramen-system/ramen-build-<group>` itself through Workload Identity Federation (no GSA, so the
  console needs no IAM rights on the platform repository). AWS: ECR `ramen/groups/<group>/worker`, role
  `/ramen/build/ramen-build-<group>` under the boundary `ramen-build-boundary`. Azure: managed identity
  `ramen-build-<group>` with `Container Registry Repository Writer` conditioned on the group's repository (registry in
  `AbacRepositoryPermissions` mode). Platform images keep their paths (`…/ramen/{console,worker,skillspector}`) — no build
  identity can write them; the chart takes `image.digest` / `image.workerDigest`. The group name `build` is reserved.
  `requirements.txt` refuses every option except `--require-hashes`, `--only-binary`, `--prefer-binary`, `--pre`, and
  any URL/VCS/path/archive requirement, unless `build.allow_urls: true`. Residual (console compromise, not group
  code): on Azure the console's RBAC administrator role would let a compromised console grant itself writer on the
  platform repository; platform images are pinned by digest, so a re-tag there does not reach a pod until an operator
  changes the digest. On AWS the console's ECR rights are fenced to `ramen/groups/*`.
- **GCP repository rights by resource tag (sec-gaps, closes the GCP residual).** Artifact Registry ignores IAM
  Conditions on `resource.name` but every service evaluates resource tags. Terraform creates the project tag key
  `ramen-repo` with values `platform` and `group`, binds `platform` to the repository `ramen`
  (`google_tags_location_tag_binding`), and binds `ramenConsoleBuildRepos` (get/update/delete/get+setIamPolicy) under
  `resource.matchTagId(<key id>, <group value id>)`. `ramenConsoleBuildRepoCreate` (unconditioned: checked before a
  repository carries a tag) holds `repositories.create`, `repositories.list` and `repositories.createTagBinding`; the
  console holds `roles/resourcemanager.tagUser` on the `group` value only and no `deleteTagBinding` anywhere. A
  resource holds one value per key, so the console can neither re-tag `ramen` nor reach it. Console
  (`gcp_api.Registry`): existence from `list` (get answers 403 for an untagged or missing repository); after create it
  binds `group` through the regional endpoint `https://<location>-cloudresourcemanager.googleapis.com/v3/tagBindings`
  (`parent = //artifactregistry.googleapis.com/projects/<project>/locations/<location>/repositories/<repo>`,
  `tagValueNamespacedName = <project>/ramen-repo/group`, or `tagValue` when `RAMEN_AR_GROUP_TAG` is `tagValues/<id>`;
  `none` = no tagging), 409 = already bound; then retries 403 on get and on the IAM calls (IAM-condition propagation,
  Resource Manager: ≤ 2 min, up to 7; 48 tries × 5 × poll) and fails naming the tag. A repository made before the tag
  (untagged) is tagged on the group's next build or on its deletion. A console from before this change cannot tag:
  terraform and console are upgraded together.
- **Judge proxy as built (§38.1).** Judge token `rjt_<b64url(group/zone/job)>.<HMAC>` valid while its deploy runs or is
  recorded in `last_deploy.judge[zone]` (current + previous); workers call `RAMEN_CONSOLE_INTERNAL_URL/llm/v1` in-cluster;
  the console identity holds `roles/aiplatform.user` (GCP, `console_vertex`) and Bedrock invoke rights (AWS,
  `console_bedrock`); the rate limit is per console replica. Local compose sets `RAMEN_CONSOLE_INTERNAL_URL=
  https://console:8443` (the console leaf names `console`), so the worker's rails reach the proxy there too; CI's compose
  profile `ci` adds `fake-llm`, a deterministic OpenAI-compatible judge over TLS (compose CA), set as the org's
  `openai` provider by the harness (`RAMEN_FAKE_LLM_URL`), so CI deploys the real demo repo with no key.

### 38.3 Throttle Redis (F3)
The chart's Redis (`redis.enabled=true`, generated password, NetworkPolicy) replaces the hand-applied one on GKE; the
worker egress policy selects the chart's labels; a worker that cannot reach its Redis reports it in `Admin/Metrics`
(`redis: "unreachable"`), the group page shows "throttling off: Redis unreachable" on the zone, and the deploy log
warns.
### 38.4 Injection hardening for LLM rails (F5)
Judged arguments and results are wrapped in an escaped, delimited block (`<<<ARGS … ARGS>>>`, with any delimiter or
`Answer:`/`Verdict:`/`Question:` line inside the payload neutralised) and the judge prompt says the block is data, never
instructions; a static pre-rail refuses arguments that contain judge-format lines; the docs and threat model state that
LLM rails reduce but cannot eliminate prompt injection.

## 39. v1.0.0c — TLS on every hop (binding, D86)
The user, 2026-11-17: every hop inside an install is TLS, with certificates from **Ramen's own per-install CA** (no
cert-manager, no service mesh); plaintext is **refused** on cloud installs, not merely off by default; the bridge
defaults to TLS; local compose and the kind harness run TLS with the generated CA too. Supersedes §11/§16.1 ("h2c
inside the cluster"), §24.1's plain-HTTP wake call and §38.1's in-cluster `/llm/v1` exemption.

### 39.1 The install CA
- EC P-256, `CN=Ramen install CA <id>`, `basicConstraints CA:true pathlen:0`, `keyCertSign|cRLSign`, 10 years. The
  console creates it the first time it starts and keeps it in store doc `config/install_ca` (`cert`, `ca_key`,
  `previous`, `created`): `ca_key` is a console secret (`SENSITIVE`), so on a cloud it lives in that cloud's secret
  manager (vaulted, §24.5) and locally Fernet-encrypted in the store. The key never leaves the console process: only
  leaf keys are written to Kubernetes Secrets. Two consoles starting together: the doc is written only when absent,
  the loser re-reads it.
- `RAMEN_TLS_CA_DIR` (`ca.crt` + `ca.key`) overrides the store: local compose and the kind harness generate the CA
  with `python -m ramen_console.tls init <dir>` before anything starts.
- The **bundle** every client trusts is `cert` + `next` + `previous` (PEM concatenation); only `cert` issues.
  **Rotation** (`POST /api/v1/config/tls/rotate`, super admin, audited `config.tls.rotate`) runs in phases so that no
  peer ever sees a leaf whose issuer it does not trust (GKE 1.0.0b33: the one-shot rotate switched Redis/Phoenix at
  once and every worker lost Redis and OTLP until its zone was renewed):
  1. **Bundle** (in the call): a new CA is staged as `next` (`next_key` vaulted like `ca_key`) and the two-CA bundle
     is written wherever it is trusted — the console's own trust at once, ConfigMap `ramen-ca` and the `ca.crt` of
     every `ramen-tls-<svc>` in the release namespace, and every zone's `ramen-tls` `ca.crt` + `ramen-ca` on every
     cluster. No leaf changes, nothing restarts. Answer: `{fingerprint (new CA), previous: true, stage: "bundle",
     switch_after: <secs>, zones, system: []}`. A bundle that cannot be written everywhere (a cluster unreachable)
     unstages the CA again: 409 `…nothing was switched; retry the rotation`. Audited only when accepted.
  2. **Leaves** (a job the call starts, after `RAMEN_TLS_ROTATE_SETTLE_SECS`, default 180 s — kubelet refreshes a
     mounted Secret/ConfigMap within ~1–2 min and every reader re-reads it, see §39.5a): the staged CA becomes `cert`,
     the old one `previous` (its key dropped); the console's own leaf and every zone's leaf are re-issued (nodes
     reload, no restart), **then** the chart services' leaves (their Deployments restarted). Logged as `tls: CA <fp>
     issues now; re-issued: …`. With a settle time of 0 both phases run in the call (`stage: "rotated"`). A job lost
     with its console is finished by any replica's renewal round once the settle time has passed.
  3. **Retire** (`?retire=1`): refused with **409** while a rotation is staged or while any leaf (the console's, a
     chart service's, a zone's on any cluster) is not from `cert` — the message names them. Otherwise `previous` is
     dropped and only the bundles shrink: no leaf is re-issued, nothing restarts. Answer `{fingerprint, previous:
     false, zones, system: []}`.
  A second rotate is refused (409) while one is staged or while `previous` is still trusted (retire first: a bundle
  holds two CAs at most). `GET /api/v1/config/tls` (super admin): `{fingerprint, next, previous, stage:
  "single"|"bundle"|"rotated", switch_after, leaves: [{name, ca: "current"|"next"|"previous"|"unknown"}]}`. No page.

### 39.2 Leaves
All leaves: EC P-256, signed by the install CA, EKU serverAuth + clientAuth, **30 days** (`RAMEN_TLS_LEAF_DAYS`),
renewed when less than a third is left. Names are DNS SANs (plus `127.0.0.1`); no pod IPs.
- **Zone** (every attach/deploy writes it; the console's scheduler renews it): Secret `ramen-tls` (`tls.crt`,
  `tls.key`, `ca.crt` = bundle) and ConfigMap `ramen-ca` (`ca.crt`) in the zone namespace. SANs:
  **`<group>.<zone>.worker.ramen.internal`** (the zone's own name, the one every in-install client verifies, D92
  §39.2a; `worker.ramen.internal` before D92), `worker`, `worker-direct`, `worker-canary` and their `.<ns>`, `.<ns>.svc`,
  `.<ns>.svc.cluster.local` forms, `localhost`, `127.0.0.1`. The
  Secret is mounted (directory, not `subPath`) at `/etc/ramen/tls`; the node re-reads the pair when the files change
  (checked every 60 s), so a renewal needs no restart. Workers get `RAMEN_TLS_CERT`, `RAMEN_TLS_KEY`,
  `RAMEN_TLS_CA=/etc/ramen/tls/ca.crt`, `RAMEN_REQUIRE_TLS=1`; the runtime sees `RAMEN_TLS_CA` only (cert and key
  paths are stripped from its env).
- **Console**: the launcher (`python -m ramen_console.serve`, the image entrypoint) ensures the CA, issues the
  console's own leaf into a private temp dir before uvicorn binds, and renews it in-process (`load_cert_chain` on the
  live context). SANs: `console`, `console.<ns>`, `console.<ns>.svc`, `console.<ns>.svc.cluster.local`, the host of
  `RAMEN_CONSOLE_INTERNAL_URL`, `localhost`, `127.0.0.1`.
- **Chart services** (Redis, Phoenix + its collector, Jaeger, promptfoo): Secret `ramen-tls-<service>` and ConfigMap
  `ramen-ca` in the release namespace, written by the console at start and renewed by its scheduler, which then
  restarts that Deployment (these servers do not reload). The pods mount them (not optional): they start once the
  console has run once. SANs: `<svc>`, `<svc>.<ns>`, `<svc>.<ns>.svc`, `<svc>.<ns>.svc.cluster.local`, `localhost`,
  `127.0.0.1`.

### 39.2a Zone names (D92)
The user, 2026-11-17 (D92, amends D90/D91): a verified worker proves its **zone**, not just the install.
- A zone's leaf carries `<group>.<zone>.worker.ramen.internal` (group and zone are DNS labels, `NAME_RE`) instead of
  `worker.ramen.internal`; the Service names and loopback stay (the node's own health check, `ramen-mcp-bridge
  --target 127.0.0.1:8080 --ca …` in the image's `HEALTHCHECK`, dials loopback — a peer that cannot be another zone).
- Every verifier checks the zone it means: the console's gRPC dial to a pod IP (`grpcclient`: `name` is
  `tls.worker_name(group, zone)` per call, `peer=(group, zone)` required — a zone-derived name without a peer is a
  `ValueError`, never a guess); the activator (`RAMEN_TLS_SERVER_NAME` in the zone's pod env, inherited from the
  `worker` Deployment); Envoy's `BackendTLSPolicy` hostname; the evals Job (`RAMEN_MCP_URL` and `hostAliases` name the
  zone). A leaf of zone A dialled as zone B fails the handshake (`UNAVAILABLE`; health `false`).
- GKE's front ends, the ALB and kubelet probes encrypt without verifying (§39.3): unchanged, they verify no name.
- Local compose runs one worker for every group of zone `local`: its leaf names `*.local.worker.ramen.internal`
  (`tls init --zone worker=local`; `--zone <leaf>=<ns>:<group>.<zone>` names one zone). `tls init` re-issues a leaf
  whose names changed. `RAMEN_WORKER_TLS_NAME` still pins one name for a console that must (a laptop run).

### 39.3 Hops
| Hop | TLS | Verified against |
|---|---|---|
| Client → edge | the edge's public certificate (unchanged) | client's roots |
| Bridge → edge | TLS by default; `--insecure` (h2c) is explicit, local only, and an error next to `--tls`/`--ca` | system roots, or `--ca` |
| Edge → worker | node serves TLS (`ramen-tls`). GKE: Service port `appProtocol: HTTP2`, HealthCheckPolicy `HTTP2` on `/readyz`. AWS: target groups `backend-protocol: HTTPS` (GRPC / HTTP1 versions kept), health checks `HTTPS`. Azure: `BackendTLSPolicy` per zone on Service `worker` (CA ConfigMap `ramen-ca`, hostname `<group>.<zone>.worker.ramen.internal`, D92) | Envoy: the install CA. GFE / ALB encrypt but do not verify backend certificates (the clouds' L7 LBs offer no backend CA on these paths) |
| Kubelet → worker | `httpGet` `scheme: HTTPS` on the node's new `/readyz` (readiness: `""` SERVING) and `/healthz` (liveness) — kubelet's gRPC probe cannot speak TLS | not verified (kubelet) |
| Console → worker (`Admin/*`, smoke, golden, metrics, packages) | gRPC over TLS to the pod IP with server name `<group>.<zone>.worker.ramen.internal` of the zone it means (D92) | install CA |
| Activator → `worker-direct` | TLS, its own zone's name (`RAMEN_TLS_SERVER_NAME`, set by the console, D92) | install CA (`RAMEN_TLS_CA`) |
| Worker / runtime → console (wake, `/llm/v1` judge proxy, `/otlp`) | `RAMEN_CONSOLE_INTERNAL_URL=https://console.<ns>.svc.cluster.local:443`; the exemption of `/api/v1/internal/` and `/llm/v1/` from `HttpsOnlyMiddleware` is removed | install CA |
| Edge → console | console Service port **443** (`https`) → pod 8000 serving TLS. GKE `appProtocol: HTTPS` + HealthCheckPolicy `HTTPS`; ALB `backend-protocol: HTTPS`; Envoy `BackendTLSPolicy` (hostname `console.<ns>.svc.cluster.local`) | as edge → worker |
| Console / workers → chart Redis | `redis-server --port 0 --tls-port 6379 --tls-auth-clients no` (the password stays the client credential), Secret `ramen-redis` `url` = `rediss://…` | install CA (managed Redis: public roots, as before) |
| Console / workers → collector | Phoenix's collector and Jaeger take OTLP over TLS on 4318 (`https://phoenix.<ns>:4318`); the collector posts to Phoenix over TLS (`PHOENIX_TLS_*`); Jaeger's query UI is TLS too | install CA |
| Jobs → promptfoo, Jobs → worker | promptfoo listens on loopback behind a TLS sidecar (`python -m ramen_console.tlsproxy`, the console image) on 3443 (Service port 3000 → 3443); the Jobs mount ConfigMap `ramen-ca` (`NODE_EXTRA_CA_CERTS`; whenever they reach the canary by name or the server URL is `https://`) and reach the canary as `https://<group>.<zone>.worker.ramen.internal:8080/mcp` through `hostAliases` → the canary pod IP (D92); the share upload carries the Jobs' upload credential (§39.7) and verifies the server (`PROMPTFOO_INSECURE_SSL=false`) | install CA |
| Console → satellite workers | as console → worker; the console writes the zone's `ramen-tls` into the satellite cluster | install CA |
| Node → runtime | stdio inside the pod, no network (unchanged) | — |

### 39.4 Plaintext refused
`RAMEN_REQUIRE_TLS=1` is set by the chart for every provider (`tls.require: true`; `false` fails the render: "D86:
plaintext is refused on cloud installs") and written into every zone's pods.
- **Node**: exits at start without `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`; refuses (exit at start, warning + off at a
  reload) an `http://` `RAMEN_CONSOLE_INTERNAL_URL` / `RAMEN_OTEL_ENDPOINT` and a `redis://` Redis URL.
- **Console**: refuses to start with `RAMEN_WORKER_TLS=0`, a `redis://` `RAMEN_REDIS_URL`, an `http://`
  `RAMEN_CONSOLE_INTERNAL_URL` / `RAMEN_EVALS_API_URL`, or no serving certificate; answers 422 to an `http://` OTLP
  endpoint (Config → Telemetry), a `redis://` group/zone Redis URL, and an `http://` LLM provider base URL. Under the
  flag `RAMEN_WORKER_TLS` defaults to 1 with the install CA and each zone's own name (D92, §39.2a).
- Without the flag (unit tests, a laptop run outside compose) everything stays as it was, so the old paths keep
  their tests; local compose and kind set it anyway.

### 39.5 Upgrading an install from before D86 (1.0.0b/c)
1. `helm upgrade` with the new chart: the console comes up on TLS, creates the CA, writes the chart services'
   Secrets; Redis and the collectors restart on TLS. Existing zones keep serving clients through the edge (their
   Services and certs are untouched) but the console can no longer reach them (`Admin/*` now dials TLS), their wake /
   judge / OTLP calls fail (plain HTTP to a TLS port) and their Redis URL is `redis://` → throttling reports
   `redis: "unreachable"`.
2. Redeploy **every zone** of every environment (the group page's Deploy, or `POST
   /api/v1/groups/<g>/environments/<env>/deploy`): the deploy writes `ramen-tls`/`ramen-ca`, the new manifests
   (HTTPS probes, backend TLS) and the https/rediss URLs, and rolls both tracks. A zone not redeployed stays
   unmanageable until it is.
3. Check: `kubectl -n ramen-<g>-<z> get secret ramen-tls`, the deploy log's `tls: issued ramen-<g>-<z> leaf (CA
   <fingerprint>)` (the fingerprint `GET /api/v1/config/tls` reports), a `tools/call` through the edge, and the Logs
   page showing no `tls handshake failed` (a connection that closes before any ClientHello — a load balancer's or
   kubelet's TCP check — is logged at `debug` as `connection closed before a tls ClientHello`, and one that closes or
   resets mid-ClientHello as `tls handshake abandoned by peer`; neither is a failure).

### 39.5a As built
- **Node**: `RAMEN_REQUIRE_TLS` is not a deploy-file key (the deploy Secret cannot switch it off). `http://`
  `RAMEN_CONSOLE_INTERNAL_URL` / `RAMEN_ACTIVATOR_UPSTREAM` exit at start; `http://` OTLP and `redis://`
  item/scope URLs exit at start and, at a reload, are dropped with a warning (feature off). The activator dials
  `worker-direct` over TLS whenever the node has a pair or `RAMEN_TLS_CA`, verifying `RAMEN_TLS_SERVER_NAME`
  (the console writes the zone's own name since D92; the node's default stays `worker.ramen.internal` for a zone
  deployed before it). redis 0.27 *replaces* the web roots with a given root, so the install CA is used
  only for an in-cluster `rediss://` host (an IP, `localhost`, a single label, `*.svc`, `*.svc.cluster.local`, or
  `<svc>.<ns>` with a hyphenated namespace); a managed Redis keeps the public roots. `RAMEN_TLS_CERT`/`KEY` never
  reach the runtime; `RAMEN_TLS_CA` and `RAMEN_REQUIRE_TLS` do (the judge client trusts the CA next to the system
  roots and refuses an `http://` base URL under the flag).
- **Console**: `RAMEN_WORKER_TLS_NAME` pins one verified name for every zone (instead of each zone's own); the CA dir may hold `ca.crt` only (local
  compose gives the console a pre-issued leaf via `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY` and keeps `ca.key` out of its
  reach). The renewal loop (`RAMEN_TLS_RENEW_SECS`) is independent of the scheduler and never creates a zone's leaf:
  only a deploy does, so a pre-D86 zone is untouched until redeployed. A leaf whose bundle alone is stale gets the
  new `ca.crt` patched in (no new leaf); a leaf not from `cert` (or due) is re-issued. The deploy log line is written
  by the adapter right after the zone's manifests are applied.
- **Chart**: Role `ramen-console-tls` (secrets/configmaps/deployments pinned by name; `create` cannot be). Envoy keeps
  `appProtocol: kubernetes.io/h2c` on worker Services (its HTTP/2 switch) beside the `BackendTLSPolicy` (v1) — to be
  proven on AKS. Jaeger: OTLP HTTP + gRPC, query and admin ports TLS; its legacy collector ports stay closed by the
  NetworkPolicy.
- **Local**: compose service `certs` (`tls init`) writes `deploy/local/.certs`; `demo-worker.sh` (a bare node, no
  compose) is the one place that runs plaintext, with the bridge's explicit `--insecure`. Harness: `RAMEN_INSTALL_CA`
  makes `Node`/`HttpNode`/`Console` default to TLS against that bundle (kind: ConfigMap `ramen-ca`).
- **Bridge**: `RAMEN_BRIDGE_INSECURE=1` is the env form of `--insecure`; `RAMEN_BRIDGE_TLS` is ignored. Bridge 1.0.0
  (PyPI) is the bridge for Ramen 1.0.0 and always dials TLS; the console's sign-in card and the docs still print `--tls`
  so every command states it explicitly.
- **Satellites** (§28; found by the §34.3 docs verification): `console.<ns>.svc.cluster.local` exists only in the
  home cluster, so a satellite zone's pods reach the console at its **public URL** (the zone's `RAMEN_OAUTH_ISSUER`):
  the adapter rewrites a satellite's `RAMEN_LLM_BASE_URL` to `<public>/llm/v1` in the deploy Secret (deploy and
  push alike) and gives its `worker-activator` `RAMEN_CONSOLE_INTERNAL_URL=<public>`. Both verify with the public
  roots next to the install CA (node `netc`, runtime judge client), so the console's edge certificate must be
  publicly trusted for them (GCP managed, Azure Let's Encrypt; an AWS self-signed ACM import or Azure `selfsigned`
  fails the handshake and the call is logged). The wake route still answers 404 through an edge
  (`X-Forwarded-For`) except for a zone whose region maps to a satellite (`Services.zone_cluster`); the admin key
  guards it as before. Not rewritten, documented: the chart's collectors (`RAMEN_OTEL_ENDPOINT`, §28.1) and the
  chart's Redis (`RAMEN_REDIS_SCOPE_URL` default, §38.3) are home-cluster Services too — a satellite needs an
  OTLP endpoint and a group scope Redis reachable from every cluster, or its workers log `telemetry export failed`
  and report `redis: "unreachable"`. The satellite chart's `ramen-console-zone` role carries `configmaps`
  (`ramen-ca`) and, on Azure, `ramen-console` carries `backendtlspolicies`, as the home chart's do.
- **CA rotation, every reader** (§39.1, b33): the node re-reads `RAMEN_TLS_CA` for each wake/OTLP request (as
  before); the activator re-reads it for each new upstream connection and rebuilds its gRPC channel when it changed
  (a broken file keeps the last good bundle); the throttle's Redis re-check dials again with the file re-read when a
  PING fails (a `ConnectionManager` keeps the roots it was built with), and the task/consent stores hold the same
  connection slot, so they follow. The runtime's judge client re-reads the file before each request and, when it
  changed, builds a fresh context and a fresh connection pool on it (`CaFile` + `CaTransport`), so a retire takes
  effect without a restart too (adding CAs to a context already in use did not apply on OpenSSL 3.5, Linux CI). The console reads its CA doc at least once a minute (its own leaf loop), so a rotation
  finished by another replica reaches its trust and its own leaf within the settle time. Chart services: their
  `ca.crt` only verifies peers inside their own pod (Phoenix's collector), which restart together with the leaf.
- **Handshake log** (b33, b35): a connection that closes or resets before sending a byte (`peek`) is `debug` `connection
  closed before a tls ClientHello`; one that started a ClientHello and then hit EOF, a reset, an abort or a broken pipe
  is `debug` `tls handshake abandoned by peer` (GKE b35: GFE health checkers 35.191.x and 169.254.4.6, 1–4 per new
  pod); every other handshake error stays `warn` `tls handshake failed` — rustls's own (an alert the peer sent, e.g. it
  does not trust our leaf; one we sent, e.g. no common version; a record that is not TLS) and a timeout (`tls
  handshake timed out`, 10 s). Decided by the error kind: a peer that gives up silently says nothing about our TLS; an
  alert does (a peer on the wrong CA mid-rotation sends one), so it stays visible. A client that distrusts us and
  closes without an alert is logged at `debug` — accepted.
- **Redis on the call path** (b35): every operation a call waits for — the throttle's `INCR`, the task store's and the
  consent store's commands — is bounded by `RAMEN_REDIS_TIMEOUT_MS` (default 500, 50–10000, start-up only), and so is
  each response and (at least 1 s) each connection attempt of the `ConnectionManager` (two retries, not six with
  back-off). Past the bound, or on a connection error, the link is reported down (`warn` `redis unreachable: throttling
  is off for this pod until it answers`, `Admin/Metrics.redis: "unreachable"`, as the 30 s re-check reports it) and the
  re-check runs at once in the background (PING, else a re-dial with the CA bundle re-read); `info` `redis reachable…`
  when it is back. The throttle fails open for that call and skips the link until then, so a call waits one bound per
  Redis at most. The task store answers the bound as an error (`tasks/get` etc. fail fast; tasks are not run from
  memory instead); the consent gate treats an unreadable store as "not on record" (the caller is asked again, a
  2025-06-18 caller refused) and a failed write as a warning (§24.3). GKE b35: one `tools/call` per CA switch hung on
  the replaced chart Redis until the LB's 30 s backend timeout (504; the worker never logged it).
- **Telemetry across a restart** (b35): a CA switch restarts Phoenix and its collector (~80 s). Phoenix's SQLite is on
  a PVC (`otel.phoenix.persistence: {enabled: true, size: 5Gi, storageClass: ""}`, RWO, Deployment maxSurge 0;
  `enabled: false` = the old emptyDir), so its history survives restarts and upgrades. On EKS a PVC needs the EBS CSI
  driver (as promptfoo's does). Jaeger stays in-memory (`--memory.max-traces`): its history is lost at a restart —
  documented, not changed. The node keeps what the collector did not take (§24.3: ≤ 8192 spans / 120 s) and logs `info`
  `telemetry export recovered {sent, lost}` once it answers; the console retries its deploy span; the console's OTLP
  ingest (§31.4) answers 502 while the collector is away, which OTLP clients retry.
- **Shown and logged values** (§34.3 N1): `redact.py` masks URL userinfo (`scheme://***@host`) and credential-named
  query parameters wherever the console shows or logs a value — except a bare user on `ssh://` (`ssh://git@host/…`;
  scp-like `git@host:…` has no scheme and is never touched), which names an account, not a credential; a group's
  `repo_url` is masked in the group API (list, get, create, update), the Groups and group pages, and the deploy log
  (a masked value posted back keeps the stored URL): `GET /api/v1/config` and the Config page's
  RAMEN_* card (beside the name rule), the telemetry endpoints and LLM base URLs (a masked value posted back keeps
  the stored one), worker logs (API and Logs page), job records' `error` fields, deploy logs and every log line. At
  start the console remembers the URL passwords of its RAMEN_* env (the chart Redis') for the log scrubber.

### 39.5b Upgrading to zone names (D92)
1. The first D92 console start writes `config/tls_zone_names` `{since}` (once per install; every replica reads the
   same) and re-issues **every zone leaf that lacks its zone name** at once (`tls.startup` → `renew_zone_tls`; the
   renewal loop treats such a leaf as due too). A leaf that carried the generic name keeps it as a second SAN — a
   *bridge leaf*: that zone's activator env and Envoy policy (written by its last deploy) still verify the generic
   name until the zone is redeployed. A leaf re-issued later for the same zone (due, CA rotation) stays a bridge leaf
   until then; a deploy writes a zone-only leaf, the activator's zone name and the zone-name `BackendTLSPolicy` in the
   same apply — the old pods already hold the bridge leaf, so the new verifiers accept them during the roll.
2. Until `since + RAMEN_TLS_ZONE_NAME_GRACE_SECS` (900 s: kubelet's Secret refresh ~1–2 min plus the node's 60 s
   re-read), the console's dial that fails with `UNAVAILABLE` under the zone name gets **one** second try with
   `worker.ramen.internal` (a pod still serving its pre-D92 leaf). After the window the generic name is never
   accepted by the console. A fresh install opens the window too; it has no pre-D92 leaf to accept.
3. `GET /api/v1/config/tls` marks a leaf that still carries the generic name `generic_name: true`: redeploy those
   zones (their activator and Envoy accept the generic name, i.e. another not-yet-redeployed zone's leaf, until then).

### 39.6 Verification
Console: CA create/load/vault/race, rotate + bundle, leaf SANs and lifetime, renewal, the refusals (start and 422),
zone manifests per cloud carrying the TLS backends and probes, the Jobs' hostAliases and CA, the launcher and the
TLS proxy. Node: refuses to start under the flag, serves TLS, reloads a renewed pair, `/readyz`/`/healthz`, trusts
`RAMEN_TLS_CA` for wake/OTLP/activator/Redis. Bridge: TLS default, `--insecure` path, the conflict error. Chart:
`helm template` shows every service on TLS and fails with `tls.require=false`. Local compose and kind come up TLS.

### 39.7 Authentication in front of the optional engines (D92)
The user, 2026-11-17 (D92): Phoenix, Jaeger and promptfoo hold every group's traces and evals; reaching their pods
(a policy that admits too much, a port-forward) must not be enough to read them. Machine ingest keeps working.
- **Secret `ramen-engines`** (release namespace), generated by the chart whenever an engine runs and kept across
  upgrades (`lookup`; `engines.existingSecret` names your own with the same keys): `username` (`admin`) + `password`
  — the operator; `upload-username` (`evals`) + `upload-password` — the evals Jobs; `phoenix-secret`,
  `phoenix-admin-secret` (≥ 32 characters with a digit and a lowercase letter, distinct). Never printed by the chart,
  the console or a job; a super admin with cluster access reads one with `kubectl -n ramen-system get secret
  ramen-engines -o jsonpath='{.data.password}' | base64 -d`. Local compose: `RAMEN_ENGINES_PASSWORD`,
  `RAMEN_ENGINES_UPLOAD_PASSWORD`, `RAMEN_PHOENIX_SECRET`, `RAMEN_PHOENIX_ADMIN_SECRET` in `deploy/local/.env`
  (`make env`); a check service stops the engine profiles when they are missing.
- **Phoenix** (21.1.0's own auth): `PHOENIX_ENABLE_AUTH=true`, `PHOENIX_SECRET`, `PHOENIX_ADMIN_SECRET`,
  `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` = `password` (applied while the `admin@localhost` account still holds
  Phoenix's default), `PHOENIX_USE_SECURE_COOKIES=true`. UI, GraphQL, REST and OTLP (HTTP and gRPC 4317) need a
  sign-in or `Authorization: Bearer <phoenix-admin-secret>`; `/healthz` stays open (the readiness probe). The
  collector sidecar posts spans to Phoenix with that bearer; its own OTLP receiver on 4318 takes no credential.
- **Jaeger** (no auth of its own): the query UI/API binds `127.0.0.1:16687` (gRPC query `127.0.0.1:16685`); the pod's
  TLS front (`python -m ramen_console.tlsproxy`, the console image) on 16686 (Service port `query`) wants the operator's
  credential. OTLP stays on 4318 (TLS, no credential); the admin port 14269 (health) is the readiness probe.
- **promptfoo**: its TLS front on 3443 wants a credential on every connection: the operator's (everything) or the
  upload credential, which may send exactly one request per connection matching `POST ^/api/eval$` or `POST
  ^/api/eval/[A-Za-z0-9:_-]+/results$` (promptfoo 0.125.1's share upload; Content-Length bodies only, forwarded with
  `Connection: close`; anything else 403, a chunked body 411) — never a read. The evals Job gets `upload-password` by
  `secretKeyRef` (`RAMEN_ENGINES_SECRET`, set by the chart) and `PROMPTFOO_REMOTE_API_BASE_URL=https://evals:$(…)@
  promptfoo.<ns>:3000` (promptfoo turns URL userinfo into Basic auth and strips it from the printed link), plus
  `PROMPTFOO_INSECURE_SSL=false` (promptfoo skips verification by default). The console never calls promptfoo.
- **The TLS front** (`tlsproxy`): with `RAMEN_PROXY_PASSWORD` set, the first request of each connection must carry
  `Authorization: Basic admin:<password>` or `Bearer <password>` (constant-time compares), then the connection is
  piped as before (a TLS connection is one client); no path is open without a credential (401 with
  `WWW-Authenticate: Basic`); `--require-auth` exits when the password is missing (chart and compose). Probes are TCP.
- **NetworkPolicies** (unchanged sources, narrowed ports): the workers reach only the collector's OTLP 4318 (they
  export directly; `RAMEN_OTEL_ENDPOINT`), never 6006/4317/16686/3443; the edge rule of the collector policy admits the
  UI port only (it admitted every port). Port-forwards bypass policies by design — hence the credentials.
- Verification: unit (`test_d92_engines.py`: the proxy's 401/403/411, upload one-shot, chart rendering, policies);
  GKE: each UI asks for a sign-in, a share upload lands, an upload credential cannot read.
