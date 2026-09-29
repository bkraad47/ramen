# How it works (and why)

## The problem
MCP servers are easy to write and hard to run. A team needs a place to put tools that talk to internal systems,
a way to ship changes without an outage, secrets that never leak into prompts or logs, an audit trail of who called
what, and a network edge that only trusted ranges can reach. Ramen is that place.

## The shape

<figure class="ramen-diagram" markdown>
![One MCP call, end to end: a stdio MCP client talks to ramen-mcp-bridge, which sends each JSON-RPC message as one ramen.v1.Mcp/Call over gRPC to the load balancer; the load balancer matches the ramen-group and ramen-zone metadata and forwards to that zone's worker pod, where the Rust node checks the key and source range and hands the message to the Python runtime, which loads the group's code from the group bucket; alongside, the console clones the group git repo, uploads it on deploy and calls the node directly on its pod IP](img/architecture.svg)
</figure>

A **group** is a tenant: it owns one git repo, one bucket prefix, its secrets, keys and users. An **environment** binds
a group to a git ref and to one or more **zones**; a zone is a Kubernetes namespace pinned to a cloud zone with a
`worker` and a `worker-canary` Deployment. Each worker pod runs the Rust node and, on demand, the Python runtime.

## Why JSON-RPC 2.0 over gRPC (v0.3.1)
The MCP messages did not change; the envelope did. Every JSON-RPC request or notification travels as the `body`
bytes of one `ramen.v1.Mcp/Call` ([contract §11](CONTRACTS.md), [`mcp.proto`](https://github.com/bkraad47/ramen/blob/main/proto/ramen/v1/mcp.proto)).
What that buys, compared with the HTTP endpoint 0.1.0–0.3.0 exposed:

- **Binary framing and HTTP/2 multiplexing**: many in-flight calls per connection, no per-request handshake, and
  a hard 4 MiB message limit enforced by the framework rather than by hand.
- **First-class health and deadlines**: `grpc.health.v1.Health` reports `SERVING` only once the runtime has
  loaded code, so the LB, Kubernetes and the console all read the same signal; every call carries a deadline
  the node honours instead of an ad-hoc timeout header.
- **Typed status for the transport, JSON-RPC for the protocol**: a bad key is `UNAUTHENTICATED`, a blocked
  source range is `PERMISSION_DENIED`, too many calls is `RESOURCE_EXHAUSTED`; a blocked tool is still JSON-RPC
  `-32601` in the body. Clients can tell "the edge rejected me" from "the tool said no".
- **Routing by metadata, not path**: the load balancer matches `ramen-group` and `ramen-zone` headers, so there
  is no path rewrite on GCP and no path alias on AWS, and the same client config works locally and in the cloud
  by changing only `--target`.

The cost is that browsers and plain MCP-over-HTTP clients cannot connect directly. `ramen-mcp-bridge` (a Python
console script, also in the worker image) is a stdio MCP server that forwards each message to `Mcp/Call`, so
Claude Desktop, Cursor and the `mcp` SDK see an ordinary stdio server. See the
[migration note](how-tos/migrate-0.3.1.md), and
[Transport and what secures each hop](wiki/transport.md) for what protects each leg of that path — including the
legs that are plaintext unless you configure TLS.

## Why a Rust node *and* a Python runtime
- The node owns everything that must not be slowed down or broken by user code: the gRPC surface, bearer-key
  auth (constant-time compare), IP allow-lists, request bounding (`RAMEN_MAX_INFLIGHT`), timeouts, health,
  metrics and structured logs.
- The runtime owns everything users write: `pip install` of `mcp/requirements.txt`, proto validation, argument
  validation, secret substitution and the call itself. It is spawned when needed and killed after
  `RAMEN_SIDECAR_IDLE_SECS` of idleness, so a crash or a leak in tool code costs one respawn, not a pod.
- They talk over newline-delimited JSON-RPC on stdin/stdout ([contract §2](CONTRACTS.md)). No sockets, no
  ports, nothing else to secure.

## Why git → bucket → worker
Workers never clone. The console clones (with a `GITHUB_TOKEN` secret if needed, passed as a git header and never
written into the remote URL), validates, and uploads to the group's bucket prefix; workers sync by content hash on
every load. This gives one auditable deploy step, a rollback path (re-deploy an older ref), and no git credentials
on worker pods.

## Why canary first
`deploy` writes the environment's config (keys, secrets, blocked tools) into the zone, restarts `worker-canary`,
waits for `Health/Check` = `SERVING`, calls `Admin/Reload`, smoke-tests `tools/list` through `Mcp/Call`, and only
then rolls `worker`. Any failure scales the canary back to 0 and leaves the stable track untouched. See
[Canary deploys](wiki/canary.md).

## Why two kinds of key
- `rmk_…` **MCP keys** are generated per group and pushed to the workers of that group on deploy. MCP clients send
  them as gRPC metadata `authorization: Bearer rmk_…` (the bridge does this for you with `--key`). No keys
  deployed = the node denies everything.
- `rmn_…` **API keys** are generated per console user, scoped to a role and a set of groups, and sent as
  `X-Ramen-Api-Key` to the console's `/api/v1/*` (still HTTP) for automation (CI deploys, rotation, backups).
  They never reach a worker.

## Why the console never shows a secret
Secrets are stored Fernet-encrypted (or in Secret Manager / Secrets Manager) and only ever leave the console as
`RAMEN_SECRET_<GROUP>__<NAME>` environment variables on worker pods. Tool code references them as
`{{$group.NAME}}`; the runtime substitutes at call time and redacts values from errors and logs.

## Why Firestore / DynamoDB and not Postgres
The console is a single stateless-ish node in front of a managed document store, so there is nothing to back up,
patch or fail over that the cloud does not already handle. Backups of console state are JSON exports tagged with
the release version, restorable at any time. See [decision D2](architecture/index.md#decisions).

## What "multizone HA" means here
The load balancer (GKE Gateway / ALB) routes calls whose metadata says `ramen-group: <g>` and `ramen-zone: <z>` to
that zone's NEG / target group, with a gRPC health check on every backend. Adding a zone adds a namespace, a
service account and a route; nothing in the data model or the deploy code assumes one zone. Rebalance adjusts
backend capacity per zone from the console; IP rules become a Cloud Armor policy at the edge (one per group) plus
a per-zone CIDR check on the node. The node takes the client address from `x-forwarded-for` at a configured
number of hops counted from the right — the end the proxies append to — so it checks the client rather than the
load balancer, and a caller cannot choose the address it reads. Get the hop count wrong and it falls back to the
peer address and denies rather than admits. What stops something reaching a worker directly is the worker
`NetworkPolicy`, not this list. See
[Transport and what secures each hop](wiki/transport.md#the-source-range-allowlist).
