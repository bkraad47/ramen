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

## Talking to workers (gRPC, CONTRACTS §11)

Since v0.3.1 workers serve gRPC (h2c) on port 8080: `ramen.v1.Mcp/Call` (one JSON-RPC 2.0 message per call),
`ramen.v1.Admin/{Reload,Metrics}` and `grpc.health.v1.Health`. The console never speaks HTTP to a node any more:
`ramen_console.grpcclient` (`call`, `reload`, `metrics`, `health`; one channel per call, 10 s deadline, 4 MiB messages)
is used by every cloud adapter for deploys (`Admin/Reload` → `Mcp/Call tools/list` smoke, or `Health/Check` when the
group has no MCP keys), the workers page (`Admin/Metrics`) and readiness. Pods are addressed directly by IP:8080; the
API-server pod-proxy mode (`RAMEN_GCP_POD_PROXY` / `RAMEN_AWS_POD_PROXY`) is gone. Node TLS (`RAMEN_TLS_CERT` on the
node) needs `RAMEN_WORKER_TLS=1` here. The vendored stubs live in `src/ramen_console/proto/ramen_proto/`; regenerate
after a proto change (`make proto` at the repo root, or):

```sh
TMP=$(mktemp -d) && mkdir -p $TMP/ramen_console/proto/ramen_proto/ramen/v1 && cp ../proto/ramen/v1/*.proto $TMP/ramen_console/proto/ramen_proto/ramen/v1/
uv run python -m grpc_tools.protoc -I $TMP --python_out=src --grpc_python_out=src --pyi_out=src $TMP/ramen_console/proto/ramen_proto/ramen/v1/*.proto
```

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
| `RAMEN_CLOUD` | `local` | `local` / `gcp` / `aws` (`aws` implemented in v0.3.0 but untested on a real account) |
| `RAMEN_SECRETS_BACKEND` | `store` | `store` keeps secret values Fernet-encrypted in the store; `gcp` writes them to Secret Manager as `ramen-<group>-<env|all>-<zone|all>-<NAME>` (labels group/env/zone) and the store keeps only name + `sm://` ref; `aws` writes them to Secrets Manager as `ramen/<group>/<env|all>/<zone|all>/<NAME>` (tags) with an `asm://` ref. Deploy resolves refs into the worker Secret |
| `RAMEN_GCP_PROJECT` | – | GCP project (Firestore, Secret Manager, Logging, Compute, IAM). Required for `RAMEN_CLOUD=gcp` / `RAMEN_SECRETS_BACKEND=gcp` |
| `RAMEN_GCP_REGION` | `us-central1` | GKE cluster region |
| `RAMEN_GROUPS_BUCKET` | `ramen-<project>-groups` | One GCS bucket, one prefix per group (`gs://<bucket>/<group>/`) |
| `RAMEN_IMAGE_WORKER` | `ramen-worker:<console version>` | Worker image used in the per-zone Deployments (the Helm chart sets the registry path) |
| `RAMEN_DEPLOY_TIMEOUT_SECS` | `300` | Max wait for a Deployment rollout during deploy |
| `RAMEN_WORKER_TLS` / `RAMEN_WORKER_CA` / `_CERT` / `_KEY` | `0` / – | `1` = TLS to worker nodes (PEM paths; without `_CA` the system roots are used). Default h2c inside the cluster |
| `RAMEN_WORKER_DEADLINE` | `10` | Per-call gRPC deadline (seconds) for Admin/Mcp/Health calls to workers |
| `RAMEN_LOGIN_RATE_LIMIT` | `20` | POST attempts per IP per minute on login/reset/magic before 429 |
| `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED` | `0` | accept SSO emails without `email_verified: true` (only for providers that never send the claim) |
| `RAMEN_WORKER_CHART` | – | Path to `deploy/helm/ramen-worker`; when set and `helm` is on PATH the zone manifests are rendered with `helm template`, otherwise the built-in Python manifest set is used |
| `RAMEN_GCP_FRESH_HTTP` | `1` | `0` lets googleapiclient reuse its shared (not thread-safe) transport instead of a per-call authorized `httplib2.Http` |
| `GOOGLE_OAUTH_ACCESS_TOKEN` | – | Explicit OAuth token for the Google clients when no ADC is available (`gcloud auth print-access-token`); dev only |
| `GOOGLE_CLOUD_PROJECT` | – | Firestore project fallback when `RAMEN_GCP_PROJECT` is unset (the compose stack sets it for the emulator) |
| `RAMEN_AWS_REGION` | `AWS_REGION` or `us-east-1` | AWS region for `RAMEN_CLOUD=aws` / `RAMEN_SECRETS_BACKEND=aws` (untested on a real account, CONTRACTS §8) |
| `RAMEN_EKS_CLUSTER`, `RAMEN_ALB_GROUP` | `ramen` / `ramen` | EKS cluster name (OIDC issuer for worker IAM roles, Container Insights log group) and the ALB IngressGroup shared by console + workers |
| `RAMEN_AWS_PERMISSIONS_BOUNDARY` | `arn:aws:iam::<account>:policy/ramen/ramen-worker-boundary` | Managed policy attached as permissions boundary to every worker role the console creates (the console role may only create roles carrying it, SEC-10). `-` disables |
| `RAMEN_LOG_GROUP` | `/aws/containerinsights/<cluster>/application` | CloudWatch log group queried by `logs()` (Logs Insights, Fluent Bit) |
| `KUBECONFIG` | – | Used when in-cluster config is unavailable (console running outside GKE) |
| `RAMEN_BUCKET_ROOT` | `./buckets` | Local adapter: filesystem "bucket" root, one dir per group (`<root>/<group>`) |
| `RAMEN_LOG_ROOT` | `<bucket root>/_logs` | Local adapter: worker logs at `<root>/<group>/<zone>/worker.log` |
| `RAMEN_LOCAL_WORKERS` | – | `group/zone=w1:8080|w2:8080,other/zone=…` worker gRPC targets per group+zone (a `http://`/`https://` scheme is accepted; `https://` means TLS) |
| `RAMEN_WORKER_URL` | `worker:8080` | Fallback worker target when a group/zone has no entry |
| `RAMEN_ADMIN_KEY` | – | Sent as gRPC metadata `x-ramen-admin-key` on `Admin/Reload` and `Admin/Metrics` |
| `RAMEN_BACKUP_ROOT` | `./backups` | Where `target=local` backups are written (`target=bucket` → `<bucket root>/_backups`) |
| `RAMEN_CONFIG` | – | YAML file; top-level keys map to `RAMEN_*` (nested keys join with `_`). Env vars win. Hot reload: `POST /api/v1/config/reload` |
| `RAMEN_OAUTH_<NAME>_ISSUER` (or `_METADATA_URL`) / `_CLIENT_ID` / `_CLIENT_SECRET` / `_SCOPES` | – / – / – / `openid email profile` | OIDC provider `<name>` (login button, `/auth/<name>/login` → `/auth/<name>/callback`); enabled when id, secret and issuer/metadata URL are set. Users are linked or created by verified email |
| `RAMEN_AUTH_OAUTH_<NAME>_ROLE_CLAIM` / `_ROLE_MAP` | – | Role mapping for *new* SSO users: claim name (string or list) and `value=role[:group,group];…` (or `_ROLE_MAP_<VALUE>=role:groups`). Default viewer, no groups |
| `RAMEN_AUTH_PASSWORD_LOGIN` / `RAMEN_AUTH_MAGIC_LINK` | `1` / `0` | Defaults for the super-admin toggles at `PUT /api/v1/config/auth` (store doc wins) |
| `RAMEN_ADMIN_FORCE_PASSWORD` | `0` | Break-glass: with password login off, `RAMEN_ADMIN_EMAIL` may still sign in with the password |
| `RAMEN_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_TLS` | – / `587` / – / – / `ramen@localhost` / `1` | Outbound mail (invite on user create, password reset, magic link). `_TLS`: `1` starttls, `ssl`, `0`. `RAMEN_SMTP_HOST=file:///dir` writes `.eml` files instead (dev/tests). Unset = mail off (reset page says so) |
| `RAMEN_PUBLIC_URL` | request base URL | Base for links in mails |
| `RAMEN_CONSOLE_PORT` | `8000` | HTTP port |
| `RAMEN_TLS` / `RAMEN_TLS_PORT` / `RAMEN_TLS_HOST` | – / `8443` / `localhost` | `RAMEN_TLS=self` serves HTTPS with a generated cert |
| `RAMEN_LOG_LEVEL` | `INFO` | Console log level |
| `RAMEN_APP_AUTOCREATE` | `1` | `0` skips building the module-level `app` on import (tests build their own with `create_app`) |

