# Autoscaling: Ramen scales the workers, not the cloud

Since 0.7.5 the console decides how many workers a group runs in each zone, the same way on GKE, EKS and AKS. There is
no HorizontalPodAutoscaler: the adapters delete the `worker` HPA an older release made, and the worker chart's `hpa`
is off. The cloud's own cluster autoscaler still adds and removes *nodes* for the pods Ramen asks for
([CONTRACTS §21.7](../CONTRACTS.md)).

## The bounds

Each group × zone runs between **1** (always) and a **max**. Only a super admin or a group admin of that group sees
and sets the max; viewers and MCP users see the count only, and an API attempt is `403`.

- **Group page → Zone actions**: *Count (min 1)*, *Max (autoscale up to)*, then **Scale workers**. A count outside
  1..max is refused (`422`). Lowering the max under load brings the count down to it at once.
- **Environments, Zones and Workers → Workers per group**: one row per group × zone with *Max workers* and **Save max**, and the last decision.
- API: `PUT /api/v1/groups/{g}/zones/{z}/workers {"max_count": 3}`.

A zone created before 0.7.5 starts with max = its current count, so nothing scales until an admin raises it.

The one case with 0 workers is a zone an admin allowed to [sleep](sleeping-workers.md) (1.0, off by default): the
autoscaler holds while a zone is asleep or waking.

<figure markdown>
![Zone actions with count, max and the last autoscale decision](../img/group-autoscale.png){ .ramen-shot }
<figcaption>Zone actions on the group page: zone <code>b</code> went from 1 to 2 because a worker was high.</figcaption>
</figure>

<figure markdown>
![The Environments, Zones and Workers page with a max per group and zone](../img/environments.png){ .ramen-shot }
<figcaption>Environments, Zones and Workers: live workers with their load, the max per group × zone, and the last decision.</figcaption>
</figure>

## Who runs it

A **rebalance** makes the decision. Two things trigger one:

- **Rebalance and autoscale** in a zone's actions (group admins and up), or `POST …/zones/{z}/rebalance`.
- The **Auto-rebalance scheduler** on the Config page (super admin; off by default, every 60 s when on). It visits
  every group × zone each interval, not only the skewed ones, so zones scale up and back down on their own. Turn it
  on, or nothing scales without a button press.

The load-balancer leg of rebalance (GCP capacity scaler, AWS target-group weights) runs as before.

## How it decides

Each worker's node reports its in-flight calls, the in-flight limit it runs with (`RAMEN_MAX_INFLIGHT`, default 32)
and its total call count. The console keeps the previous total per pod to work out calls per second.

| | When | Step |
|---|---|---|
| **Up** | any ready worker is *high* (over 80 % of its limit), or the zone's mean in-flight is 50 % or more, or calls per second per worker reach `RAMEN_AUTOSCALE_TARGET_RPS` (default 50) — and count < max | proportional: `ceil(ready × max(mean / 50 %, rate per worker / target))`, at least +1, at most the max |
| **Down** | every ready worker is *low* (under 30 %) for 3 rebalances in a row, one worker fewer would stay under the up thresholds, and count > 1 | always −1 |
| **Hold** | anything else, or a cooldown | — |

- **Cooldowns**: no step up within 30 s of the last change (`RAMEN_AUTOSCALE_UP_COOLDOWN_SECS`), no step down within
  120 s (`RAMEN_AUTOSCALE_COOLDOWN_SECS`). Up fast, down carefully.
- A worker that does not answer counts as *down*, never as low, so an outage never scales a zone in.
- **The canary counts as capacity.** A zone at count 1 runs two pods (stable and canary), so the step is slightly
  conservative.
- Worker readiness and liveness probes use a 5 s timeout and liveness allows 6 misses: with the 1 s default, GKE
  restarted CPU-saturated workers in the middle of a load test.
- Bursts shorter than two scheduler intervals do not register a rate; the in-flight signal still catches them.

## Latency trigger (1.0.0c, D83)

The 1.0.0c load runs found the real ceiling of a worker: a `s` pod has a 250m CPU cap, so a zone at the rps target was
already queueing — calls per second said "fine" while callers waited. Since 1.0.0c every node keeps the latency of the
calls it served in the **last 60 s** (a ring of per-second buckets, quantiles computed when the console polls) and
reports it in `Admin/Metrics` as `latency_p50_ms`, `latency_p95_ms` and `latency_n` (the samples in the window).
The console folds the zone's pods into one **p50** (sample-weighted) and the window's rate (`Σ latency_n / 60`).

| | When |
|---|---|
| **Up** | as before, **or** the zone p50 ≥ `RAMEN_AUTOSCALE_P50_MS` (default 250 ms since 1.0.0, was 500) with at least `RAMEN_AUTOSCALE_MIN_RPS` (default 2) calls/s in the window, so an idle zone's one slow call never scales it on noise; the step is `ceil(ready × p50 / bound)`, at least +1 |
| **Down** | only when the rate per worker is low **and** the zone p50 is under half the bound, for the same 3 rebalances |
| **Hold** | a high p50 under the minimum rate holds (and blocks a step down) |

