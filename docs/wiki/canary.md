# Canary deploys

`POST /api/v1/groups/{group}/environments/{env}/deploy {"canary": true, "zone": optional}` → `202 {id}`.
Poll `GET /api/v1/jobs/{id}`; the group page polls it every 2 s and shows *refreshing…*.

## Steps (per zone)
1. **Sync** the group repo at the environment's ref into the bucket (`gs://` / `s3://` / `/buckets`). GitHub token
   from a secret named `GITHUB_TOKEN` (env- or group-scoped), else the group's stored fallback token.
2. **Write config**: the zone Secret `ramen-deploy` (cloud) or `<bucket>/.ramen/env-<zone>` (local) with
   `RAMEN_MCP_KEYS` (every `rmk_` key generated for the group), `RAMEN_SECRET_<GROUP>__<NAME>` for secrets scoped to this env/zone,
   `RAMEN_BLOCKED`, `RAMEN_VERBOSE`, group/env/zone labels and `RAMEN_ALLOWED_CIDRS`.
3. **Canary**: scale `worker-canary` to 1, rollout restart, wait for the pod to be ready
   (`RAMEN_DEPLOY_TIMEOUT_SECS`, 300 s).
4. **Reload + smoke** (gRPC to the canary pod, [§11](../CONTRACTS.md)): `ramen.v1.Admin/Reload` with metadata
   `x-ramen-admin-key` (pip install if `requirements.txt` changed, `runtime.load`), then `tools/list` through
   `Mcp/Call` with the first MCP key; when the group has no keys yet, `grpc.health.v1.Health/Check` = `SERVING`
   is the smoke.
5. **Stable**: rollout restart `worker`, wait ready. The LB drains old pods for ~5 s; clients should retry.
6. **Record**: `last_deploy {status, at, packages, error}` on the environment; audit entry `deploy`.

Any failure in 3–5 (a gRPC status, a JSON-RPC error, or `NOT_SERVING`) scales the canary to **0** and leaves `worker` untouched; the error and the streamed log are in
the job and on the group page. A leftover canary from an earlier failure is also scaled to 0.

`{"canary": false}` skips steps 3–4 (used for the local stack and emergencies).

## Rollback
Set the environment's ref to a previous tag/commit and deploy again. Bucket sync deletes stale files.