### Auth config yaml example

```yaml
# RAMEN_CONFIG=/etc/ramen/console.yaml — keys map to RAMEN_* (nested join with _); env vars win; hot reload POST /api/v1/config/reload
auth:
  password_login: true        # RAMEN_AUTH_PASSWORD_LOGIN; toggle at runtime: PUT /api/v1/config/auth {"password_login": false}
  magic_link: false           # RAMEN_AUTH_MAGIC_LINK (needs smtp)
  oauth:
    google:
      role_claim: hd          # RAMEN_AUTH_OAUTH_GOOGLE_ROLE_CLAIM
      role_map: "example.com=group_admin:demo"   # RAMEN_AUTH_OAUTH_GOOGLE_ROLE_MAP
oauth:
  google:
    issuer: https://accounts.google.com          # RAMEN_OAUTH_GOOGLE_ISSUER
    client_id: ...
    client_secret: ...        # prefer the env var RAMEN_OAUTH_GOOGLE_CLIENT_SECRET
    scopes: openid email profile
smtp:
  host: smtp.example.com      # RAMEN_SMTP_HOST (file:///var/mail/ramen for a dev drop dir)
  port: 587
  user: ramen
  password: ...
  from: ramen@example.com
  tls: "1"
public_url: https://console.example.com
```

