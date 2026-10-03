# How it works (and why)

## Why a platform, not a server
The Model Context Protocol made it easy to give a model a tool. It said nothing about running two hundred of them
for forty teams. The traditional answer is the one every team reaches for first: a server process per team, a key
pasted into a client config, a shared secret in an environment variable, and an operator who finds out who can call
what by reading the config files. It works for one team and breaks at the second.

Ramen's answer: tools are **code in git**, run by a **platform** that knows about organizations.

- **Built on how organizations work.** A *group* is a team. It owns a repo, a bucket prefix, its secrets, its
  members and the cloud identity its workers run as. People hold a role *per group* — Group Admin, Viewer or MCP
  User — so one person can run one group and merely use another. A change with real reach, such as granting a
  service-account permission, needs a second admin to approve it.
- **Agents and people are first-class callers.** A CI job or an agent platform presents a group key; a person
  signs in — password, magic link, Microsoft Entra ID, Google Workspace — and the client holds a token in their
  name. Every worker log line says who called.
- **A deploy is a release, not a restart.** Canary first, a smoke test, then the stable track; roll back by
  deploying the previous ref. Secrets reach workers only through a deploy and never through a prompt or a log.
- **Underneath: gRPC, JSON-RPC 2.0 and a Rust node. On top: Python.** The node owns the transport, the auth, the
  limits and the metrics; your code owns the tool. You never touch the node.

The rest of this page is the engineering behind those four claims.

## The problem
MCP servers are easy to write and hard to run. A team needs a place to put tools that talk to internal systems,
a way to ship changes without an outage, secrets that never leak into prompts or logs, an audit trail of who called
what, and a network edge that only trusted ranges can reach. Ramen is that place.

## The shape

<figure class="ramen-diagram" markdown>
![One MCP call end to end: client, load balancer, the zone's worker pod, Rust node, Python runtime](img/architecture.svg)
<figcaption>An MCP client posts each JSON-RPC message to /mcp over HTTPS (a stdio client goes through ramen-mcp-bridge over gRPC). The load balancer matches the ramen-group and ramen-zone headers and forwards to that zone's worker pod, where the Rust node checks the credential and source range and hands the message to the Python runtime, which loads the group's code from the bucket. The console clones the group repo, uploads it on deploy and calls the node directly on its pod IP.</figcaption>
</figure>

A **group** is a tenant: it owns one git repo, one bucket prefix, its secrets, keys and users. An **environment** binds
a group to a git ref and to one or more **zones**; a zone is a Kubernetes namespace pinned to a cloud zone with a
`worker` and a `worker-canary` Deployment. Each worker pod runs the Rust node and, on demand, the Python runtime.

## Why Streamable HTTP at the edge, and gRPC inside (v0.5.0)
0.3.1 moved the worker to JSON-RPC 2.0 over gRPC and took the HTTP endpoint away; every standard client then needed
a locally installed stdio bridge. That ruled out phones, browsers and hosted agent platforms — anything that cannot
run a child process. 0.5.0 keeps what gRPC bought and puts Streamable HTTP back as the front door
([contract §16](CONTRACTS.md)):

- **Streamable HTTP is a URL and a header.** The MCP specification defines exactly this: `POST /mcp` with an
  `Authorization: Bearer` header, one JSON-RPC message per request. Every standard client speaks it natively, so
  there is nothing extra to install.
    - Browsers are admitted only from an allow-listed `Origin`.
    - A session id is signed and tied to the credential that created it, so it is useless under a different key.
    - A person can hold a token of their own through OAuth instead of a shared key.
- **gRPC stays for what it is good at inside.** `ramen.v1.Mcp/Call` keeps binary framing, HTTP/2 multiplexing,
  first-class health and deadlines, and typed transport status. Teams that want it internally use it directly; the
  stdio bridge speaks it for clients that only speak stdio.
- **One implementation of every guard.** The HTTP handler has no checks of its own. It turns the request headers
  into the same metadata map and calls the same guard and dispatch functions the gRPC service calls; the
  transports differ only in how a refusal is spelled — `401 / 403 / 429 / 413` against
  `UNAUTHENTICATED / PERMISSION_DENIED / RESOURCE_EXHAUSTED / OUT_OF_RANGE`. A JSON-RPC error stays a `200` with an
  error body on both, so a client can still tell "the edge rejected me" from "the tool said no". The conformance
  suite runs the whole guard table on both transports, which is what stops the two paths from drifting.
- **Routing by headers, not path**, exactly as before: the load balancer matches `ramen-group` and `ramen-zone`,
  so the same client config works locally and in the cloud by changing only the URL.

The cost is one port that speaks two protocols. hyper handles that: HTTP/1.1 for Streamable HTTP clients, HTTP/2
for gRPC, on the same listener, with TLS at the load balancer (or at the node, when configured). See
[Transport and what secures each hop](wiki/transport.md) for what protects each leg — including the legs that are
plaintext unless you configure TLS — and the [threat model](threat-model.md) for what is and is not defended.

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

## Why a managed document store by default
The console keeps no state of its own; it sits in front of a managed document store, so there is nothing to back up,
patch or fail over that the cloud does not already handle. Backups of console state are JSON exports tagged with
the release version, restorable at any time. Postgres is an optional third store for deployments that must stay off
a managed cloud database; the repository interface is the same. See decisions D2 and D37 in the
[architecture](architecture/index.md#decisions).

## What "multizone HA" means here
The load balancer (GKE Gateway / ALB) routes calls whose metadata says `ramen-group: <g>` and `ramen-zone: <z>` to
that zone's NEG / target group, with a gRPC health check on every backend. Adding a zone adds a namespace, a
service account and a route; nothing in the data model or the deploy code assumes one zone. Rebalance adjusts
backend capacity per zone from the console; IP rules become a Cloud Armor policy at the edge (one per group) plus
a per-zone CIDR check on the node. Proxies append to the right-hand end of `x-forwarded-for`, so the node counts
back from the right by a configured number of hops. That lands on the real client rather than the load balancer,
and a caller cannot forge it. Get the hop count wrong and the node falls back to the peer address and denies
rather than admits. What stops traffic from reaching a worker directly is the worker `NetworkPolicy`, not this
allow-list. See
[Transport and what secures each hop](wiki/transport.md#the-source-range-allowlist).
