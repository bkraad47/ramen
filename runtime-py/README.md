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
- GCS (§7): when `RAMEN_BUCKET_URI=gs://<bucket>/<prefix>` is set, `runtime.load` first runs
  `ramen_runtime.bucket.sync(uri, dest)` (md5-based, deletes stale files, keeps `.ramen*`/`__pycache__`) and adds a
  `sync: {uri,downloaded,unchanged,deleted,total}` summary to the load result. Needs the `gcp` extra
  (`google-cloud-storage`, ADC / Workload Identity).
- `deps.install(bucket)` uses `sys.executable -m pip`; skipped when the requirements hash is unchanged
  (`mcp/.ramen_requirements.sha256`).

## Test
`uv sync --all-extras && uv run pytest --cov` (gate 90%; currently 98%). Fixture: `tests/fixtures/demo` is a copy
of [ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group).
