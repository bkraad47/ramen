# How it works (and why)

## The problem
MCP servers are easy to write and hard to run. A team needs a place to put tools that talk to internal systems,
a way to ship changes without an outage, secrets that never leak into prompts or logs, an audit trail of who called
what, and a network edge that only trusted ranges can reach. Ramen is that place.

## The shape
```
git repo (mcp/tools, mcp/resources, mcp/prompts)
   │  deploy (console)
   ▼
bucket  gs://…/<group>  |  s3://…/<group>  |  /buckets/<group>
   │  sync on load
   ▼
worker pod  ┌───────────────┐  JSON-RPC over stdio  ┌──────────────────┐
            │ ramen-node    │ ───────────────────▶  │ ramen_runtime    │
  /mcp ───▶ │ (Rust)        │ ◀───────────────────  │ (Python 3.14)    │
            │ auth, CIDR,   │                       │ loads protos,    │
            │ metrics, logs │                       │ runs your code   │
            └───────────────┘                       └──────────────────┘
   ▲
   │ https://<lb>/mcp/<group>/<zone>   Authorization: Bearer rmk_…
MCP client (Claude Desktop, Cursor, an agent, curl)
```

A **group** is a tenant: it owns one git repo, one bucket prefix, its secrets, keys and users. An **environment** binds
a group to a git ref and to one or more **zones**; a zone is a Kubernetes namespace pinned to a cloud zone with a
`worker` and a `worker-canary` Deployment. Each worker pod runs the Rust node and, on demand, the Python runtime.

## Why a Rust node *and* a Python runtime
- The node owns everything that must not be slowed down or broken by user code: the HTTP surface, bearer-key
  auth, IP allow-lists, request bounding (`RAMEN_MAX_INFLIGHT`), timeouts, metrics and structured logs.
- The runtime owns everything users write: `pip install` of `mcp/requirements.txt`, proto validation, argument
  validation, secret substitution and the call itself. It is spawned when needed and killed after
  `RAMEN_SIDECAR_IDLE_SECS` of idleness, so a crash or a leak in tool code costs one respawn, not a pod.
- They talk over newline-delimited JSON-RPC on stdin/stdout ([contract §2](CONTRACTS.md)). No sockets, no
  ports, nothing else to secure.

## Why git → bucket → worker
Workers never clone. The console clones (with a `GITHUB_TOKEN` secret if needed), validates, and uploads to the
group's bucket prefix; workers sync by content hash on every load. This gives one auditable deploy step, a
rollback path (re-deploy an older ref), and no git credentials on worker pods.

## Why canary first
`deploy` writes the environment's config (keys, secrets, blocked tools) into the zone, restarts `worker-canary`,
waits for readiness, triggers `/admin/reload`, smoke-tests `tools/list`, and only then rolls `worker`. Any failure
scales the canary back to 0 and leaves the stable track untouched. See [Canary deploys](wiki/canary.md).

## Why two kinds of key
- `rmk_…` **MCP keys** are minted per group and pushed to the workers of that group on deploy. MCP clients send
  them as `Authorization: Bearer`. No keys deployed = the node denies everything.
- `rmn_…` **API keys** are minted per console user, scoped to a role and a set of groups, and sent as
  `X-Ramen-Api-Key` to `/api/v1/*` for automation (CI deploys, rotation, backups). They never reach a worker.

## Why the console never shows a secret
Secrets are stored Fernet-encrypted (or in Secret Manager / Secrets Manager) and only ever leave the console as
`RAMEN_SECRET_<GROUP>__<NAME>` environment variables on worker pods. Tool code references them as
`{{$group.NAME}}`; the runtime substitutes at call time and redacts values from errors and logs.

## Why Firestore / DynamoDB and not Postgres
The console is a single stateless-ish node in front of a managed document store, so there is nothing to back up,
patch or fail over that the cloud does not already handle. Backups of console state are JSON exports tagged with
the release version, restorable at any time. See [decision D2](architecture/index.md#decisions).

## What "multizone HA" means here
The load balancer (GKE Gateway / ALB) routes `/mcp/<group>/<zone>` to that zone's NEG / target group. Adding a zone
adds a namespace, a service account and a route; nothing in the data model or the deploy code assumes one zone.
Rebalance adjusts backend capacity per zone from the console; IP rules become Cloud Armor / WAF policies plus a
node-level CIDR check, so a misconfigured LB still cannot expose a worker.
