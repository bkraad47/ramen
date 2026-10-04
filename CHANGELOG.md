# Changelog
All notable changes. Versions follow semver; 0.x is pre-stable.

## [Unreleased]
- **The documentation site, reorganized around what a reader came for.** Home is an introduction with the three
  repositories and the reasons to use Ramen; *Get started* is one page with a picture per step (run it locally,
  connect an MCP repo, connect a client with a key, with OAuth, or through the bridge); *How it works* holds the
  architecture and a table of every feature with where it is managed; the *Wiki* is one page per subject (deploy
  on GCP, deploy on AWS, groups and zones and regions, the MCP repo, secrets, users and access, connect with
  OAuth, connect with a password, API keys and the API, throttling with Redis, Entra ID and Workspace, backups,
  configuration); *Changelog* and *Release* are short pages, and the contracts, threat model, transport and
  architecture pages move under *Reference*. The repeated and stale how-to pages are gone.
- **Every screenshot is now a live capture** from a real GCP deployment of this release rather than a seeded
  console, including the zones page and what an MCP user sees. The architecture drawing was redrawn wider so its
  labels no longer overlap each other.
- The deploy guides now say what the console's own service account may do on each cloud, and what each zone's
  identity starts with.
- Discoverability for 0.6.0: `llms.txt` and the landing page's JSON-LD (now a `SoftwareApplication` +
  `SoftwareSourceCode` graph) describe the release as it is, link only pages that exist, and say plainly that
  Claude Desktop and Cursor connect with a group key (no dynamic client registration, so no OAuth on their own).
  `scripts/check_docs_links.py` (CI and the pages build) fails on a site link in README/llms.txt/landing page with
  no page behind it; `check_versions.py` now also checks the version in `llms.txt` and the JSON-LD.
- **Docs: Deploy from GitHub Actions** (new wiki page): a least-privilege `rmn_` key (Group Admin of one group) as
  the repo secret `RAMEN_API_KEY`, how a deploy reaches the group's repo (the console clones the environment's ref;
  GitHub App, `GITHUB_TOKEN` secret or fallback token for private repos), a workflow that deploys on push to `main`
  under `mcp/`, polls the job and fails the run on error, self-signed CA and path prefixes, rotation and what
  401/403/404 mean. Run against the local compose stack; the demo repo ships the workflow, a no-op without settings.
- **Docs for the base URI**: the Config-page card, precedence (base URI → `RAMEN_PUBLIC_URL` → request), validation,
  recovery with `PUT /api/v1/config/base-uri {"base_uri":""}` at the root, workers on their next deploy, the Swagger
  limitation, and what a path-prefix proxy must route (GKE `ReplacePrefixMatch`; the AWS ALB cannot rewrite).
  CONTRACTS §4a, §16.3 and a new §18.
- **Docs from the 0.6.1 cloud runs**: GCP project ids held 30 days, destroy (or move the state) before deleting the
  project, the `503 unconditional drop overload` after a console restart, a dropped zone's `503` then `404`, GKE
  route rejection blocking every route; AWS EKS 1.35 and extended-support cost, `crane` pushes, re-pushed tags not
  pulled (record the digest), the `worker-http` Ingress, the canary's ALB drain (`RAMEN_ALB_SPLIT_DRAIN_SECS`), the
  `wafv2:GetWebACLForResource` grant, `0.0.0.0/0` as two `/1` halves, WAF blocks that look like node denials and
  health probes blocked by an IP lock; the canary gating the stable track and group deletion removing the Cloud
  Armor policy wherever deploy and teardown are described.
- The worker image installs `ramen-mcp-bridge` 0.2.2 (`--ca` also verifies the console during `--oauth`; Windows
  keeps tokens in `%LOCALAPPDATA%`). The Config screenshot shows the base URI card.

## [0.6.0] — instructions/v0.6.0.md: deletions that delete, HTTPS only, secrets as environment, the documentation rebuilt
**A zone that goes away is torn down.** Deleting a zone, dropping it from an environment (when no other environment
of the group still uses it) or deleting an environment now destroys that group's deployment there — the namespace
with its workers, services and routes, and the zone's service account / IAM role (`Cloud.detach_zone`) — and
removes the worker record; before, the console only forgot the zone and the cloud kept running it. On GKE the namespace
can report `Terminating` for a while afterwards: its NEG finalizer waits for the Gateway-managed backend service,
which the Gateway controller garbage-collects late (the GCP guide says what to do if it never does).

**HTTPS only.** A public console (`RAMEN_COOKIE_SECURE=1`, which the charts set) answers plain http with a 301 to
https for GET/HEAD and a 403 for anything else; the LB's `X-Forwarded-Proto` marks the real requests; `/healthz`
and `/readyz` stay plain for the kubelet. The cloud edges already listened on 443 only; the compose stack serves
TLS on 8443 only.

**Users page dropdowns** open outside their card (the card used to clip them).

**A stuck rollout now says why.** When a canary or stable deployment is not ready in time, the deploy job used to
report only the replica counts ("1/1 ready", which was the old pod); it now lists the pods that are not running
with the scheduler's or the container's reason, for example `worker-canary-… Pending (Unschedulable: 0/2 nodes
are available: 2 Insufficient cpu.)`. Found on the AWS 0.6.0 run, where two small nodes could not fit a second
zone's canary.

**The canary rolls in place.** `worker-canary` now restarts with `maxSurge: 0` (the stable track keeps its surge
pod): a canary is one pod pinned to its zone, and a second one on a full node in that zone stayed Pending until
the deploy timed out. Stable capacity never changes during a canary restart, so nothing is lost.

**AWS edge IP rules blocked nothing.** The WAF rule each group's IP rules add to web ACL `ramen` still matched the
URI path `/mcp/<group>/` that 0.3.1 removed when routing moved to the `ramen-group` / `ramen-zone` headers, so no
request ever matched it and the edge never blocked anyone (the node's own `RAMEN_ALLOWED_CIDRS` check still did).
The rule now matches the `ramen-group` header exactly, pinned by the adapter test. Found while rewriting the AWS
guide for this release.

**Password-based MCP users** are sent to the login page by a client's authorize request and land on the consent
page once signed in — pinned by a test; Claude Code, Claude Desktop and the bridge all go through it.

**`mcp/env.yaml` — the group's environment, rendered from secrets.** A flat `KEY: value` file (or `mcp/.env`) in the
group repo; the worker renders `{{$group.SECRET}}` references from the secrets the deploy handed it and exports
every key to the Python runtime at load, so tool code reads `os.environ["DB_URL"]`. A reference with no secret
behind it is skipped with a warning, rendered values are redacted from error messages, and the load result lists
the keys (`env`). The demo repo ships one.

**The documentation, rebuilt around how people use it.** New pages: *Features, and how each one is managed*;
*Connect an MCP client* (keys, OAuth, password accounts, the bridge, with the demo repo and the PyPI package
highlighted); *Write and develop an MCP repo*; *Service accounts and secrets management (GCP and AWS)*; *Back up
and restore*; *Configuration* (the config file, every variable that matters, verbose logging); *Contributing,
modifying and using Ramen*; *Related versions* (bridge and demo repo). The landing page and *How it works* open
with what Ramen is for — redefining how MCPs work, MCP management made easy, built on how organizations work, an
AI-native approach versus the traditional one — and the navigation is grouped by task (get started, connect,
deploy, operate, reference). Every page was reviewed for readability and stray characters.

## [0.5.95] — roles are per group: the roles engine of instructions/v0.5.95.md
**One role engine, per group (D41).** A person now holds a role *in each group* — `user.memberships:
{group: role}` with `group_admin`, `viewer` or `mcp_user` — and super admin stays global. Someone can be a Group
Admin of one group, a Viewer of three and an MCP User of two more. `rbac.can(p, role, group)` reads the
membership of that group; without a group it asks for that rank anywhere (page-level gates); `require()` is the
single gate for every page and `/api/v1/*` route, and a person with no membership anywhere (an MCP user not yet
approved into a group) gets 403 everywhere except their own page, `/api/v1/me` and the OAuth consent flow,
which refuses them per group. User documents from before carry `role` + `groups` and read as that role in each
listed group (migrated in place at startup); `role` and `groups` remain on every user as derived summaries, so
the API shape, API keys and the old readers keep working. Tokens are minted only for a group the person is in.

**Who may do what** (the table in the instructions, now enforced and pinned by `tests/test_roles_engine.py`
incl. a role × route matrix): Config, API keys, Backups and Audit are super-admin only (pages, nav and API;
group admins mint MCP agent keys on their group page instead); only super admins delete groups (no button
otherwise); secrets are seen and edited by super and group admins only; group admins scale workers of their
group, manage who is a viewer / MCP user there (`PUT|DELETE /api/v1/groups/{g}/members/{uid}`,
`POST /api/v1/groups/{g}/members {email, role}` — only a super admin hands out Group Admin), reset passwords of
their groups' members (never a super admin's), and approve, deny or revoke service-account requests of their
group — **never their own**: another admin of the group or a super admin must; viewers see the users of their
groups (read-only), the group's MCP key names, workers and logs; the Service-account permissions and
restrictions cards are shown to admins only. Anyone signed in may file a request.

