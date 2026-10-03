# How it works

## The AI-native approach, versus the traditional one

The Model Context Protocol made it easy to give a model a tool. It said nothing about running two hundred of them
for forty teams. The traditional answer is one server process per team, a key pasted into a client config and an
operator who learns who can reach what by reading config files. It works for one team and breaks at the second.

Ramen's answer is a platform built on how organizations work. Tools are code in git. A group is a team that owns
its repo, its secrets and its members. People hold a role per group. Clients sign in as themselves. A deploy is a
canary, a smoke test and a rollout. The transport underneath is gRPC and JSON-RPC 2.0 in a Rust node, and none of
it reaches your Python.

## The shape

<figure class="ramen-diagram" markdown>
![One MCP call end to end: client, load balancer, the zone's worker pod, Rust node, Python runtime](img/architecture.svg)
</figure>

<p class="ramen-caption" markdown>
A client posts each JSON-RPC message to `/mcp` over HTTPS. A stdio client goes through the bridge over gRPC. The
load balancer reads the `ramen-group` and `ramen-zone` headers and forwards to that zone's worker pod. The Rust
node checks the credential and the source range, then hands the message to the Python runtime, which runs your
code from the bucket. The console clones the repo, uploads it on deploy and talks to nodes on their pod IP.
</p>

- A **group** is a tenant. It owns one git repo, one bucket prefix, its secrets, its keys and its members.
- An **environment** binds the group to a git ref and to one or more zones.
- A **zone** is a Kubernetes namespace pinned to a cloud zone, with its own service account, load-balancer route
  and IP rules. It runs a `worker` and a `worker-canary` Deployment.
- A **worker** is one pod: the Rust node and, on demand, the Python runtime.

The console keeps its state in Firestore on GCP, DynamoDB on AWS, or Postgres where you host it yourself.
[Architecture](architecture/index.md) has the components, the data model and every decision.

## Why a Rust node and a Python runtime

The node owns what must never be slowed down or broken by user code: the transport, the key check, the IP
allow-list, the in-flight limit, timeouts, health and the access log. It is a small static binary with no
interpreter. The runtime owns what you write: `pip install`, validation, secret substitution and the call. The two
talk over JSON-RPC on stdin and stdout, so there is no extra port to secure. The runtime is started when needed and
stopped after an idle timeout. A crash in tool code costs one restart, not the pod that holds the keys.

## The features

| | What it is | Where |
|---|---|---|
| **Streamable HTTP and gRPC** | `POST /mcp` for every standard client, `ramen.v1.Mcp/Call` over gRPC for the bridge and internal callers. One port, one set of guards. | [Transport, hop by hop](wiki/transport.md) |
| **HTTPS only** | Cloud edges listen on 443 only, so plain http never connects. Behind a proxy that does terminate http, the console answers 301 for GET and 403 otherwise, and sets HSTS. | [Deploy on GCP](wiki/deploy-gcp.md) |
| **Canary deploys** | Sync the repo to the bucket, roll one canary pod, reload, smoke-test `tools/list`, then roll stable. A failure leaves stable untouched. | [Groups, zones and regions](wiki/groups-zones.md#deploying) |
| **Roles per group** | Super admin globally. Group Admin, Viewer or MCP User per group. One person can run one group and only use another. | [Users and access](wiki/users-access.md) |
| **OAuth for people** | The console is an OAuth 2.1 authorization server with PKCE. Claude Code and the bridge get a token for one group and zone. There is no dynamic client registration, so a client needs its id configured. | [Connect with OAuth](wiki/connect-oauth.md) |
| **OIDC sign-in** | Password, magic link, or Microsoft Entra ID and Google Workspace, with claim-to-role rules per group. | [Entra ID and Workspace](wiki/sso.md) |
| **MCP keys and API keys** | `rmk_` keys open a group's workers. `rmn_` keys drive the console API. Each side refuses the other. | [API keys and the API](wiki/api-keys.md) |
| **Secrets** | Named values per group, environment and zone. Never shown back. Reach code as `{{$group.NAME}}` or through `mcp/env.yaml`. | [Secrets](wiki/secrets.md) |
| **Fernet at rest** | `RAMEN_FERNET_KEY` encrypts password hashes, secret values, key hashes and tokens in the store. | [Configuration](wiki/configuration.md) |
| **Cloud identity per zone** | Each zone's workers run as their own service account or IAM role, with permissions a second admin approves. | [Users and access](wiki/users-access.md#service-account-permissions) |
| **IP rules** | Per zone at the node; at the edge one Cloud Armor policy per group on GCP, one WAF IP set per group on AWS. | [Groups, zones and regions](wiki/groups-zones.md#ip-rules) |
| **Throttling** | Per-IP and per-token limits per minute, shared across zones through Redis. | [Throttling](wiki/throttling.md) |
| **Blocked tools** | Per environment and per zone, enforced by the node. A blocked name vanishes from `tools/list`. | [Groups, zones and regions](wiki/groups-zones.md#packages-per-zone) |
| **Logs and audit** | One JSON line per MCP call naming the key or the person. Every console action with user, address and outcome. | [Users and access](wiki/users-access.md#audit-and-logs) |
| **Backups** | JSON exports of the console state, tagged with the release, with preview and restore. | [Backups](wiki/backups.md) |
| **Worker images** | A group can run a recorded worker image with its own system packages. | [The MCP repo](wiki/mcp-repo.md#heavy-dependencies) |
| **Agent skills** | `SKILL.md` playbooks for deploy, rotate, backup and scale. | [Cloud-ops skills](wiki/skills.md) |

## What is verified

CI runs the whole guard table on both transports on real node processes, on Linux and on a Windows runner, and
every release is deployed to a throwaway GKE project and a real AWS account emptied the same day. Node TLS is
covered by tests only, and Entra ID and Google Workspace sign-in have never been run against a real tenant.
[What each run covered, hop by hop](wiki/transport.md#what-is-verified-and-what-is-not), and
[what the threat model does and does not defend](threat-model.md).
