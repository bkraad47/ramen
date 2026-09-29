# Protos — the group repo contract

A group repo is any git repo with this layout ([contract §1](../CONTRACTS.md); live example:
[ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group)):

```
mcp/
  requirements.txt                        pip requirements installed by the worker
  tools/<name>/<name>.py                  callable code
  tools/<name>/<name>.json                proto
  tools/<name>/utils/<name>_utils.py      optional; utils/ is importable
  resources/<name>/<name>.py + .json      same shape, plus uri and mime_type
  prompts/<name>/<name>.json              + SKILL.md (agent-skills template) + settings.json
```

Rules: folder name == json `name` == file stem; `type` must match the folder; `type ∈ {tool, resource, prompt}`;
`callable` must exist in `<name>.py`. A package that breaks a rule is reported in the deploy job's `errors` list
and the rest still load.

## Tool
```json
{
  "type": "tool",
  "name": "demo_calculator_tool",
  "description": "Add, subtract, multiply or divide two numbers.",
  "callable": "calculator_func",
  "input": {
    "var1": {"type": "number", "description": "first operand"},
    "var2": {"type": "number", "description": "second operand"},
    "func": {"enum": ["add", "subtract", "multiply", "divide"], "description": "operation"}
  },
  "output": {"type": "number"},
  "error": {"type": "string"}
}
```
```python
# mcp/tools/demo_calculator_tool/demo_calculator_tool.py
from demo_calculator_tool_utils import apply   # utils/ is on sys.path

def calculator_func(var1, var2, func):
    return apply(func, var1, var2)
```
`input` becomes the MCP `inputSchema` (`number | string | integer | boolean`, `enum` → string enum; all params
required, no extras). Return values that are not strings are JSON-encoded. Exceptions become
`isError: true` with `ExceptionType: message` and no traceback.

## Resource
Adds `"uri": "demo://readme"` and `"mime_type": "text/markdown"`; the callable returns the text.

## Prompt
No `callable`. `"skill": "SKILL.md"`, `"settings": "settings.json"`; `input` params become prompt arguments and
`{{param}}` in `SKILL.md` is substituted. Front matter is stripped; `settings.json` is appended as a
`## Settings` block and exposed as `_meta.settings` in `prompts/list`.

## Secrets in code
Any string argument containing `{{$<group>.<NAME>}}` is replaced by the runtime before the call from
`RAMEN_SECRET_<GROUP>__<NAME>`; code can also call `ramen_runtime.secrets.resolve(text)`. Values are redacted
from errors and never logged. See [Secrets](../how-tos/secrets.md).

## Transport: JSON-RPC 2.0 over gRPC
How a call reaches a package is a separate contract ([§11](../CONTRACTS.md), `proto/ramen/v1/mcp.proto`): the
MCP messages your tool sees are the standard ones, but since 0.3.1 they travel as the `bytes body` of one
`ramen.v1.Mcp/Call` per request (a notification returns an empty body) rather than over HTTP.

```proto
service Mcp {
  rpc Call(JsonRpc) returns (JsonRpc);                 // one JSON-RPC 2.0 message in, its response out
  rpc Session(stream JsonRpc) returns (stream JsonRpc); // reserved; may return UNIMPLEMENTED
}
message JsonRpc { bytes body = 1; }                    // UTF-8 JSON-RPC 2.0 request or response
```

| Metadata | Meaning |
|---|---|
| `authorization: Bearer rmk_…` | the group's MCP key; missing/wrong → gRPC `UNAUTHENTICATED` (empty key set = deny all) |
| `ramen-group`, `ramen-zone` | routing at the load balancer; not an auth signal |
| (client address: the peer, or the `RAMEN_TRUST_PROXY_HOPS`-th `x-forwarded-for` entry counted from the right) | must match `RAMEN_ALLOWED_CIDRS` → else `PERMISSION_DENIED` |

Every guard behind that table — the constant-time key compare, the source-range allowlist, where TLS starts and
stops, per-zone identity — is written out hop by hop in
[Transport and what secures each hop](transport.md).

Transport failures are gRPC statuses; protocol failures stay JSON-RPC errors in the body (`-32601` for a blocked or
unknown tool, `-32602` for bad arguments, `isError: true` for an exception in your code). Messages are capped at
4 MiB; `RAMEN_MAX_INFLIGHT` overflows answer `RESOURCE_EXHAUSTED`. `grpc.health.v1.Health/Check` on the same port
is `SERVING` once your packages loaded. Standard MCP clients do not see any of this: `ramen-mcp-bridge` turns the
worker into an ordinary stdio server ([local quickstart](../how-tos/local-quickstart.md#4-connect-a-client-claude-desktop-cursor-the-mcp-sdk)).

## Validate before you push
`tests/fixtures/proto.schema.json` in the Ramen repo is the JSON Schema for `<name>.json`; the runtime's tests
load `tests/fixtures/broken_group` to show every rejection case.