**Users page** maps people per group and role: one table per group with a role dropdown (radio) per member, a
Remove button and an Add-member form for the groups the viewer administers; the Users table below keeps the
account-level actions (reset password, delete) for the people the viewer may manage; the Create-user form
takes the role and the groups from dropdowns and grants that role in each. Requests of the admin's groups are
listed with Approve/Deny only for requests they did not file.

**IdP rules build memberships per group** (D38 + D41): each matching rule adds its groups at its role, the
highest role wins per group, `super_admin` rules make a super admin. New how-to `docs/how-tos/sso.md`: Microsoft
Entra ID and Google Workspace as OIDC providers with the claims to map (untested against a real tenant — says
so). The group page gained a collapsed "How MCP users sign in" card (HTTP and bridge).

**The stdio bridge signs people in too** (`ramen-mcp-bridge` 0.2.0, instructions note 23): `--oauth <console>
--client-id <id>` runs the console's authorization-code + PKCE flow with a loopback callback (RFC 8252), opens the
browser on the console's sign-in page (so Entra ID / Google Workspace apply), keeps the refresh token in a 0600
file, refreshes before expiry and after an UNAUTHENTICATED call, and signs every gRPC call with the person's
token — the worker's log then names the account. The group page's "How MCP users sign in" card shows the line. A registered loopback redirect (`http://localhost/callback`) now matches any loopback name and port — `127.0.0.1`,
`localhost`, `::1` are one interface (RFC 8252) — so one client registration serves Claude Code and the bridge.

## [0.5.94] — group page zones and blocked packages as checkbox dropdowns
**Group page: Environments → Zones and Packages → "Blocked everywhere" are checkbox dropdowns**, the same
control the Users page got in 0.5.93 (`partials/multi.html`): the new-environment form picks zones from the
zones that exist, and the per-environment block list offers every package the zones reported on their last
deploy (plus anything already blocked), so a name is never typed. Clearing every box still saves an empty list.

## [0.5.93] — OAuth role mapping and `mcp_user`; scoped service-account permissions; every settings form works in a real browser
Builds on 0.5.91 and 0.5.92 (both deployed to the live GCP console during the same day, never tagged — folded
in like 0.4.1–0.4.3). The user drove this release page by page on the live console; each report below was
reproduced in Chromium (Playwright) before it was fixed and verified there again after.

**Service-account permission requests carry a scope.** The group page's request form has a "Scope" field:
empty or `*` keeps today's least-privilege binding (`bucket.*` on the group's own prefix of the groups bucket,
`secrets.*` on the group's secrets); naming resources (`bucket-a, bucket-b`, or secret names) binds the
permission on those alone — GCP binds the role on each named bucket / secret, AWS names them in the inline
policy's resources. A later request for the same permission replaces its scope; narrowing a scope unbinds what
left it; revoking the permission drops its scope. Permissions whose cloud roles are project-wide (logs,
metrics, queues, datastore, kms, ai) record the scope but cannot enforce it, and the apply result lists them
under `unscoped`. Live on GKE: the console's identity holds `storage.admin` on the groups bucket only (SEC-08), so a scope naming another bucket fails the approval with a message that says which `gcloud storage buckets add-iam-policy-binding` to run; with that grant in place the approve binds on the named bucket and the revoke unbinds it (verified on a throwaway bucket). API: `POST /api/v1/requests {…, "scope": "bucket-a, bucket-b"}`; the request and the zone's
worker record (`sa_scopes`) carry the parsed list; the page shows it on the request row and the granted badge.
The "Service-account restrictions" card now explains what the rules do (they gate *requests*: deny wins, allow
rules whitelist, shell globs, clash with super-admin rules → 409) with an example.

**SMTP and warning-email settings moved from the Users page to Config** (they are super-admin settings; the
API already required that). The Save button on "Server warning/error emails" never sent a request in a real
browser: the form built its body with `hx-vals='js:…'`, which htmx evaluates with `Function()`, and the
console's CSP (`script-src 'self' 'unsafe-inline'`, no `unsafe-eval`) refuses that — silently. The same
pattern broke "Add rule" on Config, "Save restrictions" on a group page, the dashboard's and audit page's
`every 60s[window.ramenAuto]` polling filter and the audit page's `hx-on::after-swap`. All of them are plain
forms or event listeners now (chips carry hidden fields, the rules travel as a JSON string the API parses,
polling elements carry `data-auto` and a listener drops polls while auto refresh is off), and a test sweeps
every template for anything that would need eval. The SMTP Save did work — it just said nothing: every
successful mutation now shows a green "Saved" toast, also after the page refresh a save triggers.

**Audit page: the filter row is a table** — Search, Outcome, row count and the two buttons (Load 500, Auto
refresh) sit in one aligned row with column headings instead of a free-flowing flex line.


