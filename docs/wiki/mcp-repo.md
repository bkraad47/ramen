# The MCP repo: structure and local development

A group's tools live in a plain git repo. You write Python and one JSON file per package. No server code, no MCP
SDK. The [demo repo](https://github.com/bkraad47/ramen-demo-mcp-group) is the smallest complete example and the
one every guide deploys.

## The layout

```
mcp/
  requirements.txt                   pip requirements the worker installs on load
  env.yaml                           environment for your code, rendered from the group's secrets
  tools/<name>/<name>.py             the callable
  tools/<name>/<name>.json           the contract: type, name, description, callable, input, output, error
  tools/<name>/utils/                optional helpers; this folder is on sys.path, so `import calculator_utils` works
  resources/<name>/<name>.py+.json   the same shape, plus uri and mime_type
  prompts/<name>/<name>.json         plus SKILL.md and settings.json
```

Three rules carry everything. The folder name, the `name` in the JSON and the Python file's stem must match.
`type` must match the parent folder. `input` maps each parameter to a JSON schema, and every parameter is required
unless its schema says otherwise. A package that breaks a rule is listed under `errors` in the deploy job and the
others still load.

## A tool

```json title="mcp/tools/demo_calculator_tool/demo_calculator_tool.json"
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

```python title="mcp/tools/demo_calculator_tool/demo_calculator_tool.py"
def calculator_func(var1: float, var2: float, func: str) -> float:
    ops = {"add": var1 + var2, "subtract": var1 - var2, "multiply": var1 * var2, "divide": var1 / var2}
    if func not in ops:
        raise ValueError(f"unknown func {func!r}")
    return ops[func]
```

`input` becomes the MCP `inputSchema`. A return value that is not a string is JSON-encoded into the result text.
An exception becomes `isError: true` with `ExceptionType: message` and no traceback, so raise `ValueError` with a
sentence a model can act on.

## Resources and prompts

A **resource** is a tool with a `uri` and a `mime_type` whose callable returns the content. The demo's
`demo_readme` returns the group's README as `text/markdown`.

A **prompt** has no code at all. Its JSON names a `skill` and a `settings` file instead of a `callable`, each
`input` parameter becomes a prompt argument, and `{{param}}` in the skill is substituted when a client asks for
it. The settings file is appended as a `## Settings` block and exposed as `_meta.settings`:

```json title="mcp/prompts/get_calculation_prompt/get_calculation_prompt.json"
{"type": "prompt", "name": "get_calculation_prompt", "description": "Ask the calculator well.",
 "skill": "SKILL.md", "settings": "settings.json",
 "input": {"request": {"type": "string", "description": "the user's arithmetic question"}}}
```
```markdown title="mcp/prompts/get_calculation_prompt/SKILL.md"
Turn {{request}} into one call to demo_calculator_tool, then state the result in a sentence.
```

Both deploy exactly like tools.

## Secrets in code

Never put a secret in the repo. The console holds it and the worker renders it at runtime, two ways:

- In a **call argument**: `{{$demo.OPENAI_API_KEY}}` inside any string a client passes is substituted before your
  function runs. Code can also call `ramen_runtime.secrets.resolve(text)`.
- In **`mcp/env.yaml`**: flat `KEY: value` lines the worker exports to the runtime process at load, with
  `{{$group.SECRET}}` references rendered. Tool code reads `os.environ["DB_URL"]`.

```yaml title="mcp/env.yaml"
DEMO_MODE: demo
DB_URL: "postgres://app:{{$demo.DB_PASSWORD}}@db.internal/app"
```

A reference with no secret behind it is skipped with a warning. Rendered values are redacted from error messages
and never logged. [Secrets](secrets.md) covers scoping and backends.

Cloud access needs no keys at all. The workers of a zone run as that zone's service account or IAM role, so a
tool that uses `google.cloud.storage` or `boto3` picks that identity up by itself.

## Test it before you push

The runtime is a Python package you can run on the folder. It speaks JSON-RPC on stdin and stdout, so one line
loads the group and one line calls a tool, exactly as the worker would:

```sh
cd ramen/runtime-py && uv sync --all-extras
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"runtime.load","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"runtime.call_tool","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' \
  | RAMEN_SECRET_DEMO__DB_PASSWORD=x uv run python -m ramen_runtime --bucket /path/to/your-repo 2>/dev/null
```

```
load: ['demo_calculator_tool']  errors: []
{"content": [{"type": "text", "text": "5"}], "isError": false}
```

`tests/fixtures/proto.schema.json` in the Ramen repo is the JSON Schema for the `<name>.json` files, for a
pre-commit check.

## The loop

1. `make up` on your laptop. Create a group pointing at your repo, attach zone `local`, deploy.
2. Call it with the key from the group page, with `curl`, the `mcp` SDK or the bridge
   ([Get started](../get-started.md#3-connect-a-client)).
3. Push, deploy again. The group page lists what each zone reported, with each tool's schema. The Logs page tails
   the worker.

## Heavy dependencies

The worker pip-installs `mcp/requirements.txt` on the first load of a new ref. Pin the versions. If the install
fails, the deploy stops at the canary with pip's output in the job log, and the previous version keeps serving. For heavy stacks
such as numpy, pandas or a CUDA wheel, build a worker image for the group and record it on the group page's
**Worker image** card:

```sh
make push-worker PROJECT=<project> REGION=<region>    # tag it yourself if you keep several
```

The image is not per group; the group page is where a group pins which image it runs. The console records and
recalls references, and never builds. The pin reaches the zones on the next deploy,
and **Recall** puts the group back on an older build.
