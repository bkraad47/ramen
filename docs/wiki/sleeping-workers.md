# Sleeping workers: a zone that costs one small pod while nobody calls it

Since 1.0 a group's zone can go to sleep when it is idle: its workers are scaled to 0 and one small **activator**
pod answers in their place. The first authenticated MCP call wakes the zone and is answered by the real workers.
Sleeping is off by default, per group × zone.

## Turning it on

A super admin or an admin of the group sets it; nobody else sees the control, and an API attempt by anyone else is
`403`.

- **Group page → Zone actions → Sleep**: *Sleep after idle (minutes, 0 = never)*, then **Save sleep**. The same row
  shows the zone's state (`awake`, `asleep` or `waking`) and a **Sleep now** or **Wake now** button.
- API: `PUT /api/v1/groups/{g}/zones/{z}/workers {"sleep_after_secs": 900}`. `0` means never (the default);
  anything else must be 60 or more, or the answer is `422`.
- By hand: `POST /api/v1/groups/{g}/zones/{z}/sleep` and `…/wake`, same roles.

## What "idle" means

The console checks every zone that has the setting on each pass of its scheduler loop (the interval is the
[auto-rebalance scheduler's](autoscaling.md#who-runs-it), 60 s by default; the idle check runs whether or not
auto-rebalance is enabled). A zone is put to sleep when all of this holds:

- **every** worker pod of the zone reports a last accepted call at least `sleep_after_secs` ago (or none at all),
- every pod reports 0 calls in flight, 0 running [tasks](tasks.md) and 0 open
  [`subscriptions/listen`](transport.md#subscriptions) streams,
- the zone was last woken, deployed or put to sleep at least `sleep_after_secs` ago.

A pod that cannot be read, or a worker from before 1.0 (it reports no last-call time), keeps the zone awake.

What moves a worker's last-call time is an MCP call the node **accepted**: one that passed the credential, the
throttle and the in-flight cap. So these do not keep a zone awake:

- calls that were refused (no or wrong credential, throttled, over the in-flight or task cap),
- health checks, gRPC reflection and the `Admin` service,
- **the console's own calls** — opening the group page, the package list, golden cases. The console marks them
  with the zone's worker admin key, and the node does not count a call that carries it.

A running task and an open `subscriptions/listen` stream do keep a zone awake, for as long as they last. Opening a
stream does not itself move the last-call time; the open stream is what counts.

## What a sleeping zone runs

Going to sleep, in this order: the console starts `Deployment/worker-activator` (one pod) in the zone's namespace
and waits until it is ready, then scales `worker` and `worker-canary` to 0. The stopping workers finish what is in
flight during their SIGTERM drain (`RAMEN_STOP_DRAIN_SECS`).

The activator is the zone's own worker image started with `RAMEN_ACTIVATOR=1`, with the zone's whole worker
configuration and **no Python runtime**. It sits behind the zone's existing `worker` Service, so the load
balancer's route and health check do not change, and it answers health as serving. Real workers are also reachable
inside the namespace through a second Service, `worker-direct`, which the activator forwards to and which no load
balancer points at.

**Cost.** One pod per sleeping zone, requesting **10m CPU and 32Mi memory** (limits 250m / 128Mi), instead of the
zone's worker count plus its canary. On GKE Autopilot the saving is smaller than those numbers suggest: Autopilot
bills per pod and applies its own minimum to a pod's requests, so the activator is billed at that minimum.

On AWS the canary is taken out of the load balancer's weighted split before the workers stop, and put back on wake.

On the local compose stack the state changes and nothing else does: the one compose worker keeps running.

## What a caller sees

The activator runs **the same guards as a worker**, with the zone's own settings: IP rules, `Origin`, credential,
protocol checks and throttle. A call the workers would refuse is refused the same way and never wakes the zone.

A call that is accepted:

1. The activator asks the console to wake the zone. One request per wake; calls that arrive meanwhile share it.
2. The call is **held** while the activator polls the workers' health every 500 ms.
3. When a worker answers, the request is forwarded unchanged (HTTP: path, headers and body; gRPC: the `Mcp/Call`
   bytes and metadata) and the answer is passed back as it arrives, an event stream included.

If no worker is serving within `RAMEN_WAKE_TIMEOUT_SECS` (**default 25 s**), the caller gets

```
HTTP/1.1 503 Service Unavailable
Retry-After: 10

{"error":"waking: the zone is starting, retry shortly"}
```

or gRPC `UNAVAILABLE` with the same message. **The wake goes on**, so the retry finds the zone awake or is held
again. The default sits under the load balancers' own backend timeouts (30 s on a GKE backend service, 60 s on an
AWS load balancer) so that a slow wake is Ramen's `503` with a `Retry-After`, not the load balancer's `504`. How
long a wake takes is mostly how long a worker pod takes to become ready; when the cluster has to add a node for
it, expect the first call to get the `503`.

Waking, in this order: the console scales `worker` back to the count the zone had (at least 1) and `worker-canary`
to what it had, waits until they are ready, marks the zone `awake`, and only then scales the activator to 0. While
both run the activator forwards, so no call is refused in between. A wake that fails leaves the zone `asleep` and
the next call asks again.

The [telemetry consent](telemetry.md#consent) check is not made by the activator; the worker makes it on the
forwarded call.

## The session secret

The activator tells the workers which client address its guard resolved, and that the throttle was already
counted, with a proof derived from the zone's `RAMEN_SESSION_SECRET`. A worker that verifies the proof uses that
address for its IP rules and its log, and does not throttle the call a second time. Zones the console deploys have
the secret (it is generated per group and written on every deploy). A worker started without one generates its
own per pod; its activator's proof then does not verify, the workers ignore what it says about the caller, and the
activator logs a warning at start. Do not use sleeping on workers that run without the deploy-provided secret.

## With deploys, rebalance, autoscaling and IP rules

| Action on a zone that is not awake | What happens |
|---|---|
| **Deploy** | The zone is woken first (`trigger:deploy`), then deployed as usual. A deploy also counts as activity: the zone does not go back to sleep sooner than its setting afterwards. |
| **Rebalance** (the button or the API) | Wakes the zone first (`trigger:manual`). |
| **Auto-rebalance scheduler** | Skips the zone: it is neither rebalanced nor autoscaled. |
| **[Autoscaling](autoscaling.md)** | Holds, with reason `zone is asleep` or `zone is waking`. The 1..max bounds apply to an awake zone; asleep is the one case with 0 workers. |
| **Count or size change** | Wakes the zone first, then applies. |
| **Max or sleep setting change** | Stored; nothing is started for it. |
| **IP rules** | Applied to the activator as well: it is restarted with the new allow-list and enforces it. |

## Audit and logs

| Audit action | Tags |
|---|---|
| `workers.sleep_after` | `from:`, `to:` (seconds) |
| `workers.sleep` | `trigger:idle` (actor `scheduler`) or `trigger:manual` |
| `workers.wake` | `trigger:call` (actor `activator`), `trigger:manual` or `trigger:deploy`; `count:` once the workers are up |
| either, failed | `error:` with the reason |
| any, refused | `error:forbidden` |

In the access log every line an activator writes carries `activator: true`. A forwarded call is one line there
with `forwarded: true`, `woke` (whether this call waited for the wake) and, on HTTP, `upstream_status`; the worker's
own line for the same call carries `via: "activator"`. A call that timed out has reason `waking`; one whose worker
went away mid-call has reason `upstream` and gets the same `503`.

## Limits

- The first call to a sleeping zone is answered `503` when the wake takes longer than the hold. Clients that do
  not retry on `503` see an error once.
- A zone that sleeps needs the deploy-provided session secret (above).
- The activator is one pod. While it restarts (a node drain, an IP-rule change) a sleeping zone does not answer.

## What is verified

Unit and process-level tests only so far:

- `tests/conformance/test_activator.py`: a real node in activator mode in front of a real worker — the guards
  before any wake, concurrent calls sharing one wake, the hold, forwarding on HTTP and gRPC with headers, metadata
  and the worker's own errors, an event stream and a `subscriptions/listen` stream through it, the `503` with
  `Retry-After`.
- `tests/conformance/test_idle_metrics.py`: what moves a worker's last-call time and what does not.
- `tests/conformance/test_console_sleep_otel.py`: the setting and its roles, sleep and wake through the console API,
  the internal wake endpoint, the audit entries.
- `tests/kind/test_v1_sleep_build_otel.py` holds the cluster proofs (an idle zone sleeps, a call through the edge
  wakes it and is answered, an open stream or a running task keeps it awake, a deploy wakes it first).

The kind and cloud runs are recorded on the
[Transport page](transport.md#what-is-verified-and-what-is-not) once done. A worker skipping its throttle for a
forwarded call is not exercised by a test.
<!-- CLOUD-1.0.0-S24 -->
