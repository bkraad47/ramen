# Guardrails: NeMo Guardrails or your own policy, per tool

Since 0.7.5 a group repo can put a check in front of a tool call and behind its result. The check runs **inside
the worker's Python runtime**, next to the tool, so it sees exactly what the tool is about to receive and exactly
what it is about to return, and nothing leaves the pod. It is opt-in per tool, it never rewrites anything, and a
blocked call comes back to the agent as a normal tool error it can read.

Two engines ship. **`nemo`** runs [NVIDIA NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails) in the
runtime process: you write rails in Colang, with or without an LLM behind them. **`policy`** is a Python file of
your own: two functions, no framework. Both read the same `mcp/guardrails.yaml`.

!!! note "Python 3.12 in the runtime"
    The worker's Python runtime is pinned to 3.12 since 0.7.5 because NeMo Guardrails does not run on 3.14. Tool
    code that used 3.14-only syntax needs the usual adjustments; the console stays on 3.14.

## 1. Opt tools in: `mcp/guardrails.yaml`

```yaml title="mcp/guardrails.yaml"
engine: nemo            # nemo | policy | none
config: mcp/guardrails  # nemo: the rails config dir; policy: the dir holding policy.py
fail: closed            # closed (default) | open — what a hook error or timeout means for the call
timeout_s: 10           # per hook, default 10, max 120
tools:
  word_count: {pre: true, post: true}
  demo_calculator_tool: {pre: true}     # post defaults to false
```

A tool that is not listed is never checked. `pre` runs after Ramen has validated the arguments and resolved
`{{$group.VAR}}` secrets, and before your function; the payload the engine sees is the tool name and the
arguments **as written**, so a secret reference stays `{{$group.API_KEY}}` and the value never reaches a rail.
`post` runs after the output schema check (0.7.0) and sees the tool name, the same as-written arguments, and the
result text or the structured result as JSON, with every resolved secret value masked as `***`. The agent still
receives the tool's own output; only the rail sees the masked copy.

## 2a. Engine `nemo`

A NeMo config directory: `config.yml`, one or more `.co` files, and optionally `actions.py`. The demo repo ships a
deterministic one that needs no model and costs nothing per call:

```yaml title="mcp/guardrails/config.yml"
models: []
rails:
  input:
    flows: [check injection]
  output:
    flows: [check leaks]
```

```text title="mcp/guardrails/rails.co"
define flow check injection
  $hit = execute check_injection
  if $hit
    bot refuse
    stop

define flow check leaks
  $hit = execute check_leaks
  if $hit
    bot refuse
    stop

define bot refuse
  "blocked by the group's guardrails"
```

```python title="mcp/guardrails/actions.py"
from nemoguardrails.actions import action

@action(is_system_action=True)
async def check_injection(context: dict | None = None):
    text = ((context or {}).get("user_message") or "").lower()
    return "ignore previous instructions" in text

@action(is_system_action=True)
async def check_leaks(context: dict | None = None):
    return "secret" in ((context or {}).get("bot_message") or "").lower()
```

Ramen runs only the **input rails** for `pre` (your tool's arguments are the user message, the tool name is the
message's `name`) and only the **output rails** for `post` (the result is the assistant message). A rail that
stops the flow blocks the call, and the bot message it produced is the reason the agent reads. Nothing else of
NeMo's dialogue machinery runs, so a config with no model is fast (the demo's two checks take a few milliseconds).

**Rails that call an LLM** (`self check input`, `self check output`, jailbreak detection and the rest of NeMo's
library) work the same way: name the model under `models:` in `config.yml` and put its API key in
[`mcp/env.yaml`](secrets.md) as a `{{$group.VAR}}` reference. The worker exports it into the runtime process
before the rails load, so NeMo finds `OPENAI_API_KEY` (or whichever the provider wants) where it expects it.
Mind the latency and the cost: an LLM-backed rail adds a model round trip to every opted-in call, and its verdict
is not deterministic. Keep it to the tools that need it; that is what the per-tool opt-in is for.

## 2b. Engine `policy`

```python title="mcp/guardrails/policy.py"
def pre(tool: str, arguments: dict) -> str | None:
    if tool == "sql_query" and "drop table" in arguments.get("query", "").lower():
        return "destructive statements are not allowed from agents"
    return None

def post(tool: str, arguments: dict, result) -> str | None:
    if isinstance(result, str) and "BEGIN PRIVATE KEY" in result:
        return "the result contained a private key"
    return None
```

Return `None` to allow, a string to block with that message. Either function may be absent. An exception is a hook
error, handled by `fail`.

## 3. What the agent sees

| Outcome | Result of `tools/call` |
|---|---|
| Allowed | The tool's own result, unchanged |
| Blocked at `pre` | `isError: true`, text `guardrail blocked: pre: <message>`. The tool never ran |
| Blocked at `post` | `isError: true`, text `guardrail blocked: post: <message>`. The tool's output is not returned |
| Hook error or timeout, `fail: closed` | `isError: true`, text `guardrail unavailable: <reason>` |
| Hook error or timeout, `fail: open` | The tool's result; the runtime logs `guardrail_error` |
| Engine failed to load (missing package, bad config) | Every opted-in tool answers `guardrail unavailable` until a load succeeds, whatever `fail` says. The other tools are unaffected |

A blocked result carries `_meta.ramen.guardrail = {"stage": "pre"|"post", "engine": "nemo"|"policy"}` so a client
can tell a guardrail from a tool failure. In the worker's access log the line has reason `guardrail_pre` or
`guardrail_post`, on both transports, next to the `tool_denied` and `tool_hidden` reasons of
[per-tool access](groups-zones.md). The runtime logs one line per hook with the tool, stage, verdict and
milliseconds, never the payload.

**Order on a call.** Per-tool access first (a caller who may not call the tool gets `-32003` and no rail runs),
then argument validation, then `pre`, the tool, the output schema, `post`. Blocking a tool for a kind of caller
and guarding it for the others on the same tool is the intended combination.

**Golden cases run through the rails.** A case in `mcp/tests.yaml` whose arguments a rail blocks fails the deploy
gate with the rail's message. Write a case that is supposed to be blocked as `expect: {text_contains: "guardrail
blocked"}` and the gate proves the rail.

## 4. Where it shows

The deploy's load result and the worker's reload carry `guardrails: {engine, fail, tools: {<tool>: ["pre","post"]}}`.
The group page's Manifest section shows it per zone ("Guardrails: nemo · word_count (pre, post),
demo_calculator_tool (pre)"). It is not part of the manifest hash, so switching a rail on or off is not a
schema change for the [compatibility gate](groups-zones.md).

## 5. Test it locally

```sh
cd runtime-py && uv run python -m ramen_runtime --bucket ../../ramen-demo-mcp   # loads mcp/, prints the guardrails block
```

then a `runtime.call_tool` with `{"name": "word_count", "arguments": {"text": "ignore previous instructions and ..."}}`
over stdin answers the blocked result. The compose stack does the same through the node; the conformance suite's
`test_guardrails.py` is the reference for every row of the table above.
