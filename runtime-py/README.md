# ramen_runtime (runtime-py)

Python 3.14 worker runtime. Loads a group's `mcp/` packages from a bucket directory, validates each proto
against the §1 contract, and executes them over newline-delimited JSON-RPC 2.0 on stdin/stdout for the Rust
node (`docs/CONTRACTS.md` §2). Logs are JSON lines on stderr.

```
python -m ramen_runtime --bucket <dir>   # <dir> contains mcp/{tools,resources,prompts}
                        [--load]         # run runtime.load before serving
```

## Methods
`runtime.load {bucket}` (also pip-installs `mcp/requirements.txt` when it changed) → `{tools,resources,prompts,errors}`;
`runtime.call_tool {name,arguments}` → `{content:[{type:"text",text}], isError}`; `runtime.read_resource {uri}`;
`runtime.get_prompt {name,arguments}`; `runtime.ping`; `runtime.shutdown`.
Errors: `-32002` not loaded, `-32004` unknown package, `-32602` bad params, `-32601`, `-32700`, `-32603`.

## Behaviour
- Per-package validation (folder == json `name` == file stem, `type` matches folder, callable exists, JSON Schema
  for the proto); a bad package lands in `errors[]`, the rest still load. `utils/` is added to `sys.path`.
- `inputSchema`: `number|string|integer|boolean`, `enum` → string enum; all params required, no extras.
- Arguments are validated against `inputSchema`; `{{$group.VAR}}` in string arguments (nested too) is replaced
  from `RAMEN_SECRET_<GROUP>__<VAR>`; code may call `ramen_runtime.secrets.resolve(text)`. Secret values are
  redacted from error messages and never logged.
- Tool exceptions → `isError: true` with `ExceptionType: message` only (no traceback). Non-string results are JSON.
- Prompts: SKILL.md front matter stripped, `{{param}}` substituted, `settings.json` appended as a `## Settings`
  block; `prompts/list` entries carry `_meta.settings`.
- Bucket sync (§7 GCS, §8 S3): when `RAMEN_BUCKET_URI=gs://<bucket>/<prefix>` (extra `gcp`) or `s3://<bucket>/<prefix>` (extra `aws`, IRSA) is set, `runtime.load` first runs
  `ramen_runtime.bucket.sync(uri, dest)` (md5-based, deletes stale files, keeps `.ramen*`/`__pycache__`) and adds a
  `sync: {uri,downloaded,unchanged,deleted,total}` summary to the load result. Needs the `gcp` extra
  (`google-cloud-storage`, ADC / Workload Identity).
- `deps.install(bucket)` uses `sys.executable -m pip`; skipped when the requirements hash is unchanged
  (`mcp/.ramen_requirements.sha256`).

## ramen-mcp-bridge (CONTRACTS §11)
Workers speak JSON-RPC over gRPC (`ramen.v1.Mcp/Call`). Standard MCP clients (Claude Desktop, Cursor, the `mcp`
SDK) connect through this stdio bridge, installed by the `grpc` extra (`pip install 'ramen-runtime[grpc]'`, also in
the worker image at `/opt/venv/bin/ramen-mcp-bridge`):
```sh
ramen-mcp-bridge --target <host:port> --key rmk_… --group <g> --zone <z> [--tls|--insecure] [--ca ca.pem] [--timeout 120]
ramen-mcp-bridge --target <host:port> --health [ramen.v1.Admin]     # grpc.health.v1 probe: exit 0 when SERVING
```
Every flag has an env fallback `RAMEN_BRIDGE_TARGET|KEY|GROUP|ZONE|TLS|CA|TIMEOUT`; prefer `RAMEN_BRIDGE_KEY` to
`--key` on a shared machine, since `ps` shows every other user your command line. Input is newline-delimited JSON-RPC
(what the MCP stdio transport uses) or `Content-Length:` framed; replies use the same framing. Each message is sent
as one `Mcp/Call` with metadata `authorization: Bearer <key>`, `ramen-group`, `ramen-zone` (the LB routes on the
last two; omitted when empty). Notifications produce no output. A gRPC status becomes a JSON-RPC error for requests
(`UNAUTHENTICATED` → `-32001`, other statuses → `-32000`, message `<CODE>: <details>`), never for notifications.
Plaintext h2c is the default; `--tls` (or `--ca`) switches to TLS. Two sharp edges: `--insecure` **overrides**
`--tls`/`--ca` rather than conflicting with them, so a stale `--insecure` in an `mcpServers` entry silently keeps
the connection in cleartext; and `--ca` replaces the trust store with that PEM (hostname verification still
applies) rather than pinning a certificate — against a self-signed load-balancer certificate `--tls` alone fails
with `CERTIFICATE_VERIFY_FAILED`, so `--ca` is required in practice. Claude Desktop / Cursor config:
```json
{"mcpServers": {"ramen-demo": {"command": "ramen-mcp-bridge",
  "args": ["--target", "localhost:8080", "--key", "rmk_…", "--group", "demo", "--zone", "local"]}}}
```
Stubs: `src/ramen_proto/ramen/v1/*_pb2*.py` are generated from `../proto` by `./gen_proto.sh` (dev extra,
grpcio-tools); regenerate after any proto change. The same script vendors the package elsewhere (`./gen_proto.sh <src_dir>`).

## Test
`uv sync --all-extras && uv run pytest --cov` (gate 90%; currently 98%). `tests/test_bridge.py` runs the bridge
against an in-process grpcio fake, including the official `mcp` stdio client end to end. Fixture: `tests/fixtures/demo` is a copy
of [ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group).
