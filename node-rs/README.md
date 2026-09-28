# ramen-node (node-rs)

Rust MCP server node: JSON-RPC 2.0 (MCP protocol `2025-06-18`) carried over gRPC as `ramen.v1.Mcp/Call`, bearer
auth (constant-time), CIDR allowlists, structured JSON logs, and a supervised Python sidecar
(`python -m ramen_runtime --bucket $RAMEN_BUCKET`) that runs the group's code. Contract: `docs/CONTRACTS.md` §2, §11
(§3 lists the MCP methods and error codes). Protos: `proto/ramen/v1/{mcp,admin}.proto`, compiled by `build.rs`
with protox (no `protoc` needed).

## Services (one h2c port, `RAMEN_NODE_PORT`, default 8080)
| Service / method | Auth | Purpose |
|---|---|---|
| `ramen.v1.Mcp/Call` | metadata `authorization: Bearer <key>` + `RAMEN_ALLOWED_CIDRS` | one JSON-RPC message in (`body` bytes), its response out; notifications return an empty body. Methods: `initialize`, `notifications/*`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get` |
| `ramen.v1.Mcp/Session` | same | reserved, answers `UNIMPLEMENTED` |
| `ramen.v1.Admin/Reload` | metadata `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` | re-read config, pip install `mcp/requirements.txt`, `runtime.load`; returns the load result as JSON bytes |
| `ramen.v1.Admin/Metrics` | same | `{inflight,total,errors,load,sidecar_alive,loaded_at,packages}` as JSON bytes |
| `grpc.health.v1.Health` | none | `""` and `ramen.v1.Mcp`: `NOT_SERVING` until the first successful `runtime.load`, then `SERVING` (readiness); `ramen.v1.Admin`: always `SERVING` (liveness) |
| `grpc.reflection.v1[alpha].ServerReflection` | none | lets `grpcurl` work without `-proto` |

gRPC status codes: `PERMISSION_DENIED` (CIDR), `UNAUTHENTICATED` (missing/wrong key), `RESOURCE_EXHAUSTED`
(`RAMEN_MAX_INFLIGHT` reached), `OUT_OF_RANGE` (message over 4 MiB, enforced by the codec), `UNIMPLEMENTED`
(`Session`), `INTERNAL` (`Reload` when the load fails). Everything else is `OK` with a JSON-RPC body: `-32700` parse
error, `-32600` method missing, `-32601` unknown method or blocked name, `-32002` not loaded, `-32004` unknown
tool/resource/prompt, `-32602` bad params, `-32603` sidecar failure/timeout.

Routing metadata `ramen-group` / `ramen-zone` is sent by clients and the bridge for the load balancer; the node ignores it.

## Environment
| Var | Default | Notes |
|---|---|---|
| `RAMEN_NODE_PORT` | `8080` | h2c, or TLS with the two vars below |
| `RAMEN_TLS_CERT` / `RAMEN_TLS_KEY` | unset | PEM cert chain + key; both or neither. Otherwise the LB terminates TLS |
| `RAMEN_BUCKET` | `/buckets/default` | group repo root containing `mcp/` |
| `RAMEN_PYTHON` / `RAMEN_PYTHONPATH` | `python3` / unset | interpreter with `ramen_runtime` installed (image: `/opt/venv/bin/python`) |
| `RAMEN_MCP_KEYS` | empty = deny all | comma list; union of env, `RAMEN_CONFIG` and the deploy file; compared in constant time |
| `RAMEN_ALLOWED_CIDRS` | `0.0.0.0/0,::/0` | `Mcp/*` allowlist (peer address) |
| `RAMEN_ADMIN_CIDRS` | `0.0.0.0/0,::/0` | `Admin/*` allowlist, independent of the MCP lock |
| `RAMEN_TRUST_PROXY` | `0` | `1` → first `x-forwarded-for` hop is the client IP |
| `RAMEN_ADMIN_KEY` | unset = admin disabled | |
| `RAMEN_GROUP` / `RAMEN_ZONE` / `RAMEN_ENV` | `default` / `local` / `default` | log fields |
| `RAMEN_VERBOSE` | `0` | `1` logs full request/response bodies |
| `RAMEN_BLOCKED` | – | comma list of tool/prompt names or resource names/URIs hidden from `*/list` and answered `-32601` on call (console block toggle, CONTRACTS §9) |
| `RAMEN_SIDECAR_IDLE_SECS` | `300` | kill sidecar after idle; respawn (and re-load) on demand |
| `RAMEN_MAX_INFLIGHT` | `32` | concurrency bound; `load` = low <30%, high >80% |
| `RAMEN_CALL_TIMEOUT_SECS` | `120` | per sidecar call; timeout kills the sidecar |
| `RAMEN_LOAD_RETRY_SECS` | `5` | retry the initial bucket load this often (backoff to 60s) until it succeeds; `0` waits for `Admin/Reload` instead |
| `RAMEN_BUCKET_URI` | unset | `gs://bucket/prefix` or `s3://bucket/prefix`; passed to the sidecar, which syncs it into `RAMEN_BUCKET` on every load; `Admin/Reload` then carries a `sync` summary |
| `RAMEN_LOG_FILE` | unset | mirror JSON log lines (node + sidecar stderr) to this file |
| `RAMEN_CONFIG` | unset | flat `key: value` yaml with the same names (env wins) |

