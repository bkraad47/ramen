# LLM configs from the group repo (1.0.0c Phase 3)

A group can ship the configuration its LLM clients should start from — a Claude Code `settings.json` fragment, a
`CLAUDE.md` block, a Cursor rule, a Codex or Gemini CLI table, an `env` file — in `mcp/llm-config/` of its repo. Since
1.0.0c Phase 3 the console validates and versions that directory on every deploy, every person who signs in to the
group reads the files and accepts them once per version before any token or key is handed out, the bridge writes
them onto the person's machine, and the workers refuse calls that do not carry the accepted version. The same release
adds an OTLP ingest on the console so a client's own telemetry reaches the group's collector through Ramen.

Contract: [§31](../CONTRACTS.md). A repo without the directory is unaffected: no `configs` key anywhere, nothing gated.

## The directory

```text
mcp/llm-config/
  manifest.yaml                 version: 1, description, targets
  claude-code.settings.json     a fragment per target, named by the manifest
  CLAUDE.md
  cursor.rules.mdc
  …
```

```yaml title="mcp/llm-config/manifest.yaml"
version: 1
description: How this group's LLM clients should use its tools.
targets:
  - {client: claude-code, file: claude-code.settings.json, path: ~/.claude/settings.json, format: json, merge: deep}
  - {client: claude-code, file: CLAUDE.md, path: ~/.claude/CLAUDE.md, format: text, merge: block}
  - {client: cursor, file: cursor.rules.mdc, path: ~/.cursor/rules/ramen-{group}.mdc, format: text, merge: replace}
  - {client: codex, file: codex.config.toml, path: ~/.codex/config.toml, format: toml, merge: deep}
  - {client: env, file: ramen.env, path: ~/.ramen/env.sh, format: env, merge: keys}
```

| Field | Means |
|---|---|
| `client` | A free label (`claude-code`, `cursor`, `vscode`, `codex`, `gemini-cli`, `env`, anything). |
| `file` | The fragment in the same directory. |
| `path` | Where it lands on the person's machine; `~`, `{group}`, `{zone}` and `{host}` are expanded. |
| `format` | `json`, `toml`, `yaml`, `text` or `env`. |
| `merge` | `deep` (json/toml/yaml: Ramen's keys win, every other key is left alone), `block` (text: one `# ramen:<group>` … `# /ramen` block, replaced in place), `replace` (the whole file is Ramen's), `keys` (env: `KEY=value` lines replaced by key). |

A fragment may use `${RAMEN_GROUP}`, `${RAMEN_HOST}`, `${RAMEN_USER}`, `${RAMEN_OTLP_ENDPOINT}` and
`${RAMEN_OTLP_HEADERS}`; the bridge fills them in when it writes the file, so the person's token is never in the repo
nor in the console's answer in clear.

### What a fragment may and may not say

The console checks every file at sync; a violation **fails the deploy** with the key named (`mcp/llm-config:
claude-code.settings.json: key 'apiKeyHelper' is not allowed (only env, hooks, model, permissions)`).

| Fragment | Allowed | Never |
|---|---|---|
| Claude Code `settings.json` | `hooks`, `env`, `permissions`, `model`, telemetry env (`OTEL_*`, `CLAUDE_CODE_ENABLE_TELEMETRY`) | `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `forceLoginMethod`, `otelHeadersHelper`, any hook command that does not start with `ramen-mcp-bridge` |
| Codex, Gemini CLI, any other | tables and keys of the tool's own config | a key matching `*_API_KEY*` or `*_TOKEN*`, a `path` outside `~` |
| `text` | anything | more than 32 KiB |

The whole directory is at most 256 KiB. Its **version** is the first 12 hex digits of a SHA-256 over every file (sorted)
plus the manifest, so any edit is a new version. The demo repo ships one:
[`mcp/llm-config/`](https://github.com/bkraad47/ramen-demo-mcp-group/tree/main/mcp/llm-config).

## Acceptance, on every sign-in path

Acceptance is **per person and per version**, recorded as `users/<id>.accepted_configs = {<group>: {version, at}}`
and audited as `configs.accept`. Every way into the group reaches it:

- **OAuth** (Claude Code over HTTP, the bridge, any client): the consent page shows the manifest and every file
  verbatim with an Accept control, as it shows the telemetry notice today.
- **Password or magic link**: a sign-in that lands on a group with unaccepted configs is redirected to
  `/groups/<g>/configs` — the same files, the same Accept — before anything else.
- **The API**: `POST /api/v1/groups/{g}/configs/accept {"version": "<v>"}` (any member).
- **A group key** counts as accepted by the admin who minted it (`mcp_keys[].accepted_configs`); a key whose version
  is behind the repo's is shown stale on the group page.

Until accepted, `POST /oauth/token` and MCP-key minting for that person answer `403 configs not accepted`
(`data.version` names the current one). A new version invalidates every acceptance; the next sign-in asks again, and
the worker gate makes "next sign-in" now. The group page lists acceptance per member (group admins).

The token answer gains

```json
"configs": {"version": "4b1e9c0a7d22", "required": true, "accepted": true,
            "manifest": "<manifest.yaml>", "files": {"claude-code.settings.json": "…", "CLAUDE.md": "…"}}
