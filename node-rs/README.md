# ramen-node (node-rs)

Rust MCP server node: MCP Streamable HTTP (JSON-RPC 2.0, protocol `2025-06-18`, JSON responses only) on
`POST /mcp`, bearer auth, CIDR allowlist, structured JSON logs, and a supervised Python sidecar
(`python -m ramen_runtime --bucket $RAMEN_BUCKET`) that runs the group's code. Contract: `docs/CONTRACTS.md` §2, §3.

## Endpoints
| Route | Auth | Purpose |
|---|---|---|
| `POST /mcp` | `Authorization: Bearer <key>` + CIDR | `initialize`, `notifications/initialized` (202), `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get` |
| `GET /healthz` | none | process up |
| `GET /readyz` | none | 200 once `runtime.load` succeeded |
| `GET /metrics` | none | `{inflight,total,errors,load,sidecar_alive,loaded_at,packages}` |
| `POST /admin/reload` | `X-Ramen-Admin-Key` + CIDR | re-read config, pip install `mcp/requirements.txt`, `runtime.load`; returns the load result |

Errors: 401 `-32001` unauthorized, 403 `-32000` ip not allowed, 400 `-32700`/`-32600`, 503 `-32000` busy,
200 with JSON-RPC `error` for everything else (`-32002` not loaded, `-32004` unknown tool/resource/prompt,
`-32602` bad params, `-32603` sidecar failure/timeout).

## Environment
| Var | Default | Notes |
|---|---|---|
| `RAMEN_NODE_PORT` | `8080` | |
| `RAMEN_BUCKET` | `/buckets/default` | group repo root containing `mcp/` |
| `RAMEN_PYTHON` / `RAMEN_PYTHONPATH` | `python3` / unset | interpreter with `ramen_runtime` installed (image: `/opt/venv/bin/python`) |
| `RAMEN_MCP_KEYS` | empty = deny all | comma list; union of env, `RAMEN_CONFIG` and the deploy file |
| `RAMEN_ALLOWED_CIDRS` | `0.0.0.0/0,::/0` | |
| `RAMEN_TRUST_PROXY` | `0` | `1` → first `X-Forwarded-For` hop is the client IP |
| `RAMEN_ADMIN_KEY` | unset = admin disabled | |
| `RAMEN_GROUP` / `RAMEN_ZONE` / `RAMEN_ENV` | `default` / `local` / `default` | log fields |
| `RAMEN_VERBOSE` | `0` | `1` logs full request/response bodies |
| `RAMEN_SIDECAR_IDLE_SECS` | `300` | kill sidecar after idle; respawn (and re-load) on demand |
| `RAMEN_MAX_INFLIGHT` | `32` | concurrency bound; `load` = low <30%, high >80% |
| `RAMEN_CALL_TIMEOUT_SECS` | `120` | per sidecar call; timeout kills the sidecar |
| `RAMEN_BUCKET_URI` | unset | `gs://bucket/prefix`; passed to the sidecar, which syncs it into `RAMEN_BUCKET` on every load; `/admin/reload` then carries a `sync` summary |
| `RAMEN_LOG_FILE` | unset | mirror JSON log lines (node + sidecar stderr) to this file |
| `RAMEN_CONFIG` | unset | flat `key: value` yaml with the same names (env wins) |

Precedence: `RAMEN_CONFIG` < process env < `<bucket>/.ramen/env-<zone>` (or `.ramen/env`), the file the
console writes on deploy. Only deploy-scoped keys are read from that file (`RAMEN_MCP_KEYS`, `RAMEN_ALLOWED_CIDRS`,
`RAMEN_ENV`, `RAMEN_VERBOSE`, idle/inflight/timeout, `RAMEN_LOG_FILE`); MCP keys are unioned so console-minted
`rmk_` keys work alongside static ones.

Log line per call: `ts, ip, group, zone, env, method, name, status, http, ms, key_id` (`key_id` is a non-secret hash).

## Run
```sh
RAMEN_BUCKET=/path/to/group RAMEN_MCP_KEYS=k1 RAMEN_ADMIN_KEY=adm \
RAMEN_PYTHON=../runtime-py/.venv/bin/python cargo run --release
curl -X POST localhost:8080/mcp -H 'Authorization: Bearer k1' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
```
Image (context = repo root): `docker build -f node-rs/Dockerfile -t ramen-worker:$(cat VERSION) .` → non-root,
`python:3.14-slim` + `ramen_runtime` in `/opt/venv`, port 8080, entrypoint `ramen-node`.

## Test
`cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test`. Integration tests spawn the real
runtime using `../runtime-py/.venv/bin/python` (run `uv sync --all-extras` there first), `RAMEN_TEST_PYTHON`, or
`python3`; they skip when no interpreter has `ramen_runtime`+`jsonschema`. Coverage: `cargo llvm-cov`.
