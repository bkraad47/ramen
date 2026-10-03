# Features, and how each one is managed

A **group** is a team. It owns a repo of tools, and everything else —
who may change it, where it runs, what it may reach, who may call it — is decided per group. This page lists every
feature with what it is, where you manage it (console page, API) and the how-to that goes deeper.

## Groups, environments, zones, workers
| | What it is | Manage it |
|---|---|---|
| **Group** | A tenant: one git repo (URL + ref), one bucket prefix, its own secrets, keys, members and service-account rules. | Super admins create and delete (deleting destroys the group's cloud resources). *Groups* page · `POST/DELETE /api/v1/groups`. |
| **Environment** | A group's deployable configuration: a git ref, the zones it runs in, a verbose flag, the blocked-package list and the last deploy result. | Group admins. Group page → *Environments* (zones are a checkbox dropdown) · `/api/v1/groups/{g}/environments`. |
| **Zone** | Where workers run: a Kubernetes namespace `ramen-<group>-<zone>` pinned to a cloud location, with its own service account, load-balancer route and IP rules. `local` is the compose worker. | Super admins create zones (*Zones and workers* page · `POST /api/v1/zones`); group admins attach them to environments. A zone that is deleted, or dropped by every environment of a group, is **torn down for real** (namespace, workers, identity). |
| **Workers** | One pod = the Rust MCP node + the Python runtime. Two tracks per zone, `worker` and `worker-canary`. | Count and size on the group page's *Zone actions* (group admins; sizes allowed per zone by super admins) · `PUT /api/v1/groups/{g}/zones/{z}/workers`. |

## Deploying code
| | What it is | Manage it |
|---|---|---|
| **Git → bucket → worker** | The console clones the group repo at the environment's ref, validates the packages and uploads them to the bucket; workers sync by content hash on every load. No git credentials on workers. | Group page → *Deploy* (canary or straight) · `POST /api/v1/groups/{g}/environments/{e}/deploy`. [Add and deploy a tool](how-tos/add-a-tool.md). |
| **Canary by default** | Every deploy rolls the canary track first, smoke-tests `tools/list`, then rolls stable; a failure scales the canary to 0 and leaves stable untouched. | The *Deploy jobs* table on the group page; `GET /api/v1/jobs/{id}`. [Canary deploys](wiki/canary.md). |
| **Packages per zone** | What each zone's workers reported after the last deploy (tools, resources, prompts) with Enable/Disable per zone and *Blocked everywhere* per environment (a checkbox dropdown). | Group page → *Packages per zone*. Blocked names vanish from `tools/list` and answer the JSON-RPC `-32601` (method not found) error. |
| **Worker image pins** | A group can run a recorded worker image instead of the release one (extra system packages, a pinned Python stack). | Group page → *Worker image* (super admins) · `POST /api/v1/groups/{g}/images`. [DevOps with the API](how-tos/devops-api.md#pin-a-worker-image-per-group). |
| **Auto-rebalance** | Watches every group × zone for a skewed worker and rebalances it; each run is audited as `scheduler`. | Config page → *Auto-rebalance scheduler* (super admins). [Rebalance and load](wiki/rebalance.md). |

## Secrets and cloud identity
| | What it is | Manage it |
|---|---|---|
| **Secrets** | Named values scoped to a group, optionally to an environment and zone; never shown back. Reach workers as `RAMEN_SECRET_<GROUP>__<NAME>` on deploy; tool code writes `{{$group.NAME}}` (call arguments) or reads them from `mcp/env.yaml`. | *Secrets* page (super and group admins) · `/api/v1/groups/{g}/secrets`. [Secrets](how-tos/secrets.md), [Service accounts and secrets](how-tos/service-accounts.md). |
| **Service-account permissions** | Each zone's workers run as their own cloud identity (GCP service account with Workload Identity, AWS IAM role with IRSA). A group admin requests a permission from the catalogue, optionally scoped to named buckets/secrets; another admin of the group or a super admin approves; the console binds the cloud roles. | Group page → *Service-account permissions* (admins only). [Service accounts and secrets](how-tos/service-accounts.md). |
| **Service-account rules and restrictions** | Super-admin rules gate what may be requested at all (deny wins, allow whitelists); group restrictions add the group's own limits. | Config page (super admins) and group page → *Service-account restrictions* (group admins). |

## People and access
| | What it is | Manage it |
|---|---|---|
| **Roles per group** | A person holds a role in each group: *Group Admin*, *Viewer* or *MCP User*; super admins are global. One person can be an admin of one group and only an MCP user of another. | *Users* page: one member table per group with a role dropdown, Remove and Add member · `/api/v1/groups/{g}/members`. [Security](how-tos/security.md). |
| **Sign-in** | Password, magic link, or any OpenID Connect provider (Microsoft Entra ID, Google Workspace, …). | Config page → *Authentication* and *OAuth role mapping*. [Sign in with Entra ID / Google Workspace](how-tos/sso.md). |
| **Role mapping from the IdP** | Rules per provider: a claim value → a Ramen role in named groups, authoritative on every login. | Config page (super admins) · `PUT /api/v1/config/auth/role-map/{provider}`. |
| **MCP keys** `rmk_` | A group's shared bearer key for automation and agents; pushed to the group's workers on deploy. | Group page → *MCP auth keys* (group admins). [Connect a client](how-tos/mcp-clients.md). |
| **OAuth for people** | The console is the OAuth 2.1 authorization server: a registered client (Claude Code, Claude Desktop, Cursor, the bridge) signs the person in and gets a token scoped to one group and zone. | Config page → *OAuth clients* (super admins). [Connect a client](how-tos/mcp-clients.md). |
| **API keys** `rmn_` | Console automation keys for `/api/v1/*`, scoped to a role and groups. | *API keys* page (super admins only). [DevOps with the API](how-tos/devops-api.md). |

## Network and transport
| | What it is | Manage it |
|---|---|---|
| **Streamable HTTP at the edge, gRPC inside** | `POST /mcp` for every standard client; `ramen.v1.Mcp/Call` over gRPC for the bridge and internal callers; one port, one set of guards. HTTPS only on a public console. | Nothing to switch; the load balancer routes on `ramen-group` / `ramen-zone` headers. [Transport and security](wiki/transport.md). |
| **IP allow-lists** | Per zone at the node (`RAMEN_ALLOWED_CIDRS`) and at the edge (one Cloud Armor policy / WAF IP set per group). | Group page → *Zone actions* → IP rules. |
| **Throttling** | Per-IP and per-token limits per minute, shared across zones through Redis. | Group page → *Throttling* (group/environment) and *Zone actions* → Item throttle. |

## Operations
| | What it is | Manage it |
|---|---|---|
| **Logs** | Each worker's access log (one JSON line per call naming the key id or `user:<id>`), Cloud Logging / CloudWatch in the cloud. | *Logs* page (group admins and viewers; MCP Users have no console pages). |
| **Audit** | Every console action with user, address, outcome and tags. | *Audit* page (super admins) · `GET /api/v1/audit`. |
| **Backups and restore** | JSON exports tagged with the release version (no secrets, no password hashes); preview, restore, restore-and-prune. | *Backups* page (super admins). [Backup and restore](how-tos/backup-restore.md). |
| **Console store** | Firestore on GCP, DynamoDB on AWS, or Postgres where self-hosted; one repository interface. | `RAMEN_STORE` · [Storage backend](how-tos/storage-backend.md). |
| **Configuration** | A yaml file (`RAMEN_CONFIG`) under `RAMEN_*` environment variables, hot-reloadable; SMTP and warning emails; the GitHub App. | Config page (super admins). [Configuration](how-tos/configuration.md). |
| **Agent skills** | `SKILL.md` playbooks for deploy, rotate, backup and scale, for an AI operator. | [Cloud-ops skills](wiki/skills.md). |
