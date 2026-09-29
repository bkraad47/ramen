# The release worker image plus two kind-only environment defaults. Nothing else differs: same binary, same
# entrypoint, same env contract, and both values are overridden by the deploy Secret if it ever sets them.
#  * STORAGE_EMULATOR_HOST — the runtime's real gs:// bucket sync (ramen_runtime.bucket, CONTRACTS §7) runs against
#    the in-cluster fake GCS server instead of Google.
#  * RAMEN_MAX_INFLIGHT=8 — the `load` bands in Admin/Metrics are inflight/RAMEN_MAX_INFLIGHT (§3), and the default
#    32 needs ~26 concurrent in-flight calls before a worker reports `high`. 8 makes `high` reachable from a laptop,
#    so the rebalance decision (capacity scaler 0.5 when high, 1.0 otherwise, §7) can be proven here.
ARG BASE=ramen-worker:0.4.0
FROM ${BASE}
ENV STORAGE_EMULATOR_HOST=http://fake-gcs.ramen-system.svc.cluster.local:4443 \
    RAMEN_MAX_INFLIGHT=8
