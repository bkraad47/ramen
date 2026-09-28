# Rebalance and load

## Load colours
Every node reports `ramen.v1.Admin/Metrics` (gRPC, metadata `x-ramen-admin-key`; JSON in the reply) →
`{inflight, total, errors, load, sidecar_alive, loaded_at, packages}` where
`load` is `low` (< 30 % of `RAMEN_MAX_INFLIGHT`), `even`, or `high` (> 80 %). The dashboard aggregates per
zone × group: **blue** = low, **green** = even, **red** = high, grey = down/none.

## Rebalance
`POST /api/v1/groups/{group}/zones/{zone}/rebalance` → `{ok, load, capacity_scaler, backend_service, applied, note?, scaled_to?}`.

| Adapter | Action |
|---|---|
| local | records the action; nothing to balance |
| gcp | sets the zone backend's `capacityScaler` on the Gateway-managed backend service (0.5 when `high`, 1.0 otherwise), discovered by NEG name `ramen-<group>-<zone>`; then aligns replicas with the HPA minimum. While the Gateway is still reconciling it answers `applied:false` with a note and retries in the background. Backends are health-checked over gRPC, so a worker that has not loaded code yet takes no traffic. |
| aws | adjusts stable/canary target-group weights through the Ingress `actions` annotation (**untested**) |

## Scaling
`PUT /api/v1/groups/{group}/zones/{zone}/workers {"count": n, "size": "s|m|l", "allowed_sizes": [...]}`.
Admins change `count` (HPA min = count, max = 2 × count); only super admins change `size` and the allowed list.
Sizes: `s` 250m / 512Mi, `m` 500m / 1Gi, `l` 1 CPU / 2Gi.

## Adding a zone
Super admin: `POST /api/v1/zones {"name": "b", "provider": "gcp", "region": "us-central1-b"}`. Group admin: add the
zone to the environment's `zones` and deploy. The adapter creates the namespace, service account, the header-matched
LB route (`ramen-group`/`ramen-zone`), Role/RoleBinding and Secret; nothing else changes. Clients reach the new zone
by changing `--zone` on the bridge, same key. See the [scale-zone skill](skills.md).
