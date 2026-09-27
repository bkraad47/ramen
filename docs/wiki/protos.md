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

## Validate before you push
`tests/fixtures/proto.schema.json` in the Ramen repo is the JSON Schema for `<name>.json`; the runtime's tests
load `tests/fixtures/broken_group` to show every rejection case.