Decisions name the trigger: `up 1→2 — p50 300 ms ≥ 250`, `hold 2→2 — p50 ok, rps low 1/3`,
`hold 1→1 — p50 812 ms, rps 0.5 < 2: no step up`, `hold 2→2 — p50 150 ms ≥ 125, not low`. The cooldowns, the
proportional step and the min/max rules are unchanged. A worker older than 1.0.0c reports no latency fields and keeps
the rps-only decisions and their wording (`low 1/3`).

**Why 250 ms (1.0.0).** The p50 is a one-minute window, and a new worker can take a minute or two to serve behind a
cloud load balancer (pod start, image pull, runtime load at 250m, the LB's health checks). At 500 ms
the 8→64-client ramp asked for the second worker when the first was already near its limit, and the zone's p95
crossed 1.5 s before help arrived (GKE 1.87 s, EKS 2.12 s on 1.0.0b34–b36). At 250 ms it asks at roughly half the
load. A zone whose tools are slow by nature (an LLM rail, an outside API at 300 ms) and that serves ≥ 2 calls/s now
grows to its max sooner; lower that zone's max, or raise `RAMEN_AUTOSCALE_P50_MS` (it applies to every zone).

Where it shows: the Config page's scheduler card and each zone's **Autoscale** row list the three bounds; the zone row
on the group page shows the live last-minute p50/p95 and rate next to the workers, and the last decision carries the
p50 it judged.

## Staying up while nodes come and go

Ramen owns the worker count, so the cluster autoscaler may not evict a worker or the console to empty a node
(`cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` on both). Ramen's own scale-down still frees nodes. The
console's rollout never drops below one ready pod (`maxUnavailable: 0`, `minReadySeconds: 90`) and an old console pod
drains for 40 s before it stops, because the GKE Gateway takes that long to detach it.

## Audit and logs

Every action is on the Audit page with the actor — a person, an API key or `scheduler` — the group and the zone:

| Action | Tags |
|---|---|
| `workers.autoscale` | `action:up|down`, `from:`, `to:`, `reason:` |
| `workers.max_count` | `from:`, `to:` |
| `workers.count` | a manual count |
| `workers.rebalance` | who pressed it |
| any of the above, refused | `error:forbidden` or `error:invalid` |

A *hold* is not audited (it would flood the log) but is kept as the last decision and shown on the group page. Each
scale change also logs a `ramen.autoscale` line on the console; a zone pinned at its max under load logs a warning,
which the [warning email digest](configuration.md#email) carries.

## Load tests

`scripts/load_test.py` drives 64 concurrent `tools/call` through the edge against one zone with max 3 and the
scheduler every 20 s, in five scenarios: a viewer refused, max 1 never scales, sustained load up to the max and back
to 1, a 45 s burst, and the max lowered under load. It also polls the console's `/readyz` and fails on anything but
200. Results from the 0.7.5 round (2026-10-11; the 1.0.0 round is on the
[Transport page](transport.md#what-is-verified-and-what-is-not)):

| Cloud | Up to 3 | Back to 1 | Calls ok | `429` (back-pressure) | 5xx |
|---|---|---|---|---|---|
| GKE | 178 s | 748 s | 25 909 | 117 | 0 |
| EKS | 184 s | 613 s | 14 975 | 1 193 | 0 |
| AKS | 137 s | 601 s | 21 567 | 0 | 0 |

Every scenario passed on all three clouds. The `429`s are the node refusing calls beyond its in-flight limit until the
next worker is ready, which is the intended back-pressure. An earlier round with one-at-a-time steps and a single
120 s cooldown took 309 s to reach 3 on GKE with 57 % of calls refused; the proportional step and the 30 s up cooldown
brought that to 2 %. Times are from the start of the run. The kind-cluster proofs for this design are written but were
not run for 0.7.5.

Since 1.0.0c the release gate is `tests/load/` (`RAMEN_LOAD=1` on top of the cloud test env; `scripts/cloud_load.sh
<cloud>` runs it and appends the table to the status report): four scenarios through the public edge over Streamable
HTTP and gRPC with one group key and one OAuth token, every call carrying the policy `_meta` and a telemetry report —
`steady` (32 clients, 120 s), `mixed` (lists, prompts, the Iris resource and tool, a task round trip, 90 s; the
slow-by-design task is timed on its own, outside the p95 bound), `burst` (256 clients for 30 s against a scope
throttle of 1000/min, or half of what the run will spend when that is less: `429`s, never `5xx`) and `scale` (8→64
clients over 4 min on a zone with max 4 and the scheduler every 20 s: the count grows — the first decision is awaited
up to 3 min after the ramp — p95 stays under 1.5 s, and the zone is back at 1 after `RAMEN_LOAD_IDLE_SECS` — three −1
steps of the 120 s cooldown, so allow ≥ 400 s). The row per scenario (requests, rps,
errors by code, p50/p95/p99, workers before→after, Phoenix span delta, verdict) is what the 1.0.0c report shows under
"Load tests".