```

and the public `GET /.well-known/ramen-policy?group=&zone=` answers `configs: {version, required}` — version and
flag only, never a file.

## The worker gate

Workers learn `RAMEN_CONFIGS_VERSION` and `RAMEN_CONFIGS_REQUIRED` (`1` whenever the repo has the directory) with the
deploy secret and by the same config push that carries a telemetry change. A gated call (`tools/call`, `prompts/get`,
`resources/read`) must carry `params._meta["io.ramen/configs"] = {"version": "<v>"}` (a 2025-06-18 body through the
bridge: `params.configs`). Missing → `-32028 configs required` (HTTP 428, `data.version`); another version → `-32028
configs changed: sign in again`. It runs after the [SkillSpector gate](skillspector.md#client-policy-the-bridges-local-scan);
lists and `server/discover` stay open. Access-log reasons `configs_required` and `configs_stale`. The gate holds for
every client, whatever sent the call.

## What the bridge writes (bridge 0.6.0)

Bridge 0.6.0 is not on PyPI yet; the published 0.3.0 writes none of this. At sign-in, when the token answer says
`accepted: false`, the bridge opens `/groups/<g>/configs?next=…` in the
browser and waits, as it does for the OAuth consent. Then it writes **every target** as its `merge` says, records each
in `~/.ramen/clients.json` (path, format, SHA-256 of the Ramen block or keys, version), and carries the version on
every call; on `-32028` it signs in again once.

**Telemetry targets** are built in, not read from the repo, and written when the group requires telemetry
([Telemetry](telemetry.md#client-telemetry-through-the-console-otlp)):

| Where | What |
|---|---|
| `~/.claude/settings.json` → `env` | `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp`, `OTEL_TRACES_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_EXPORTER_OTLP_ENDPOINT=<console>/otlp`, `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <user token>`, `OTEL_RESOURCE_ATTRIBUTES=ramen.group=<g>,ramen.user=<email>,ramen.host=<host>` |
| `~/.ramen/env.sh` (`format: env`) | the same `OTEL_*` lines, for every tool that honours the OpenTelemetry SDK environment — Codex, Gemini CLI, the Python and Node SDKs, harnesses |
| `~/.zshrc` / `~/.bashrc` | one `source ~/.ramen/env.sh` block (`merge: block`; skipped with `--no-shell-rc`) |
| `~/.codex/config.toml` | `[otel] exporter = "otlp-http"` with the same endpoint and headers, when Codex is installed |

`ramen-mcp-bridge policy show` lists every target and its state; `policy check` — the Claude Code `SessionStart` hook,
the Cursor rule, VS Code — verifies that every recorded target still holds its block or keys (hash compare) and that
the telemetry env is present where written. Anything missing is **exit 3**, `Ramen configs removed or changed:
reconnect (sign in) — Ramen cannot be used without them`, and the bridge refuses to forward calls while the check
fails, the same path as a missing bundle. `policy remove` reverts every write; where Ramen owned a block the file
is restored byte for byte.

## The OTLP ingest

`POST <console>/otlp/v1/traces`, `/otlp/v1/metrics`, `/otlp/v1/logs` take OTLP/HTTP — protobuf or JSON, gzip accepted,
at most 4 MiB — with a bearer that is the person's token (OAuth access token or console session) or an MCP key. The
console stamps the resource attributes `ramen.group`, `ramen.user` (the email, or `key:<name>`), `ramen.zone` when
known, `openinference.project.name = <group>` (so Phoenix shows one project per group) and `service.namespace =
ramen/<group>`, and forwards the request to **Config → Telemetry**'s `endpoint` with its headers: one upstream call
per request, 10 s, an upstream error answered as `502` with the upstream status, `204` while telemetry is off. Sixty
requests per minute per token, then `429`. Nothing is stored on the console; one audit line per 1000 requests per
token (`telemetry.ingest`). The Config page shows the ingest URL; this is the endpoint every pushed telemetry setting
points at, while [`client_endpoint`](telemetry.md#what-clients-are-told) stays for clients that already export directly.

## The honest limit

Ramen enforces where Ramen is. The worker refuses a call without the accepted version, whatever client sent it; the
bridge writes the files, checks them on every session start and refuses to forward when they are gone; the consent
and configs pages make sure every person has read and accepted them. A client that reads none of the files and
never calls a Ramen worker cannot be forced — the configs make the group's policy present in every editor Ramen
knows about, they cannot reach a tool that ignores them.

## What is verified

- `tests/conformance/test_console_configs.py` — a real console wired to one worker, with a group repo the test owns:
  the version and the token answer, the policy endpoint, acceptance (403 until accepted, a new version, a group key,
  outsiders, the audit), the configs and consent pages rendering every file verbatim, a password sign-in landing on
  the configs page, forbidden fragments failing the deploy with the key named, the ingest against a fake collector
  (JSON and protobuf, stamping, gzip, 401 / 413 / 429 / 502 / 204).
- `tests/cloud/test_release_features.py` (`RAMEN_CONFIGS=1`) — on a cluster running the demo repo: the public policy
  names the version, the analyst accepts and the token answer carries the files, a trace with the group key reaches
  the ingest, the Config page shows the URL.
- `scripts/cloud_smoke.sh` prints `configs: <version|none>` after the deploy and accepts before it mints its key.
- The node's `-32028` gate and the bridge's merges (every `format` and `merge` in a temporary home, idempotent, the
  person's own keys untouched, `policy check` on a removed block, `policy remove` byte for byte) are covered by their
  own unit tests.
