# ramen/tests — cross-component conformance + e2e

Black-box suites that run against a console **URL** and a worker **gRPC target**, so the same tests cover the local compose
stack and a cloud deployment. Contract under test: `docs/CONTRACTS.md` (transport: **§11**, v0.3.1). Every suite skips
(never fails) when its env is missing. gRPC stubs: `src/ramen_proto/` generated from `../proto/ramen/v1` by `./gen_proto.sh`
(CI fails when they drift); `proto/grpc/health/v1/health.proto` is vendored for grpcurl.

| Suite | Env needed | Covers |
|---|---|---|
| `conformance/test_fixtures.py` | none | fixtures vs `fixtures/proto.schema.json` (CONTRACTS §1) |
| `conformance/test_sidecar_protocol.py` | importable `ramen_runtime` (auto-detects `../runtime-py/.venv`, or `RAMEN_RUNTIME_PYTHON`) | §2: every `runtime.*` method, broken packages → `errors`, `RAMEN_SECRET_<GROUP>__<VAR>` substitution |
| `conformance/test_mcp_node.py` | `RAMEN_NODE_URL` (`host:port`) + `RAMEN_MCP_KEY` (or a key minted by e2e earlier in the same run; opt: `RAMEN_ADMIN_KEY`, `RAMEN_EXPECT_CIDR_DENIED=1`, `RAMEN_EXPECT_BLOCKED=<tool>`, `RAMEN_WORKER_LOG=<path>`) | §11 over gRPC: Health SERVING, initialize/list/call/read/get, `-32601`, UNAUTHENTICATED (no/bad/malformed key), 4 MiB → RESOURCE_EXHAUSTED/INVALID_ARGUMENT, notifications → empty body, Session reserved, Admin/* key gate, Admin/Metrics fields, Admin/Reload, access-log fields |
| `conformance/test_mcp_node_local.py` | built `ramen-node` (`../node-rs/target/release`, or `RAMEN_NODE_BIN`) + importable `ramen_runtime`; opt `RAMEN_TEST_TLS=1` | §11 guards that need a specific node config, on real processes: Health NOT_SERVING→SERVING, empty key set = deny all, CIDR PERMISSION_DENIED (+ `x-forwarded-for` only with `RAMEN_TRUST_PROXY`), `RAMEN_ADMIN_CIDRS`, `RAMEN_BLOCKED` hidden + `-32601`, size limit, `RAMEN_MAX_INFLIGHT` → RESOURCE_EXHAUSTED, log file fields (`grpc_code`, never key values), verbose, TLS (`RAMEN_TLS_CERT/KEY`), bridge |
| `conformance/test_bridge.py` | `RAMEN_NODE_URL` + key + `ramen-mcp-bridge` (`RAMEN_BRIDGE_CMD`, PATH, or `../runtime-py/.venv`) | §11 bridge: official `mcp` SDK stdio client → `ramen-mcp-bridge` → `Mcp/Call`: initialize, list/call/read/get, unknown tool, bad key fails cleanly |
| `conformance/test_console_api.py` | `RAMEN_CONSOLE_URL` (opt: `RAMEN_ADMIN_EMAIL`/`RAMEN_ADMIN_PASSWORD`, default `admin@ramen.local`/`ramen-admin`) | §4: bootstrap login, RBAC matrix, secret values never in responses, API keys, audit, backup |
| `conformance/test_console_auth.py` | `RAMEN_CONSOLE_URL` (reset-mail case also needs `RAMEN_MAIL_DIR` = host path of the console's `RAMEN_SMTP_HOST=file://` dir, compose: `deploy/local/.mail`) | §9: CSRF (cookie sessions 403 without `X-Ramen-CSRF`, API keys exempt), `GET|PUT /api/v1/config/auth` super admin + audited, SA permission request → 409 on denied rule (audited) → approve → `sa_permissions` on the zone, `PUT …/blocked` → env + group page, password reset via emailed link (single use) |
| `e2e/test_demo_flow.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` (+`RAMEN_ADMIN_KEY` for the metrics case) | console → zone/group/env → mint `rmk_` key → deploy job → Health SERVING → MCP calls over gRPC → only the minted key works → Admin/Metrics, audit, dashboard |
| `e2e/test_tool_blocking.py` | same (runs after the demo flow) | §9: `PUT …/blocked [demo_calculator_tool]` → deploy → node hides it from `tools/list` and denies the call → unblock → deploy → restored |
| `cloud/test_canary.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_NODE_URL`; canary-at-0 check needs `RAMEN_GCP_PROJECT`) | §7 deploy: bad ref → job `error` + audited, main worker still serves; good ref → `ok`; `canary:false` |
| `cloud/test_rebalance.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`) | POST rebalance → `{ok}` + audit; GCP: NEG backend `capacityScaler` ∈ {0.5, 1.0} |
| `cloud/test_ip_rules.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` + `RAMEN_TRUSTED_CIDRS` (+`RAMEN_GCP_PROJECT`) | PUT cidrs → deploy → node PERMISSION_DENIED outside (health stays reachable) → restore; invalid CIDR 4xx; GCP: Cloud Armor policy `ramen-<group>` |
| `cloud/test_logs.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` | `/api/v1/logs` returns worker JSON lines, `tail`, `worker`, `download=1` attachment, no key values, 401 anon |
| `cloud/test_service_account.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`) | POST service-account → `{name}` recorded + audited; GCP: GSA `ramen-<group>-<zone>@` with objectViewer+secretAccessor, WI binding |
| `cloud/test_secrets_gcp.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`, console `RAMEN_SECRETS_BACKEND=gcp`) | value never returned; GCP: SM secret `ramen-<group>-<env>-<zone>-<NAME>` with labels, deleted on delete |

Other env: `RAMEN_NODE_TLS=1` (TLS gRPC channel, e.g. through the cloud LB; `grpcs://`/`https://` prefixes imply it),
`RAMEN_NODE_CA=<pem>` (trust anchor; without it and with `RAMEN_TLS_INSECURE=1` the server's own self-signed cert is pinned),
`RAMEN_TLS_INSECURE=1` (accept self-signed certs, console and node), `RAMEN_DEMO_REPO` (default the public demo repo),
`RAMEN_TEST_SUFFIX` (stable names for created groups/users), `RAMEN_E2E_GROUP`/`RAMEN_E2E_ENV`/`RAMEN_E2E_ZONE` (default `demo`/`dev`/`local`;
sent as `ramen-group`/`ramen-zone` metadata on every gRPC call, which is how the cloud LB routes to the worker;
the zone must be one the local cloud adapter maps to the compose worker), `RAMEN_ZONE_PROVIDER`/`RAMEN_ZONE_REGION`
(zone record fields, default `local`/`local`; GCP: `gcp`/`us-central1-a`), `RAMEN_GCP_PROJECT` (enables gcloud-backed
assertions; needs `gcloud` on PATH with viewer access), `RAMEN_TRUSTED_CIDRS` (the console's address as the worker sees it,
kept allowed while the IP-rules test locks the node; compose: `docker inspect ramen-console-1` → `172.18.0.4/32`).

**Run order matters against a fresh stack:** `pytest e2e conformance cloud`. e2e deploys the demo group and mints the MCP key
that `test_mcp_node.py` / `test_bridge.py` then use; the worker's Health is `NOT_SERVING` before the first deploy.

## Run
```sh
cd tests && uv sync
uv run pytest                                   # no env: fixtures run, everything else skips (CI "tests" job)
uv run pytest conformance/test_sidecar_protocol.py   # after `uv sync` in ../runtime-py
```
Local compose stack (`deploy/local/docker-compose.yml`):
```sh
docker compose -f deploy/local/docker-compose.yml up -d --build
scripts/wait_ready.sh https://localhost:8443/login && scripts/wait_ready.sh localhost:8080 120 2 --any
cd tests && RAMEN_CONSOLE_URL=https://localhost:8443 RAMEN_NODE_URL=localhost:8080 RAMEN_ADMIN_KEY=local-admin-key \
  RAMEN_ADMIN_EMAIL=admin@ramen.local RAMEN_ADMIN_PASSWORD=ramen-admin RAMEN_TLS_INSECURE=1 \
  uv run pytest -rs e2e conformance cloud --cov --cov-report=xml
```
`RAMEN_NODE_URL` is a gRPC target `host:port`: a worker directly (compose `localhost:8080`, h2c) or the cloud LB (`RAMEN_NODE_TLS=1`);
there is no path — the LB routes on the `ramen-group`/`ramen-zone` metadata the harness always sends. `Admin/*` cases skip
with a reason when `RAMEN_ADMIN_KEY` is unset or the worker's `RAMEN_ADMIN_CIDRS` exclude the caller (typical through an LB).
Node-configuration guards (CIDR deny, blocked names, TLS, log file, NOT_SERVING) run in `test_mcp_node_local.py` on real
`ramen-node` processes instead (`cargo build --release` in `node-rs`, `uv sync` in `runtime-py`).

Cloud: same command with `RAMEN_CONSOLE_URL=https://console.<host> RAMEN_NODE_URL=mcp.<host>:443 RAMEN_NODE_TLS=1`
(omit `RAMEN_TLS_INSECURE` with a real cert). A node deliberately deployed with an excluding `RAMEN_ALLOWED_CIDRS` is asserted with `RAMEN_EXPECT_CIDR_DENIED=1`.

## Scripts (`../scripts`)
- `check_versions.py [--tag vX.Y.Z]` — VERSION == Cargo.toml == pyprojects == Helm chart versions/tags == mkdocs `version_current` == Dockerfile ARG defaults == compose/.env defaults (CONTRACTS §6).
- `wait_ready.sh <url|host:port> [timeout] [interval] [--any]` — poll for HTTP 200, or (gRPC target) `grpc.health.v1` SERVING (`--any`: answers at all; `RAMEN_NODE_TLS=1`). grpcurl or a python with grpcio.
- `coverage_report.py [--artifacts dir] [--allow-missing] [--no-fail]` — one markdown table, ≥90% verdict, exit 1 otherwise.
- `gcp_test_project.sh create|delete` — throwaway `ramen-test-<yymmdd>` project (`DRY_RUN=1` prints only).
- `cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_target]` — group demo → zone → env → key → deploy → Health → `tools/call` over gRPC with grpcurl (routing metadata, `RAMEN_NODE_TLS=1` through the LB), UNAUTHENTICATED and Admin gates; prints PASS/FAIL (`RAMEN_SMOKE_ZONE`, `RAMEN_ZONE_PROVIDER`, `RAMEN_ZONE_REGION`, `RAMEN_SMOKE_ADMIN=1`).
- `gcp_cost_check.sh [project] [--expect-empty]` — lists billable resources (cluster, forwarding rules, addresses, buckets, AR, secrets, Firestore) to confirm teardown.

CI: `ci.yml` runs the no-env harness + stub drift check (`tests`), the local-node guards + bridge on the freshly built binary
(`node-conformance`), and e2e + conformance + smoke against the compose stack (`e2e`). `.github/workflows/cloud-e2e.yml` is
`workflow_dispatch` only (inputs `console_url`, `node_url` = `host:port`, `node_tls`, `group`, `gcp_project`, `zone`, `zone_region`,
`trusted_cidrs`; secrets `RAMEN_ADMIN_EMAIL/PASSWORD/KEY`, optional `GCP_SA_KEY`) and runs smoke + e2e + conformance + cloud.

## Console routes
CONTRACTS §4a is the binding route table. `src/ramen_tests/console.py::ROUTES` mirrors it and
`console/tests/test_api.py` (form `POST /login` → 303 + `ramen_session` cookie; users by id; global `/api/v1/zones`;
environments bound to zones; `POST .../environments/{env}/deploy` → 202 job polled at `/api/v1/jobs/{id}`;
group secrets by id, never returning `value`; `POST /api/v1/groups/{g}/mcp-keys` → `rmk_` worker key;
`POST /api/v1/api-keys` → `rmn_` key returned once; `POST /api/v1/backups {target}` → `{id, release_version}` + `/download`; v0.2.0 additions:
`POST .../zones/{zone}/service-account` → `{name}` (super admin), `PUT .../groups/{g}/sa-restrictions {rules}`,
`POST .../environments/{env}/verbose {verbose}`, `POST /api/v1/config/reload`, `GET|PUT /api/v1/config/sa-rules`,
`GET /api/v1/logs?group&zone&worker?&tail&download=1` → `text/plain` (+ `Content-Disposition: attachment`)).
v0.3.0 (§9): `POST /api/v1/requests {group,zone,permission}` + `POST /api/v1/requests/{id}/approve`, `GET /api/v1/policy/permissions`,
`PUT …/environments/{env}/blocked {blocked}`, `GET|PUT /api/v1/config/auth`, forms `POST /auth/reset`, `POST /auth/reset/{token}`,
`POST /auth/magic`, `GET /auth/magic/{token}`, OAuth `GET /auth/{name}/login` → `/auth/{name}/callback`. Cookie sessions must send
`X-Ramen-CSRF: <ramen_csrf cookie>` on mutations (`Console` does this automatically; API-key clients are exempt).
Edit `ROUTES` (and §4a) if the console moves them. Console probes `/healthz` and `/readyz` are in §4; the harness waits on `/login` so the UI is proven up too.

## Fixtures
- `fixtures/demo_group/` — verbatim copy of `ramen-demo-mcp-group`.
- `fixtures/broken_group/` — the demo tool plus `bad_type_tool` (type widget), `wrong_type_tool` (resource proto under tools/), `misnamed_tool` (folder ≠ name), `no_callable_tool` (callable missing). Loader must report each in `errors` and still load the demo tool.
- `fixtures/secrets_group/` — `echo_secret_tool` (runtime substitutes `{{$demo.TOKEN}}`), `resolve_secret_tool` (calls `ramen_runtime.secrets.resolve`).
- `fixtures/proto.schema.json` — JSON Schema 2020-12 for `<name>.json`; runtime-py may load it directly.