**IdP roles map to Ramen roles and MCP groups, on every login (D38).** A super admin names, per sign-in
provider, the token claim to read (`groups`, `roles`, …) and adds rules: a claim value (an AD group, an
IdP role) → a Ramen role, optionally plus the MCP groups it may reach. The rules are keyed by the IdP role,
never by a person: whoever signs in carrying `AD-Developers` gets what that rule says. Once a claim is named
the mapping is **authoritative** — re-applied at each OIDC login, replacing a role or groups set by hand,
and a person whose values match no rule becomes a viewer with no groups. Several matching values take the
highest role and the union of groups (as before). The bootstrap super admin (`RAMEN_ADMIN_EMAIL`) is never
demoted by a provider. A provider with no claim named keeps today's behaviour (role set at first login,
then kept). Env rules (`RAMEN_AUTH_OAUTH_<NAME>_ROLE_CLAIM` / `_ROLE_MAP`) still work and merge under the
store's rules, the store winning per claim value. API: `PUT /api/v1/config/auth/role-map/{provider}` with
`{claim}`, `{value, role, groups}` or `{remove}` (one change per call; 404 for an unknown provider, 422 for
an unknown role); `GET /api/v1/config/auth` reports the merged `role_claim` / `role_map`. Nobody is signed
out by a rule change — it takes effect at each person's next login. Audited as `config.auth
role-map:<provider>`.

**OAuth clients live on the Config page now**, next to the role mapping, not on the API keys page: both
are super-admin settings, and together they are the "sign in with your IdP, reach workers with a token
instead of a shared agent key" story. The registration form is aligned like every other admin form.

**New role `mcp_user` (D39): a person who only connects MCP clients.** Below viewer, scoped to groups like every
other role: an MCP user may sign in and approve an OAuth client for a zone of their groups (Claude Code,
Claude Desktop, Cursor, …), and nothing else — every console page and every `/api/v1/*` call answers 403
("MCP users connect MCP clients to their groups' workers; the console is not available to them"), the
sidebar has no navigation, and their only page (`/`) names their groups and shows the `claude mcp add-json`
line to connect. Grantable by a group admin within their own groups (Users page, "MCP User — connects MCP
clients only"), mappable from an IdP role on the Config page (`mcp_user` + groups), and re-checked at every
token mint like any other role. `/api/v1/me` answers for them (clients may ask who they are); API keys cannot
be minted for or by them. The OAuth authorize endpoints now accept any signed-in person whose groups include
the requested one (`rbac.can_connect`), replacing the viewer check.

**Users page: groups are picked from a checkbox dropdown; SSO users get no password reset.** A user's groups
were a comma-typed field; the row now has a dropdown of every group with a checkbox per group (the summary
names the current ones), and clearing every box saves an empty list (a hidden empty `groups` field carries
it; list fields drop empty strings). A user who signs in through an OAuth/OIDC provider has no password, so
their row says "Signs in through <provider> — no password to reset" instead of offering a reset, and
`POST /api/v1/users/{id}/password` (and `/users/me/password`) answers 422 for them.

**Found by pointing Claude Code at the live GCP worker as an OAuth MCP client: the worker's protected-resource
metadata was not RFC 9728.** Its `resource` field carried the scope string (`mcp:demo:a`); RFC 9728 says it
is the resource's URL, and Claude Code checks that it equals the server URL it was given — so it refused to
even start the flow ("Protected resource mcp:…:a does not match expected https://…"). The worker now reports
`<RAMEN_PUBLIC_URL>/mcp` (or, with no public base, the request's `X-Forwarded-Proto`/`Host`), keeping
`scopes_supported` as `mcp:<group>:<zone>`; and the console's authorization server accepts that URL as the
RFC 8707 `resource` indicator (an MCP client sends the server URL it talks to) as well as the scope string
it took before — anything else is still `invalid_target`. Token audience is unchanged (`mcp:<group>:<zone>`).
Claude Code is registered as a client with `claude mcp add-json … {"type":"http","url":"https://<host>/mcp",
"headers":{"ramen-group":…,"ramen-zone":…},"oauth":{"clientId":…,"scopes":"mcp:<group>:<zone>"}}`; its
loopback callback matches a registered `http://localhost/callback` on any port (RFC 8252). No DCR (D34).

## [0.5.91] — Group page: per-zone package list back, Enable/Disable fixed, every admin page aligned
Defects the user hit on the live GCP console right after 0.5.8, each reproduced first and verified in a real
browser (Playwright/chromium against the local compose stack) before shipping. The number was the user's
choice; a 0.5.9 was built along the way but never tagged — folded into this release, like 0.4.1–0.4.3.

**"Packages per zone" was empty after a deploy.** `GcpCloud.deploy()` (inherited by AWS) only ran
`Admin/Reload` + the smoke `tools/list` on *canary* pods, so a stable-only deploy (`canary=false`) never
recorded any worker's `result` and `last_deploy.packages` stayed `{}` — the group page then had nothing to
list and no Enable/Disable buttons to show. Stable pods are now reloaded the same way (their package list
is recorded; a reload hiccup there is logged, not treated as a failed rollout — readiness and drain already
proved it). Regression assertion added to the existing no-canary deploy test.

**Enable/Disable answered `blocked: Field required`.** The console's `json` htmx extension built the request
body from htmx's FormData, which loses JSON types: a one-item list arrived as a plain string (it only ever
worked because `ListFields` splits comma strings) and an empty list — exactly what Enable sends when the
last disabled package is re-enabled — vanished entirely, so the API saw `{}` and rejected it. The extension
now re-reads `hx-vals` typed via htmx's own `getExpressionVars`, which also covers the three `js:`-computed
forms (SA restrictions, notify list, SA rules) that had the same latent empty-list gap. Verified in the
browser: Disable sends `{"blocked":["demo_calculator_tool"]}`, Enable sends `{"blocked":[]}`, both 200.

**Zone actions misaligned.** Three multi-field forms plus three buttons were crammed into one cell of the
8-column zones table. They now live in their own "Zone actions" table: one row per setting (Workers / IP
allow-list / Throttle / Zone), inputs and the equal-width `.act` buttons each in their own column, zone name
as a row header. Every pinned UI contract still holds (one `zone-actions` block per zone, fixed button
order, `act ghost` classes, visible labels, hidden from viewers). Screenshots recaptured (`ui_contract`
0.5.9).

**API keys page.** The create-key row had labels and dropdowns of uneven width and "Add" (group picker)
sitting next to "Generate key" at a different size. Now one *New key* section with a description of both
key types and the one-time display: name, client type, role and groups all at the `.controls` width, "Add"
an action-width button on the field row, and the equal-width "Generate key" on its own row below (it submits
the same form via `form=`). Same API, same pinned contracts (`#group-pick`, chips, `.controls` width rule).

**Logs page: clicking an entry did nothing.** The entries' bodies are embedded as JSON in a
`<script type="application/json">` for the click handler, and the template's autoescape HTML-encoded the
quotes inside it (`[&#34;{…`), so `JSON.parse` threw at load, `bodies` never existed, and every click
failed silently. The JSON is now emitted unescaped (with `</` escaped so a body can't close the script
early) and the handler is delegated on the list (no inline `onclick`, no implicit `window.event`).
Regression test pins the embedding. Verified in a real browser: the body pane updates and the highlight
moves.

**"Packages per zone" listed the same packages once per worker.** Once stable pods reported their package
list (the first fix above), a canary deploy produced two identical tables per zone — canary pod and stable
pod — and a scaled zone would have produced one per replica. Every worker in a zone runs the same code, so
the group page now shows one list per zone and says how many workers reported it. Regression test added.

**Bridge leftovers removed from this repo** (the bridge moved to its own package in 0.5.7):
`deploy/local/mcp_call.py` still spawned `python -m ramen_runtime.bridge` — it now runs `ramen-mcp-bridge`
from PATH (`RAMEN_BRIDGE=<cmd>` still overrides); the local and runtime-py READMEs no longer tell you to
install `ramen-runtime[grpc]`. Also fixed a 0.5.7 regression found while doing it: the `mcp` SDK had been
dropped from `runtime-py`'s dev extra as "bridge-only", but `deploy/local/mcp_call.py` (what `demo.sh`
runs) needs it — restored. The remaining `grpcio-health-checking` deps in `console/` and `tests/` are
their own health probes, not bridge code, and stay.

**Four more pages aligned, with descriptions.** *Config → Auto-rebalance scheduler*: a settings table
(state badge + toggle, interval + "Set interval") instead of two loose inline forms, and a line on what it
does. *Backups*: a "New backup" section explaining target/path, the create row's button now the same 180px
as its fields, and each backup's four actions as a 2×2 block instead of a tall stack. *Users*: every row's
actions share one grid (Role / Groups (comma) / Save, New password / Reset password, Delete user) with
labels on the previously bare select and inputs, buttons in one column. *Zones and workers*: a "New zone"
section (what a zone is, naming, how it gets used), the create row on the shared `.controls` sizing, and
"Workers per group" now says what a worker is — and that a sidecar reported as stopped is just idle:
the Python sidecar starts on the first call and is reaped after `RAMEN_SIDECAR_IDLE_SECS` (default 300 s)
without calls, by design (F3.4/D3); set a very large value on the deploy to keep it warm (0 is clamped to
1 s, there is no "never"). All pinned contracts kept; screenshots recaptured.

## [0.5.8] — The published ramen-mcp-bridge package verified live against real AWS and GCP deployments
A full live bring-up/deploy/teardown round on fresh throwaway AWS and GCP environments, specifically to
server-test the real `pip install ramen-mcp-bridge` package (v0.5.7's extraction) against real cloud load
balancers — not just a local kind-equivalent process. Found and fixed one real bug.

**AWS — a genuine TLS hostname-verification bug, found only because the bridge checks strictly**: the
self-signed console certificate's SAN only ever covered the placeholder `ramen-console.local`, never the
real ALB hostname (which isn't known until after the ALB exists — the terraform resource that creates the
cert runs first). `curl -k` and `grpcurl -insecure` both skip hostname verification entirely, so this never
surfaced before; a real gRPC client doing the documented `--ca ramen-lb.pem` flow fails with
`Hostname Verification Check failed`. Fixed in `deploy/terraform/aws/main.tf`: the cert's SAN now also
covers `*.<region>.elb.amazonaws.com` (every AWS ALB hostname is exactly one label under that), which
matches any ALB this terraform creates without needing to know the literal hostname in advance. The ACM
certificate updates in place (same ARN), so no Helm/chart changes were needed. Verified live: the real
published bridge completed `initialize` → `tools/list` → `tools/call` through the fixed ALB.

**GCP — 0 new bugs.** The bridge completed the same full flow against the GKE Gateway's publicly-trusted
managed cert without any fix needed — confirms the AWS issue was specific to AWS's self-signed-cert path,
not a bridge-side gap.

**Stale docs found and fixed along the way** (both genuinely outdated since v0.5.6 N10 first proved AWS
live, just never updated): the Helm charts' own `NOTES.txt` and `values.yaml`/`Chart.yaml` comments still
said "UNTESTED provider (no AWS account)"; `deploy/README.md`'s AWS infra row still said `**UNTESTED**`
(the CloudFormation alternative row correctly still says so — that path genuinely hasn't been tried); and a
dangling reference to the `ramen-bridge/` folder removed in v0.5.7 pointed nowhere.

Both throwaway environments were stood up fresh for this round (AWS's tiny fixed 2-node EKS cluster hit the
same single-node-per-zone capacity crunch during rolling restarts as v0.5.6 N10 — not a regression, just the
cluster being small on purpose; GCP's Autopilot cluster auto-provisions and had no such issue). Per the
request, only AWS was torn down and verified fully clean afterward (same checklist as N10); GCP was left
running for live inspection.

## [0.5.7] — The MCP bridge becomes its own pip-installable package
`ramen-mcp-bridge` moves out of `runtime-py` into its own standalone, public repo
and package — [github.com/bkraad47/ramen-mcp-bridge](https://github.com/bkraad47/ramen-mcp-bridge),
`pip install ramen-mcp-bridge` — finishing what F2.4 deferred back in v0.5.5 ("code stays in runtime-py for
now"). No behavior change to the bridge itself: same CLI, same flags, same env vars, same wire protocol.

**Package**: own minimal dependencies (`grpcio`, `grpcio-health-checking`, `protobuf` — not `jsonschema`,
`mcp`, `boto3`, `google-cloud-storage`, none of which the bridge needs); own vendored proto (just
`mcp.proto`, not the unrelated `admin.proto` the console's Admin RPCs use); `requires-python` loosened from
`>=3.14` to `>=3.10` since nothing in the bridge actually needs 3.14 — the one thing that did, an
`except ValueError, AttributeError:` relying on Python 3.14's new bare-tuple exception syntax (PEP 758), was
parenthesized for portability with no behavior change. GitHub Actions release workflow publishes to PyPI via
Trusted Publishing (OIDC) on a `v*` tag — no stored API token.

**ramen (this repo)**: `runtime-py` loses `bridge.py`, its test, the vendored `ramen_proto` package, the
`grpc` extra, and the `ramen-mcp-bridge` entry point — none of it was used by anything else there. The
worker image (`node-rs/Dockerfile`) still installs `ramen-mcp-bridge` (used only as a generic
`grpc.health.v1` probe for its `HEALTHCHECK`), now from the new repo instead of a local extra. CI jobs that
needed a working bridge binary (`node-conformance`, `client-windows`, `e2e`) now install it from the new
repo too. The dead `ramen-bridge/` staging folder (mirrored by hand into the old docs-only `ramen-mcp-grpc`
repo) is removed — that repo was found already deleted outside this session when checked mid-task, so there
was nothing left to redirect.

## [0.5.6] — Auto-rebalance, email alerts, and the AWS deploy path verified for real on a live account
11 user-filed items (`instructions/v0.5.6.md`), worked one at a time with a failing test written first for
each (`logs/reasoning/2026-10-02-v0.5.6-iterate.md`). Headline: the AWS bring-up path, documented as
"UNTESTED ON A REAL ACCOUNT" since v0.3.0, finally ran end to end on a real account — and a live GCP
redeploy for parity — turning up real bugs that no amount of mocked testing had caught.

**Scheduler**: a new health-check node watches every group and auto-rebalances when load skews to one
worker, independent of the existing on-demand `POST .../rebalance`; a super admin toggles it and sets the
check interval from the Config page. Proven on a real kind cluster driving sustained one-sided load with no
manual rebalance call anywhere in the test.

**Email alerts**: super admins set server SMTP credentials and pick which users get notified of server
warnings/errors; a process-wide log handler batches WARNING+ records into a digest every 30s rather than one
email per line.

**Logging**: worker pods already stream into GKE Cloud Logging and AWS CloudWatch natively (verified, not
new code) and already carry group/zone labels for external filtering; Rust's `CallLog::emit` no longer
hardcodes level "info" for denied/errored calls, and 15 console routes that called `note()` with empty tags
now carry real identifying data.

**Git auth**: groups can use a GitHub App installation token instead of a stored PAT — minted fresh (~1h)
per deploy, no long-lived credential to rotate.

**Rust-side validation**: `tools/call` and `prompts/get` arguments are now schema-validated in the node
itself, before reaching the Python sidecar, matching the console's existing error conventions exactly
(`isError` for tools, a real `-32602` for prompts).

**Throttling**: two independent Redis-backed limits — per-worker item throttle and cross-zone group-scope
throttle — enforced in the Rust workers with fixed per-minute windows, failing open on a Redis outage.

**Secrets audit**: swept storage encryption, API response masking, audit tags, git-credential handling and
backups. Found and fixed a real leak — `RAMEN_POSTGRES_DSN` (which commonly embeds a password) was shown
unmasked on the Config page because it matched none of the SECRET/PASSWORD/KEY/TOKEN substrings `masked_env`
checked against.

**AWS — verified live on a real account**: found and fixed 2 real bugs. `storage/dynamodb.py` used an
implicit boto3 region lookup that raised `NoRegionError` under EKS IRSA credentials even with `AWS_REGION`
set in the pod env; now passes `region_name` explicitly. The Helm chart's AWS branch never wired
`RAMEN_DDB_TABLE` at all, so the console silently defaulted to table "ramen" while IAM was scoped to the
real table name (`AccessDeniedException`); now wired through `values.yaml`/`deployment.yaml`. A live
teardown (not a dry run) also found Terraform leaving the console's own IAM objects behind (a worker IRSA
role using the terraform-managed boundary policy, plus a Secrets Manager secret) — deploy/README.md now
documents explicit cleanup commands for both. Bring-up and teardown sections both dropped "(untested)".

**GCP — re-verified live**: a full live redeploy found 0 new product bugs; confirms parity with the AWS
proof on the same release.

**HTTPS-only audit**: the edge (browser/client → load balancer) was already HTTPS-only on both clouds with
no plaintext listener, but the Helm chart never defaulted the console's Secure-cookie/HSTS flag on even
though every real cloud deploy is always served over HTTPS — fixed, `RAMEN_COOKIE_SECURE` now defaults to
`1` for both `provider: aws` and `provider: gcp` (kind's plaintext NodePort stack overrides it back to `0`,
matching how it already runs). Intra-cluster (LB→pod, pod-to-pod) traffic stays the existing documented
opt-in (`RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`) — confirmed with the user as a deliberate trade-off, not silently
left open.
17 user-filed items (`instructions/v0.5.5.md`), worked one at a time with a failing test written first for
each. Several turned out to already be fixed and just needed a stale note corrected; several surfaced real
defects that automated testing had not caught because nothing had ever exercised that path for real.

**Console UI**: create-group/create-user/create-backup forms moved behind a button with a proper
dropdown+chips group picker (matching the existing API-keys page pattern) instead of an always-open inline
form or a 4-row multi-select listbox; zone-actions, api-keys, logs and audit header rows now share one
`.controls`/`.act` sizing convention instead of each control sizing to its own content; the audit page gained
auto-refresh behind the same toggle the dashboard already used; admins get a reset-password button per user
row (the API already existed and was tested — only the UI was missing).

**Logs**: the "All workers" view only ever read a file literally named `worker.log` — any zone with more
than one worker showed nothing. GCP and AWS log fetchers were prefixing each line with `ts severity pod `
plain text before the worker's JSON, which silently broke `logview.py`'s `json.loads` on every real line and
rendered every row as `-`/`-` for consumer and timestamp on GKE. Both fixed; the page is now one bounded
scrollable frame instead of 15 unbounded rows then a separate scrollbox.

**TLS (D17 superseded)**: the console's GKE Gateway now provisions a Google-managed certificate automatically
— a free `sslip.io` hostname derived from the load balancer's own static IP, no domain purchase required —
instead of the D17 self-signed fallback. `ramen-mcp-bridge --tls` needs no `--ca` and no `kubectl` step
against it; verified live end to end on two throwaway GKE projects (cert reached `ACTIVE`, the console
answered `200` over plain `curl` with no `-k`, issuer `Google Trust Services`, and — on the second round —
`ramen-mcp-bridge --tls` forwarded a real `demo_calculator_tool` call through the load balancer and got
`5` back for 2+3). Self-signed remains available as an opt-out (`gateway.certificateMap` unset).

**Storage**: Postgres joins Firestore and DynamoDB as a third `RAMEN_STORE` backend (self-hosted/on-prem use,
D2/G1 partly superseded) — one table, four methods, no migrations. Found and fixed during testing: the
adapter's lazy connection-pool lock was named `self._lock`, silently shadowing the base `Store` class's own
`_lock()` method and breaking every backend's generic `transaction()` — caught immediately by a test that
actually calls it. New `docs/how-tos/storage-backend.md`.

**Multi-zone load and rebalance**: `rebalance`'s known defect (D-CONSOLE-1: a non-`ApiError` from the
compute API turned the whole call into a 502 instead of degrading, losing the HPA re-scale leg that needs no
cloud API) was already fixed with a regression test — only a kind test's explanatory comment describing the
old bug was stale; corrected it. The kind load generator was gRPC-only; it now also drives Streamable HTTP
traffic, and two new tests prove autoscale and rebalance work under HTTP load too, not just gRPC — all
live-verified against a real kind cluster.

**A real pip-install test, and what it found**: no fixture group had ever listed a real third-party package,
so nothing had proven a group's `requirements.txt` actually reaches the worker's `pip install`. Added one
that installs and imports `numpy` for real. It immediately found that the `uv`-managed dev/CI virtualenv for
`runtime-py` has no `pip` at all (uv doesn't need one; `runtime_py/deps.py` shells out to `python -m pip`
directly) — not a production bug (the real worker image's plain `python -m venv` includes pip), but it had
silently made every local/CI conformance run of this path untestable. Fixed in three CI jobs and documented.

**Three new public repos / a public dev-contribution path (I17)**: `ramen-mcp-grpc` (bridge install/use docs;
code stays in `runtime-py/` for now) and `ramen-demo-mcp-group` submoduled locally as `ramen-demo-mcp/` (D8
partly superseded). `skills/test/` and `skills/iterate/` ship an AI-agnostic (agentskills.io format, any
compatible agent — not just one vendor's) version of this project's own reproduce-fix-verify workflow, reading
a new `facts/STATE.md` scratchpad — the public analogue of the private workspace's own fact-block convention.

**Docs**: fixed a real self-contradiction — the README and docs landing page both still said HTTP/OAuth had
"not yet run in a cloud" while their own accurate sections, right below, already said 0.5.1 proved it live on
GKE.

**`log::tests::mirrors_to_file`, actually fixed this time**: the 0.5.4 fix (count only this test's own lines)
addressed concurrent *writers* to the process-global log file, not the real cause — `server::run()` calls
`log::set_file` as an ordinary startup side effect, and every test that starts a node (`server.rs`, and
`http.rs` via a shared helper — about a dozen call sites) could redirect the file pointer mid-test on a
parallel thread. Fixed at the root with a test-only lock shared between the two; 10 consecutive clean runs
confirmed it where the first run of this release had failed.

Carried forward, not yet done: a Gmail SMTP smoke test (`scripts/mail_smoke.py`, written and verified against
the local file-backend; needs the user's own Gmail App Password in `deploy/local/.env`).

## [0.5.4] — honest deploy pictures, aligned environment buttons, docs brought current
- The docs' group and deploy-job screenshots showed a deploy that had **failed** ("Authentication failed for https://github.com/…"): the screenshot rig deployed the demo group from GitHub through a laptop with a stale keychain token. The rig now clones a local git copy of the demo group, so the seeded deploy succeeds and the pictures show a job that ends in `ok` with packages per zone.
- Group page → Environments: the verbose toggle, **Deploy (canary)** and **Delete** were three sizes on two baselines; all three now use the equal-width `row-actions` pattern (the toggle reads `Verbose: on/off`). A test pins it.
- Architecture page brought to 0.5.x (Streamable HTTP and OAuth in the call path, decisions D31–D35, version history); every README relative path, every external link and every docs link checked; screenshots re-captured (`ui_contract` 0.5.4).
- Test-only: `log::tests::mirrors_to_file` counted every line of the process-global log file and flaked when another test emitted concurrently (it failed CI on the 0.5.3 commit and took the pipeline with it); it now counts only its own lines.

## [0.5.3] — how to add and deploy a tool
- `docs/how-tos/add-a-tool.md`: from an empty folder to a tool an AI client can call, with the demo group as the worked example — the layout, the calculator read line by line, a new `word_count` tool in two files, a local check by driving the worker's own runtime over stdio (`runtime.load`, `runtime.call_tool`; real transcript), push, deploy from the console (canary, per-package errors, disable), the call, and resources and prompts in brief. Linked from the README header, the docs home, the nav and the quickstart. A test keeps every guide the README promises present, in the nav and linked.

## [0.5.2] — the mark is the logo
- The console's sidebar and its sign-in, password-reset and OAuth consent cards now show the square bowl mark the favicon is made of (`/static/logo-mark.png`, 256 px), not the wide "PROJECT RAMEN" wordmark that rendered as a blurry 56 px rectangle. Square sizes and rounded corners in the CSS; the wordmark stays in the README and the docs. A test pins every branded page to the mark; screenshots re-captured (`ui_contract` 0.5.2).

## [0.5.1] — the end-to-end guide, and 0.5.0 proven on GKE
Contract §17. No protocol or API change.

- **`docs/how-tos/end-to-end.md`**: console → worker → AI client, the same eight steps on the local compose stack and on GKE — sign in, zone, group and environment, agent key, deploy, connect a Streamable HTTP client (Claude Desktop, Cursor, curl, the `mcp` SDK), per-user access with OAuth, and what the audit and access logs then show. Every console step has a screenshot.
- **Screenshots**: the rig (`make shots`) gained the one-time key display and the OAuth consent page, and a live mode (`scripts/shots.py --base https://… --email … --password …`) that captures the same set from a real console; the GKE pictures in the guide come from it. Seeded set re-captured; `ui_contract` 0.5.1.
- **GKE run of 0.5.0** on a throwaway Autopilot cluster: the console journey, `cloud_smoke.sh` (TLS verified against the balancer's certificate), Streamable HTTP through the Gateway, the OAuth flow end to end with the token calling a tool, and the harness — `e2e conformance cloud`, **218 passed, 0 failed, 15 skipped** with reasons — including 0.4.1's permissions (IAM bind and unbind against Google's APIs) and backup-restore cases. Found and fixed: the deploy did not hand workers `RAMEN_PUBLIC_URL` (challenge was relative); Terraform did not enable the Resource Manager API, so project-wide role bindings failed as a bare `403` (the console reported it as a missing role; the permissions suite now skips with the console's note instead of failing when `console_project_iam` is off); a harness helper opened fresh gRPC channels without the CA. Learned: the Gateway takes up to seven minutes to program a new zone's route, and a console rollout answers `503` through the balancer for about a minute.
- **Fixed, found by CI after the 0.5.0 push:** Streamable HTTP over node TLS (`RAMEN_TLS_CERT/KEY`) failed at the handshake — tonic's acceptor offers only `h2` on ALPN and every Streamable HTTP client speaks HTTP/1.1. The node now terminates TLS with its own acceptor offering `h2` and `http/1.1`, one handshake task per connection with a 10 s bound. The TLS conformance case had been skipped on the laptop (no `openssl` on PATH); it runs in CI and now passes. Also: the CI compose smoke step sets `RAMEN_SMOKE_INSECURE=1` for the container's self-signed certificate, the one place that flag is used.
- **Fixed after the tag:** `deploy/kind/values.yaml` still named the 0.5.0 images, so every kind deploy — CI's `kind` job included — pulled a tag that no longer existed (`ImagePullBackOff`, canary never ready). Bumped, and the file is now under `check_versions.py`, so a bump cannot miss it again.
- `scripts/oauth_roundtrip.py`: the OAuth flow as a client does it, against a live console and worker — register, authorize, consent, PKCE exchange, call `/mcp` with the token, refresh rotation and reuse detection, a token for another zone refused.

## [0.5.0] — Streamable HTTP at the edge, gRPC inside
Contract §16, decisions D31–D35. The user's decision: gRPC needs a locally installed bridge, which rules out phones,
browsers and hosted agent platforms; Streamable HTTP is a URL and a header and is what the MCP spec defines.

**Transport (breaking for nothing: every 0.3.1–0.4.x client keeps working)**
- Every worker now serves `POST /mcp` (Streamable HTTP, MCP 2025-06-18) on the same port as `ramen.v1.Mcp/Call`, through the same guard functions — key, source range, blocked names, 4 MiB and in-flight caps are one implementation, spelled `401 / 403 / 413 / 429` on HTTP and `UNAUTHENTICATED / PERMISSION_DENIED / OUT_OF_RANGE / RESOURCE_EXHAUSTED` on gRPC. JSON-RPC errors stay `200` with an error body on both. `GET /mcp` is `405`; `DELETE /mcp` ends a session. The access log names the transport.
- Origin validation: a browser `Origin` is refused unless it is on `RAMEN_ALLOWED_ORIGINS` — empty by default — so a foreign page cannot reach a worker through a victim's browser. A group may allow its own web origins from its deploy file.
- Stateless sessions: `initialize` returns an `Mcp-Session-Id` that is an HMAC over a nonce, an expiry and the credential, keyed with the zone's `RAMEN_SESSION_SECRET`; any pod verifies it, no affinity or store, and under another credential it is `404`, never `401`. The console generates one secret per group and hands it to every zone's deploy Secret.
- HTTP/1.1 is now accepted on the node port (Streamable HTTP clients speak it); gRPC stays HTTP/2.

**Per-user access (OAuth 2.1)**
- The console is an authorization server: `/.well-known/oauth-authorization-server`, `/oauth/authorize` with a consent page, `/oauth/token` with PKCE S256 and refresh rotation. Clients are pre-registered by a super admin on the API keys page (name and exact redirect URIs; no secret). Access tokens are HS256 JWTs a worker verifies with a key derived from the zone secret; `aud` and `scope` are `mcp:<group>:<zone>`; one hour of life. Refresh tokens live 30 days, rotate on use, and die with the user's session epoch. Workers publish `/.well-known/oauth-protected-resource` and challenge with `WWW-Authenticate: Bearer resource_metadata=…`. Dynamic client registration is deliberately left out.
- The login redirect keeps the query string, so an authorize request survives sign-in.

**Defaults and clients**
- The quickstart, `make demo`, the client config example, the README and the how-tos land a new user on `http://localhost:8080/mcp` with `Authorization: Bearer`. The stdio bridge is documented once, as the path for clients that only speak stdio. `RAMEN_MCP_KEY` is read by the bridge and used by every example, so a key never has to sit in a config file.
- `deploy/local/mcp_call.py` speaks Streamable HTTP when given a URL and the bridge when given `host:port`.

**Verification**
- `tests/conformance/test_transports_local.py`: the whole guard table on both transports on real node processes, the HTTP-only cases, and the official `mcp` SDK's Streamable HTTP client end to end. CI runs it on Linux and, new, on `windows-latest` with the node built natively there — the client flow on the platform where fresh-machine setups break.
- `scripts/cloud_smoke.sh` gains the HTTP steps; `tests/kind/test_http.py` proves the path through the cluster's ports.
- Found on the way: a stale release binary from 0.4.x hid that the node was h2c-only; the harness now finds `ramen-node.exe` and `Scripts/` venvs on Windows.

**Security review (reports/security-v0.5.0.md in the private workspace) — every finding fixed or documented**
- High: the Python runtime that executes a group's tool code inherited the node's whole environment. The sidecar now strips the node's credentials (`RAMEN_MCP_KEYS`, `RAMEN_ADMIN_KEY`, `RAMEN_SESSION_SECRET`, TLS key, CIDR lists, origins, issuer, proxy trust) before spawning it; a conformance tool that lists its own environment on a real node proves it.
- OAuth: authorization is re-checked at every mint (user exists, may log in, still has the group, epoch unchanged); codes and refresh tokens are burned in the same store transaction they are read in and stored under their SHA-256; a rotated-out refresh token presented again revokes the whole grant; `/oauth/token` is rate-limited like `/login`; the authorize query is redacted from the access log; API keys cannot authorize a client; loopback clients may use any port (RFC 8252); redirect URIs keep their own query; `scopes_supported` no longer lists every tenant.
- HTTP: an unparsable `Origin` is refused, not ignored; address and Origin are checked before the credential; sessions are bound to a full hash of the key; CORS headers and an `OPTIONS /mcp` preflight for a listed origin; a token for another zone is `401`, and the challenge names an absolute `resource_metadata` when the worker knows its public URL (`RAMEN_PUBLIC_URL`).
- The local stack keeps the zone secret out of the group's bucket (the worker gets it from compose, the console the same value as `RAMEN_LOCAL_SESSION_SECRET`); `cloud_smoke.sh` verifies TLS against `RAMEN_SMOKE_CA` and skips verification only on `RAMEN_SMOKE_INSECURE=1`; `mcp_call.py` reads `RAMEN_MCP_KEY`.
- Deferred to 0.5.1 with the reason stated: session-secret rotation (needs a two-key overlap on the node) and a per-zone admin key.

**Docs**
- `docs/threat-model.md`: assets, adversaries, what is defended, what is not, and what is unverified. The README leads with what is verified today, gives the cheaper claim a number (one Deployment per zone for a thirty-tool team, not thirty), claims the git-push story for HTTP clients only, and states security in one sentence: user code never runs in the process that holds the keys.

## [0.4.3] — screenshots that cannot go stale
Contract §15. The 0.4.0 screenshots had survived two UI releases in `docs/img/`: `api-keys.png` still advertised the group list box that 0.4.2 replaced, and nothing in the repo noticed.

- `make shots` (`scripts/shots.py`) captures every console page from a throwaway instance — memory store, local adapter, two in-process fake workers, fictional groups, users, keys, secrets, an image pin, a backup, a deploy and a worker log — with Playwright. It seeds and tears down everything it uses and never touches a cloud or a real key.
- `docs/img/shots.json` records what each screenshot shows and which release it was captured for, plus the release whose UI it must match. `console/tests/test_docs_images.py` fails on a missing image, an unrecorded screenshot, or a capture older than that contract — so a UI change now forces fresh captures before release.
- All twelve shots re-captured for 0.4.3, including five pages the docs had never shown (audit, backups, users, environments, config), and the console how-to illustrates the pages it describes.

**Fixed**
- The `Auto refresh` and `Refresh discovery` buttons 0.4.2 added were floated into the dashboard legend and overflowed the card: the label wrapped onto three lines and `Refresh discovery` ran off the edge. The legend is a flex bar now, and a test rejects `float:right` on that page. The screenshot round is what found it — the tests only saw the markup.

## [0.4.2] — console usability and docs
Contract §14, from the user's own list of ten things that were awkward to use.

**Console**
- API keys: the groups list box is a dropdown with an `Add` button; chosen groups show as removable chips and a key can still name several.
- Dashboard: the grid refreshes every minute instead of every ten seconds, and `Auto refresh: on` stops the polling when you want to read a busy cluster.
- Audit: the newest 100 entries in a scrollable frame, with a search box across every column and a succeeded-or-failed filter that work on what is already rendered. `?limit=` fetches up to 500.
- Backups: `Download`, `Preview restore`, `Restore` and `Restore and prune` are one evenly spaced row of equal buttons.
- Config: super-admin service-account rules are added from an effect dropdown and the permission catalogue (or a pattern), and removed per row, instead of hand-written JSON — which is still shown as what will be sent.
- Environments: the last deploy is flattened into outcome, time and error columns instead of a JSON blob, and the row action is sized like every other.
- Users: `Save` and `Delete user` sit in one actions row, delete on the right, and the confirmation names the account.
- The favicon is the icon on every page, the sign-in and password-reset pages included.

**Versioning**
- `ramen_console.__version__` and `ramen_runtime.__version__` are read from the installed distribution's metadata, falling back to the repo `VERSION`. Nothing hard-codes a version any more, and `scripts/check_versions.py` fails if anything starts to.

**Docs and the site**
- New page: *The console, page by page* — what every page does, what each role sees, and which actions are destructive.
- GitHub Pages deployed from `main` but **failed on every release tag**: the `github-pages` environment allowed only the `main` branch, so the tag build succeeded and its deploy was rejected. Release tags (`v*`) are now allowed to deploy.

## [0.4.1] — the three half-finished promises
Contract §13. The three F-requirements the first four releases only half kept: F7.2 restore, F4.2 revocation, F9.3 per-group worker images.

**Behaviour**
- `POST /api/v1/backups/{id}/restore` takes `{dry_run, prune, reconcile, force}`. `dry_run` returns the plan and writes nothing; `prune` deletes what the backup does not contain (never `config`, never the account running the restore); `reconcile` re-applies the restored zones so counts, sizes and image pins take effect, reporting namespaces the backup never knew about as `orphans` rather than deleting them; `force` overrides the refusal to restore a backup from a newer release. A restore merges over the live documents, so hashes, secret values and tokens survive it, and it bumps every restored user's session epoch — everyone else is signed out at once, so a role a restore lowers cannot be outlived by an open session. A user the store had lost comes back `login_disabled` until a password reset or an SSO sign-in.
- Granted service-account permissions can be taken back: `DELETE /api/v1/groups/{g}/zones/{z}/permissions/{permission}`, plus `POST /api/v1/requests/{id}/deny` for a pending request and `POST /api/v1/requests/{id}/revoke` for an approved one. Revoking a role grant puts the role and group back to what the approval recorded and ends that user's sessions immediately.
- `Cloud.apply_sa_permissions` is now a **set** operation: called with a shorter list it unbinds the cloud roles no remaining permission needs. On GCP that means removing the member from the bucket, secret and project bindings; AWS already rewrote its inline policy. The zone identity's baseline roles (`storage.objectViewer` on the group prefix, the group secret accessor) are never unbound and are reported as `retained` — so revoking `bucket.read` or `secrets.read` takes the grant off the record while the identity keeps that baseline read access.
- Each group can pin its own worker image: `POST /api/v1/groups/{g}/images {tag,digest?,note?}` records a build and pins it, `GET …/images` lists the history, `PUT …/images/current {id}` recalls an earlier one and `DELETE …/images/current` returns to the release image. The pin travels in the zone spec, so both cloud adapters render it into the worker and canary Deployments. A reference must carry a `:tag` or a `sha256:` digest; there is no implicit `latest`. The console records and recalls, it never builds — `make build-worker GROUP=<g>` and `make push-worker GROUP=<g>` do that, so the console needs no registry or build credentials.

**Console**
- Backups page: preview a restore, restore, or restore and prune, each showing what it did per collection with its warnings.
- Group page: a `Worker image` section with the current pin, the history and `Recall`; a revoke button beside every granted permission; approve, deny and revoke on the requests table.

**Verification**
- Kind: a per-group image side-loaded, pinned, deployed and answering tool calls in both zones, then recalled; a role grant revoked mid-session; a restore that prunes and reconciles a populated two-zone deployment while both zones keep serving.
- GKE: the cloud IAM path — an approved permission bound, then unbound by a revoke with the baseline roles kept — and a restore against the Firestore-backed store with a bucket backup.

## [0.4.0] — console, docs, and evidence
**Breaking / behaviour**
- API keys carry a client type and it is enforced: an `agent` key (`rmk_`) is accepted only by workers, a `devops` key (`rmn_`) only by the console API, each refused by the other side. Keys made before 0.4.0 keep working, typed by their prefix.
- `RAMEN_TRUST_PROXY` is replaced by `RAMEN_TRUST_PROXY_HOPS=N`, which reads the Nth `x-forwarded-for` entry **counted from the right**. The old spelling still works and means one hop. Deployed values: GCP 2, AWS 1 — **measured on a live GKE Gateway**, not assumed. Any failure falls back to the peer address, so a wrong count denies rather than admits.
- Server reflection is gated by `RAMEN_REFLECTION` and is **off on deployed workers**: `grpcurl` against a deployed worker now needs `-import-path proto -proto ramen/v1/mcp.proto`.
- A super admin changing the authentication configuration signs out every other session, their own other clients included.

**Security**
- The worker address allowlist could be bypassed: proxy trust shipped enabled and the node read the leftmost `x-forwarded-for` entry, which a caller controls. Found by an independent review of the documentation's own claims.
- Setting IP rules wrote the cloud edge policy before the worker allowlist on both clouds, so a cloud failure left the zone unlocked while reporting an error. The worker is now configured first; the edge policy is best effort.
- A `canary:false` deploy left the previous canary pod serving the zone with the old configuration, so a disabled tool still ran, a revoked key still worked and a tightened allowlist did not apply. Stale canaries are now removed before the stable roll.
- Sessions are revoked on password, role, group and authentication-configuration changes, and on delete.
- Passwords and generated keys are at least 12 characters across four character classes.

**Console**
- Per-zone enable and disable for tools, resources and prompts, on top of the environment-wide list.
- Two-pane logs with the consumer, timestamp, method and outcome per entry, and a worker filter.
- One equal-width Actions group per zone; identity and role in the sidebar with the role named and coloured; health described by load rather than by colour; sentence case throughout; group pickers instead of typed lists.
- `GET` for a single zone and a single environment. `refresh` survives a namespace that is terminating or unreadable.

**Docs and repository**
- Dark-only site, aligned badges, no edit button, no heading permalink symbols, `llms.txt` served but unlinked.
- A hand-drawn architecture diagram that names its own plaintext hops, and a transport page whose every claim was checked against the code by a reviewer that did not write it.
- Operator facts stated plainly: which hops are plaintext, what key rotation actually costs, what the deploy file can and cannot change, and that the AWS path has never been applied.
- Repository description, homepage and topics set.

**Verification**
- `deploy/kind/` brings up a local two-zone cluster: `make kind-up`, `kind-test`, `kind-down`, and a CI job off the pull-request path.
- Proven live: two zones serving independently, autoscaling one to two replicas under real load with the neighbouring zone untouched, rebalance, per-zone tool isolation, a 48-case role and group security matrix, both key types, and Claude Code driven as a real MCP client.
- Proven on GKE: the forwarded-for hop count measured position by position, spoofed headers refused, reflection unreachable through the load balancer, and none of the eight defects from the 0.3.2 run recurring.

## [0.3.2] — verified on GKE
- gRPC header routing verified on a live GKE Gateway over cleartext HTTP/2 (h2c); no TLS fallback needed. Live harness: 126 passed, 0 failed.
- Fixes from the live run: zone identity (GSA + Workload Identity + baseline grants) is ensured when a zone is attached, not only by an explicit service-account call; IAM bindings on new service accounts wait for propagation; all worker services (Mcp, Health, reflection) are routed through the Gateway; the node retries its initial load instead of waiting for an admin reload; the bridge pins the server certificate in insecure-TLS mode.
- API: `GET /api/v1/zones/{zone}` and `GET /api/v1/groups/{group}/environments/{env}`. `make env` warns when `.env` is older than the example. Terraform lock files are committed.
## [0.3.1] — gRPC transport
- **Breaking for HTTP MCP clients** (D19, contract §11): the worker's HTTP surface (`POST /mcp`, `/healthz`, `/readyz`, `/metrics`, `/admin/reload`, `RAMEN_MCP_PATH_PREFIX`) is removed. Workers speak JSON-RPC 2.0 over gRPC: `ramen.v1.Mcp/Call` carries one JSON-RPC message as `bytes body`, `ramen.v1.Admin/{Reload,Metrics}` replace the admin routes, `grpc.health.v1.Health` reports `SERVING` once code is loaded; one h2c port `RAMEN_NODE_PORT` (8080), optional node TLS via `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`. Standard MCP clients (Claude Desktop, Cursor, the mcp SDK) connect through **`ramen-mcp-bridge`** (stdio; `ramen-runtime[grpc]` console script, also in the worker image): `--target <host:port> --key <rmk_> --group <g> --zone <z> [--tls|--insecure] [--ca <pem>]`. Migration: docs *Migrate 0.3.0 → 0.3.1*.
- Security parity on gRPC: metadata `authorization: Bearer <rmk_key>` with constant-time compare (`UNAUTHENTICATED`; empty key set = deny all), `RAMEN_ALLOWED_CIDRS` / `x-forwarded-for` with `RAMEN_TRUST_PROXY=1` (`PERMISSION_DENIED`), `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` on `Admin/*`, blocked names hidden from `*/list` and answered `-32601`, 4 MiB message limit, `RAMEN_MAX_INFLIGHT` → `RESOURCE_EXHAUSTED`, unauthenticated health, access log gains `grpc_code`.
- Edge routing by metadata: clients and the bridge send `ramen-group` / `ramen-zone`; GKE Gateway HTTPRoutes match on those headers (worker Service `appProtocol: kubernetes.io/h2c`, `HealthCheckPolicy` type `GRPC`, no path rewrite); AWS ALB target groups `backend-protocol-version: GRPC` with header listener rules and gRPC health code 0; Cloud Armor / WAF unchanged.
- Console: `ramen_console.grpcclient` (grpcio) replaces every HTTP call to workers (`Admin/Reload` + `tools/list` smoke, `Admin/Metrics` for load, `Health/Check` for readiness); `RAMEN_GCP_POD_PROXY` / `RAMEN_AWS_POD_PROXY` removed. Local stack, `make demo`, `mcp_call.py` and `mcp-client-config.example.json` use gRPC / the bridge; harness `ramen_tests.mcp_client` speaks gRPC and covers the bridge end to end through the mcp stdio client; `scripts/cloud_smoke.sh` uses `grpcurl`.
- Protos: `proto/ramen/v1/{mcp,admin}.proto` are the single source; Rust via tonic-build, Python stubs (`ramen_proto`) vendored in console, runtime-py and tests, regenerated with `make proto`.
- Carried security mediums fixed: GCP console GSA drops `resourcemanager.projectIamAdmin` for `iam.serviceAccountAdmin` + a custom role limited to `setIamPolicy` on `ramen-*` service accounts, with bucket-/secret-level bindings; AWS console role narrows `wafv2:*` to the `ramen` web ACL / IP sets and `iam:PutRolePolicy` to `/ramen/` roles; console ClusterRole loses cluster-wide `secrets`/`serviceaccounts` in favour of a namespaced Role + RoleBinding per attached zone; `sync_repo` passes the GitHub token via `http.extraheader`/`GIT_ASKPASS` and strips credentials from `.git/config`; OIDC uses PKCE (S256) + `nonce`; node key compares are constant-time.
- Logo v2 (same coral/off-white palette) in the console, docs site (`docs/img/logo.png`, `logo-mark.png`, `favicon.png`), README and launch drafts. Docs: architecture v0.3.1, transport sections in how-it-works / protos / concepts, bridge + grpcurl quickstart, GCP/AWS header routing and gRPC health, security parity table, migration note; `mkdocs.yml` `version_current: 0.3.1`.
## [0.3.0] — AWS, auth & policy, docs
- AWS path (**untested on a real account**, D18): Terraform `deploy/terraform/aws` (EKS, DynamoDB, S3, ECR, IRSA roles, AWS Load Balancer Controller + Fluent Bit via Helm, self-signed cert in ACM), CloudFormation `deploy/cloudformation/ramen.yaml`, Helm `provider: aws` (ALB IngressGroup, weighted stable/canary target groups, IRSA), console `aws` cloud adapter (S3 repo sync, CloudWatch Logs Insights, ALB weights, WAFv2 IP rules, IAM role per group+zone, `apply_sa_permissions`) and `aws` secrets backend (Secrets Manager `ramen/<group>/<env|all>/<zone|all>/<NAME>`); runtime syncs `s3://` buckets; node `RAMEN_MCP_PATH_PREFIX` serves `/mcp/<group>/<zone>` behind an ALB.
- Auth: OAuth/OIDC providers (`RAMEN_OAUTH_<NAME>_*`, role mapping by claim), email via SMTP or the `file://` dev backend (invite on user create, password reset, magic-link login), super-admin toggles `GET|PUT /api/v1/config/auth` with break-glass `RAMEN_ADMIN_FORCE_PASSWORD`, CSRF double-submit token for cookie sessions (API keys exempt).
- SA policy engine: permission catalogue `GET /api/v1/policy/permissions`, group-admin requests `POST /api/v1/requests`, super-admin approval → `Cloud.apply_sa_permissions` (gcp IAM roles with prefix conditions, aws inline policy, local recorded); denied-by-rule → 409, audited.
- Tool blocking per environment: `PUT …/environments/{env}/blocked` → `RAMEN_BLOCKED` on deploy; the node hides blocked names from `*/list` and answers `-32601`; block/unblock toggle on the group page.
- Docs: MkDocs Material site on GitHub Pages (architecture per version, how-tos for local/GCP/AWS/security/secrets/DevOps API, wiki, generated version tracker, `llms.txt` + JSON-LD), README with screenshots and a 5-command quickstart, `skills/` cloud-ops agent skills, launch drafts.
- Tooling: `ruff` lint/format config shared via `ruff.toml`, `make lint`, CI lint job; Dockerfiles pin `uv` and carry OCI labels.
- Hardening after the security audit: no constant signing secret, security headers, failure-only login rate limit, token redaction in logs, same-origin login redirect, strict names in log queries, OIDC email verification, CSV formula escaping, worker NetworkPolicy + container securityContext, pinned CI actions. Standards pass: ruff across the repo, `make lint`, CI lint job.

