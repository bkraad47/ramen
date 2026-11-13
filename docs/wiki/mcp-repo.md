# The MCP repo: structure and local development

A group's tools live in a plain git repo. You write Python and one JSON file per package. No server code, no MCP
SDK. The [demo repo](https://github.com/bkraad47/ramen-demo-mcp-group) is the smallest complete example and the
one every guide deploys.

## The layout

```
mcp/
  requirements.txt                   pip requirements the worker installs on load
  env.yaml                           environment for your code, rendered from the group's secrets
  tests.yaml                         golden tools/call cases, run on the canary before stable takes the code (0.7.0)
  guardrails.yaml                    which tools get a pre/post check and with which engine (0.7.5)
  guardrails/                        the NeMo rails config (config.yml, *.co, actions.py) or your policy.py
  tools/<name>/<name>.py             the callable
  tools/<name>/<name>.json           the contract: type, name, description, callable, input, output, error
  tools/<name>/utils/                optional helpers; this folder is on sys.path, so `import calculator_utils` works
  resources/<name>/<name>.py+.json   the same shape, plus uri and mime_type
  prompts/<name>/<name>.json         plus SKILL.md and settings.json, or a <name>.py callable (1.0)
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

`output` is a JSON Schema and becomes the MCP `outputSchema`; the worker validates every result against it.
A scalar schema such as `{"type": "number"}` is published as an object with one `result` property and the value
comes back as `structuredContent: {"result": 5}` next to the text; an object schema is used as declared and the
returned dict is the `structuredContent`. A result that does not match is a tool error, `invalid output: <path>:
<message>`, so a tool cannot quietly change shape under a client. Tools without `output` behave as before.

Write the description for a model, not a human: what the tool returns, what fails and how (division by zero, an
unknown unit), whether it has side effects, and when to use it or not. Scorers such as Glama's Tool Definition
Quality Score read exactly that, and so does the agent choosing between tools.

## Golden cases

`mcp/tests.yaml` turns your tests into a deploy gate. The console runs every case against the canary with
the group's first MCP key before the stable workers take the new code; one failing case aborts the deploy and the
job log names it and the difference.

```yaml title="mcp/tests.yaml"
cases:
  - name: add
    tool: demo_calculator_tool
    args: {var1: 40, var2: 2, func: add}
    expect: {text_contains: "42"}
  - name: word count
    tool: word_count
    args: {text: "ramen is a bowl of noodles"}
    expect: {subset: {structuredContent: {words: 6}}}
```

`expect` is one of `text_contains` (a substring of the result text), `subset` (part of the `tools/call` result,
matched field by field) or `exact` (the whole result). `args` may reference the group's secrets as
`{{$group.VAR}}`; the console resolves them before the call. A JSON-RPC error counts as a failure. A group without
an MCP key skips the cases with a warning; a malformed file fails the deploy before any zone changes.

**A tool that asks for input** (1.0, [multi round-trip requests](mrtr.md)) gets two more forms, both from the demo
repo:

```yaml title="mcp/tests.yaml"
  - name: confirm_delete_note asks before it deletes
    tool: confirm_delete_note
    args: {note: todo}
    expect: {input_required: [confirm]}
  - name: confirm_delete_note deletes when the user confirms
    tool: confirm_delete_note
    args: {note: todo}
    respond: {confirm: {action: accept, content: {confirm: true}}}
    expect: {text_contains: 'Deleted the note "todo"'}
```

`expect: {input_required: [<keys>]}` passes when the tool's first answer asks for exactly those keys. `respond:
{<key>: <answer>}` makes the case a two-step call: when the tool asks, the console calls again with these answers
(and the `requestState`, if there was one), and `expect` applies to the final result. The console makes such a case
as a 2026-07-28 client that declares elicitation (form and URL), sampling and roots. It retries once: a tool that
asks again after the answers fails the case, and so does a tool that asks when the case has no `respond`. Every
other case is sent exactly as before.

## Guardrails

`mcp/guardrails.yaml` opts tools into a check before the call and after the result, run in the runtime next to the
tool: NeMo Guardrails rails or a `policy.py` of your own, fail-closed by default. Golden cases run through them.
The whole story, with the demo repo's deterministic rails, is on [Guardrails](guardrails.md).

## Resources and prompts

A **resource** is a tool with a `uri` and a `mime_type` whose callable returns the content. The demo's
`demo_readme` returns the group's README as `text/markdown`.

A **prompt** needs no code. Its JSON names a `skill` and a `settings` file instead of a `callable`, each
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

## New in 1.0: what a package can do

Everything in this section is optional. A repo written for 0.8 loads unchanged and keeps its manifest hash (one
exception: a prompt whose argument already declared an `enum` now lists it, which changes that hash once). The
helpers come from the runtime that runs your code, so there is nothing to add to `requirements.txt`:

```python
from ramen_runtime import (text, image, audio, resource,                       # content
                           InputRequired, elicit, elicit_url, sample, list_roots,  # asking the client
                           input_responses, request_state, client_capabilities,
                           MissingCapability, progress)
```

### Content blocks

A tool may return one block or a list of blocks instead of a JSON value:

```python title="mcp/tools/test_multiple_content_types/test_multiple_content_types.py"
from ramen_runtime import image, resource, text

def run():
    return [
        text("Multiple content types test:"),
        image(_png(), "image/png"),               # bytes in, base64 on the wire
        resource("test://mixed-content-resource", text='{"test":"data","value":123}', mime="application/json"),
    ]
```

`image(data, mime)` and `audio(data, mime)` take `bytes`. `resource(uri, text=… or blob=…, mime="text/plain")`
takes exactly one of `text` and `blob`. The blocks go into the result's `content` as given. A block result has no
`structuredContent`, and a declared `output` schema is not applied to it. Only what these builders return counts as
a block: a dict you write by hand, `{"type": "text", …}`, is still JSON-encoded into the result text as in 0.8.

### A binary resource

A resource callable that returns `bytes` is served as a base64 `blob` instead of `text`:

```json title="mcp/resources/bowl_logo/bowl_logo.json"
{"type": "resource", "name": "bowl_logo", "description": "A binary resource: eight bytes served as a base64 blob.",
 "callable": "read", "input": {}, "uri": "ramen://bowl/logo", "mime_type": "application/octet-stream"}
```
```python title="mcp/resources/bowl_logo/bowl_logo.py"
def read() -> bytes:
    return bytes(range(8))
```

### A resource template

A `uri` that contains `{var}` is a template. It is listed by `resources/templates/list` (as `uriTemplate`) and not
by `resources/list`, and a `resources/read` of any matching URI calls the function with the variables as keyword
strings:

```json title="mcp/resources/bowl_recipe/bowl_recipe.json"
{"type": "resource", "name": "bowl_recipe", "description": "A templated resource: the recipe for one flavour.",
 "callable": "read", "uri": "ramen://bowl/{flavour}/recipe", "mime_type": "text/plain",
 "input": {"flavour": {"enum": ["shoyu", "shio", "miso", "tonkotsu"], "description": "which bowl"}}}
```
```python title="mcp/resources/bowl_recipe/bowl_recipe.py"
def read(flavour: str) -> str:
    return f"recipe for {flavour}"
```

The rules: the variables in the `uri` must be exactly the parameters in `input`, each a `string` or an `enum`; a
variable stands for one path segment (it never matches a `/`) and its value is passed as it appears in the URI; a
value outside a declared `enum` is "resource not found"; a resource with that exact URI wins over a template. An
`enum` is also what `completion/complete` suggests for the variable. Blocking the template by name on the group
page blocks every URI it matches.

### A callable prompt

A prompt may name a Python `callable` instead of a `skill` file (exactly one of the two):

```json title="mcp/prompts/pick_prompt/pick_prompt.json"
{"type": "prompt", "name": "pick_prompt", "description": "A callable prompt: order a bowl of the given flavour and size.",
 "callable": "build",
 "input": {"flavour": {"enum": ["shoyu", "shio", "miso", "tonkotsu"], "description": "which bowl"},
           "size": {"type": "string", "description": "small, regular or large"}}}
```
```python title="mcp/prompts/pick_prompt/pick_prompt.py"
def build(flavour: str, size: str) -> str:
    return f"Order one {size} bowl of {flavour} ramen."
```

The function gets the declared arguments as keywords and returns a string, one content block, or a list whose items
are strings, blocks, or `{"role": …, "content": …}` messages. Anything without a role is a `user` message. Arguments
reach a callable prompt as the client wrote them: a `{{$group.VAR}}` reference is **not** resolved, because a
prompt's answer goes back to the client. An argument's `enum` is listed in `prompts/list` and is what
`completion/complete` suggests.

### A parameter mirrored in a header

Add `"header": "<Name>"` to a tool parameter and a 2026-07-28 HTTP client sends that argument in an
`Mcp-Param-<Name>` request header as well, where a load balancer can read it:

```json title="mcp/tools/route_tool/route_tool.json (abridged)"
{"type": "tool", "name": "route_tool", "callable": "run",
 "input": {
   "region": {"type": "string", "description": "mirrored as Mcp-Param-Region", "header": "Region"},
   "count":  {"type": "integer", "description": "mirrored as Mcp-Param-Count", "header": "Count"},
   "note":   {"type": "string", "description": "body only"}}}
```

The tool's code does not change; it still receives the argument from the body. The published `inputSchema` marks
the property with `"x-mcp-header": "Region"`, which is how a client knows to send the header, and the worker
refuses a call whose header disagrees with the body ([Transport → Standard headers](transport.md#standard-headers)).

Only tool parameters of type `string`, `integer`, `boolean` or an `enum` can have a header (not `number`). The name
must be a plain HTTP token and unique within the tool, ignoring case; otherwise the package fails to load. Adding,
removing or renaming a `header` is a **breaking change** for the deploy's
[compatibility gate](groups-zones.md), because a client holding the old schema would send the wrong headers.

### Asking the client for input

Raise `InputRequired` to ask the client for something and be called again with the answer. The demo repo's
`confirm_delete_note`:

```python title="mcp/tools/confirm_delete_note/confirm_delete_note.py (abridged)"
from ramen_runtime import InputRequired, elicit, input_responses

CONFIRM = {
    "type": "object",
    "properties": {"confirm": {"type": "boolean", "title": "Delete it?", "description": "true deletes the note"}},
    "required": ["confirm"],
}

def confirm_delete(note: str) -> str:
    answer = input_responses().get("confirm")
    if answer is None:
        raise InputRequired({"confirm": elicit(f'Delete the note "{note}"? This cannot be undone.', CONFIRM)})
    confirmed = answer.get("action") == "accept" and (answer.get("content") or {}).get("confirm") is True
    ...
```

The dictionary's keys are yours; the client answers under the same keys. The function runs again from the top on
the retry, so ask before doing anything with a side effect. The whole flow, the other three kinds of request
(`elicit_url`, `sample`, `list_roots`), carrying `state` between rounds and what an older client sees are on
[Multi round-trip requests](mrtr.md).

**A form may not ask for a secret.** `elicit()` is refused when a property of its schema is named like a
credential, and the call answers the tool error `input request refused: <key>.<field> looks like a secret; use
elicit_url`. The check reads the property names of the form, ignoring case:

- refused when the name **contains** `password`, `passwd`, `secret`, `token`, `api_key` (also `api-key`, `apikey`),
  `credential` or `cvv`: so `accessToken` and `db_password` are refused;
- refused when `card` or `ssn` is a **whole word** of the name: `card_number` and `cardNumber` are refused,
  `discard` is not.

The refusal is an ordinary tool error, so a golden case that expects the tool to ask (or to succeed) fails on it and
the mistake stops at the canary.

### Saying a client cannot be served

Raise `MissingCapability` with the capabilities the tool needs; a 2026-07-28 client gets error `-32021` naming
them. `client_capabilities()` returns what this request declared:

```python title="mcp/tools/needs_sampling/needs_sampling.py"
from ramen_runtime import MissingCapability, client_capabilities, text

def run():
    if "sampling" not in client_capabilities():
        raise MissingCapability({"sampling": {}})
    return text("has sampling")
```

### Progress

```python title="mcp/tools/progress_tool/progress_tool.py"
import time
from ramen_runtime import progress, text

def run(steps: int):
    for n in range(1, int(steps) + 1):
        progress(n, total=int(steps), message=f"step {n}")
        time.sleep(0.02)
    return text(f"done {steps}")
```

`progress(progress, total=None, message=None)` does nothing unless the client asked for progress and can receive
it ([Transport → Progress](transport.md#progress)), so it is safe to call always.

### A long-running tool

`"task": true` in a tool's JSON lets a client that speaks the MCP tasks extension get a task id at once and poll
for the result; every other client waits for the answer as before. The demo repo's `slow_report`:

```json title="mcp/tools/slow_report/slow_report.json (abridged)"
{"type": "tool", "name": "slow_report", "callable": "report", "task": true,
 "input": {"seconds": {"type": "integer", "description": "how long the report takes, in seconds"}},
 "output": {"type": "string"}}
```

Lifecycle, limits and the Redis the zone needs for more than one worker: [Tasks](tasks.md).

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
3. Push, deploy again, or let [GitHub Actions deploy on push](deploy-github-actions.md). The group page lists what
   each zone reported, with each tool's schema. The Logs page tails the worker.

## Heavy dependencies

The worker pip-installs `mcp/requirements.txt` on the first load of a new ref. Pin the versions. If the install
fails, the deploy stops at the canary with pip's output in the job log, and the previous version keeps serving.

For heavy stacks such as numpy, pandas or a CUDA wheel, put the dependencies in the image instead. Since 1.0 a
cloud deployment with a builder does that on deploy, from the same `requirements.txt`; or build an image yourself
and pin it on the group. Both are on [Worker images](worker-images.md).
