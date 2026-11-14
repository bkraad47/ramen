# Multi round-trip requests: a tool that asks before it answers

Since 1.0 a tool, or a callable prompt, can stop and ask the client for something: a form for the user to fill in, a
URL for the user to visit, a completion from the client's model, or the client's roots. The client answers by
making **the same call again** with the answers attached. MCP 2026-07-28 calls this a multi round-trip request.

There is no session and nothing is kept on the worker between the two calls. That is what lets any pod of the zone
take the second call.

## The flow

The demo repo's `confirm_delete_note` asks before it deletes:

```python title="mcp/tools/confirm_delete_note/confirm_delete_note.py"
from ramen_runtime import InputRequired, elicit, input_responses

def confirm_delete(note: str) -> str:
    if note not in NOTES:
        return f"Nothing was deleted: there is no note named {note!r}. Notes: {', '.join(NOTES)}."
    answer = input_responses().get("confirm")
    if answer is None:
        raise InputRequired({"confirm": elicit(f'Delete the note "{note}"? This cannot be undone.', CONFIRM)})
    confirmed = answer.get("action") == "accept" and (answer.get("content") or {}).get("confirm") is True
    if not confirmed:
        return f'Nothing was deleted: the note "{note}" is still there.'
    remaining = [n for n in NOTES if n != note]
    return f'Deleted the note "{note}". Remaining: {", ".join(remaining)}.'
```

**Round one.** The client calls the tool and declares that it can show a form:

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
  "name": "confirm_delete_note", "arguments": {"note": "todo"},
  "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}}}
```

The function raises `InputRequired`, and the worker answers with a result, not an error:

```json
{"jsonrpc": "2.0", "id": 1, "result": {
  "resultType": "input_required",
  "inputRequests": {"confirm": {"method": "elicitation/create", "params": {
    "mode": "form", "message": "Delete the note \"todo\"? This cannot be undone.",
    "requestedSchema": {"type": "object", "properties": {"confirm": {"type": "boolean", "title": "Delete it?",
      "description": "true deletes the note"}}, "required": ["confirm"]}}}},
  "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "ramen-node", "version": "1.0.0"}}}}
```

**Round two.** The client shows the form, then sends the same `name` and `arguments` with the answer under the
same key:

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
  "name": "confirm_delete_note", "arguments": {"note": "todo"},
  "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": true}}},
  "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}}}
```

The function runs again **from the top**, `input_responses()` now returns the answers, and the result is an
ordinary one (`resultType: "complete"`, text `Deleted the note "todo". Remaining: groceries, ideas.`).

A tool may ask for several things at once (several keys), and may ask again on the next round. A key the tool does
not know is the tool's to ignore. `inputResponses` must be an object whose values are objects; anything else is
`-32602`.

## What a tool can ask for

| Builder | The client receives | The client must have declared |
|---|---|---|
| `elicit(message, schema)` | `elicitation/create`, `mode: "form"` | `elicitation` |
| `elicit_url(message, url)` | `elicitation/create`, `mode: "url"` | `elicitation.url` |
| `sample(messages_or_text, max_tokens=100, **params)` | `sampling/createMessage` | `sampling` |
| `list_roots()` | `roots/list` | `roots` |

Capabilities are read from **this request's** `clientCapabilities`. When the tool asks for something the client did
not declare, the call is error `-32021` with `data.requiredCapabilities` naming what is missing (HTTP `400`), and
the input request is not sent. A tool can look first with `client_capabilities()`, or refuse outright:

```python title="mcp/tools/needs_sampling/needs_sampling.py"
from ramen_runtime import MissingCapability, client_capabilities, text

def run():
    if "sampling" not in client_capabilities():
        raise MissingCapability({"sampling": {}})
    return text("has sampling")
```

**A form may not ask for a secret.** A form's answer passes through the client and the model's context, so the
runtime refuses a form with a field named like a credential, and the call answers a tool error:
`input request refused: <key>.<field> looks like a secret; use elicit_url`. The exact rule is on
[The MCP repo](mcp-repo.md#asking-the-client-for-input). Send the user to a page of yours with `elicit_url` instead.

## State between the rounds

Because the function runs again from the top, anything it worked out in round one is gone unless it hands it over:

```python
from ramen_runtime import InputRequired, elicit, input_responses, request_state

def run():
    state = request_state()                   # None on the first round
    answer = input_responses().get("confirm")
    if state is None or answer is None:
        raise InputRequired({"confirm": elicit("Please confirm", SCHEMA)}, state={"kind": "request-state"})
    ...
```

The worker returns that `state` to the client as `requestState`, an opaque string the client sends back unchanged
on the retry. The node verifies it before the function runs, and `request_state()` returns the original value.

**What the string is.** `base64url(payload) "." base64url(HMAC-SHA256(key, payload))`. It is signed, **not
encrypted**: the client can read the state, so do not put anything in it that the caller should not see. (Values
of the group's secrets that the call resolved are masked in input requests and in the state before they leave the
runtime.)

**What it is bound to.** The payload carries, next to the tool's state: the caller (the credential that made the
call), the group, the zone, the method, the tool or prompt name, a digest of the call's arguments, and an expiry. A
state presented by another caller, to another tool, with other arguments, in another zone or group, after its
expiry, or altered in any way is refused: `-32602 requestState integrity check failed`, with reason `state_invalid`
in the access log. The answer is the same for every one of those cases.

**How long it lives.** `RAMEN_MRTR_TTL_SECS`, default 600 seconds. It is read from the worker's own environment
or config file only; the group's deploy file cannot change it. A state is not single-use: within its lifetime the same retry can be
sent again, and the tool runs again.

**Which pod can verify it.** The key is derived from the zone's `RAMEN_SESSION_SECRET`, which the console writes
into every zone, so any pod of the zone verifies a state another pod issued. A node started without that secret
generates one per process, and then only the pod that issued a state accepts it.

`requestState` is present only when the tool gave a `state`. A tool that keeps nothing between rounds needs none.

## Guardrails run on every round

A [guardrail](guardrails.md) on the tool sees each round, not only the first:

- **`pre` runs before the function on the first call and on every retry.** On a retry the rail sees the arguments
  plus the client's answers under `"$inputResponses"`, so an answer typed by a user (or injected into one) is
  checked before the tool acts on it.
- **`post` runs on the final result only.** A round that ends in an input request has no result to check.
- A blocked round is the usual tool error with `_meta.ramen.guardrail`.

## What a 2025-06-18 client sees

A client on the older protocol cannot be asked. The same tool, called by such a client, answers a tool error it can
read:

```
this tool needs more input (confirm); it can only ask a client that speaks MCP 2026-07-28
```

For a callable prompt the same text is error `-32602`. A tool that raises `MissingCapability` answers
`this tool needs a client capability (<names>); it can only run for a client that speaks MCP 2026-07-28`. The
function has run up to the point where it asked, so do the asking before any side effect. Say in the tool's
`description` that it needs a 2026-07-28 client, as the demo tool does.

## Golden cases

A golden case can answer the input request, so a tool that asks still gates its deploy. See
[The MCP repo → Golden cases](mcp-repo.md#golden-cases).

## Limits

- Tools and callable prompts can ask. A resource callable cannot.
- A tool with `"task": true`, called by a client that declared the tasks extension, starts as a task at once; its
  input request then arrives through the task ([Tasks](tasks.md#a-task-that-needs-input)), not as a first answer.
- Works the same over gRPC, where the body carries everything.
