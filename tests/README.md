# ramen/tests — cross-component conformance + e2e

Black-box suites that run against **URLs**, so the same tests cover the local compose stack and a cloud deployment.
Contract under test: `docs/CONTRACTS.md`. Every suite skips (never fails) when its env is missing.

| Suite | Env needed | Covers |
|---|---|---|
| `conformance/test_fixtures.py` | none | fixtures vs `fixtures/proto.schema.json` (CONTRACTS §1) |
| `conformance/test_sidecar_protocol.py` | importable `ramen_runtime` (auto-detects `../runtime-py/.venv`, or `RAMEN_RUNTIME_PYTHON`) | §2: every `runtime.*` method, broken packages → `errors`, `RAMEN_SECRET_<GROUP>__<VAR>` substitution |
| `conformance/test_mcp_node.py` | `RAMEN_NODE_URL` + `RAMEN_MCP_KEY` (or a key minted by e2e earlier in the same run; opt: `RAMEN_ADMIN_KEY`, `RAMEN_EXPECT_CIDR_403=1`) | §3 via the official `mcp` client: initialize, list/call/read/get, 401, health/metrics |
| `conformance/test_console_api.py` | `RAMEN_CONSOLE_URL` (opt: `RAMEN_ADMIN_EMAIL`/`RAMEN_ADMIN_PASSWORD`, default `admin@ramen.local`/`ramen-admin`) | §4: bootstrap login, RBAC matrix, secret values never in responses, API keys, audit, backup |
| `e2e/test_demo_flow.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` | console → zone/group/env → mint `rmk_` key → deploy job → worker ready → MCP calls → metrics/audit/dashboard |
| `cloud/test_canary.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_NODE_URL`; canary-at-0 check needs `RAMEN_GCP_PROJECT`) | §7 deploy: bad ref → job `error` + audited, main worker still serves; good ref → `ok`; `canary:false` |
| `cloud/test_rebalance.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`) | POST rebalance → `{ok}` + audit; GCP: NEG backend `capacityScaler` ∈ {0.5, 1.0} |
| `cloud/test_ip_rules.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` + `RAMEN_TRUSTED_CIDRS` (+`RAMEN_GCP_PROJECT`) | PUT cidrs → deploy → node 403 outside → restore; invalid CIDR 4xx; GCP: Cloud Armor policy `ramen-<group>` |
| `cloud/test_logs.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` | `/api/v1/logs` returns worker JSON lines, `tail`, `worker`, `download=1` attachment, no key values, 401 anon |
| `cloud/test_service_account.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`) | POST service-account → `{name}` recorded + audited; GCP: GSA `ramen-<group>-<zone>@` with objectViewer+secretAccessor, WI binding |
| `cloud/test_secrets_gcp.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`, console `RAMEN_SECRETS_BACKEND=gcp`) | value never returned; GCP: SM secret `ramen-<group>-<env>-<zone>-<NAME>` with labels, deleted on delete |

Other env: `RAMEN_TLS_INSECURE=1` (accept self-signed certs), `RAMEN_DEMO_REPO` (default the public demo repo),
`RAMEN_TEST_SUFFIX` (stable names for created groups/users), `RAMEN_E2E_GROUP`/`RAMEN_E2E_ENV`/`RAMEN_E2E_ZONE` (default `demo`/`dev`/`local`;
the zone must be one the local cloud adapter maps to the compose worker), `RAMEN_ZONE_PROVIDER`/`RAMEN_ZONE_REGION`
(zone record fields, default `local`/`local`; GCP: `gcp`/`us-central1-a`), `RAMEN_GCP_PROJECT` (enables gcloud-backed
assertions; needs `gcloud` on PATH with viewer access), `RAMEN_TRUSTED_CIDRS` (the console's address as the worker sees it,
kept allowed while the IP-rules test locks the node; compose: `docker inspect ramen-console-1` → `172.18.0.4/32`).

**Run order matters against a fresh stack:** `pytest e2e conformance cloud`. e2e deploys the demo group and mints the MCP key
that `test_mcp_node.py` then uses; the worker is not `/readyz` before the first deploy.

