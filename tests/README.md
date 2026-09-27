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

Other env: `RAMEN_TLS_INSECURE=1` (accept self-signed certs), `RAMEN_DEMO_REPO` (default the public demo repo),
`RAMEN_TEST_SUFFIX` (stable names for created groups/users), `RAMEN_E2E_GROUP`/`RAMEN_E2E_ENV`/`RAMEN_E2E_ZONE` (default `demo`/`dev`/`local`;
the zone must be one the local cloud adapter maps to the compose worker).

**Run order matters against a fresh stack:** `pytest e2e conformance`. e2e deploys the demo group and mints the MCP key
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
  uv run pytest -rs e2e conformance --cov --cov-report=xml
```
Cloud: same command with the LB URLs, e.g. `RAMEN_CONSOLE_URL=https://console.<host> RAMEN_NODE_URL=https://mcp.<host>`
(omit `RAMEN_TLS_INSECURE`). A node deliberately deployed with an excluding `RAMEN_ALLOWED_CIDRS` is asserted with `RAMEN_EXPECT_CIDR_403=1`.

## Scripts (`../scripts`)
- `check_versions.py [--tag vX.Y.Z]` — VERSION == Cargo.toml == pyprojects (CONTRACTS §6).
- `wait_ready.sh <url> [timeout] [interval]` — poll for HTTP 200.
- `coverage_report.py [--artifacts dir] [--allow-missing] [--no-fail]` — one markdown table, ≥90% verdict, exit 1 otherwise.
- `gcp_test_project.sh create|delete` — throwaway `ramen-test-<yymmdd>` project (`DRY_RUN=1` prints only).

## Console routes
CONTRACTS §4 fixes auth/roles/`/api/v1/*` but not paths. `src/ramen_tests/console.py::ROUTES` mirrors
`console/tests/test_api.py` (form `POST /login` → 303 + `ramen_session` cookie; users by id; global `/api/v1/zones`;
environments bound to zones; `POST .../environments/{env}/deploy` → 202 job polled at `/api/v1/jobs/{id}`;
group secrets by id, never returning `value`; `POST /api/v1/groups/{g}/mcp-keys` → `rmk_` worker key;
`POST /api/v1/api-keys` → `rmn_` key returned once; `POST /api/v1/backups {target}` → `{id, release_version}` + `/download`).
Edit `ROUTES` if the console moves them. Console `/healthz` is not in the contract yet; the harness waits on `/login`.

## Fixtures
- `fixtures/demo_group/` — verbatim copy of `ramen-demo-mcp-group`.
- `fixtures/broken_group/` — the demo tool plus `bad_type_tool` (type widget), `wrong_type_tool` (resource proto under tools/), `misnamed_tool` (folder ≠ name), `no_callable_tool` (callable missing). Loader must report each in `errors` and still load the demo tool.
- `fixtures/secrets_group/` — `echo_secret_tool` (runtime substitutes `{{$demo.TOKEN}}`), `resolve_secret_tool` (calls `ramen_runtime.secrets.resolve`).
- `fixtures/proto.schema.json` — JSON Schema 2020-12 for `<name>.json`; runtime-py may load it directly.
