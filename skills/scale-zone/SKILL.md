---
name: scale-zone
description: Add a new zone to a Ramen group, change worker count or size in a zone, rebalance load across zones, or remove a zone — through the console API with the cloud adapter doing the Kubernetes/LB work. Use when asked to scale, add capacity, add a region/zone, or drain a zone.
---

# Scale a zone

Hierarchy: Group → Environment → Zone → Worker (`docs/wiki/concepts.md`). Zones are global objects created by a
super admin; environments attach them. Group admins change **count**; only super admins change **size**
(`s` 250m/512Mi, `m` 500m/1Gi, `l` 1 CPU/2Gi) and the allowed-size list. `U=$RAMEN_CONSOLE_URL/api/v1`, `H='X-Ramen-Api-Key: $RMN'`.

## Add a zone (multi-zone)
1. Super admin: `POST $U/zones {name:"b",provider:"gcp"|"aws"|"local",region:"<cloud zone e.g. us-central1-b>"}`. Locally the zone must be one the compose adapter maps to a worker (`RAMEN_LOCAL_WORKERS`); otherwise stay with `local`.
2. Group admin: `PUT $U/groups/<g>/environments/<e> {zones:[...existing,"b"]}` — the adapter creates namespace `ramen-<g>-b`, service account, Service/NEG or Ingress, route `/mcp/<g>/b`, Secret.
3. (super admin, cloud) `POST $U/groups/<g>/zones/b/service-account` if the environment update did not report one; `PUT $U/groups/<g>/zones/b/workers {count:1,size:"s",allowed_sizes:["s","m"]}`.
4. Deploy: `POST $U/groups/<g>/environments/<e>/deploy {canary:true,zone:"b"}` → poll job to `ok` (first run 1–3 min). Deploying without `zone` rolls every zone of the environment.
5. Copy IP rules if the group uses them: `PUT $U/groups/<g>/zones/b/ip-rules {cidrs:[...]}` (same list as zone a).
6. Tell MCP client owners the new endpoint `https://<lb>/mcp/<g>/b` (same `rmk_` keys).

## Change count / size
- Count (group admin): `PUT $U/groups/<g>/zones/<z>/workers {count:N}` → HPA min N, max 2N (cloud) / no-op locally. No deploy needed.
- Size (super admin): `PUT ... {size:"m"}`; workers roll. Do it zone by zone.
- Watch: `GET $U/groups/<g>/zones/<z>/workers` → `live[]` with `load`, `track`, `phase`.

## Rebalance
`POST $U/groups/<g>/zones/<z>/rebalance` → `{ok, load, capacity_scaler, applied, note?}`. On GCP it sets the
backend capacity scaler (0.5 when the zone is `high`, else 1.0). `applied:false` + note means the Gateway is still
reconciling; it retries in the background — do not loop. Repeat for the other zones so traffic shifts.

## Remove a zone
1. Drain: `POST rebalance` on the other zones; then `PUT $U/groups/<g>/environments/<e> {zones:[without z]}`. The adapter deletes the route first so the LB stops sending traffic, then the namespace and service account.
2. Only delete the global zone (`DELETE $U/zones/<z>`) when no environment of any group references it.

## Validate
(read-only sub-agent; viewer `rmn_` key, an `rmk_` key of the group)
- V1 `GET $U/zones` lists the zone with the expected provider/region.
- V2 `GET $U/groups/<g>/environments/<e>` → `zones` contains it; `last_deploy.status == "ok"`.
- V3 `GET $U/groups/<g>/zones/<z>/workers` → `count` as requested, `size` as requested, `live` has ≥1 `stable` worker with `phase:"Running"` (cloud) and `load` set.
- V4 `curl -sk https://<lb>/mcp/<g>/<z> -H "Authorization: Bearer $RMK" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -H 'Content-Type: application/json'` → 200 with tools (locally `http://localhost:8080/mcp`).
- V5 `GET $U/dashboard` shows the zone×group cell as low/even/high (not down).
- V6 For removals: `kubectl get ns ramen-<g>-<z>` → NotFound; `GET $U/audit` has the `environments.update` entry `ok:true`.

## Boundaries
- Never set `count` to 0 to "save money" — remove the zone instead; a zone with 0 workers still owns LB and IAM resources.
- Never change `size` or `allowed_sizes` without super-admin authority; the API returns 403 and it is audited.
- Do not create zones in a region without confirming quotas (GKE Autopilot regional pods, EKS node group capacity).
- A deploy job `error` after adding a zone: read the streamed log; typical causes are a missing service account (step 3) or the route not yet programmed (wait 2 min, redeploy). Stop after three failed attempts and ask.
