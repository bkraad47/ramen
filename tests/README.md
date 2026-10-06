# ramen/tests — cross-component conformance + e2e

Black-box suites that run against a console **URL** and a worker **gRPC target**, so the same tests cover the local compose
stack and a cloud deployment. Contract under test: `docs/CONTRACTS.md` (transport: **§11**, v0.3.1). Every suite skips
(never fails) when its env is missing. gRPC stubs: `src/ramen_proto/` generated from `../proto/ramen/v1` by `./gen_proto.sh`
(CI fails when they drift); `proto/grpc/health/v1/health.proto` is vendored for grpcurl.

| Suite | Env needed | Covers |
|---|---|---|
| `conformance/test_fixtures.py` | none | fixtures vs `fixtures/proto.schema.json` (CONTRACTS §1) |
| `conformance/test_sidecar_protocol.py` | importable `ramen_runtime` (auto-detects `../runtime-py/.venv`, or `RAMEN_RUNTIME_PYTHON`) | §2: every `runtime.*` method, broken packages → `errors`, `RAMEN_SECRET_<GROUP>__<VAR>` substitution |
| `conformance/test_mcp_node.py` | `RAMEN_NODE_URL` (`host:port`) + `RAMEN_MCP_KEY` (or a key minted by e2e earlier in the same run; opt: `RAMEN_ADMIN_KEY`, `RAMEN_EXPECT_CIDR_DENIED=1`, `RAMEN_EXPECT_BLOCKED=<tool>`, `RAMEN_EXPECT_NO_REFLECTION=1`, `RAMEN_WORKER_LOG=<path>`) | §11 over gRPC: Health SERVING, initialize/list/call/read/get, `-32601`, UNAUTHENTICATED (no/bad/malformed key), 4 MiB → RESOURCE_EXHAUSTED/INVALID_ARGUMENT, notifications → empty body, Session reserved, Admin/* key gate, Admin/Metrics fields, Admin/Reload, access-log fields |
| `conformance/test_mcp_node_local.py` | built `ramen-node` (`../node-rs/target/release`, or `RAMEN_NODE_BIN`) + importable `ramen_runtime`; opt `RAMEN_TEST_TLS=1` | §11 guards that need a specific node config, on real processes: Health NOT_SERVING→SERVING, empty key set = deny all, CIDR PERMISSION_DENIED (`x-forwarded-for` ignored unless `RAMEN_TRUST_PROXY_HOPS>0`; hops counted from the right, a spoofed left-hand entry is never checked, a wrong hop count falls back to the peer), `RAMEN_REFLECTION=0`, `RAMEN_ADMIN_CIDRS`, `RAMEN_BLOCKED` hidden + `-32601`, size limit, `RAMEN_MAX_INFLIGHT` → RESOURCE_EXHAUSTED, log file fields (`grpc_code`, never key values), verbose, TLS (`RAMEN_TLS_CERT/KEY`), bridge |
| `conformance/test_numpy_deps.py` | built `ramen-node` + importable `ramen_runtime`, and that venv needs a real `pip` (I13: `uv sync` alone does not install one — `cd runtime-py && uv sync --all-extras && uv pip install pip`) | a group's `mcp/requirements.txt` really reaches the worker's `pip install`: real `numpy` install + import at call time, not a mocked subprocess |
| `conformance/test_tool_propagation.py` | built `ramen-node` + importable `ramen_runtime` (bridge case: `ramen-mcp-bridge`) | 0.7.0 R2: `mcp/tools` changed on disk → `Admin/Reload` → the SAME session (raw HTTP `Mcp-Session-Id`, SDK client, running bridge) lists the new set, calls the new tool, gets `-32004` for the removed one; the node declares no `listChanged` and `GET /mcp` is 405 (clients learn only by re-listing); reload `hash` == Metrics `manifest_hash` and moves with the set (C1/C2) |
| `conformance/test_auth_expiry.py` | built `ramen-node` + importable `ramen_runtime`; part 2 also a console venv (`../console/.venv` or `RAMEN_CONSOLE_PYTHON`) and the bridge | 0.7.0 A1/A2/A4/D2: expired token → 401 with the same `WWW-Authenticate` as no token; session bound to `user:<sub>` survives a new token; a call started before `exp` finishes; SDK client sees the 401 as `MCPError -32603` and continues on the same session with a fresh bearer; real console with `RAMEN_OAUTH_ACCESS_TTL=3`: mint via PKCE → expiry → `/oauth/token refresh_token` rotates → same session; bridge `--oauth` with a seeded token file refreshes with no browser, a dead refresh token is the one case that asks for sign-in; denied log lines carry `reason` |
| `conformance/test_output_schema.py` | built `ramen-node` + importable `ramen_runtime` | 0.7.0 B5/C9 on both transports + the SDK: `outputSchema` in `tools/list` verbatim (`schema_group`) and wrapped `{result}` for a scalar `output` (demo calc); `structuredContent` on a good call; a violating result is `isError` with `invalid output: <path>: <message>`, never a transport error |
| `conformance/test_tool_access.py` | built `ramen-node` + importable `ramen_runtime`; part 2 a console venv | 0.7.2 C10 on both transports: `RAMEN_TOOL_ACCESS` — a group key lists the restricted tool but `tools/call` is `-32003`; tokens with `role` `viewer`/none do not see it and get `-32601`, `group_admin`/`super_admin` list and call; unrestricted tool unaffected; log reasons `tool_hidden`/`tool_denied`; console `PUT …/tool-access` (422 on unknown kind / `call ⊄ list`) → deploy env carries the map; token `role` claim |
| `conformance/test_rollout_notify.py` | built `ramen-node` + importable `ramen_runtime` | 0.7.2 C11: `initialize` declares `tools.listChanged`, session id `<nonce>.<expiry>.<hash12>.<mac>`; after `mcp/tools` change + `Admin/Reload` the session's next POST accepting `text/event-stream` is SSE (`notifications/tools/list_changed`, then the response), the next is JSON; JSON-only Accept → JSON; a 0.7.1 3-part id still works; the SDK client is notified and completes a flow |
| `conformance/test_call_args_hash.py` | built `ramen-node` + importable `ramen_runtime` | 0.7.2 C12: `tools/call` log lines carry `args` = sha256(canonical arguments)[:12], equal for equal arguments in any key order, `""` without arguments, never the values |
| `conformance/test_scripts.py` | none | `scripts/check_versions.py --demo`: `../ramen-demo-mcp/VERSION` is a row, mismatch fails |
| `conformance/test_bridge.py` | `RAMEN_NODE_URL` + key + `ramen-mcp-bridge` (`RAMEN_BRIDGE_CMD`, PATH, or `../../ramen-mcp-bridge/.venv`) | §11 bridge: official `mcp` SDK stdio client → `ramen-mcp-bridge` → `Mcp/Call`: initialize, list/call/read/get, unknown tool, bad key fails cleanly |
| `conformance/test_console_api.py` | `RAMEN_CONSOLE_URL` (opt: `RAMEN_ADMIN_EMAIL`/`RAMEN_ADMIN_PASSWORD`, default `admin@ramen.local`/`ramen-admin`) | §4: bootstrap login, RBAC matrix, secret values never in responses, API keys, audit, backup |
| `conformance/test_console_auth.py` | `RAMEN_CONSOLE_URL` (reset-mail case also needs `RAMEN_MAIL_DIR` = host path of the console's `RAMEN_SMTP_HOST=file://` dir, compose: `deploy/local/.mail`) | §9: CSRF (cookie sessions 403 without `X-Ramen-CSRF`, API keys exempt), `GET|PUT /api/v1/config/auth` super admin + audited, SA permission request → 409 on denied rule (audited) → approve → `sa_permissions` on the zone, `PUT …/blocked` → env + group page, password reset via emailed link (single use) |
| `e2e/test_demo_flow.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` (+`RAMEN_ADMIN_KEY` for the metrics case) | console → zone/group/env → mint `rmk_` key → deploy job → Health SERVING → MCP calls over gRPC → only the minted key works → Admin/Metrics, audit, dashboard |
| `e2e/test_tool_blocking.py` | same (runs after the demo flow) | §9: `PUT …/blocked [demo_calculator_tool]` → deploy → node hides it from `tools/list` and denies the call → unblock → deploy → restored |
| `cloud/test_canary.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_NODE_URL`; canary-at-0 check needs `RAMEN_GCP_PROJECT`) | §7 deploy: bad ref → job `error` + audited, main worker still serves; good ref → `ok`; `canary:false` |
| `cloud/test_rebalance.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`) | POST rebalance → `{ok}` + audit; GCP: NEG backend `capacityScaler` ∈ {0.5, 1.0} |
| `cloud/test_ip_rules.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` + `RAMEN_TRUSTED_CIDRS` (+`RAMEN_GCP_PROJECT`) | PUT cidrs → deploy → node PERMISSION_DENIED outside (health stays reachable) → restore; invalid CIDR 4xx; GCP: Cloud Armor policy `ramen-<group>` |
| `cloud/test_logs.py` | `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` | `/api/v1/logs` returns worker JSON lines, `tail`, `worker`, `download=1` attachment, no key values, 401 anon |
| `cloud/test_service_account.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`) | POST service-account → `{name}` recorded + audited; GCP: GSA `ramen-<group>-<zone>@` with objectViewer+secretAccessor, WI binding |
| `conformance/test_security_matrix.py` | `RAMEN_CONSOLE_URL` (+ `RAMEN_NODE_URL` for the worker half; `RAMEN_ALLOW_SESSION_RESET=1` for the auth-config case) | U25: role x group x credential kind — every cross-group and cross-role action refused, an `agent` key refused by the console API and a `devops` key by a worker (D21), a key not replayable under the other prefix, and a session ending at once on delete, role change, group change and password reset |
| `cloud/test_forwarded_for.py` | `RAMEN_XFF_PROBE=1` + `RAMEN_CONSOLE_URL` + `RAMEN_NODE_URL` (the LB) + `RAMEN_KUBE_CONTEXT` (+ opt `RAMEN_CLIENT_IP`) | measures the real `x-forwarded-for` hop count against a live load balancer: reconfigures the deployed worker for hops 1/2/3, reads back the address the node checked from its access log, asserts the shipped count (gcp 2, aws 1) resolves this machine's address, that the allowlist matches it, that a spoofed left-hand entry is still denied, and that reflection is `UNIMPLEMENTED` through the LB |
| `cloud/test_secrets_gcp.py` | `RAMEN_CONSOLE_URL` (+`RAMEN_GCP_PROJECT`, console `RAMEN_SECRETS_BACKEND=gcp`) | value never returned; GCP: SM secret `ramen-<group>-<env>-<zone>-<NAME>` with labels, deleted on delete |

Other env: `RAMEN_ALLOW_SESSION_RESET=1` (lets `test_security_matrix.py` run the `PUT /config/auth` case, which bumps **every** user's session epoch and so kills the logged-in clients of any suite that ran earlier in the same process — run that file on its own), `RAMEN_NO_CLOUD=iam,logs,armor` (**kind only**, set by `deploy/kind/test.sh`: names the cloud services this target genuinely lacks, so cases needing them skip with a reason instead of failing — never set it for a cloud run), `RAMEN_NODE_TLS=1` (TLS gRPC channel, e.g. through the cloud LB; `grpcs://`/`https://` prefixes imply it),
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
`ramen-node` processes instead (`cargo build` in `node-rs` — the newest of `target/{release,debug}` is used, `uv sync` in `runtime-py`).
`test_auth_expiry.py` part 2 also starts a real console (`ramen_tests.localconsole.LocalConsole`: uvicorn, memory store,
`RAMEN_LOCAL_SESSION_SECRET` shared with the node) from `../console/.venv`.

Cloud: same command with `RAMEN_CONSOLE_URL=https://console.<host> RAMEN_NODE_URL=mcp.<host>:443 RAMEN_NODE_TLS=1`
(omit `RAMEN_TLS_INSECURE` with a real cert). A node deliberately deployed with an excluding `RAMEN_ALLOWED_CIDRS` is asserted with `RAMEN_EXPECT_CIDR_DENIED=1`.

## Scripts (`../scripts`)
- `check_versions.py [--tag vX.Y.Z] [--demo <path>]` — VERSION == Cargo.toml == pyprojects == Helm chart versions/tags == mkdocs `version_current` == Dockerfile ARG defaults == compose/.env defaults == `docs/llms.txt` == landing JSON-LD (CONTRACTS §6); 0.7.0: also `../ramen-demo-mcp/VERSION` when that checkout exists (ramen-master), skipped otherwise.
- `check_docs_links.py` — every site URL in README.md / docs/llms.txt / docs/index.md has a page under docs/; README relative links exist. Offline.
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
- `fixtures/schema_group/` — `schema_tool` with an object `output` schema; digits → `{"n": int}`, anything else → a schema violation (C9).
- `fixtures/secrets_group/` — `echo_secret_tool` (runtime substitutes `{{$demo.TOKEN}}`), `resolve_secret_tool` (calls `ramen_runtime.secrets.resolve`).
- `fixtures/proto.schema.json` — JSON Schema 2020-12 for `<name>.json`; runtime-py may load it directly.
