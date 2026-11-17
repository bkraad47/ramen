# Throttling with Redis

Two limits, each a fixed window per minute: calls per **source IP** and calls per **token**, where a token is an
MCP key or a person's OAuth token. A limit that trips answers `429` on HTTP and `RESOURCE_EXHAUSTED` on gRPC.
Zero means unlimited.

The scope counters live in one Redis shared by every zone of the group's environment, so a client throttled on
zone `a` is throttled on zone `b` too. The item counters are per zone. The chart can run one Redis for every group
(`redis.enabled`, below); otherwise point it at your own: a container in the cluster, Memorystore, ElastiCache.

## Two places to set it

| | Where | What it throttles |
|---|---|---|
| **Scope** throttle | Group page → *Throttling*, once per group and environment | Every call to that environment, across its zones and tools |
| **Item** throttle | Group page → *Zone actions* → *Item throttle* | Repeat calls to the same tool, resource or prompt on that one zone |

Both take a Redis URL and the two limits. The URL is write-only: stored encrypted, never shown back. A Redis
outage fails open rather than blocking traffic.

```sh
curl -s -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' -X PUT https://<edge>/api/v1/groups/demo/throttle \
  -d '{"redis_url":"rediss://:<password>@redis.ramen-system.svc.cluster.local:6379","ip_per_min":600,"token_per_min":120}'
```

The URL and both limits reach the workers in their deploy config, so a change takes effect on the next deploy;
until then the workers count against the old limits.

## A Redis in the cluster

Since 1.0.0c the chart runs one for you: `redis.enabled: true` adds a password-protected Redis to `ramen-system`
and the console hands its URL to every zone whose group sets none here
([Configuration](configuration.md#network-policies-and-redis-100c)). Since D86 it is TLS only (`rediss://`, a leaf
from the install CA), and a cloud install refuses a `redis://` URL here (422) — a hand-made plaintext Redis like the
one below works only on a stack without `RAMEN_REQUIRE_TLS` (a test outside the chart):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: redis}
spec:
  replicas: 1
  selector: {matchLabels: {app: redis}}
  template:
    metadata: {labels: {app: redis}}
    spec: {containers: [{name: redis, image: redis:7-alpine, ports: [{containerPort: 6379}]}]}
---
apiVersion: v1
kind: Service
metadata: {name: redis}
spec: {selector: {app: redis}, ports: [{port: 6379}]}
```

The URL is then `redis://redis.ramen-system.svc.cluster.local:6379`. For production use a managed instance with
a password in the URL: the chart value `redis.externalUrl` (keep `redis.enabled: false`) writes it into Secret
`ramen-redis` and the console hands it to every zone. Managed instances are TLS-only — `rediss://host:port/0`; the
node speaks it since 1.0.0c. On Azure, Terraform creates an Azure Managed Redis and `helm_set` carries the URL
([Deploy on Azure](deploy-azure.md)).

### Replacing a hand-applied Redis with the chart's (1.0.0c, D85)

The chart's Redis is a Deployment and a Service both named `redis` in `ramen-system`, so Helm refuses to install it
next to a hand-applied one ("exists and cannot be imported into the current release"). Move over in three steps:

```sh
kubectl -n ramen-system delete deployment/redis service/redis          # the hand-applied one (labels app: redis)
helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set redis.enabled=true
# a group that typed the old URL on its Throttling card: clear it (the chart's Redis is then the default) or set the
# new one with its password — kubectl -n ramen-system get secret ramen-redis -o jsonpath='{.data.url}' | base64 -d
```

then redeploy the groups (the zones get the new URL and password in their deploy config). Throttle counters and
in-flight tasks in the old Redis are lost, which is harmless: both are short-lived by contract. The per-zone
`worker-egress` policy admits both the chart's Redis (`app.kubernetes.io/name: redis`) and a legacy `app: redis` one,
so a cluster that keeps its hand-applied Redis still throttles — but that Redis has no password and no NetworkPolicy.

## When Redis is unreachable

A Redis outage fails open: calls are served, not throttled. Since 1.0.0c it is no longer silent:

- every worker re-checks each configured Redis every 30 s (a `PING`, or a fresh dial when it was never reached) and
  logs `redis unreachable: throttling is off for this pod until it answers` / `redis reachable: throttling is on for
  this pod` when that changes — a Redis that comes back turns throttling on again without a restart;
- `Admin/Metrics` (and the `Admin/Reload` answer) carry `redis`: `"ok"`, `"unreachable"` or `"off"` (none configured);
- the group page's zone row shows **throttling off: Redis unreachable** (with how many of the zone's workers), and the
  worker line says `redis unreachable`;
- a deploy whose canary reports it logs `zone <z>: warning: throttling off: Redis unreachable from the canary` — a
  warning, not a refusal.

The shared task store is chosen when the pod starts: a pod that started without its Redis keeps tasks in memory (or
off in a cluster) until it restarts, even after throttling is back.

## What it looks like

With `token_per_min` at 3, a burst on one key starts answering `429` after two or three calls, because the
deploy's own smoke test spends one of the window, and a call through another zone is a `429` as well. Both 0.6.0
cloud runs showed that.

Separate from these: the node bounds in-flight calls per pod with `RAMEN_MAX_INFLIGHT`, and the console limits
failed sign-ins per address with `RAMEN_LOGIN_RATE_LIMIT`.
