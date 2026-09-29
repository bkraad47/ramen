# Add and deploy a tool

From an empty folder to a tool an AI client can call, using the demo group
([`ramen-demo-mcp-group`](https://github.com/bkraad47/ramen-demo-mcp-group)) as the worked example. A **group** is
one git repository of tools, resources and prompts; Ramen syncs it into a bucket, a worker's runtime loads it, and
the console deploys it to zones. You write Python and one JSON file per package — no server code, no MCP SDK.

## 1. The layout
```
mcp/
  requirements.txt                      # pip packages your code needs (may be empty)
  tools/<name>/<name>.json              # the contract: name, description, callable, input, output
  tools/<name>/<name>.py                # the code: a module with the callable
  tools/<name>/utils/…                  # optional helpers; utils/ is on sys.path for that tool
  resources/<name>/<name>.json + .py    # like a tool, plus "uri" and "mime_type"
  prompts/<name>/<name>.json + SKILL.md + settings.json
```
The demo group has one of each: `demo_calculator_tool`, `demo_readme`, `get_calculation_prompt`. Fork it or copy
the layout; the full contract is in [Protos](../wiki/protos.md).

## 2. Read the demo tool
`mcp/tools/demo_calculator_tool/demo_calculator_tool.json`:
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
`demo_calculator_tool.py`:
```python
import calculator_utils as cu          # mcp/tools/demo_calculator_tool/utils/calculator_utils.py

def calculator_func(var1: float, var2: float, func: str) -> float:
    ops = {"add": cu.add, "subtract": cu.sub, "multiply": cu.mul, "divide": cu.div}
    if func not in ops:
        raise ValueError(f"unknown func {func!r}; expected one of {sorted(ops)}")
    return ops[func](var1, var2)
```
Three rules carry everything: the folder, the JSON file and the Python file share the **name**; `callable` names a
function in that module; `input` is the MCP `inputSchema` (`number | string | integer | boolean`, `enum` → string
enum; every parameter is required, extras are refused). A non-string return value is JSON-encoded into the
result text; an exception becomes `isError: true` with `ExceptionType: message` and no traceback — so raise
`ValueError` with a sentence a model can act on.

## 3. Add a tool
A word counter, in two files under `mcp/tools/word_count/`:

`word_count.json`
```json
{
  "type": "tool",
  "name": "word_count",
  "description": "Count the words in a text, and how often the most common one appears.",
  "callable": "count",
  "input": {
    "text": {"type": "string", "description": "the text to count"}
  },
  "output": {"type": "string"},
  "error": {"type": "string"}
}
```
`word_count.py`
```python
from collections import Counter

def count(text: str) -> dict:
    words = text.split()
    if not words:
        raise ValueError("text has no words")
    top, n = Counter(w.lower() for w in words).most_common(1)[0]
    return {"words": len(words), "most_common": top, "times": n}
```
Needs a package? Add it to `mcp/requirements.txt`; the worker installs the file on the first deploy of a new ref
(that is the one to three minutes the job log spends in `reload`). Needs a secret? Never in the repo: the console
holds it and the runtime substitutes `{{$demo.API_KEY}}` in string arguments, or your code calls
`ramen_runtime.secrets.resolve(text)` — see [Secrets](secrets.md).

## 4. Check it before you push
The worker's runtime is a Python package you can run on the folder itself. It speaks JSON-RPC on stdin/stdout, so
one line loads the group and one line calls the tool, exactly as the worker would:
```sh
cd ramen/runtime-py            # the Ramen checkout; uv creates the venv
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"runtime.load","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"runtime.call_tool","params":{"name":"word_count","arguments":{"text":"the cat sat on the mat"}}}' \
  '{"jsonrpc":"2.0","id":3,"method":"runtime.call_tool","params":{"name":"word_count","arguments":{"text":"   "}}}' \
  | uv run python -m ramen_runtime --bucket /path/to/your-group 2>/dev/null
```
```
load: ['demo_calculator_tool', 'word_count']  errors: []
{"content": [{"type": "text", "text": "{\"words\": 6, \"most_common\": \"the\", \"times\": 2}"}], "isError": false}
{"content": [{"type": "text", "text": "ValueError: text has no words"}], "isError": true}
```
The first answer is the catalogue the worker will publish (`tools`, `resources`, `prompts`); a package that fails
to load is listed under `errors` with the reason, and the others still load — a broken tool never takes the group
down. `tests/fixtures/proto.schema.json` in the Ramen repo is the JSON Schema for the `<name>.json` files if you
want a pre-commit check.

## 5. Push, then deploy from the console
```sh
git add mcp/tools/word_count && git commit -m "word_count" && git push
```
In the console (local stack or cloud, the pages are the same — [End to end](end-to-end.md) has the screenshots):

1. **Groups → Add group** once: your repo URL and ref. A private repo needs a secret named `GITHUB_TOKEN` on the
   group; the console clones with it and never writes it to disk.
2. **Group page → Add environment**: a name and the zones it deploys to.
3. **Deploy (canary)**. The job log reads `sync → canary → reload → smoke → stable`: the repo is synced into the
   group's bucket at that ref, one canary pod reloads and must publish the catalogue and answer a smoke call,
   then the stable pods follow. A load error in your package shows up here, per package, and the deploy stops at
   the canary — the previous version keeps serving.
4. **Packages per zone** on the group page lists what each zone now publishes, with the exact `inputSchema`, and
   a **Disable** button per package: a disabled name disappears from `tools/list` and answers `-32601` until the
   next deploy re-enables it.

![The group page: environments, deploy jobs, packages per zone](../img/group.png){ .ramen-shot }

Every later change is the same push and the same **Deploy**; the environment's ref decides what is deployed, so
a `main` environment follows `main` and a `prod` environment can pin a tag.

## 6. Call it
With the group's agent key (API keys page, client type *Agent*) and the routing headers:
```sh
curl -s http://localhost:8080/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: local' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"word_count","arguments":{"text":"the cat sat on the mat"}}}'
```
```json
{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"words\": 6, \"most_common\": \"the\", \"times\": 2}"}],"isError":false}}
```
Claude Desktop, Cursor and the `mcp` SDK see `word_count` in `tools/list` with the schema from your JSON file; the
client configuration is in [End to end §6](end-to-end.md#6-connect-an-ai-client-over-streamable-http). The
worker's access log (console → Logs) shows the call under the key's id, or under the person when the client used
an OAuth token.

## 7. Resources and prompts, briefly
A **resource** is a tool with a `uri` and a `mime_type` whose callable returns the content
(`demo_readme` returns the group's README as `text/markdown`); clients read it with `resources/read`. A **prompt**
has no code: `SKILL.md` with `{{param}}` placeholders for the `input` parameters, and a `settings.json` that is
appended as a `## Settings` block and exposed as `_meta.settings` — `get_calculation_prompt` shows a model how to
use the calculator. Both deploy exactly like tools.
