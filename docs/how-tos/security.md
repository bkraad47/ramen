# Security

## Threat model in one paragraph
Workers run untrusted-ish user code (your team's tools) and are reachable by MCP clients across the internet
through a load balancer. The console holds secrets and cloud credentials. Ramen keeps protocol handling, auth and
network policy in the Rust node and the console, and keeps user code in an on-demand Python process that only ever
sees the secrets it was scoped to.

## Identities and roles
| Actor | Auth | Scope |
|---|---|---|
| Console user | email + argon2 password, OAuth/OIDC provider button (PKCE S256 + `nonce` since 0.3.1), optional magic link | `super_admin` / `group_admin` (groups) / `viewer` (groups) |
| Automation | `rmn_` API key in `X-Ramen-Api-Key` (console HTTP API) | role + groups ≤ creator's |
| MCP client | `rmk_` MCP key in gRPC metadata `authorization: Bearer` (the bridge's `--key`) | one group's workers |
| Console → worker | metadata `x-ramen-admin-key` (`RAMEN_ADMIN_KEY`) on `ramen.v1.Admin/*` | separate `RAMEN_ADMIN_CIDRS` |
| Worker → cloud | Workload Identity (GSA) / IRSA (IAM role) per group+zone, least privilege (bucket prefix, secret prefix) | |
| Console → cloud | GSA / IAM role from Terraform, narrowed in 0.3.1 (below) | |

Bootstrap super admin comes from `RAMEN_ADMIN_EMAIL` / `RAMEN_ADMIN_PASSWORD` and is re-applied on every start
(cloud admins reset it by changing the env). `auth.password_login: false` (super admin toggle) disables password
login except break-glass with `RAMEN_ADMIN_FORCE_PASSWORD=1`.

## Keys
- **MCP keys** `rmk_<id>_<secret>`: stored hashed in the group's secrets, shown once, unioned into
  `RAMEN_MCP_KEYS` on the workers at deploy, compared in constant time. **No keys = deny all** (gRPC
  `UNAUTHENTICATED`). Revoke on the group page, then deploy to push the change.
- **API keys** `rmn_<id>_<secret>`: stored hashed, shown once, scoped. Revoke on the API Keys page.
- **Admin key**: gates `Admin/Reload` and `Admin/Metrics`; rotate with the [rotate-keys skill](../wiki/skills.md).
- **Fernet key** `RAMEN_FERNET_KEY`: encrypts password hashes, secret values, API-key hashes and git tokens at
  rest. Required in production; rotation = re-encrypt (see the skill).

## Transport (gRPC, v0.3.1) — parity with the old HTTP surface
The worker exposes one port with `ramen.v1.Mcp`, `ramen.v1.Admin` and `grpc.health.v1.Health`
([contract §11](../CONTRACTS.md)). Every control that existed on `/mcp` exists on `Mcp/Call`:

| Control | HTTP (≤ 0.3.0) | gRPC (0.3.1) |
|---|---|---|
| Bearer key | `Authorization` header → 401 / `-32001` | metadata `authorization` → `UNAUTHENTICATED` (16) |
| Source range | `RAMEN_ALLOWED_CIDRS` → 403 | same env → `PERMISSION_DENIED` (7); `RAMEN_TRUST_PROXY=1` honours the first `x-forwarded-for` hop only |
| Admin surface | `X-Ramen-Admin-Key` + `RAMEN_ADMIN_CIDRS` | metadata `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` |
| Blocked tools | hidden from `*/list`, `-32601` | identical (inside the JSON-RPC body) |
| Body size | 4 MiB | 4 MiB (`RESOURCE_EXHAUSTED` / `INVALID_ARGUMENT` from the framework) |
| Concurrency | `RAMEN_MAX_INFLIGHT` → 429 | `RESOURCE_EXHAUSTED` (8) |
| Health | `/healthz`, `/readyz` unauthenticated | `Health/Check` unauthenticated; `NOT_SERVING` until code is loaded |
| Access log | one JSON line per call | same fields plus `grpc_code` |

TLS: the load balancer terminates it (GKE Gateway / ALB, self-signed until a domain exists — D17). To encrypt
inside the cluster too, set `RAMEN_TLS_CERT` + `RAMEN_TLS_KEY` (PEM) on the workers and the port switches to h2;
the GCP Gateway then uses `HTTP2` instead of h2c. Clients: the bridge's `--tls [--ca <pem>]`; `--insecure` is
plaintext and is for the local compose stack only.

Routing metadata `ramen-group` / `ramen-zone` is **not** an authorisation signal: the LB uses it to pick a zone,
then that zone's node still checks the key and the CIDR. Sending someone else's group name with your key gets
`UNAUTHENTICATED` from their workers.

## Network
- Node: `RAMEN_ALLOWED_CIDRS` (IPv4 + IPv6) for `Mcp/Call`, `RAMEN_ADMIN_CIDRS` for `Admin/*`, `RAMEN_TRUST_PROXY=1`
  to honour the first `x-forwarded-for` hop behind the LB.
- IP rules from the console become **Cloud Armor** (GCP) / **WAFv2** (AWS) policies on the zone's backend *and*
  node CIDRs, so the node still enforces if the edge is misconfigured. Changing rules rolls the zone's pods.
- Worker pods carry a NetworkPolicy (ingress only on the node port) and a restrictive `securityContext`.
- Console sessions: signed cookie (`ramen_session`, 12 h, `SameSite=Lax`, `Secure` with `RAMEN_COOKIE_SECURE=1`).
- CSRF: per-session `ramen_csrf` token on HTML forms / `X-Ramen-CSRF` header; JSON API with an API key is exempt.

## What changed in 0.3.1
The security mediums carried from the 0.3.0 audit are fixed in this release:

| Item | Before | Now |
|---|---|---|
| GCP console GSA | `roles/resourcemanager.projectIamAdmin` | `serviceAccountCreator`/`Deleter`/`User` + custom role `ramenConsoleSaIam` (get/list/getIamPolicy/setIamPolicy on `ramen-*` service accounts); `storage.admin` only on the groups bucket; bucket- and secret-level bindings replace project-level ones (`console_project_iam=true` re-adds `projectIamAdmin` for project-wide permission roles) |
| AWS console role | `wafv2:*`, `iam:PutRolePolicy` on `*` | `wafv2` scoped to the `ramen` web ACL / IP sets by ARN pattern; `iam:PutRolePolicy` limited to `/ramen/` roles |
| Console Kubernetes RBAC | ClusterRole with cluster-wide `secrets` / `serviceaccounts` | ClusterRole keeps namespaces, networkpolicies, httproutes and read verbs; a namespaced Role + RoleBinding is created for the console KSA in every zone namespace it attaches |
| Git token during repo sync | in the clone URL, left in `.git/config` of the bucket copy | passed via `http.extraheader` / `GIT_ASKPASS`; credentials stripped from `.git/config` |
| OIDC login | no PKCE | PKCE S256 + `nonce` |
| Node key compare | early-exit string compare | constant-time |

## Secrets
Never returned by any endpoint, page, backup or log. Delivered to workers only as `RAMEN_SECRET_<GROUP>__<NAME>`
env vars scoped to env + zone. Redacted from tool errors. Details: [Secrets](secrets.md).

## Audit and logs
- `audit`: every mutating console request `{ts, user, ip, action, target, ok, tags}` (also failed logins).
- `activity`: deploys, permission requests, invocations summary.
- Worker log: one JSON line per MCP call `ts, ip, group, method, name, status, grpc_code, ms, key_id` (key id,
  never the key). `RAMEN_VERBOSE=1` per environment logs full request/response bodies — turn it on only while
  debugging.

## Isolation
- Runtime is spawned per worker with the deploy env only; killed after `RAMEN_SIDECAR_IDLE_SECS`; a call timeout
  (`RAMEN_CALL_TIMEOUT_SECS`, 120 s) kills and respawns it.
- Containers run as uid 10001; the worker image has no shell tools beyond python/pip (and the bridge).
- Zone namespaces are labelled `ramen.io/group=<group>`; group delete destroys them and their service accounts.
- Tool blocking (v0.3.0): per-environment block list enforced by the node (`-32601`), so a bad tool can be pulled
  without a redeploy of code.

## Reporting
Open a private security advisory on GitHub (Security → Advisories) rather than a public issue.
