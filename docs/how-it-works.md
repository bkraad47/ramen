# How it works

## The AI-native approach, versus the traditional one

The Model Context Protocol made it easy to give a model a tool. It said nothing about running two hundred of them
for forty teams. The traditional answer is one server process per team, a key pasted into a client config and an
operator who learns who can reach what by reading config files. It works for one team and breaks at the second.

Ramen's answer: tools are code in git. A group is a team that owns its repo, its secrets and its members. People
hold a role per group. Clients sign in as themselves. A deploy is a canary, a smoke test and a rollout. The
transport is Streamable HTTP, gRPC and JSON-RPC 2.0 in a Rust node, and none of it reaches your Python.

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
- A **zone** is a Kubernetes namespace pinned to a cloud zone, with its own cloud identity, load-balancer route
  and IP rules. It runs a `worker` and a `worker-canary` Deployment, and Ramen scales `worker` between 1 and the
  zone's max.
- A **worker** is one pod: the Rust node and, on demand, the Python runtime.

The same shape runs on GKE, EKS and AKS. The console keeps its state in Firestore on GCP, DynamoDB on AWS, Table
Storage on Azure, or Postgres where you host it yourself.
[Architecture](architecture/index.md) has the components, the data model and every decision.

## Why a Rust node and a Python runtime

The node owns what must never be slowed down or broken by user code: the transport, the key check, the IP
allow-list, the in-flight limit, timeouts, health and the access log. It is a small static binary with no
interpreter. The runtime owns what you write: `pip install`, validation, secret substitution and the call. The two
talk over JSON-RPC on stdin and stdout, so there is no extra port to secure. The runtime is started when needed and
stopped after an idle timeout. A crash in tool code costs one restart, not the pod that holds the keys.
Guardrails run in the runtime too: a rail sees exactly the arguments the tool is about to get and the result
it is about to return, and secret values never reach it.

## The features

| | What it is | Where |
|---|---|---|
| **Streamable HTTP and gRPC** | `POST /mcp` for every standard client, `ramen.v1.Mcp/Call` over gRPC for the bridge and internal callers. One port, one set of guards. | [Transport, hop by hop](wiki/transport.md) |
| **HTTPS only** | Cloud edges listen on 443 only, so plain http never connects. Behind a proxy that does terminate http, the console answers 301 for GET and 403 otherwise, and sets HSTS. | [Deploy on GCP](wiki/deploy-gcp.md) |
| **Canary deploys** | Sync the repo to the bucket, roll one canary pod, reload, smoke-test `tools/list`, then roll stable. A failure leaves stable untouched. | [Groups, zones and regions](wiki/groups-zones.md#deploying) |
| **Three clouds** | GKE (Gateway, Cloud Armor), EKS (ALB, WAF), AKS (Envoy Gateway, Let's Encrypt), from Terraform and Helm. | [GCP](wiki/deploy-gcp.md), [AWS](wiki/deploy-aws.md), [Azure](wiki/deploy-azure.md) |
| **Autoscaling** | The console scales each zone's workers between 1 and an admin's max from in-flight calls and call rate: up fast, down carefully, every step audited. No HPA. | [Autoscaling](wiki/autoscaling.md) |
| **Guardrails** | Per tool, before the call and after the result, inside the worker: NeMo Guardrails rails or your own `policy.py`. Fail-closed. | [Guardrails](wiki/guardrails.md) |
| **Roles per group** | Super admin globally. Group Admin, Viewer, MCP User, or a custom role on one of those bases, per group; a custom role can be OAuth-only. | [Users and access](wiki/users-access.md) |
| **OAuth for people** | The console is an OAuth 2.1 authorization server with PKCE. Claude Code (over HTTP) and the bridge (for any stdio client) get a token for one group and zone. There is no dynamic client registration, so a client needs its id configured; the group page prints the commands. | [Connect with OAuth](wiki/connect-oauth.md) |
| **Identity federation** | Microsoft Entra ID, Google Workspace or any OIDC provider, set up on the Config page; groups read from Graph or Cloud Identity at sign-in, mapped to roles per group, next to what admins set by hand. | [Entra ID and Workspace](wiki/sso.md) |
| **MCP keys and API keys** | `rmk_` keys open a group's workers. `rmn_` keys drive the console API. Each side refuses the other. | [API keys and the API](wiki/api-keys.md) |
| **Secrets** | Named values per group, environment and zone. Never shown back. Reach code as `{{$group.NAME}}` or through `mcp/env.yaml`. | [Secrets](wiki/secrets.md) |
| **Fernet at rest** | `RAMEN_FERNET_KEY` encrypts password hashes, secret values, key hashes and tokens in the store; key and token hashes are salted. | [Configuration](wiki/configuration.md) |
| **Cloud identity per zone** | Each zone's workers run as their own service account or IAM role, with permissions a second admin approves. | [Users and access](wiki/users-access.md#service-account-permissions) |
| **IP rules** | Per zone at the node; at the edge one Cloud Armor policy per group on GCP, one WAF IP set per group on AWS, an Envoy `SecurityPolicy` per zone on AKS. | [Groups, zones and regions](wiki/groups-zones.md#ip-rules) |
| **Throttling** | Per-IP and per-token limits per minute, shared across zones through Redis. | [Throttling](wiki/throttling.md) |
| **Blocked tools** | Per environment and per zone, enforced by the node. A blocked name vanishes from `tools/list`. | [Groups, zones and regions](wiki/groups-zones.md#packages-per-zone) |
| **Logs and audit** | One JSON line per MCP call naming the key or the person. Every console action with user, address and outcome. | [Users and access](wiki/users-access.md#audit-and-logs) |
| **Backups** | JSON exports of the console state, tagged with the release, with preview and restore. | [Backups](wiki/backups.md) |
| **Worker images** | A group can run a recorded worker image with its own system packages. | [The MCP repo](wiki/mcp-repo.md#heavy-dependencies) |
| **Agent skills** | `SKILL.md` playbooks for deploy, rotate, backup and scale. | [Cloud-ops skills](wiki/skills.md) |

## What is verified

0.8.0 passed the release suite and the load tests live on GKE, EKS and AKS; what each run covered, and what is tested only:
[Transport → What is verified](wiki/transport.md#what-is-verified-and-what-is-not). The boundaries:
[Threat model](threat-model.md).
