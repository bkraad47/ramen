# Throttling with Redis

Two limits, each a fixed window per minute: calls per **source IP** and calls per **token**, where a token is an
MCP key or a person's OAuth token. A limit that trips answers `429` on HTTP and `RESOURCE_EXHAUSTED` on gRPC.
Zero means unlimited.

The scope counters live in one Redis shared by every zone of the group's environment, so a client throttled on
zone `a` is throttled on zone `b` too. The item counters are per zone. Ramen does not provision Redis. Point it
at your own: a container in the cluster, Memorystore, ElastiCache.

## Two places to set it

| | Where | What it throttles |
|---|---|---|
| **Scope** throttle | Group page → *Throttling*, once per group and environment | Every call to that environment, across its zones and tools |
| **Item** throttle | Group page → *Zone actions* → *Item throttle* | Repeat calls to the same tool, resource or prompt on that one zone |

Both take a Redis URL and the two limits. The URL is write-only: stored encrypted, never shown back. A Redis
outage fails open rather than blocking traffic.

```sh
curl -s -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' -X PUT https://<edge>/api/v1/groups/demo/throttle \
  -d '{"redis_url":"redis://redis.ramen-system.svc.cluster.local:6379","ip_per_min":600,"token_per_min":120}'
```

Changes reach the workers on the next deploy.

## A Redis in the cluster

For a test, one Deployment and Service in `ramen-system` is enough:

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
a password in the URL.

## What it looks like

With `token_per_min` at 3, a burst on one key starts answering `429` after two or three calls, because the
deploy's own smoke test spends one of the window, and a call through another zone is a `429` as well. Both 0.6.0
cloud runs showed that.

The Redis URLs are dialed when a node starts, so changing a URL takes effect on the next deploy. The limit
numbers are read on every call.

Separate from these: the node bounds in-flight calls per pod with `RAMEN_MAX_INFLIGHT`, and the console limits
failed sign-ins per address with `RAMEN_LOGIN_RATE_LIMIT`.
