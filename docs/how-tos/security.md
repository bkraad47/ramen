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

## Granting and taking back (v0.4.1)
A group admin requests an extra cloud permission for one of their zones; a super admin approves it and the mapped
cloud role is bound to that zone's service account ([contract §9](../CONTRACTS.md)). Since 0.4.1 the other direction
exists too ([§13.2](../CONTRACTS.md)):

- `POST /api/v1/requests/{id}/deny` refuses a pending request. `POST /api/v1/requests/{id}/revoke` takes an approved
  one back. `DELETE /api/v1/groups/{g}/zones/{z}/permissions/{permission}` revokes a grant directly.
- Revoking calls the adapter with the permissions that **remain**, and the adapter treats that as the full set: the
  cloud roles nothing needs any more are unbound (GCP removes the member from the bucket, secret and project
  bindings; AWS rewrites the inline policy and deletes it when the set is empty).
- **Two permissions cannot be fully revoked, by design.** `bucket.read` and `secrets.read` map onto the roles every
  zone identity holds anyway — `roles/storage.objectViewer` on the group's bucket prefix and the group's secret
  accessor — because without them a worker cannot load its own code or read its own secrets. Revoking them removes
  the grant from the record; the identity keeps that baseline read access, and the response says so in `retained`.
  If a zone must lose bucket or secret access entirely, delete the zone or the group.
- Revoking a **role** grant returns the user to the role and groups the approval recorded, and ends their sessions
  immediately: sessions carry a per-user epoch, and the epoch is bumped on role, group, password, delete,
  authentication-configuration changes and on a backup restore. There is no window where an open tab keeps the
  permission it just lost.

## Keys
- **MCP keys** `rmk_<id>_<secret>`: stored hashed in the group's secrets, shown once, unioned into
  `RAMEN_MCP_KEYS` on the workers at deploy, compared in constant time. **No keys = deny all** (gRPC
  `UNAUTHENTICATED`). Revoke on the group page, then deploy to push the change — the deploy rewrites the zone's
  Secret and rolls the pods for you, but the old pods keep accepting the old key set until they finish draining
  (the job waits up to 180 s for that). Rotation is therefore quick, not instant: treat a leaked key as live for
  a few minutes after you revoke it.
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
| Source range | `RAMEN_ALLOWED_CIDRS` → 403 | same env → `PERMISSION_DENIED` (7); the address checked is the `RAMEN_TRUST_PROXY_HOPS`-th `x-forwarded-for` entry **from the right** (GCP 2, AWS 1, `0` = the peer address) |
| Admin surface | `X-Ramen-Admin-Key` + `RAMEN_ADMIN_CIDRS` | metadata `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` |
| Blocked tools | hidden from `*/list`, `-32601` | identical (inside the JSON-RPC body) |
| Body size | 4 MiB | 4 MiB → `OUT_OF_RANGE` (11), enforced by the tonic codec |
| Concurrency | `RAMEN_MAX_INFLIGHT` → 429 | `RESOURCE_EXHAUSTED` (8). The limit is sized when the pod starts, so changing it takes a restart even though a deploy may set it |
| Health | `/healthz`, `/readyz` unauthenticated | `Health/Check` unauthenticated; `NOT_SERVING` until code is loaded |
| Access log | one JSON line per call | same fields plus `grpc_code` |

TLS: the load balancer terminates it (GKE Gateway / ALB, HTTPS-only — no plaintext listener exists on either
cloud; self-signed until a domain exists — D17). This is a deliberate trust boundary, not a gap: LB→pod and
pod-to-pod traffic inside the cluster is plaintext h2c by default (confirmed during the v0.5.6 N12 HTTPS audit
— re-checked rather than assumed, since "ensure all communication is over https only" could be read either
way). Enforcing TLS for every internal hop by default would mean auto-provisioning and rotating a cert per
zone and changing the LB backend protocol on both clouds — treated as out of scope for N12 (asked and
confirmed with the user) in favor of keeping it the existing opt-in: set `RAMEN_TLS_CERT` + `RAMEN_TLS_KEY`
(PEM) on the workers and the port switches to h2; the GCP Gateway then uses `HTTP2` instead of h2c. Clients: the bridge's `--tls [--ca <pem>]`; `--insecure` is
plaintext and is for the local compose stack only — and note that `--insecure` **overrides** `--tls`/`--ca`
rather than conflicting with them, so a leftover `--insecure` in an `mcpServers` entry quietly downgrades the
connection. `--ca` replaces the trust store with that PEM; it is not certificate pinning, and because the
Gateway certificate is self-signed until you supply a domain certificate, `--tls` on its own fails with
`CERTIFICATE_VERIFY_FAILED` and `--ca` is required in practice.

