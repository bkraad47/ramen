# Ramen interface contracts (v0.1.0)
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

## 3. Node HTTP surface (node-rs)
Port `RAMEN_NODE_PORT` (default 8080).
- `POST /mcp` MCP Streamable HTTP, JSON-RPC 2.0. Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`. JSON responses only (no SSE) in v0.1.0. Protocol version `2025-06-18`.
- `GET /healthz` 200 ; `GET /readyz` 200 once `runtime.load` succeeded.
- `GET /metrics` JSON `{inflight, total, errors, load: "low|even|high", sidecar_alive, loaded_at, packages:{tools,resources,prompts,errors}}`. load = low <30% of `RAMEN_MAX_INFLIGHT`, high >80%.
- `POST /admin/reload` header `X-Ramen-Admin-Key` → re-run pip install for `requirements.txt` + `runtime.load`. Returns the load result.
Auth: `Authorization: Bearer <key>`; keys in `RAMEN_MCP_KEYS` (comma list) reloaded on `/admin/reload`; unauth → 401 JSON-RPC error. `RAMEN_ALLOWED_CIDRS` (comma list, default 0.0.0.0/0) → 403 outside. `RAMEN_ADMIN_KEY` for admin endpoints. `RAMEN_VERBOSE=1` logs full request/response bodies; otherwise one JSON line per call: `ts, ip, group, method, name, status, ms, key_id`. Group name from `RAMEN_GROUP`, zone from `RAMEN_ZONE`, environment from `RAMEN_ENV`.
Config precedence: `RAMEN_CONFIG` file (flat `KEY=value` / `key: value`) < env < `<bucket>/.ramen/env-<zone>` (or `.ramen/env`) written by the console on deploy. The deploy file may only set deploy-scoped keys (MCP keys are unioned, group/env/zone labels, verbose, timeouts); it can never change bucket, port, python path, admin key, or proxy trust. Extra env: `RAMEN_LOG_FILE` (log mirror the console Logs page tails), `RAMEN_CALL_TIMEOUT_SECS`. No `RAMEN_MCP_KEYS` and no deploy file = deny all.

## 4. Console (console/)
Port `RAMEN_CONSOLE_PORT` (default 8000; TLS terminated by ingress/LB; local compose serves https on 8443 with a self-signed cert).
Storage interface `ramen_console.storage.base.Store` (async): `get/put/delete/list(collection, filters)` + `transaction`. Collections: `users, groups, environments, zones, workers, secrets, api_keys, audit, activity, config, backups`. Adapters: `memory` (tests), `firestore` (honors `FIRESTORE_EMULATOR_HOST`), `dynamodb` (tests use moto). Selected by `RAMEN_STORE=memory|firestore|dynamodb`. Fields marked sensitive (password hashes, secret values, API key hashes, github tokens) are Fernet-encrypted with `RAMEN_FERNET_KEY` before write.
Cloud interface `ramen_console.cloud.base.Cloud`: `sync_repo(group, repo_url, ref, token) -> bucket_uri`, `deploy(group, env, zone, canary=True)`, `rebalance(group, zone)`, `workers(group, zone) -> [{id, load, metrics}]`, `logs(group, zone, worker=None, tail=500)`, `set_ip_rules(group, zone, cidrs)`, `create_service_account(group, zone)`, `refresh()`. Adapters: `local` (filesystem bucket at `RAMEN_BUCKET_ROOT/<group>`, deploy = write env file + POST worker `/admin/reload`, logs = worker container stdout file), `gcp`, `aws` (Phase 2/3 stubs raising NotImplemented with a clear message). Selected by `RAMEN_CLOUD=local|gcp|aws`.
Roles: `super_admin`, `group_admin` (per group), `viewer` (per group). Bootstrap super admin from `RAMEN_ADMIN_EMAIL`/`RAMEN_ADMIN_PASSWORD` on first start. Sessions: signed cookie. Passwords: argon2. API keys: `rmn_<id>_<secret>`, stored hashed, scoped to role+groups, header `X-Ramen-Api-Key` on `/api/v1/*`.
Pages (left sidebar): Dashboard (zone→group load map, blue/green/red), Groups, Environments, Zones/Workers, Secrets, Users, API Keys, Logs, Audit, Backups, Config. Theme: coral `#F26B3A` on `#F4F1EC`, logo at `/static/logo.png`. HTMX for partial refresh; no JS build step.
Every mutating request writes `audit` `{ts, user, ip, action, target, ok, tags}`.
Probes: `GET /healthz` 200 always; `GET /readyz` 200 once the store answers and a super admin exists, else 503.
Additive details settled in v0.1.0: `Cloud.deploy(..., config: dict)` carries the RAMEN_* / RAMEN_SECRET_* vars; local adapter writes `<bucket>/.ramen/env` and `.ramen/env-<zone>`; local workers are addressed via `RAMEN_LOCAL_WORKERS="group/zone=url,..."` with `RAMEN_WORKER_URL` fallback; permission requests live in `activity`.

### 4a. Console routes (binding; mirrored by tests/src/ramen_tests/console.py)
| key | route | notes |
|---|---|---|
| login/logout | `POST /login` form {email,password} → 303 + `ramen_session` cookie; `/logout` | 401 on bad password |
| me | `GET /api/v1/me` | |
| users | `/api/v1/users`, `/api/v1/users/{id}` | POST {email,password,role,groups} → 201 |
| groups | `/api/v1/groups`, `/api/v1/groups/{group}` | POST {name,repo_url,ref} → 201 |
| zones | `/api/v1/zones`, `/api/v1/zones/{zone}` | POST {name,provider,region}, super admin |
| environments | `/api/v1/groups/{group}/environments[/{env}]`, `/api/v1/environments?group=` | POST {name,ref,zones:[...]} |
| deploy | `POST /api/v1/groups/{group}/environments/{env}/deploy` {canary,zone?} → 202 {id,status} | poll `GET /api/v1/jobs/{id}` |
| workers | `GET /api/v1/groups/{group}/zones/{zone}/workers` → {live:[{load}],count,size} | scale count via PUT (size super admin only) |
| rebalance / ip-rules | `POST .../zones/{zone}/rebalance`, `PUT .../zones/{zone}/ip-rules` | |
| secrets | `/api/v1/groups/{group}/secrets[/{id}]` | POST {name,value,env?,zone?} → 201 {id,name}; value never returned |
| mcp-keys | `/api/v1/groups/{group}/mcp-keys[/{id}]` | POST {name} → 201 {id,key:"rmk_..."}; written to workers on deploy |
| api-keys | `/api/v1/api-keys[/{id}]` | POST {name,role?,groups?} → 201 {id,key:"rmn_..."} |
| audit / logs / dashboard | `GET /api/v1/audit`, `/api/v1/logs`, `/api/v1/dashboard` | |
| backups | `POST /api/v1/backups` {target} → 201 {id,release_version}; `GET /api/v1/backups/{id}/download` | |
| config / refresh | `/api/v1/config` (GET, POST reload), `POST /api/v1/refresh` | super admin |

## 5. Local stack (deploy/local/docker-compose.yml)
Services: `firestore` (emulator, 8081), `console` (8443), `worker` (node-rs + runtime-py in one image, 8080), shared volume `buckets`. `make demo` = up, wait ready, create group `demo` pointing at the demo repo, deploy, call `demo_calculator_tool` via an MCP client, print result.

## 6. Versioning
`ramen/VERSION` is the single source; Cargo.toml and both pyproject versions must equal it (CI checks).

## 7. GCP deployment (v0.2.0, binding)
Decisions: D6 one zone first but multi-zone capable, D16 throwaway project, D17 static IP + self-signed cert.
- **Terraform (`deploy/terraform/gcp`)** creates: regional GKE Autopilot cluster `ramen` (var `region`, default `us-central1`), Firestore Native DB `(default)`, Artifact Registry docker repo `ramen`, global static IP `ramen-console`, master GSA `ramen-console@<project>` with roles `storage.admin`, `secretmanager.admin`, `container.developer`, `logging.viewer`, `iam.serviceAccountAdmin`, `iam.serviceAccountUser`, `compute.securityAdmin` (Cloud Armor), `compute.loadBalancerAdmin`, and a Workload Identity binding to KSA `ramen-system/console`. Outputs: `cluster_name`, `region`, `console_ip`, `artifact_repo`, `console_gsa`. Nothing per-group is created by Terraform.
- **Images**: `<region>-docker.pkg.dev/<project>/ramen/console:<VERSION>` and `.../ramen/worker:<VERSION>`, linux/amd64, pushed by `make push PROJECT=… REGION=…`.
- **Helm (`deploy/helm/ramen`)**: namespace `ramen-system` holds the console Deployment (KSA `console`, WI-annotated), Service (NEG), GKE Ingress class `gce` with the static IP and a self-signed TLS Secret, BackendConfig health check `/healthz`. Values: `project`, `region`, `image.*`, `console.env`.
- **Zone = k8s namespace** `ramen-<group>-<zone>` created by the console when a zone is attached to a group. It holds Deployments `worker` and `worker-canary` (same image, `nodeSelector: topology.kubernetes.io/zone=<gcp-zone>` from the zone record's `region` field), Service `worker` (port 8080, NEG annotation, selects both), KSA `worker` bound to the group+zone GSA, Secret `ramen-deploy` (deploy env: RAMEN_* + RAMEN_SECRET_*). Worker pods get `RAMEN_BUCKET_URI=gs://<bucket>/<group>` and `RAMEN_BUCKET=/data/bucket`.
- **Bucket sync**: on `runtime.load` (and thus on `/admin/reload`) the runtime syncs `RAMEN_BUCKET_URI` → `RAMEN_BUCKET` (google-cloud-storage, ADC/Workload Identity) before loading when the URI is set. Sync is content-hash based and deletes stale files.
- **Console GCP adapter (`ramen_console.cloud.gcp`)**: in-cluster kube config (fallback `KUBECONFIG`). `sync_repo` → git clone/fetch to temp then upload to `gs://ramen-<project>-groups/<group>/` (bucket created by Terraform var `groups_bucket`, one bucket, per-group prefix; IAM per group via prefix conditions). `deploy(canary=True)`: write Secret `ramen-deploy` (secret values fetched from Secret Manager), `kubectl rollout restart worker-canary` → wait ready → POST `/admin/reload` on the canary pod IP → `tools/list` smoke → then restart `worker` → wait ready; on any failure scale canary to 0 and return the error; job log lines are streamed to the console job record. `workers()` = pods of `worker`+`worker-canary` + each pod's `/metrics`. `logs()` = Cloud Logging `resource.type="k8s_container" AND resource.labels.namespace_name="ramen-<group>-<zone>"` (+ `pod_name` for one worker), newest N, downloadable. `rebalance(group, zone)` = set the backend service capacity scaler for that zone's NEG backend (0.5 when `high`, 1.0 otherwise) via compute API, then trigger an HPA-friendly `scale` if count differs. `set_ip_rules` = Cloud Armor policy `ramen-<group>` (allow listed CIDRs, deny rest) attached to the worker backend service; also patched into `RAMEN_ALLOWED_CIDRS` of the deploy Secret. `create_service_account(group, zone)` = GSA `ramen-<group>-<zone>@<project>` with `storage.objectViewer` (conditioned to the group prefix) + `secretmanager.secretAccessor` (conditioned to `ramen-<group>-*` secrets) + WI binding to KSA `ramen-<group>-<zone>/worker`; extra roles only via super-admin rules (F4.2). `refresh()` = list namespaces/deployments/GSAs and reconcile the store.
- **Secrets backend**: `RAMEN_SECRETS_BACKEND=store|gcp`. `gcp` stores values in Secret Manager as `ramen-<group>-<env>-<zone>-<NAME>` (labels group/env/zone) and the store keeps only the name + resource ref. Viewers/admins never read values back through the console.
- **Console runtime env on GCP**: `RAMEN_STORE=firestore`, `RAMEN_CLOUD=gcp`, `RAMEN_SECRETS_BACKEND=gcp`, `RAMEN_GCP_PROJECT`, `RAMEN_GCP_REGION`, `RAMEN_GROUPS_BUCKET`, `RAMEN_IMAGE_WORKER`.
- **Sizes**: worker size presets `s` (250m/512Mi), `m` (500m/1Gi), `l` (1/2Gi); super admin sets allowed sizes per group+zone, admins set count (HPA min/max).