Security notes (v0.3.1 additions): OIDC always requests `openid` and uses PKCE S256 + `nonce`; the GitHub token for
repo sync is passed to git as an `http.extraheader` through `GIT_CONFIG_*` env only (never in the remote URL or the
bucket's `.git/config`); reset/magic nonces and API keys compare in constant time; on GKE the console needs no
`projectIamAdmin` (bucket- and secret-level bindings; `terraform … -var console_project_iam=true` re-enables project-wide
roles for approved permission requests) and only zone-scoped k8s permissions (RoleBinding to `ramen-console-zone` per
namespace); on AWS the console role is limited to the `ramen` web ACL / `ramen-*` IP sets and to `/ramen/` roles that
carry the worker permissions boundary. Cookie sessions must send `X-Ramen-CSRF` (value of the `ramen_csrf` cookie) on `/api/*` mutations
(HTMX does this from `base.html`); HTML forms carry the hidden `csrf_token`; API-key requests are exempt. Reset and
magic-link tokens are signed with `RAMEN_SESSION_SECRET`, single use (nonce on the user doc), 24 h / 15 min. Password
login cannot be disabled while no OAuth provider, magic link or break-glass exists. Mail bodies are never logged.

## Deploy flow (local adapter)

`POST /api/v1/groups/{g}/environments/{env}/deploy` returns `202 {id}`; poll `/api/v1/jobs/{id}` (the UI polls
`/ui/jobs/{id}` every 2 s and shows "refreshing…"). The job clones/pulls the group repo into the bucket (token from a
secret named `GITHUB_TOKEN`, or the group's fallback token), writes `<bucket>/.ramen/env-<zone>` with `RAMEN_*`,
`RAMEN_MCP_KEYS` (minted MCP keys), `RAMEN_BLOCKED` (tools/resources/prompts blocked on the group page, hidden from
list calls and answered `-32601` by the node) and `RAMEN_SECRET_<GROUP>__<NAME>` for secrets scoped to that env/zone, then
calls `Admin/Reload` on every worker of the zone. Errors are surfaced in the job, the group page and the audit log.

## Deploy flow (gcp adapter, CONTRACTS §7)

Zone = namespace `ramen-<group>-<zone>` (created when a zone is attached to an environment, or on first deploy):
Deployments `worker` and `worker-canary` pinned to the zone's `region` (GCP zone), Service `worker` (NEG
`ramen-<group>-<zone>`), KSA `worker` (Workload Identity → `ramen-<group>-<zone>@<project>`), Secret `ramen-deploy`,
HPA `worker` (min = count, max = 2×count). `sync_repo` clones the group repo and uploads it to
`gs://<bucket>/<group>/` (md5 diff, stale objects deleted). Deploy: write `ramen-deploy` (values resolved from the
secrets backend) → scale canary to 1 and restart it → wait ready → `Admin/Reload` → `Mcp/Call tools/list` smoke with
the first MCP key (`Health/Check` SERVING when no keys exist) → restart `worker` → wait ready. Any failure scales the
canary to 0, leaves `worker` untouched and is reported in the job with the streamed log. `workers()` = pods +
`Admin/Metrics`;
`logs()` = Cloud Logging (`k8s_container`, namespace filter, `pod_name` for one worker); `rebalance` = capacity
scaler 0.5/1.0 on the `ramen-<group>` backend service NEG for that zone (no-op with a note when the backend service
does not exist) + replicas ← HPA min; `ip-rules` = Cloud Armor policy `ramen-<group>` (allow CIDRs, deny rest; empty
list = allow all) attached to the backend service + `RAMEN_ALLOWED_CIDRS` in the deploy Secret; `service-account` =
GSA with `storage.objectViewer` bound on the groups bucket (prefix condition) + `secretmanager.secretAccessor` bound
on each `ramen-<group>-*` secret (new secrets are bound at creation) + Workload Identity binding, idempotent; nothing
is bound at project level (SEC-08). The zone namespace also gets RoleBinding `ramen-console` → ClusterRole
`ramen-console-zone` (SEC-09). Traffic reaches the zone through the Gateway by gRPC metadata `ramen-group`/`ramen-zone`
(HTTPRoute header match, HealthCheckPolicy GRPC). Sizes `s` 250m/512Mi, `m` 500m/1Gi, `l` 1/2Gi: super admins pick
the size and the per-zone allowed list; group admins may switch within the allowed list and set the count.

## Service-account permissions (CONTRACTS §9)

Group admins request abstract permissions (`GET /api/v1/policy/permissions`: `bucket.read`, `secrets.read`,
`logs.write`, …) for a group+zone from the group page; super-admin rules (`/config`) and the group's own restrictions
gate them (409 + audit when denied). A super admin approves on the Users page →
`Cloud.apply_sa_permissions(group, zone, permissions)`: gcp binds the mapped `roles/*` to the zone GSA (bucket/secret
roles on the resources with the group-prefix conditions; project-wide roles such as `logging.logWriter` only when the
console GSA has `projectIamAdmin`, else they come back as `skipped` with a note), local records `<bucket>/.ramen/sa_permissions_<zone>.json`, aws binds the
mapped actions (see `cloud/aws.py`). Approved permissions show per zone on the group page (`sa_permissions`).

## Roles

`super_admin` > `group_admin` (per group) > `viewer` (per group). API keys `rmn_<id>_<secret>` (header
`X-Ramen-Api-Key`) carry a role and group list never wider than their creator's. MCP worker keys are `rmk_…` and are
stored as secrets (never shown again). Every mutation writes an `audit` record `{ts,user,ip,action,target,ok,tags}`.
