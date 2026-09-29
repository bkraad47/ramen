# `deploy/kind` — the local cluster behind the v0.4.0 multi-zone proofs

`make kind-up` → `make kind-test` → `make kind-down`. It exists to prove four things on a laptop, before any money
is spent on GKE (CONTRACTS §12.3, D22):

1. two zones of one group serve MCP traffic independently (U20);
2. a zone's HPA scales its workers up under generated gRPC load and back down afterwards (U21);
3. `rebalance` changes the split between the zones (U21);
4. each zone exposes its own package list, so a language model on one zone cannot see the other's tools (U22).

## Which console adapter runs here, and why

**The GCP adapter's Kubernetes paths** — the console runs with `RAMEN_CLOUD=gcp` and `RAMEN_GCP_PROJECT=kind`.

The alternative was a `kind` flavour of the local adapter. It was rejected on both counts that mattered:

* **Less invasive.** The GCP path needs *no console code at all*: `deploy/kind/values.yaml` is a values file and
  everything else in this directory is cluster furniture. A `kind` adapter would have meant a new file in
  `console/src/ramen_console/cloud/`, a new `RAMEN_CLOUD` value, a branch in `make_cloud()` and its own tests — new
  production code written only to be tested.
* **Worth more.** The local adapter has no namespaces, no Deployments, no HPA and no `rebalance` (it returns
  `{"ok": true, "note": "local adapter: no load balancer to rebalance"}`), so a `kind` flavour would have had to
  *reimplement* the thing under test. With the GCP adapter the manifests applied here are the ones GKE gets, from
  `ramen_console.cloud.gcp_k8s.manifests()`: same Deployments, same `worker`/`worker-canary` tracks, same HPA, same
  NetworkPolicy, same `nodeSelector: topology.kubernetes.io/zone`, same HTTPRoute, same canary-then-stable deploy.
  A green run here is evidence about the code that runs in production.

## What has no kind equivalent, and what stands in for it

| GCP dependency | On kind | Effect |
|---|---|---|
| GCS groups bucket | `fake-gcs-server` in `ramen-system`, reached through `STORAGE_EMULATOR_HOST` | real code path: the console's `sync_repo` (git clone → bucket) and the runtime's `gs://` sync both run unchanged |
| GKE Gateway (header routing, TLS termination) | nothing; each zone gets a NodePort | routing by `ramen-group`/`ramen-zone` metadata is **not** proven here — it is proven on GKE (v0.3.2 and `reports/cloud-v0.4.0.md`). HTTPRoutes are still created and accepted, just inert |
| LB backend service (capacity scaler) | nothing | `rebalance` answers 200 with `applied:false` and a note, which is what §7 requires when no backend is programmed. The replica half of `rebalance` runs for real |
| Cloud Armor | nothing | `ip-rules` is not exercised here |
| Cloud Logging | nothing | `GET /api/v1/logs` returns a 502 from the adapter; the logs suites are not part of `kind-test` |
| IAM + Workload Identity | `zones.yaml` pre-annotates each zone's `worker` ServiceAccount | the console skips its identity path instead of failing on it. Proven on GKE |
| Firestore | `RAMEN_STORE=memory` | console state resets when its pod restarts; `kind-test` re-seeds itself, so that is harmless |
| Secret Manager | `RAMEN_SECRETS_BACKEND=store` | secret values live in the console store |

Two more kind-only deviations, both deliberate and both visible in the files:

* **`netpol-host.yaml`** — `kindnetd` *does* enforce NetworkPolicy. The worker policy the console renders (SEC-04)
  admits only the `ramen-system` namespace and the Google front-end CIDRs `35.191.0.0/16` + `130.211.0.0/22`, so
  NodePort traffic is dropped. This adds the kind node and pod CIDRs — the same hole, on the range that plays the
  load balancer's part here.
* **`worker.Dockerfile`** — the release worker image plus `STORAGE_EMULATOR_HOST` and `RAMEN_MAX_INFLIGHT=8`. The
  `load` bands in `Admin/Metrics` are `inflight / RAMEN_MAX_INFLIGHT`, and the default 32 needs ~26 concurrent
  in-flight calls before a worker reports `high`; 8 makes the `high` band reachable from a laptop so the rebalance
  decision can be proven.

## Files

| File | What it is |
|---|---|
| `cluster.yaml` | 1 control-plane + 2 workers labelled `topology.kubernetes.io/zone=kind-a|kind-b`; host ports 18000 (console), 18081/18082 (zones a/b, gRPC h2c), 18083 (fake GCS) |
| `crds-gke.yaml` | `HealthCheckPolicy` and `GCPBackendPolicy` CRD stubs, so the GKE manifests apply unchanged |
| `fake-gcs.yaml` | the groups-bucket stand-in, bucket `ramen-kind-groups` |
| `worker.Dockerfile` | release worker image + the two kind-only env defaults above |
| `values.yaml` | console chart values (`gateway.enabled=false`, memory store, store secrets, fake GCS endpoint) |
| `nodeports.yaml` | the console's host NodePort |
| `zones.yaml` | namespaces `ramen-demo-a`/`ramen-demo-b`, their pre-annotated `worker` ServiceAccounts and their NodePorts |
| `netpol-host.yaml` | the kind stand-in for the Google front-end CIDRs (templated by `up.sh`) |
| `up.sh` / `test.sh` / `down.sh` | `make kind-up` / `kind-test` / `kind-down` |

## Running it

```sh
make kind-up            # builds the images, creates the cluster, installs everything (~6 min cold)
make kind-test          # seeds two zones + group demo, deploys, runs tests/kind + e2e + conformance
make kind-down          # deletes the cluster
```

`kind-test` seeds itself through the console API, so it is safe to re-run. Endpoints after `kind-up`:

```
console   http://localhost:18000        admin@ramen.local / Kind-Console-1!
zone a    localhost:18081  (gRPC h2c)   namespace ramen-demo-a on node label kind-a
zone b    localhost:18082  (gRPC h2c)   namespace ramen-demo-b on node label kind-b
fake GCS  http://localhost:18083/storage/v1/b/ramen-kind-groups/o
```

Needs ~4 CPU and ~5 GiB for the container runtime, and ~8 GiB of free image space. On colima:
`colima start --cpu 4 --memory 5`.
