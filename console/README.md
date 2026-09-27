# Ramen console

FastAPI + Jinja2 + HTMX manager UI for Ramen (multizone HA MCP server). One console node manages every group,
environment, zone and worker. Server-rendered, no JS build step (HTMX is vendored under `static/`).

## Run locally

```sh
uv sync --all-extras
export RAMEN_ADMIN_EMAIL=root@example.com RAMEN_ADMIN_PASSWORD=change-me
export RAMEN_FERNET_KEY=$(uv run python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
uv run uvicorn ramen_console.app:app --port 8000 --reload
```

Open http://localhost:8000, sign in with the bootstrap super admin. API docs: `/api/docs`. Probes: `/healthz`, `/readyz`.

## Docker

```sh
docker build -t ramen-console --build-arg RAMEN_VERSION=$(cat ../VERSION) .
docker run -p 8443:8443 -e RAMEN_TLS=self -e RAMEN_ADMIN_EMAIL=... -e RAMEN_ADMIN_PASSWORD=... -e RAMEN_FERNET_KEY=... ramen-console
```

`RAMEN_TLS=self` generates a self-signed certificate at start and serves HTTPS on 8443; otherwise plain HTTP on
`RAMEN_CONSOLE_PORT` (8000) for TLS termination at the ingress/LB. Runs as non-root uid 10001.

## Test

```sh
uv run pytest --cov --cov-fail-under=90
```

Unit tests need no Docker: storage uses the memory adapter, moto for DynamoDB and a fake client for Firestore.
`FIRESTORE_EMULATOR_HOST=localhost:8081 uv run pytest -m integration` exercises the real Firestore client.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RAMEN_ADMIN_EMAIL` / `RAMEN_ADMIN_PASSWORD` | – | Bootstrap super admin (re-applied on every start, so cloud admins can reset the master password) |
| `RAMEN_FERNET_KEY` | – | Fernet key; encrypts password hashes, secret values, API-key hashes and git tokens at rest. Required in production |
| `RAMEN_SESSION_SECRET` | falls back to Fernet key | Signs the `ramen_session` cookie (12 h) |
| `RAMEN_COOKIE_SECURE` | `0` | `1` sets the `Secure` cookie flag (use behind HTTPS) |
| `RAMEN_STORE` | `memory` | `memory` / `firestore` / `dynamodb` |
| `FIRESTORE_EMULATOR_HOST`, `RAMEN_GCP_PROJECT`, `RAMEN_FIRESTORE_PREFIX` | – / – / `ramen_` | Firestore adapter (emulator honored automatically) |
| `RAMEN_DDB_TABLE`, `RAMEN_DDB_ENDPOINT` + AWS creds | `ramen` / – | DynamoDB adapter (single table, pk=collection, sk=id; created if missing) |
| `RAMEN_CLOUD` | `local` | `local` / `gcp` / `aws` (aws is a stub until v0.3.0) |
| `RAMEN_SECRETS_BACKEND` | `store` | `store` keeps secret values Fernet-encrypted in the store; `gcp` writes them to Secret Manager as `ramen-<group>-<env|all>-<zone|all>-<NAME>` (labels group/env/zone) and the store keeps only name + `sm://` ref. Deploy resolves refs into the worker Secret |
| `RAMEN_GCP_PROJECT` | – | GCP project (Firestore, Secret Manager, Logging, Compute, IAM). Required for `RAMEN_CLOUD=gcp` / `RAMEN_SECRETS_BACKEND=gcp` |
| `RAMEN_GCP_REGION` | `us-central1` | GKE cluster region |
| `RAMEN_GROUPS_BUCKET` | `ramen-<project>-groups` | One GCS bucket, one prefix per group (`gs://<bucket>/<group>/`) |
| `RAMEN_IMAGE_WORKER` | `ramen-worker:0.2.0` | Worker image used in the per-zone Deployments |
| `RAMEN_DEPLOY_TIMEOUT_SECS` | `300` | Max wait for a Deployment rollout during deploy |
| `RAMEN_GCP_POD_PROXY` | `0` | `1` reads worker `/metrics` through the API-server pod proxy (for running the console outside the cluster) |
| `RAMEN_WORKER_CHART` | – | Path to `deploy/helm/ramen-worker`; when set and `helm` is on PATH the zone manifests are rendered with `helm template`, otherwise the built-in Python manifest set is used |
| `KUBECONFIG` | – | Used when in-cluster config is unavailable (console running outside GKE) |
| `RAMEN_BUCKET_ROOT` | `./buckets` | Local adapter: filesystem "bucket" root, one dir per group (`<root>/<group>`) |
| `RAMEN_LOG_ROOT` | `<bucket root>/_logs` | Local adapter: worker logs at `<root>/<group>/<zone>/worker.log` |
| `RAMEN_LOCAL_WORKERS` | – | `group/zone=http://w1:8080|http://w2:8080,other/zone=…` worker URLs per group+zone |
| `RAMEN_WORKER_URL` | `http://worker:8080` | Fallback worker URL when a group/zone has no entry |
| `RAMEN_ADMIN_KEY` | – | Sent as `X-Ramen-Admin-Key` to worker `POST /admin/reload` |
| `RAMEN_BACKUP_ROOT` | `./backups` | Where `target=local` backups are written (`target=bucket` → `<bucket root>/_backups`) |
| `RAMEN_CONFIG` | – | YAML file; top-level keys map to `RAMEN_*` (nested keys join with `_`). Env vars win. Hot reload: `POST /api/v1/config/reload` |
| `RAMEN_OAUTH_<NAME>_CLIENT_ID` / `_CLIENT_SECRET` / `_METADATA_URL` / `_SCOPES` | – | OIDC provider `<name>`; enabled only when id, secret and metadata URL are all set. SSO users are created as viewers with no groups |
| `RAMEN_CONSOLE_PORT` | `8000` | HTTP port |
| `RAMEN_TLS` / `RAMEN_TLS_PORT` / `RAMEN_TLS_HOST` | – / `8443` / `localhost` | `RAMEN_TLS=self` serves HTTPS with a generated cert |
| `RAMEN_LOG_LEVEL` | `INFO` | Console log level |

