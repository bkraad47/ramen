# Versions

Generated from [`CHANGELOG.md`](https://github.com/bkraad47/ramen/blob/main/CHANGELOG.md) by `scripts/gen_versions.py`; do not edit by hand. Semver, `0.x` is pre-stable; each release is tagged `v<version>` and GitHub Actions attaches downloadable zips.

| Version | Date | Theme | Links |
|---|---|---|---|
| `0.5.94` **(current)** | 2026-10-03 | group page zones and blocked packages as checkbox dropdowns | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.94) |
| `0.5.93` | 2026-10-03 | OAuth role mapping and `mcp_user`; scoped service-account permissions; every settings form works in a real browser | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.93) |
| `0.5.91` | — | Group page: per-zone package list back, Enable/Disable fixed, every admin page aligned | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.91) |
| `0.5.8` | 2026-10-03 | The published ramen-mcp-bridge package verified live against real AWS and GCP deployments | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.8) |
| `0.5.7` | 2026-10-03 | The MCP bridge becomes its own pip-installable package | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.7) |
| `0.5.6` | 2026-10-03 | Auto-rebalance, email alerts, and the AWS deploy path verified for real on a live account | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.6) |
| `0.5.4` | 2026-09-30 | honest deploy pictures, aligned environment buttons, docs brought current | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.4) |
| `0.5.3` | 2026-09-30 | how to add and deploy a tool | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.3) |
| `0.5.2` | 2026-09-30 | the mark is the logo | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.2) |
| `0.5.1` | 2026-09-30 | the end-to-end guide, and 0.5.0 proven on GKE | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.1) |
| `0.5.0` | 2026-09-29 | Streamable HTTP at the edge, gRPC inside | [release](https://github.com/bkraad47/ramen/releases/tag/v0.5.0) |
| `0.4.3` | — | screenshots that cannot go stale | [release](https://github.com/bkraad47/ramen/releases/tag/v0.4.3) |
| `0.4.2` | — | console usability and docs | [release](https://github.com/bkraad47/ramen/releases/tag/v0.4.2) |
| `0.4.1` | — | the three half-finished promises | [release](https://github.com/bkraad47/ramen/releases/tag/v0.4.1) |
| `0.4.0` | 2026-09-29 | console, docs, and evidence | [release](https://github.com/bkraad47/ramen/releases/tag/v0.4.0) · [architecture](architecture/v0.4.0.md) |
| `0.3.2` | 2026-09-28 | verified on GKE | [release](https://github.com/bkraad47/ramen/releases/tag/v0.3.2) |
| `0.3.1` | 2026-09-28 | gRPC transport | [release](https://github.com/bkraad47/ramen/releases/tag/v0.3.1) · [architecture](architecture/v0.3.1.md) |
| `0.3.0` | 2026-09-28 | AWS, auth & policy, docs | [release](https://github.com/bkraad47/ramen/releases/tag/v0.3.0) · [architecture](architecture/v0.3.0.md) |
| `0.2.0` | 2026-09-28 | GCP | [release](https://github.com/bkraad47/ramen/releases/tag/v0.2.0) · [architecture](architecture/v0.2.0.md) |
| `0.1.0` | 2026-09-27 | local core | [release](https://github.com/bkraad47/ramen/releases/tag/v0.1.0) · [architecture](architecture/v0.1.0.md) |

## 0.5.94 — group page zones and blocked packages as checkbox dropdowns

- (nothing yet)

## 0.5.93 — OAuth role mapping and `mcp_user`; scoped service-account permissions; every settings form works in a real browser

- (nothing yet)

## 0.5.91 — Group page: per-zone package list back, Enable/Disable fixed, every admin page aligned

- (nothing yet)

## 0.5.8 — The published ramen-mcp-bridge package verified live against real AWS and GCP deployments

- (nothing yet)

## 0.5.7 — The MCP bridge becomes its own pip-installable package

- (nothing yet)

## 0.5.6 — Auto-rebalance, email alerts, and the AWS deploy path verified for real on a live account

- (nothing yet)

## 0.5.4 — honest deploy pictures, aligned environment buttons, docs brought current

- The docs' group and deploy-job screenshots showed a deploy that had **failed** ("Authentication failed for https://github.com/…"): the screenshot rig deployed the demo group from GitHub through a laptop with a stale keychain token. The rig now clones a local git copy of the demo group, so the seeded deploy succeeds and the pictures show a job that ends in `ok` with packages per zone.
- Group page → Environments: the verbose toggle, **Deploy (canary)** and **Delete** were three sizes on two baselines; all three now use the equal-width `row-actions` pattern (the toggle reads `Verbose: on/off`). A test pins it.
- Architecture page brought to 0.5.x (Streamable HTTP and OAuth in the call path, decisions D31–D35, version history); every README relative path, every external link and every docs link checked; screenshots re-captured (`ui_contract` 0.5.4).
- Test-only: `log::tests::mirrors_to_file` counted every line of the process-global log file and flaked when another test emitted concurrently (it failed CI on the 0.5.3 commit and took the pipeline with it); it now counts only its own lines.

## 0.5.3 — how to add and deploy a tool

- `docs/how-tos/add-a-tool.md`: from an empty folder to a tool an AI client can call, with the demo group as the worked example — the layout, the calculator read line by line, a new `word_count` tool in two files, a local check by driving the worker's own runtime over stdio (`runtime.load`, `runtime.call_tool`; real transcript), push, deploy from the console (canary, per-package errors, disable), the call, and resources and prompts in brief. Linked from the README header, the docs home, the nav and the quickstart. A test keeps every guide the README promises present, in the nav and linked.

## 0.5.2 — the mark is the logo

- The console's sidebar and its sign-in, password-reset and OAuth consent cards now show the square bowl mark the favicon is made of (`/static/logo-mark.png`, 256 px), not the wide "PROJECT RAMEN" wordmark that rendered as a blurry 56 px rectangle. Square sizes and rounded corners in the CSS; the wordmark stays in the README and the docs. A test pins every branded page to the mark; screenshots re-captured (`ui_contract` 0.5.2).

## 0.5.1 — the end-to-end guide, and 0.5.0 proven on GKE

- **`docs/how-tos/end-to-end.md`**: console → worker → AI client, the same eight steps on the local compose stack and on GKE — sign in, zone, group and environment, agent key, deploy, connect a Streamable HTTP client (Claude Desktop, Cursor, curl, the `mcp` SDK), per-user access with OAuth, and what the audit and access logs then show. Every console step has a screenshot.
- **Screenshots**: the rig (`make shots`) gained the one-time key display and the OAuth consent page, and a live mode (`scripts/shots.py --base https://… --email … --password …`) that captures the same set from a real console; the GKE pictures in the guide come from it. Seeded set re-captured; `ui_contract` 0.5.1.
- **GKE run of 0.5.0** on a throwaway Autopilot cluster: the console journey, `cloud_smoke.sh` (TLS verified against the balancer's certificate), Streamable HTTP through the Gateway, the OAuth flow end to end with the token calling a tool, and the harness — `e2e conformance cloud`, **218 passed, 0 failed, 15 skipped** with reasons — including 0.4.1's permissions (IAM bind and unbind against Google's APIs) and backup-restore cases. Found and fixed: the deploy did not hand workers `RAMEN_PUBLIC_URL` (challenge was relative); Terraform did not enable the Resource Manager API, so project-wide role bindings failed as a bare `403` (the console reported it as a missing role; the permissions suite now skips with the console's note instead of failing when `console_project_iam` is off); a harness helper opened fresh gRPC channels without the CA. Learned: the Gateway takes up to seven minutes to program a new zone's route, and a console rollout answers `503` through the balancer for about a minute.
- **Fixed, found by CI after the 0.5.0 push:** Streamable HTTP over node TLS (`RAMEN_TLS_CERT/KEY`) failed at the handshake — tonic's acceptor offers only `h2` on ALPN and every Streamable HTTP client speaks HTTP/1.1. The node now terminates TLS with its own acceptor offering `h2` and `http/1.1`, one handshake task per connection with a 10 s bound. The TLS conformance case had been skipped on the laptop (no `openssl` on PATH); it runs in CI and now passes. Also: the CI compose smoke step sets `RAMEN_SMOKE_INSECURE=1` for the container's self-signed certificate, the one place that flag is used.
- **Fixed after the tag:** `deploy/kind/values.yaml` still named the 0.5.0 images, so every kind deploy — CI's `kind` job included — pulled a tag that no longer existed (`ImagePullBackOff`, canary never ready). Bumped, and the file is now under `check_versions.py`, so a bump cannot miss it again.
- `scripts/oauth_roundtrip.py`: the OAuth flow as a client does it, against a live console and worker — register, authorize, consent, PKCE exchange, call `/mcp` with the token, refresh rotation and reuse detection, a token for another zone refused.

## 0.5.0 — Streamable HTTP at the edge, gRPC inside

- Every worker now serves `POST /mcp` (Streamable HTTP, MCP 2025-06-18) on the same port as `ramen.v1.Mcp/Call`, through the same guard functions — key, source range, blocked names, 4 MiB and in-flight caps are one implementation, spelled `401 / 403 / 413 / 429` on HTTP and `UNAUTHENTICATED / PERMISSION_DENIED / OUT_OF_RANGE / RESOURCE_EXHAUSTED` on gRPC. JSON-RPC errors stay `200` with an error body on both. `GET /mcp` is `405`; `DELETE /mcp` ends a session. The access log names the transport.
- Origin validation: a browser `Origin` is refused unless it is on `RAMEN_ALLOWED_ORIGINS` — empty by default — so a foreign page cannot reach a worker through a victim's browser. A group may allow its own web origins from its deploy file.
- Stateless sessions: `initialize` returns an `Mcp-Session-Id` that is an HMAC over a nonce, an expiry and the credential, keyed with the zone's `RAMEN_SESSION_SECRET`; any pod verifies it, no affinity or store, and under another credential it is `404`, never `401`. The console generates one secret per group and hands it to every zone's deploy Secret.
- HTTP/1.1 is now accepted on the node port (Streamable HTTP clients speak it); gRPC stays HTTP/2.
- The console is an authorization server: `/.well-known/oauth-authorization-server`, `/oauth/authorize` with a consent page, `/oauth/token` with PKCE S256 and refresh rotation. Clients are pre-registered by a super admin on the API keys page (name and exact redirect URIs; no secret). Access tokens are HS256 JWTs a worker verifies with a key derived from the zone secret; `aud` and `scope` are `mcp:<group>:<zone>`; one hour of life. Refresh tokens live 30 days, rotate on use, and die with the user's session epoch. Workers publish `/.well-known/oauth-protected-resource` and challenge with `WWW-Authenticate: Bearer resource_metadata=…`. Dynamic client registration is deliberately left out.
- The login redirect keeps the query string, so an authorize request survives sign-in.
- The quickstart, `make demo`, the client config example, the README and the how-tos land a new user on `http://localhost:8080/mcp` with `Authorization: Bearer`. The stdio bridge is documented once, as the path for clients that only speak stdio. `RAMEN_MCP_KEY` is read by the bridge and used by every example, so a key never has to sit in a config file.
- `deploy/local/mcp_call.py` speaks Streamable HTTP when given a URL and the bridge when given `host:port`.
- `tests/conformance/test_transports_local.py`: the whole guard table on both transports on real node processes, the HTTP-only cases, and the official `mcp` SDK's Streamable HTTP client end to end. CI runs it on Linux and, new, on `windows-latest` with the node built natively there — the client flow on the platform where fresh-machine setups break.
- `scripts/cloud_smoke.sh` gains the HTTP steps; `tests/kind/test_http.py` proves the path through the cluster's ports.
- Found on the way: a stale release binary from 0.4.x hid that the node was h2c-only; the harness now finds `ramen-node.exe` and `Scripts/` venvs on Windows.
- High: the Python runtime that executes a group's tool code inherited the node's whole environment. The sidecar now strips the node's credentials (`RAMEN_MCP_KEYS`, `RAMEN_ADMIN_KEY`, `RAMEN_SESSION_SECRET`, TLS key, CIDR lists, origins, issuer, proxy trust) before spawning it; a conformance tool that lists its own environment on a real node proves it.
- OAuth: authorization is re-checked at every mint (user exists, may log in, still has the group, epoch unchanged); codes and refresh tokens are burned in the same store transaction they are read in and stored under their SHA-256; a rotated-out refresh token presented again revokes the whole grant; `/oauth/token` is rate-limited like `/login`; the authorize query is redacted from the access log; API keys cannot authorize a client; loopback clients may use any port (RFC 8252); redirect URIs keep their own query; `scopes_supported` no longer lists every tenant.
- HTTP: an unparsable `Origin` is refused, not ignored; address and Origin are checked before the credential; sessions are bound to a full hash of the key; CORS headers and an `OPTIONS /mcp` preflight for a listed origin; a token for another zone is `401`, and the challenge names an absolute `resource_metadata` when the worker knows its public URL (`RAMEN_PUBLIC_URL`).
- The local stack keeps the zone secret out of the group's bucket (the worker gets it from compose, the console the same value as `RAMEN_LOCAL_SESSION_SECRET`); `cloud_smoke.sh` verifies TLS against `RAMEN_SMOKE_CA` and skips verification only on `RAMEN_SMOKE_INSECURE=1`; `mcp_call.py` reads `RAMEN_MCP_KEY`.
- Deferred to 0.5.1 with the reason stated: session-secret rotation (needs a two-key overlap on the node) and a per-zone admin key.
- `docs/threat-model.md`: assets, adversaries, what is defended, what is not, and what is unverified. The README leads with what is verified today, gives the cheaper claim a number (one Deployment per zone for a thirty-tool team, not thirty), claims the git-push story for HTTP clients only, and states security in one sentence: user code never runs in the process that holds the keys.

## 0.4.3 — screenshots that cannot go stale

- `make shots` (`scripts/shots.py`) captures every console page from a throwaway instance — memory store, local adapter, two in-process fake workers, fictional groups, users, keys, secrets, an image pin, a backup, a deploy and a worker log — with Playwright. It seeds and tears down everything it uses and never touches a cloud or a real key.
- `docs/img/shots.json` records what each screenshot shows and which release it was captured for, plus the release whose UI it must match. `console/tests/test_docs_images.py` fails on a missing image, an unrecorded screenshot, or a capture older than that contract — so a UI change now forces fresh captures before release.
- All twelve shots re-captured for 0.4.3, including five pages the docs had never shown (audit, backups, users, environments, config), and the console how-to illustrates the pages it describes.
- The `Auto refresh` and `Refresh discovery` buttons 0.4.2 added were floated into the dashboard legend and overflowed the card: the label wrapped onto three lines and `Refresh discovery` ran off the edge. The legend is a flex bar now, and a test rejects `float:right` on that page. The screenshot round is what found it — the tests only saw the markup.

## 0.4.2 — console usability and docs

- API keys: the groups list box is a dropdown with an `Add` button; chosen groups show as removable chips and a key can still name several.
- Dashboard: the grid refreshes every minute instead of every ten seconds, and `Auto refresh: on` stops the polling when you want to read a busy cluster.
- Audit: the newest 100 entries in a scrollable frame, with a search box across every column and a succeeded-or-failed filter that work on what is already rendered. `?limit=` fetches up to 500.
- Backups: `Download`, `Preview restore`, `Restore` and `Restore and prune` are one evenly spaced row of equal buttons.
- Config: super-admin service-account rules are added from an effect dropdown and the permission catalogue (or a pattern), and removed per row, instead of hand-written JSON — which is still shown as what will be sent.
- Environments: the last deploy is flattened into outcome, time and error columns instead of a JSON blob, and the row action is sized like every other.
- Users: `Save` and `Delete user` sit in one actions row, delete on the right, and the confirmation names the account.
- The favicon is the icon on every page, the sign-in and password-reset pages included.
- `ramen_console.__version__` and `ramen_runtime.__version__` are read from the installed distribution's metadata, falling back to the repo `VERSION`. Nothing hard-codes a version any more, and `scripts/check_versions.py` fails if anything starts to.
- New page: *The console, page by page* — what every page does, what each role sees, and which actions are destructive.
- GitHub Pages deployed from `main` but **failed on every release tag**: the `github-pages` environment allowed only the `main` branch, so the tag build succeeded and its deploy was rejected. Release tags (`v*`) are now allowed to deploy.

## 0.4.1 — the three half-finished promises

- `POST /api/v1/backups/{id}/restore` takes `{dry_run, prune, reconcile, force}`. `dry_run` returns the plan and writes nothing; `prune` deletes what the backup does not contain (never `config`, never the account running the restore); `reconcile` re-applies the restored zones so counts, sizes and image pins take effect, reporting namespaces the backup never knew about as `orphans` rather than deleting them; `force` overrides the refusal to restore a backup from a newer release. A restore merges over the live documents, so hashes, secret values and tokens survive it, and it bumps every restored user's session epoch — everyone else is signed out at once, so a role a restore lowers cannot be outlived by an open session. A user the store had lost comes back `login_disabled` until a password reset or an SSO sign-in.
- Granted service-account permissions can be taken back: `DELETE /api/v1/groups/{g}/zones/{z}/permissions/{permission}`, plus `POST /api/v1/requests/{id}/deny` for a pending request and `POST /api/v1/requests/{id}/revoke` for an approved one. Revoking a role grant puts the role and group back to what the approval recorded and ends that user's sessions immediately.
- `Cloud.apply_sa_permissions` is now a **set** operation: called with a shorter list it unbinds the cloud roles no remaining permission needs. On GCP that means removing the member from the bucket, secret and project bindings; AWS already rewrote its inline policy. The zone identity's baseline roles (`storage.objectViewer` on the group prefix, the group secret accessor) are never unbound and are reported as `retained` — so revoking `bucket.read` or `secrets.read` takes the grant off the record while the identity keeps that baseline read access.
- Each group can pin its own worker image: `POST /api/v1/groups/{g}/images {tag,digest?,note?}` records a build and pins it, `GET …/images` lists the history, `PUT …/images/current {id}` recalls an earlier one and `DELETE …/images/current` returns to the release image. The pin travels in the zone spec, so both cloud adapters render it into the worker and canary Deployments. A reference must carry a `:tag` or a `sha256:` digest; there is no implicit `latest`. The console records and recalls, it never builds — `make build-worker GROUP=<g>` and `make push-worker GROUP=<g>` do that, so the console needs no registry or build credentials.
- Backups page: preview a restore, restore, or restore and prune, each showing what it did per collection with its warnings.
- Group page: a `Worker image` section with the current pin, the history and `Recall`; a revoke button beside every granted permission; approve, deny and revoke on the requests table.
- Kind: a per-group image side-loaded, pinned, deployed and answering tool calls in both zones, then recalled; a role grant revoked mid-session; a restore that prunes and reconciles a populated two-zone deployment while both zones keep serving.
- GKE: the cloud IAM path — an approved permission bound, then unbound by a revoke with the baseline roles kept — and a restore against the Firestore-backed store with a bucket backup.

## 0.4.0 — console, docs, and evidence

- API keys carry a client type and it is enforced: an `agent` key (`rmk_`) is accepted only by workers, a `devops` key (`rmn_`) only by the console API, each refused by the other side. Keys made before 0.4.0 keep working, typed by their prefix.
- `RAMEN_TRUST_PROXY` is replaced by `RAMEN_TRUST_PROXY_HOPS=N`, which reads the Nth `x-forwarded-for` entry **counted from the right**. The old spelling still works and means one hop. Deployed values: GCP 2, AWS 1 — **measured on a live GKE Gateway**, not assumed. Any failure falls back to the peer address, so a wrong count denies rather than admits.
- Server reflection is gated by `RAMEN_REFLECTION` and is **off on deployed workers**: `grpcurl` against a deployed worker now needs `-import-path proto -proto ramen/v1/mcp.proto`.
- A super admin changing the authentication configuration signs out every other session, their own other clients included.
- The worker address allowlist could be bypassed: proxy trust shipped enabled and the node read the leftmost `x-forwarded-for` entry, which a caller controls. Found by an independent review of the documentation's own claims.
- Setting IP rules wrote the cloud edge policy before the worker allowlist on both clouds, so a cloud failure left the zone unlocked while reporting an error. The worker is now configured first; the edge policy is best effort.
- A `canary:false` deploy left the previous canary pod serving the zone with the old configuration, so a disabled tool still ran, a revoked key still worked and a tightened allowlist did not apply. Stale canaries are now removed before the stable roll.
- Sessions are revoked on password, role, group and authentication-configuration changes, and on delete.
- Passwords and generated keys are at least 12 characters across four character classes.
- Per-zone enable and disable for tools, resources and prompts, on top of the environment-wide list.
- Two-pane logs with the consumer, timestamp, method and outcome per entry, and a worker filter.
- One equal-width Actions group per zone; identity and role in the sidebar with the role named and coloured; health described by load rather than by colour; sentence case throughout; group pickers instead of typed lists.
- `GET` for a single zone and a single environment. `refresh` survives a namespace that is terminating or unreadable.
- Dark-only site, aligned badges, no edit button, no heading permalink symbols, `llms.txt` served but unlinked.
- A hand-drawn architecture diagram that names its own plaintext hops, and a transport page whose every claim was checked against the code by a reviewer that did not write it.
- Operator facts stated plainly: which hops are plaintext, what key rotation actually costs, what the deploy file can and cannot change, and that the AWS path has never been applied.
- Repository description, homepage and topics set.
- `deploy/kind/` brings up a local two-zone cluster: `make kind-up`, `kind-test`, `kind-down`, and a CI job off the pull-request path.
- Proven live: two zones serving independently, autoscaling one to two replicas under real load with the neighbouring zone untouched, rebalance, per-zone tool isolation, a 48-case role and group security matrix, both key types, and Claude Code driven as a real MCP client.
- Proven on GKE: the forwarded-for hop count measured position by position, spoofed headers refused, reflection unreachable through the load balancer, and none of the eight defects from the 0.3.2 run recurring.

## 0.3.2 — verified on GKE

- gRPC header routing verified on a live GKE Gateway over cleartext HTTP/2 (h2c); no TLS fallback needed. Live harness: 126 passed, 0 failed.
- Fixes from the live run: zone identity (GSA + Workload Identity + baseline grants) is ensured when a zone is attached, not only by an explicit service-account call; IAM bindings on new service accounts wait for propagation; all worker services (Mcp, Health, reflection) are routed through the Gateway; the node retries its initial load instead of waiting for an admin reload; the bridge pins the server certificate in insecure-TLS mode.
- API: `GET /api/v1/zones/{zone}` and `GET /api/v1/groups/{group}/environments/{env}`. `make env` warns when `.env` is older than the example. Terraform lock files are committed.

## 0.3.1 — gRPC transport

- **Breaking for HTTP MCP clients** (D19, contract §11): the worker's HTTP surface (`POST /mcp`, `/healthz`, `/readyz`, `/metrics`, `/admin/reload`, `RAMEN_MCP_PATH_PREFIX`) is removed. Workers speak JSON-RPC 2.0 over gRPC: `ramen.v1.Mcp/Call` carries one JSON-RPC message as `bytes body`, `ramen.v1.Admin/{Reload,Metrics}` replace the admin routes, `grpc.health.v1.Health` reports `SERVING` once code is loaded; one h2c port `RAMEN_NODE_PORT` (8080), optional node TLS via `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`. Standard MCP clients (Claude Desktop, Cursor, the mcp SDK) connect through **`ramen-mcp-bridge`** (stdio; `ramen-runtime[grpc]` console script, also in the worker image): `--target <host:port> --key <rmk_> --group <g> --zone <z> [--tls|--insecure] [--ca <pem>]`. Migration: docs *Migrate 0.3.0 → 0.3.1*.
- Security parity on gRPC: metadata `authorization: Bearer <rmk_key>` with constant-time compare (`UNAUTHENTICATED`; empty key set = deny all), `RAMEN_ALLOWED_CIDRS` / `x-forwarded-for` with `RAMEN_TRUST_PROXY=1` (`PERMISSION_DENIED`), `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` on `Admin/*`, blocked names hidden from `*/list` and answered `-32601`, 4 MiB message limit, `RAMEN_MAX_INFLIGHT` → `RESOURCE_EXHAUSTED`, unauthenticated health, access log gains `grpc_code`.
- Edge routing by metadata: clients and the bridge send `ramen-group` / `ramen-zone`; GKE Gateway HTTPRoutes match on those headers (worker Service `appProtocol: kubernetes.io/h2c`, `HealthCheckPolicy` type `GRPC`, no path rewrite); AWS ALB target groups `backend-protocol-version: GRPC` with header listener rules and gRPC health code 0; Cloud Armor / WAF unchanged.
- Console: `ramen_console.grpcclient` (grpcio) replaces every HTTP call to workers (`Admin/Reload` + `tools/list` smoke, `Admin/Metrics` for load, `Health/Check` for readiness); `RAMEN_GCP_POD_PROXY` / `RAMEN_AWS_POD_PROXY` removed. Local stack, `make demo`, `mcp_call.py` and `mcp-client-config.example.json` use gRPC / the bridge; harness `ramen_tests.mcp_client` speaks gRPC and covers the bridge end to end through the mcp stdio client; `scripts/cloud_smoke.sh` uses `grpcurl`.
- Protos: `proto/ramen/v1/{mcp,admin}.proto` are the single source; Rust via tonic-build, Python stubs (`ramen_proto`) vendored in console, runtime-py and tests, regenerated with `make proto`.
- Carried security mediums fixed: GCP console GSA drops `resourcemanager.projectIamAdmin` for `iam.serviceAccountAdmin` + a custom role limited to `setIamPolicy` on `ramen-*` service accounts, with bucket-/secret-level bindings; AWS console role narrows `wafv2:*` to the `ramen` web ACL / IP sets and `iam:PutRolePolicy` to `/ramen/` roles; console ClusterRole loses cluster-wide `secrets`/`serviceaccounts` in favour of a namespaced Role + RoleBinding per attached zone; `sync_repo` passes the GitHub token via `http.extraheader`/`GIT_ASKPASS` and strips credentials from `.git/config`; OIDC uses PKCE (S256) + `nonce`; node key compares are constant-time.
- Logo v2 (same coral/off-white palette) in the console, docs site (`docs/img/logo.png`, `logo-mark.png`, `favicon.png`), README and launch drafts. Docs: architecture v0.3.1, transport sections in how-it-works / protos / concepts, bridge + grpcurl quickstart, GCP/AWS header routing and gRPC health, security parity table, migration note; `mkdocs.yml` `version_current: 0.3.1`.

## 0.3.0 — AWS, auth & policy, docs

- AWS path (**untested on a real account**, D18): Terraform `deploy/terraform/aws` (EKS, DynamoDB, S3, ECR, IRSA roles, AWS Load Balancer Controller + Fluent Bit via Helm, self-signed cert in ACM), CloudFormation `deploy/cloudformation/ramen.yaml`, Helm `provider: aws` (ALB IngressGroup, weighted stable/canary target groups, IRSA), console `aws` cloud adapter (S3 repo sync, CloudWatch Logs Insights, ALB weights, WAFv2 IP rules, IAM role per group+zone, `apply_sa_permissions`) and `aws` secrets backend (Secrets Manager `ramen/<group>/<env|all>/<zone|all>/<NAME>`); runtime syncs `s3://` buckets; node `RAMEN_MCP_PATH_PREFIX` serves `/mcp/<group>/<zone>` behind an ALB.
- Auth: OAuth/OIDC providers (`RAMEN_OAUTH_<NAME>_*`, role mapping by claim), email via SMTP or the `file://` dev backend (invite on user create, password reset, magic-link login), super-admin toggles `GET|PUT /api/v1/config/auth` with break-glass `RAMEN_ADMIN_FORCE_PASSWORD`, CSRF double-submit token for cookie sessions (API keys exempt).
- SA policy engine: permission catalogue `GET /api/v1/policy/permissions`, group-admin requests `POST /api/v1/requests`, super-admin approval → `Cloud.apply_sa_permissions` (gcp IAM roles with prefix conditions, aws inline policy, local recorded); denied-by-rule → 409, audited.
- Tool blocking per environment: `PUT …/environments/{env}/blocked` → `RAMEN_BLOCKED` on deploy; the node hides blocked names from `*/list` and answers `-32601`; block/unblock toggle on the group page.
- Docs: MkDocs Material site on GitHub Pages (architecture per version, how-tos for local/GCP/AWS/security/secrets/DevOps API, wiki, generated version tracker, `llms.txt` + JSON-LD), README with screenshots and a 5-command quickstart, `skills/` cloud-ops agent skills, launch drafts.
- Tooling: `ruff` lint/format config shared via `ruff.toml`, `make lint`, CI lint job; Dockerfiles pin `uv` and carry OCI labels.
- Hardening after the security audit: no constant signing secret, security headers, failure-only login rate limit, token redaction in logs, same-origin login redirect, strict names in log queries, OIDC email verification, CSV formula escaping, worker NetworkPolicy + container securityContext, pinned CI actions. Standards pass: ruff across the repo, `make lint`, CI lint job.

## 0.2.0 — GCP

- Console GCP adapter: zone = namespace, canary deploy flow, workers/metrics, Cloud Logging, rebalance via backend capacity, Cloud Armor IP rules, per-group+zone service accounts with Workload Identity, refresh, group destruction.
- Secrets backend `store|gcp` (Secret Manager). Runtime syncs the group bucket from GCS on load.
- Deploy: Terraform (GKE Autopilot, Firestore, Artifact Registry, static IP, groups bucket, console GSA), Helm `ramen` (console, GKE Gateway, RBAC, backend policy) and `ramen-worker` (zone namespace, canary, HTTPRoute `/mcp/<group>/<zone>`), `make push`, GCP how-to.
- Node: separate `RAMEN_ADMIN_CIDRS`; console: IPv6 CIDRs, zone validation on rebalance, drain-aware deploys, thread-safe Google API transport.
- Tests: cloud suites (canary, rebalance, IP rules, logs, service accounts, secrets), cloud smoke and cost-check scripts, manual `cloud-e2e` workflow. Verified on a throwaway GKE project: 89 passed, 0 failed.

## 0.1.0 — local core

- Python runtime sidecar (`ramen_runtime`): loads group repos, validates protos, executes tools/resources/prompts, resolves `{{$group.VAR}}` secrets.
- Rust MCP node (`ramen-node`): JSON-RPC 2.0 over MCP Streamable HTTP, bearer auth, CIDR allowlist, metrics, admin reload, sidecar supervisor.
- FastAPI console: auth, RBAC (super admin / group admin / viewer), groups, environments, zones, secrets (names only), deploy jobs, rebalance, logs, audit, API keys, backups, config; Firestore, DynamoDB and memory stores with Fernet encryption.
- Local stack: docker-compose (Firestore emulator + console + worker), `make demo`; Helm chart; GCP Terraform skeleton (Phase 2).
- Test harness: conformance (node, sidecar, console API), e2e demo flow, coverage report, version check, CI with e2e job, tag-driven releases.
