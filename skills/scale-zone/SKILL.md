---
name: scale-zone
description: Add a zone to a Ramen 1.0 environment, set a zone's worker count, autoscale max and size, rebalance, let an idle zone sleep or wake it, or remove a zone — through the console API, which does the Kubernetes and load-balancer work. Use when asked to scale, add capacity or a region, put zones to sleep to save cost, rebalance load, or drain and remove a zone.
license: BSD-3-Clause
compatibility: Needs a reachable Ramen 1.0 console and an rmn_ key or session of a group admin (counts, max, sleep) or super admin (zones, sizes); kubectl read access helps validation.
metadata:
  version: "1.0.0"
  author: bkraad47
  docs: https://bkraad47.github.io/ramen/wiki/groups-zones/
---

# Scale a zone

`U=$RAMEN_CONSOLE_URL/api/v1`, header `X-Ramen-Api-Key: $RMN`. Source of truth: `docs/wiki/groups-zones.md`,
`docs/wiki/autoscaling.md`, `docs/wiki/sleeping-workers.md`.

Model (1.0.0b): *a group has environments, an environment has zones, a zone has workers.* An environment is a name
shared by the groups it lists; a zone names the environments it serves and its groups follow. Every derived
group × zone pair is deployed the moment it appears and torn down when it disappears.

## Add a zone
1. Where: a zone must be in a region that has a cluster — the home region or a registered satellite
   (`GET $U/config/regions` → `clusters`, super admin). Otherwise `422` naming the regions that have one; adding a
   satellite is the deploy skills' job. Creating a zone that serves no group yet is a super admin's; a group admin
   may create or edit zones whose environments are wholly their groups'.
2. `POST $U/zones {"name":"b","provider":"<console's cloud>","region":"<us-central1-b | us-east-1b | eastus-2>","environments":["prod"]}`
   → the zone with its derived `groups`, `cluster`, `host` and the `jobs` it started. Poll each `GET $U/jobs/<id>` to
   `ok`. To attach an existing zone: `PUT $U/zones/<z> {"environments":[…]}` (the whole list).
3. Copy the group's IP rules if it uses them: `PUT $U/groups/<g>/zones/b/ip-rules {"cidrs":[…]}` (include the
   console's own range: the deploy smoke test calls the worker as an ordinary caller).
4. Clients reach the zone at its cluster's host with `ramen-zone: b`; keys and tokens do not change.

## Count, max, size
`PUT $U/groups/<g>/zones/<z>/workers` with one field per call:
- `{"max_count": 4}` — group admins and super admins. Ramen scales between **1** and max itself (no HPA); the
  cluster autoscaler adds nodes. Nothing scales on its own unless **Config → Auto-rebalance scheduler** is on
  (`PUT $U/config/scheduler {"enabled":true,"interval_seconds":60}`, super admin).
- `{"count": 2}` — a manual count inside `1..max` (`422` outside it).
- `{"size": "m"}` — `s` 250m/512Mi, `m` 500m/1Gi, `l` 1 CPU/2Gi; group admins only within the zone's
  `allowed_sizes`, which super admins set (`{"allowed_sizes":["s","m"]}`). A size change rolls the canary first.
Watch: `GET $U/groups/<g>/zones/<z>/workers` → `live[]` (`load`, `track`, latency) and the last autoscale decision.

## Rebalance
`POST $U/groups/<g>/zones/<z>/rebalance` → the load-balancer leg (GCP capacity scaler, AWS target-group weights)
plus `autoscale: {action, from, to, reason}`. `applied:false` with a note means the edge is still reconciling and
retries in the background: do not loop.

## Sleep (cost: one 10m/32Mi activator pod instead of the workers)
- `PUT $U/groups/<g>/zones/<z>/workers {"sleep_after_secs": 900}` — `0` = never (default), else ≥ 60 (`422`).
- `POST …/zones/<z>/sleep` and `…/wake` by hand. State on the group page: `awake`, `asleep`, `waking`.
- The first authenticated call wakes the zone; when the wake takes longer than 25 s the caller gets `503` with
  `Retry-After: 10` (cold GKE Autopilot wakes can). Deploys, rebalance and count/size changes wake it first; the
  autoscaler holds while it sleeps. Sleep needs the deploy-written session secret, so only on console-deployed zones.

## Remove a zone
1. Make sure the group's other zones can take the load (raise their max; rebalance them).
2. `PUT $U/zones/<z> {"environments":[…without the one]}` to detach, or `DELETE $U/zones/<z>` to delete it with every
   pair. The route goes first, then the namespace, identity and record. On GKE the network endpoint groups are
   reaped in the background (up to fifteen minutes).

## Rules
- Never set a count of 0 to save money: count is 1..max; sleeping is the zero-worker state.
- Sizes and allowed sizes outside a group admin's reach are `403` and audited; do not retry with another key.
- Do not add zones without confirming quota (Autopilot regional pods, EKS node group capacity and pods per node,
  AKS VM family cores). A deploy `error` after adding a zone: read the job log; stop after three failed attempts.

## Validate
Read-only sub-agent with a viewer `rmn_` key and an `rmk_` key of the group. Reply `PASS` or `FAIL: <id> — <evidence>`.
- V1 `GET $U/zones/<z>` → provider, region, `environments`, `cluster` as intended.
- V2 `GET $U/groups/<g>/zones/<z>/workers` → `count`, `max_count`, `size`, `sleep_after_secs` as requested; `live`
  has ≥ 1 `stable` worker (or `asleep`, when that was the point).
- V3 `tools/call` through `<host>/mcp` with `ramen-group`/`ramen-zone` → result (a sleeping zone: a `503` with
  `Retry-After` at most, then the result on retry).
- V4 `GET $U/environments/by-name` → the environment's last deploy for the group is `ok`.
- V5 Removal: `kubectl get ns ramen-<g>-<z>` → NotFound; `GET $U/zones` no longer lists a deleted zone.