## [0.2.0] — GCP
- Console GCP adapter: zone = namespace, canary deploy flow, workers/metrics, Cloud Logging, rebalance via backend capacity, Cloud Armor IP rules, per-group+zone service accounts with Workload Identity, refresh, group destruction.
- Secrets backend `store|gcp` (Secret Manager). Runtime syncs the group bucket from GCS on load.
- Deploy: Terraform (GKE Autopilot, Firestore, Artifact Registry, static IP, groups bucket, console GSA), Helm `ramen` (console, GKE Gateway, RBAC, backend policy) and `ramen-worker` (zone namespace, canary, HTTPRoute `/mcp/<group>/<zone>`), `make push`, GCP how-to.
- Node: separate `RAMEN_ADMIN_CIDRS`; console: IPv6 CIDRs, zone validation on rebalance, drain-aware deploys, thread-safe Google API transport.
- Tests: cloud suites (canary, rebalance, IP rules, logs, service accounts, secrets), cloud smoke and cost-check scripts, manual `cloud-e2e` workflow. Verified on a throwaway GKE project: 89 passed, 0 failed.
## [0.1.0] — local core
- Python runtime sidecar (`ramen_runtime`): loads group repos, validates protos, executes tools/resources/prompts, resolves `{{$group.VAR}}` secrets.
- Rust MCP node (`ramen-node`): JSON-RPC 2.0 over MCP Streamable HTTP, bearer auth, CIDR allowlist, metrics, admin reload, sidecar supervisor.
- FastAPI console: auth, RBAC (super admin / group admin / viewer), groups, environments, zones, secrets (names only), deploy jobs, rebalance, logs, audit, API keys, backups, config; Firestore, DynamoDB and memory stores with Fernet encryption.
- Local stack: docker-compose (Firestore emulator + console + worker), `make demo`; Helm chart; GCP Terraform skeleton (Phase 2).
- Test harness: conformance (node, sidecar, console API), e2e demo flow, coverage report, version check, CI with e2e job, tag-driven releases.