## Deploy flow (local adapter)

`POST /api/v1/groups/{g}/environments/{env}/deploy` returns `202 {id}`; poll `/api/v1/jobs/{id}` (the UI polls
`/ui/jobs/{id}` every 2 s and shows "refreshing…"). The job clones/pulls the group repo into the bucket (token from a
secret named `GITHUB_TOKEN`, or the group's fallback token), writes `<bucket>/.ramen/env-<zone>` with `RAMEN_*`,
`RAMEN_MCP_KEYS` (minted MCP keys) and `RAMEN_SECRET_<GROUP>__<NAME>` for secrets scoped to that env/zone, then
POSTs `/admin/reload` to every worker of the zone. Errors are surfaced in the job, the group page and the audit log.

## Deploy flow (gcp adapter, CONTRACTS §7)

Zone = namespace `ramen-<group>-<zone>` (created when a zone is attached to an environment, or on first deploy):
Deployments `worker` and `worker-canary` pinned to the zone's `region` (GCP zone), Service `worker` (NEG
`ramen-<group>-<zone>`), KSA `worker` (Workload Identity → `ramen-<group>-<zone>@<project>`), Secret `ramen-deploy`,
HPA `worker` (min = count, max = 2×count). `sync_repo` clones the group repo and uploads it to
`gs://<bucket>/<group>/` (md5 diff, stale objects deleted). Deploy: write `ramen-deploy` (values resolved from the
secrets backend) → scale canary to 1 and restart it → wait ready → `POST /admin/reload` → `tools/list` smoke with the
first MCP key (`/readyz` when no keys exist) → restart `worker` → wait ready. Any failure scales the canary to 0,
leaves `worker` untouched and is reported in the job with the streamed log. `workers()` = pods + `/metrics`;
`logs()` = Cloud Logging (`k8s_container`, namespace filter, `pod_name` for one worker); `rebalance` = capacity
scaler 0.5/1.0 on the `ramen-<group>` backend service NEG for that zone (no-op with a note when the backend service
does not exist) + replicas ← HPA min; `ip-rules` = Cloud Armor policy `ramen-<group>` (allow CIDRs, deny rest; empty
list = allow all) attached to the backend service + `RAMEN_ALLOWED_CIDRS` in the deploy Secret; `service-account` =
GSA with `storage.objectViewer` (bucket prefix condition) + `secretmanager.secretAccessor` (`ramen-<group>-*`
condition) + Workload Identity binding, idempotent. Sizes `s` 250m/512Mi, `m` 500m/1Gi, `l` 1/2Gi: super admins pick
the size and the per-zone allowed list; group admins may switch within the allowed list and set the count.

## Roles

`super_admin` > `group_admin` (per group) > `viewer` (per group). API keys `rmn_<id>_<secret>` (header
`X-Ramen-Api-Key`) carry a role and group list never wider than their creator's. MCP worker keys are `rmk_…` and are
stored as secrets (never shown again). Every mutation writes an `audit` record `{ts,user,ip,action,target,ok,tags}`.