Routing metadata `ramen-group` / `ramen-zone` is **not** an authorisation signal: the LB uses it to pick a zone,
then that zone's node still checks the key and the CIDR. Sending someone else's group name with your key gets
`UNAUTHENTICATED` from their workers.

## Network
- Node: `RAMEN_ALLOWED_CIDRS` (IPv4 + IPv6) for `Mcp/Call`, `RAMEN_ADMIN_CIDRS` for `Admin/*`.
- **Which address is checked**: `RAMEN_TRUST_PROXY_HOPS = n` takes the n-th `x-forwarded-for` entry counted from
  the **right**, the end proxies append to, so the entries a caller supplies on the left are ignored and the
  address checked is the one the outermost trusted proxy saw. Deployed values: **2 on GCP** (the external load
  balancer appends the client, then itself), **1 on AWS** (the ALB appends the client), set by the worker chart
  and both renderers and overridable, including to `0`. The default is `0` — never read the header, use the peer
  address — which is right for local and compose. `RAMEN_TRUST_PROXY=1` is the legacy spelling of one hop; an
  explicit hop count wins over it either way. Trust off, no header, too few entries or an unparseable entry all
  fall back to the peer address, so a wrong count **fails closed**: behind a load balancer the peer is the proxy,
  so a client-range allowlist denies instead of admitting.
- IP rules from the console become a **Cloud Armor** policy at the edge (GCP; one policy per group, so changing
  one zone's rules changes the whole group's edge) or the **WAFv2** equivalent (AWS, never applied) *and* the
  zone's node CIDRs. With the hop count right the node check is a real check on the client address; what bounds
  direct access to a pod is the worker `NetworkPolicy`. Changing rules rolls the zone's pods.
- Worker pods carry a NetworkPolicy (ingress only on the node port) and a restrictive `securityContext`.
- Console sessions: signed cookie (`ramen_session`, 12 h, `SameSite=Lax`, `Secure` with `RAMEN_COOKIE_SECURE=1`
  — the Helm chart defaults this to `1` for both cloud providers since the Gateway/ALB are HTTPS-only at the
  edge with no plaintext listener either way; `deploy/kind/values.yaml` overrides it back to `0` since kind
  has no real Gateway (plaintext NodePort). The app-level default (no chart involved, e.g. local compose) stays
  `0` so a plaintext dev stack still gets its cookie back).
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
  never the key). `key_id` is a short unsalted hash of the presented key, so it is not key material — but anyone
  with log access can hash a guessed key and check whether it matches a line. Treat worker logs as sensitive.
  `RAMEN_VERBOSE=1` per environment logs full request/response bodies — turn it on only while debugging.

## Isolation
- Runtime is spawned per worker with the deploy env only; killed after `RAMEN_SIDECAR_IDLE_SECS`; a call timeout
  (`RAMEN_CALL_TIMEOUT_SECS`, 120 s) kills and respawns it.
- Containers run as uid 10001; the worker image has no shell tools beyond python/pip (and the bridge).
- Zone namespaces are labelled `ramen.io/group=<group>`; group delete destroys them and their service accounts.
- Tool blocking (v0.3.0): per-environment block list enforced by the node (`-32601`), so a bad tool can be pulled
  without a redeploy of code.
- The deploy file in the group's bucket (`.ramen/env-<zone>`) is a **configuration channel**, not just data: the
  node reads a whitelist of keys from it, and that whitelist includes `RAMEN_MCP_KEYS` and
  `RAMEN_ALLOWED_CIDRS`. Write access to a group's bucket prefix therefore means the ability to add an MCP key
  and to widen that zone's address allowlist. The whitelist deliberately excludes the bucket, the port, the
  group and zone identity, the admin key and the proxy-trust setting, so the same write access cannot redirect a
  worker, take over its admin surface or change how it decides a caller's address. Bucket write access is a
  console-side privilege — keep it that way.