Precedence: `RAMEN_CONFIG` < process env < `<bucket>/.ramen/env-<zone>` (or `.ramen/env`), the file the
console writes on deploy. Only deploy-scoped keys are read from that file (`RAMEN_MCP_KEYS`, `RAMEN_ALLOWED_CIDRS`,
`RAMEN_ENV`, `RAMEN_VERBOSE`, `RAMEN_BLOCKED`, idle/inflight/timeout, `RAMEN_LOG_FILE`); MCP keys are unioned so console-minted
`rmk_` keys work alongside static ones. The file can never change bucket, port, python, admin key, TLS or proxy trust.

Log line per call (denied calls included): `ts, ip, group, zone, env, method, name, status, grpc_code, ms, key_id`
(`key_id` is a non-secret hash; `status` is `ok`, `error` or `denied`).

## Run
```sh
RAMEN_BUCKET=/path/to/group RAMEN_MCP_KEYS=k1 RAMEN_ADMIN_KEY=adm \
RAMEN_PYTHON=../runtime-py/.venv/bin/python cargo run --release
```
With `grpcurl` (bytes fields are base64 in its JSON):
```sh
grpcurl -plaintext localhost:8080 list                                   # reflection
grpcurl -plaintext localhost:8080 grpc.health.v1.Health/Check            # {"status":"SERVING"} once loaded
BODY=$(printf '%s' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' | base64)
grpcurl -plaintext -H 'authorization: Bearer k1' -d "{\"body\":\"$BODY\"}" localhost:8080 ramen.v1.Mcp/Call \
  | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["body"]).decode())'
# {"id":1,"jsonrpc":"2.0","result":{"content":[{"text":"5","type":"text"}],"isError":false}}
grpcurl -plaintext -H 'x-ramen-admin-key: adm' localhost:8080 ramen.v1.Admin/Metrics
```
Standard MCP clients use the stdio bridge from `runtime-py`: `ramen-mcp-bridge --target localhost:8080 --key k1 --group demo --zone local`
(see `runtime-py/README.md`). Image (context = repo root): `docker build -f node-rs/Dockerfile -t ramen-worker:$(cat VERSION) .`
→ non-root, `python:3.14-slim` + `ramen_runtime[gcp,aws,grpc]` in `/opt/venv`, port 8080, entrypoint `ramen-node`,
`HEALTHCHECK` = `ramen-mcp-bridge --health ramen.v1.Admin`.

## Test
`cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test`. Integration tests start the server on
a loopback port (tonic client, TLS via a throwaway rcgen cert) and spawn the real runtime using
`../runtime-py/.venv/bin/python` (run `uv sync --all-extras` there first), `RAMEN_TEST_PYTHON`, or `python3`; the
`py_*` tests skip when no interpreter has `ramen_runtime`+`jsonschema`. Coverage: `cargo llvm-cov` (94% regions).
