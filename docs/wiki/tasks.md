# Tasks: long-running tools

Since 1.0 a tool can run as a **task**: the call returns a task id at once, the tool keeps running in the worker,
and the client polls for the result. This is the MCP tasks extension, `io.modelcontextprotocol/tasks`, on protocol
version 2026-07-28.

## Who gets a task

Two things must both be true:

1. The tool's JSON has `"task": true`.
2. The client declared the extension **on that request**, in
   `params._meta["io.modelcontextprotocol/clientCapabilities"]`: `{"extensions": {"io.modelcontextprotocol/tasks": {}}}`.

Any other caller — a 2025-06-18 client, or a 2026-07-28 client that did not declare the extension — gets the
ordinary synchronous call, bounded by `RAMEN_CALL_TIMEOUT_SECS` (default 120). So one tool serves both kinds of
client, and a tool that can run longer than the call timeout is only useful to clients that speak tasks.

The demo repo's `slow_report` is the example:

```json title="mcp/tools/slow_report/slow_report.json (abridged)"
{"type": "tool", "name": "slow_report", "callable": "report", "task": true,
 "input": {"seconds": {"type": "integer", "description": "how long the report takes, in seconds"}},
 "output": {"type": "string"}}
```

The Python is an ordinary function; nothing in it knows about tasks.

## The lifecycle

**Start.** `tools/call` answers at once:

```json
{"resultType": "task", "taskId": "3f9c…32 hex characters", "status": "working",
 "createdAt": "2026-11-13T09:00:00Z", "lastUpdatedAt": "2026-11-13T09:00:00Z",
 "ttlMs": 3600000, "pollIntervalMs": 2000}
```

**Poll.** `tasks/get` with `{"taskId": …}` returns the same fields with the current `status`, plus one of:

| `status` | Extra field | Meaning |
|---|---|---|
| `working` | | still running |
| `input_required` | `inputRequests` | the tool asked the client for input and is waiting |
| `completed` | `result` | the tool's result, exactly what a synchronous call would have returned, `isError` included |
| `failed` | `error` | the task could not finish: the worker was lost, the runtime failed, or the tool needs a client capability |
| `cancelled` | | `tasks/cancel` was accepted |

A tool that raises an exception ends `completed`, with the usual `isError: true` result. `failed` is about the
task, not about the tool's answer.

**Cancel.** `tasks/cancel` with `{"taskId": …}` answers `{"resultType": "complete"}` and the task ends
`cancelled`. Cancelling is cooperative: **the result is discarded, but a function that is already running is not
interrupted**; it runs to its end in the background. Do not rely on cancel to stop a side effect.

On HTTP, `tasks/get`, `tasks/update` and `tasks/cancel` need `Mcp-Name` set to the task id, like every other
named call ([standard headers](transport.md#standard-headers)).

### A task that needs input

A task tool can raise `InputRequired` like any other tool ([multi round-trip requests](mrtr.md)). The task then
shows `status: "input_required"` with the `inputRequests`. The client answers with `tasks/update`:

```json
{"jsonrpc": "2.0", "id": 7, "method": "tasks/update", "params": {
  "taskId": "3f9c…", "inputResponses": {"confirm": {"action": "accept", "content": {"ok": true}}},
  "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}}}}
```

The function runs again from the top with every answer given so far, and the `state` it last raised
(`request_state()`). There is no `requestState` on the wire for a task: the runtime keeps the state itself. An
update that answers none of the outstanding requests is accepted and changes nothing. `tasks/update` and
`tasks/cancel` are handed to the pod that runs the task and applied on its next poll, so a `tasks/get` sent
immediately afterwards can still show the previous state.

## Who can see a task

A task belongs to the credential that started it, in that group and zone. `tasks/get`, `tasks/update` and
`tasks/cancel` from anyone else, for an id that does not exist, or after the task expired, all answer the same
`-32602 Unknown task`. Called without the extension declared, the three methods answer `-32021`.

## Where tasks live, and why Redis matters

The pod that started a task runs it: it polls the Python runtime every 500 ms and writes the task's record to the
**task store**. The store is the first of these that the zone has:

1. the scope Redis (`RAMEN_REDIS_SCOPE_URL`),
2. the item Redis (`RAMEN_REDIS_ITEM_URL`),
3. the pod's own memory.

These are the same Redis instances the [throttle](throttling.md) uses, set on the group page.

**With Redis**, any pod of the zone answers `tasks/get` from the store, and a `tasks/update` or `tasks/cancel`
that lands on another pod is queued there for the owner. A load balancer can send each poll to a different pod.

**Without Redis, a worker in a cluster hands out no tasks.** A zone normally runs two pods, the stable worker and
the canary, and a poll that reached the other pod would answer `Unknown task`. So a pod with no Redis store does not
advertise the extension and a `task: true` tool simply answers when it is done, as it does for a client that did not
declare the extension; the worker logs one warning at start. A single worker outside Kubernetes (the local stack)
keeps tasks in process memory. `RAMEN_TASKS=1` or `0` forces either behaviour.

If the owning pod stops, its tasks cannot continue. With Redis, the next `tasks/get` after 30 seconds without a
heartbeat from the owner marks the task `failed` with `-32603 worker lost`. The same happens at once on the owner
when the Python runtime dies under a task. While a task runs, the runtime is not stopped for idleness.

The Redis URLs are removed from the environment of tool code since 1.0, so a tool cannot read the task store.

## Limits

- **Tasks per pod:** `RAMEN_MAX_TASKS`, default 4. A call that would start one more is refused with `429`, like the
  in-flight cap, and logged with reason `max_tasks`. A running task does not hold an in-flight permit.
- **Lifetime:** `RAMEN_TASK_TTL_SECS`, default 3600. The `ttlMs` a client sees is this value in milliseconds.
  After it the record is gone (`Unknown task`) and the owner cancels the work.
- **Polling:** `pollIntervalMs` is always 2000.
- **No progress for a task.** `progress()` does nothing in a task; a client follows the task's status instead.
- **No task-required tools.** A tool cannot refuse callers that did not declare the extension; they get the
  synchronous call.
- **A task tool does not ask before it starts.** For a client that declared the extension the task is created
  first and any input request comes through it.

## Guardrails

A [guardrail](guardrails.md) on a task tool runs as it does on a multi round-trip call: `pre` before the function
starts and before every re-run after an update, `post` on the final result. A block does not fail the call that
created the task: the task ends `completed` and its `result` is the usual `guardrail blocked` tool error.

## Watching a task instead of polling

A [`subscriptions/listen`](transport.md#subscriptions) stream can name `taskIds`; the node accepts the caller's
own tasks and sends `notifications/tasks` with the task's record when its status changes. These notifications
reach only streams open on the pod that runs the task, so behind a load balancer treat them as a hint and keep
polling.

## Where it shows

- `server/discover` lists the extension under `capabilities.extensions` when a loaded tool has `"task": true`.
- The group page marks such a tool with a `task` badge, and a zone's live-workers cell shows the number of live
  tasks.
- In the worker's access log, `tasks/*` lines carry the tool's name as `name` and the task id as `task`; so does
  the `tools/call` that started it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RAMEN_MAX_TASKS` | 4 | tasks running on one pod at a time |
| `RAMEN_TASK_TTL_SECS` | 3600 | how long a task and its result are kept |
| `RAMEN_REDIS_SCOPE_URL`, `RAMEN_REDIS_ITEM_URL` | unset | the task store; without either, the pod's memory |

The two task settings are read by the worker like its other limits
([Configuration](configuration.md#the-variables-that-matter)). The console has no control for them in 1.0, so
workers it deploys run with the defaults.