## Run
```sh
cd tests && uv sync
uv run pytest                                   # no env: fixtures run, everything else skips (CI "tests" job)
uv run pytest conformance/test_sidecar_protocol.py   # after `uv sync` in ../runtime-py
```
Local compose stack (`deploy/local/docker-compose.yml`):
```sh
docker compose -f deploy/local/docker-compose.yml up -d --build
scripts/wait_ready.sh https://localhost:8443/login && scripts/wait_ready.sh http://localhost:8080/healthz
cd tests && RAMEN_CONSOLE_URL=https://localhost:8443 RAMEN_NODE_URL=http://localhost:8080 \
  RAMEN_ADMIN_EMAIL=admin@ramen.local RAMEN_ADMIN_PASSWORD=ramen-admin RAMEN_TLS_INSECURE=1 \
  uv run pytest -rs e2e conformance cloud --cov --cov-report=xml
```
`RAMEN_NODE_URL` may be a bare node (`http://host:8080`: health/metrics/admin asserted too) or an MCP-only LB route such as
`https://<console_ip>/mcp/<group>/<zone>` (GKE Gateway `HTTPRoute` rewrites it to the worker's `/mcp`); on a route the
harness posts MCP to that URL as given and skips the `/healthz`, `/readyz`, `/metrics`, `/admin/*` checks with a reason.

Cloud: same command with the LB URLs, e.g. `RAMEN_CONSOLE_URL=https://console.<host> RAMEN_NODE_URL=https://mcp.<host>`
(omit `RAMEN_TLS_INSECURE`). A node deliberately deployed with an excluding `RAMEN_ALLOWED_CIDRS` is asserted with `RAMEN_EXPECT_CIDR_403=1`.

## Scripts (`../scripts`)
- `check_versions.py [--tag vX.Y.Z]` — VERSION == Cargo.toml == pyprojects (CONTRACTS §6).
- `wait_ready.sh <url> [timeout] [interval]` — poll for HTTP 200.
- `coverage_report.py [--artifacts dir] [--allow-missing] [--no-fail]` — one markdown table, ≥90% verdict, exit 1 otherwise.
- `gcp_test_project.sh create|delete` — throwaway `ramen-test-<yymmdd>` project (`DRY_RUN=1` prints only).
- `cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_url]` — group demo → zone → env → key → deploy → `tools/call` through the LB; prints PASS/FAIL (`RAMEN_SMOKE_ZONE`, `RAMEN_ZONE_PROVIDER`, `RAMEN_ZONE_REGION`).
- `gcp_cost_check.sh [project] [--expect-empty]` — lists billable resources (cluster, forwarding rules, addresses, buckets, AR, secrets, Firestore) to confirm teardown.

CI: `.github/workflows/cloud-e2e.yml` is `workflow_dispatch` only (inputs `console_url`, `node_url`, `gcp_project`, `zone`,
`zone_region`, `trusted_cidrs`; secrets `RAMEN_ADMIN_EMAIL/PASSWORD/KEY`, optional `GCP_SA_KEY`) and runs smoke + e2e + conformance + cloud.

## Console routes
CONTRACTS §4 fixes auth/roles/`/api/v1/*` but not paths. `src/ramen_tests/console.py::ROUTES` mirrors
`console/tests/test_api.py` (form `POST /login` → 303 + `ramen_session` cookie; users by id; global `/api/v1/zones`;
environments bound to zones; `POST .../environments/{env}/deploy` → 202 job polled at `/api/v1/jobs/{id}`;
group secrets by id, never returning `value`; `POST /api/v1/groups/{g}/mcp-keys` → `rmk_` worker key;
`POST /api/v1/api-keys` → `rmn_` key returned once; `POST /api/v1/backups {target}` → `{id, release_version}` + `/download`; v0.2.0 additions:
`POST .../zones/{zone}/service-account` → `{name}` (super admin), `PUT .../groups/{g}/sa-restrictions {rules}`,
`POST .../environments/{env}/verbose {verbose}`, `POST /api/v1/config/reload`, `GET|PUT /api/v1/config/sa-rules`,
`GET /api/v1/logs?group&zone&worker?&tail&download=1` → `text/plain` (+ `Content-Disposition: attachment`)).
Edit `ROUTES` if the console moves them. Console `/healthz` is not in the contract yet; the harness waits on `/login`.

## Fixtures
- `fixtures/demo_group/` — verbatim copy of `ramen-demo-mcp-group`.
- `fixtures/broken_group/` — the demo tool plus `bad_type_tool` (type widget), `wrong_type_tool` (resource proto under tools/), `misnamed_tool` (folder ≠ name), `no_callable_tool` (callable missing). Loader must report each in `errors` and still load the demo tool.
- `fixtures/secrets_group/` — `echo_secret_tool` (runtime substitutes `{{$demo.TOKEN}}`), `resolve_secret_tool` (calls `ramen_runtime.secrets.resolve`).
- `fixtures/proto.schema.json` — JSON Schema 2020-12 for `<name>.json`; runtime-py may load it directly.