## The local stack is not a deployment
`deploy/local/docker-compose.yml` publishes the worker on host port 8080 as plaintext h2c with
`RAMEN_ALLOWED_CIDRS=0.0.0.0/0,::/0`, a committed `RAMEN_MCP_KEYS=local-mcp-key` and
`RAMEN_ADMIN_KEY=local-admin-key`, and no `RAMEN_ADMIN_CIDRS` at all. On a developer laptop that means
`Admin/Reload` and `Admin/Metrics` are available to anything that can reach the port, with a key that is in the
repository. It is meant for `make up` on a machine you control: do not expose the compose stack, and do not
reuse those values anywhere real.

## Reporting
Open a private security advisory on GitHub (Security → Advisories) rather than a public issue.

## Streamable HTTP, sessions and OAuth (v0.5.0)
`POST /mcp` runs the same key, source-range, blocked-name and in-flight checks as `ramen.v1.Mcp/Call` — literally
the same functions, with the HTTP headers turned into the metadata map the gRPC path reads
([contract §16.1](../CONTRACTS.md)). Three things exist only on HTTP:

- **Sessions.** `initialize` returns an `Mcp-Session-Id` that is an HMAC over a nonce, an expiry and the credential
  that made the call, keyed with the zone's `RAMEN_SESSION_SECRET`. Any pod verifies it without state; under another
  credential, expired or altered it is `404`, never `401`. Nothing is stored, so "revoking" a session is the TTL
  (30 min by default) — a stolen id is useless without the key that minted it. The console generates the secret
  per group and writes it into every zone's deploy Secret; it is encrypted at rest and never returned by the API.
- **Origin.** A browser `Origin` must be on `RAMEN_ALLOWED_ORIGINS`, which is empty by default — every browser
  origin refused — so a page on a foreign site cannot use a victim's browser to reach a worker (DNS rebinding). A
  group may allow its own web origins from its deploy file.
- **Per-user access through OAuth.** The console is the authorization server: a super admin registers a *client*
  (name and exact redirect URIs; no secret, PKCE S256 is the proof) on the API keys page, the client sends the user
  to `/oauth/authorize`, the user signs in as usual and approves *this client* for *this group and zone*, and
  `/oauth/token` mints an HS256 access token the worker verifies with a key derived from the same zone secret.
  Claims: issuer (the console's `RAMEN_PUBLIC_URL`), the user id and email, audience and scope
  `mcp:<group>:<zone>`, one hour of life, a `jti`. The worker's access log then names the user, not a key id.
  Refresh tokens live 30 days, rotate on every use, belong to one client, and die when the user's session epoch
  moves (password or role change, delete, restore). **An access token is a bearer with a one-hour life and no
  revocation list**; that is the trade-off for a node that needs no callback to the console per call.
  Every authorize-time failure is an error page, never a redirect — a redirect URI is only trusted once it has
  matched the registered client. Dynamic client registration (RFC 7591) is deliberately not in this release: it is
  an unauthenticated write endpoint, and it waits for a security review of its own.
- **What a viewer can delegate.** The token a viewer approves runs every tool of the group in that zone — the same
  access as an `rmk_` key of that group. That is deliberate: OAuth is how a *person* gets what a key holder gets,
  with their name on every call. The consent page says so. A viewer who should not be able to run tools should not
  be in the group. Codes and refresh tokens are stored under their SHA-256; a rotated-out refresh token presented a
  second time revokes every refresh token of that grant; and each mint re-checks that the user still exists, may
  still log in and still has the group.
- **Restore and the zone secret.** A backup carries no `session_secret`, so a group restored into a fresh console
  gets a new one on its next deploy — and the workers keep the old one until then. Redeploy each restored
  environment before expecting sessions or console-minted tokens to verify.

What none of this changes: `Admin/*` stays gRPC-only and cluster-internal; the NetworkPolicy still bounds direct
access to a pod; and the runtime still executes a group's code with that group's secrets in its environment.
