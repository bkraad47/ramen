# Threat model

What Ramen defends against, what it does not, and what is unverified — stated so a buyer can check it against the
code and the reports rather than take it on trust. Terms: a **group** is a tenant; a **zone** is a Kubernetes
namespace with that group's workers; a **worker** is one Rust node plus one Python runtime; the **console** is the
management plane. Verification status is the section at the end, and it is the part most likely to change.

## Assets
1. A group's **tool code and its secrets** (`RAMEN_SECRET_*`, GitHub tokens, whatever the tools reach with them).
2. The **credentials that reach a worker**: `rmk_` MCP keys, OAuth access and refresh tokens, session ids.
3. The **console's own state**: users and their password hashes, `rmn_` API keys, the per-group session secret, the
   Fernet key, cloud identities the console can create and bind.
4. **Availability** of a zone's workers to the clients that are allowed to use them.

## Adversaries
- **A1 Anyone on the internet** who can reach the load balancer.
- **A2 A holder of one credential** — a leaked `rmk_` key, a stolen access token, a copied session id — who is not
  the person or team it was issued to.
- **A3 A tenant**: a group admin or a tool author, acting against another group or against the platform.
- **A4 Someone inside the cluster network** — another workload, or an attacker who has compromised one.
- **A5 A malicious web page** loaded in the browser of someone who holds a credential (DNS rebinding, CSRF).
- **A6 The cloud provider and Kubernetes itself** — out of scope; Ramen assumes the control plane, the load
  balancer and IAM behave as documented.

## What Ramen defends against

| Threat | Adversary | Defence | Where |
|---|---|---|---|
| Calling a worker without a credential | A1 | every `Mcp/Call` and `POST /mcp` requires a bearer credential; an empty key set denies everyone | `node-rs/src/auth.rs`, `grpc.rs::guard` |
| Guessing or timing a key | A1 | constant-time comparison folded over every key with no early exit; 12-character minimum with four character classes | `auth.rs::check_key`, `console/security.py` |
| Reaching a worker from an address the group did not allow | A1, A2 | `RAMEN_ALLOWED_CIDRS` at the node, checked against the proxy hop the load balancer appended (a caller cannot choose it; a wrong count fails closed), plus Cloud Armor at the GCP edge | `auth.rs::client_ip`, `cloud/gcp.py` |
| Using a stolen session id under another credential | A2 | session ids are HMACs over the credential; a mismatch is `404` | `session.rs` |
| Replaying an OAuth token against another zone or issuer | A2 | audience, scope and issuer checks; one-hour life; refresh rotation bound to the user's session epoch | `token.rs`, `console/oauth_server.py` |
| A web page using a victim's browser to reach a worker | A5 | `Origin` refused unless allow-listed (empty by default); the console's forms and cookie-authenticated API mutations require a CSRF token | `http.rs::origin_allowed`, `console/auth/csrf.py` |
| A tool author reading another group's secrets | A3 | one namespace, one pod set and one cloud identity per group and zone; the identity can read only that group's bucket prefix and secrets; the console never returns a secret value | `cloud/gcp_k8s.py`, `cloud/gcp_api.py`, `services.py` |
| A group widening its own trust from its deploy file | A3 | the deploy file may set only an allow-list of keys; the session secret, the issuer, proxy trust and reflection are not among them | `config.rs::DEPLOY_KEYS` |
| Tool code crashing or hanging the process that holds the keys | A3 | user code runs in a separate Python process the node spawns, bounds with a call timeout and kills after idleness; the node holds the keys and does the auth | `sidecar.rs` |
| Another pod reaching a worker directly | A4 | the worker NetworkPolicy admits the node port only from the console namespace and the load balancer's ranges; hardened container context | `deploy/helm/ramen-worker` |
| Enumerating the services from the edge | A1 | gRPC reflection is off on deployed workers; health and the OAuth resource metadata are the only unauthenticated surface, by design | `grpc.rs::routes`, `http.rs` |
| A revoked user keeping access | A2 | sessions carry a per-user epoch bumped on password, role and group change, delete, auth-config change and restore; refresh tokens carry the same epoch | `accounts.py`, `oauth_server.py` |
| Flooding a zone | A1 | `RAMEN_MAX_INFLIGHT` per node (`RESOURCE_EXHAUSTED` / `429`), a 4 MiB message cap, an HPA per zone | `grpc.rs`, `deploy/helm/ramen-worker` |

## What Ramen does not defend against
- **A holder of a valid `rmk_` key is that group, until the key is revoked.** Keys are shared secrets; use OAuth
  when access should belong to a person.
- **An OAuth access token cannot be revoked before it expires** (one hour). There is no revocation list, because a
  worker checks tokens without calling the console.
- **A tool author can read their own group's secrets** — the runtime executes group code with that group's secrets
  in its environment. Isolation is between groups, not within one.
- **A compromised console is a compromised platform.** It holds the Fernet key, mints tokens and binds cloud IAM.
  Its own hardening (narrow IAM, per-namespace RBAC, CSRF, rate limits, argon2) is real, but it is the crown jewel.
- **Plaintext hops that are plaintext by default**: load balancer → node is h2c unless node TLS is configured;
  the local compose stack is plain HTTP on a laptop; the stdio bridge is h2c unless `--tls`.
- **The key's length can leak** through the length check in front of the constant-time compare.
- **Denial of service at the edge** beyond the per-node caps: that is the load balancer's and the cloud's job.
- **The cloud provider** (A6).

## What is verified, and what is not
Kept current in [Transport → What is verified](wiki/transport.md#what-is-verified-and-what-is-not) and the
`reports/` directory of the private workspace. In short, as of 0.5.0: both transports and every guard above are
proven on real node processes in CI on Linux and Windows; the gRPC path was proven live on one GKE cluster in 0.3.2
and 0.4.0; the HTTP path, sessions and OAuth have **not yet run in a cloud**; the AWS path has **never** been applied
to a real account; there has been no third-party penetration test. An independent agent reviews each release's new
surface against the code (`reports/security-v<version>.md`), which is the closest thing to an audit this project
has had.

## Reporting
A vulnerability report goes to the maintainer through a private GitHub security advisory on the repository. Say what
you found, how to reproduce it and which release; expect an acknowledgement, a fix in a patch release, and credit in
the changelog if you want it.
