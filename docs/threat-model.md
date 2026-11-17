# Threat model, security and isolation

What Ramen defends against, what it does not, where the trust boundaries are, and the status of every finding from
the 1.0.0c security passes. Terms: a **group** is a tenant (a team, its repo, its members); a **zone** is a Kubernetes
namespace `ramen-<group>-<zone>` with that group's workers; a **worker** is one pod running the Rust node and, in a
separate process, the Python runtime with the group's code; the **console** is the management plane. Identities and
network rules per cloud: [GCP](wiki/iam-network-gcp.md), [AWS](wiki/iam-network-aws.md), [Azure](wiki/iam-network-azure.md).

## Trust boundaries

| # | Boundary | What crosses it | What guards it |
|---|---|---|---|
| B1 | Internet → cloud edge | MCP calls (`/mcp`, gRPC), console pages, OAuth | TLS on 443 only; Cloud Armor / WAF / Envoy `SecurityPolicy` IP rules per group or zone; header routes to one zone |
| B2 | Edge → node | the same calls | TLS to the pod (install CA, §39); bearer credential (MCP key, OAuth token), source range at the load balancer's forwarded hop, Origin, size and in-flight caps, throttle, the policy gates |
| B3 | Node → runtime | one JSON-RPC message on stdin | process boundary: the node holds keys and does auth; the runtime holds the group's code and its secrets |
| B4 | Runtime → cloud | bucket, secrets, approved services | the zone's own cloud identity, limited to the group's prefix and secrets |
| B5 | Console → workers | deploy config, reload, metrics, golden cases | gRPC over TLS to the pod IP, verified against the install CA; the per-deploy admin key; NetworkPolicy `worker` |
| B6 | Workers → console | OTLP ingest, wake, the LLM judge proxy | TLS to the console (install CA, plain HTTP refused); per-zone judge token, NetworkPolicy `worker-egress` |
| B7 | Console → cloud | IAM, registry repos, secrets, load balancer | the console's identity, narrowed per cloud (and its residuals below) |
| B8 | Ramen's Jobs | build (kaniko), evals (promptfoo), scan (SkillSpector) in `ramen-system` | each Job's own identity: `ramen-build-<group>` may push one repository; `ramen-evals` holds logging only; no API-server token |
| B9 | Person's machine | the token answer's policy bundle, client config files, client telemetry | acceptance per version, the worker gates, the bridge's `policy check` |

## What a group can and cannot reach

| A group's code or admin… | can | cannot |
|---|---|---|
| Bucket | read its own prefix `<group>/` | read another group's objects (GCP: object **names** are listable, contents are not; AWS and Azure: list only its prefix) |
| Secrets | read its own secrets as env vars in its own runtime | read another group's secrets, the console's own secrets (`ramenc-…`, `ramen-console/…`), or any provider key |
| Images | build its own image into its own repository (`ramen-groups-<group>` / `ramen/groups/<group>/worker`) | write the platform images or another group's; use pip option, URL or VCS lines unless its admin sets `build.allow_urls: true` (audited) |
| Workers | run in its own namespaces under its own identity | reach another group's pods directly (NetworkPolicy `worker`, where the cluster enforces policies) or any ClusterIP in the cluster (`worker-egress`) |
| `ramen-system` | reach the console port, Redis 6379 and the collector 4318 | reach the Phoenix UI, promptfoo, or the console's other ports |
| LLM providers | call the console's judge proxy with its zone's token, for the models the group's or the org's provider allows, rate-limited per group | read a provider key; set `RAMEN_*` names in `mcp/env.yaml` (refused at sync) |
| Its repo's config | ship evals, skills, guardrails, client configs | run `exec:` / `python:` / `file://` promptfoo providers, inline assertions that read the environment, or client-config hooks other than `ramen-mcp-bridge …` (all refused at sync) |
| Console | edit its own settings as a group admin | push config to other groups' zones (F4), read another group's job records (S1), see its secret values |

Isolation is between groups, not within one: a tool author reads their own group's secrets, and anyone holding a
group's `rmk_` key is that group until the key is revoked.

## Secrets at rest

| Secret | Where it lives | Notes |
|---|---|---|
| Passwords | the store, argon2 | |
| `rmk_` MCP keys, `rmn_` API keys | the store, `s1$<salt>$<HMAC-SHA256>` | shown once |
| OAuth codes, refresh tokens | the store, HMAC with a per-install salt (Fernet-encrypted) | |
| Group secrets | Secret Manager / Secrets Manager / Key Vault, or Fernet in the store locally | never shown back; reach code as env vars |
| Console secrets (SMTP, provider client secrets, OTel headers, repo tokens, Redis URLs, **LLM provider keys**) | the cloud secret manager under `ramenc-…` / `ramen-console/…`, outside every worker's reach | Config pages show set / not set; scrubbed from logs, job records and backups |
| Deploy Secret per zone | Kubernetes Secret `ramen-deploy` | MCP key hashes, admin key, the per-deploy evals key and judge token, policy versions; no provider key since D85 |
| Evals key `rek_…`, judge token `rjt_…` | not stored: HMAC of the group session secret and the deploy job | rotated each deploy |

## Gates on every call

Before the runtime sees a call the node checks the source range, Origin, the credential, the throttle and in-flight
limit, tool access and blocked names, and the client-policy gates below (in the order of the table). A gate applies to `tools/call`,
`prompts/get` and `resources/read` (lists and `server/discover` are not gated) from every client — the bridge, a
direct HTTP client, a group key, a person's token — on both protocol eras and both transports. Only Ramen's own calls
(the console's golden cases with the admin key, the evals Job with its per-deploy key) skip them.

| Code | When the group requires | Missing | Stale |
|---|---|---|---|
| consent | telemetry consent | modern + elicitation: an `input_required` prompt; modern without: `-32021`; 2025-06-18: `-32003` | — |
| `-32024` / `-32025` | telemetry reports | `telemetry report required` | `telemetry config changed: sign in again` |
| `-32026` | evals | `evals config required` | `evals config changed: sign in again` |
| `-32027` | SkillSpector | `skillspector policy required` | `skillspector policy changed: sign in again` |
| `-32028` | LLM configs (whenever the repo has `mcp/llm-config/`) | `configs required` | `configs changed: sign in again` |
| `-32029` | guardrails | `guardrails policy required` | `guardrails policy changed: sign in again` |

On the 2026-07-28 HTTP wire these answer HTTP 428 with `data.version`; on 2025-06-18, HTTP 200 with the same error
(except a missing telemetry report on a 2025-06-18 body, which is `-32003`).
Versions come from the token answer of every sign-in path; acceptance of LLM configs is per person and version, and
the console refuses a token or a key until it is given ([LLM configs](wiki/llm-configs.md)).

## What Ramen defends against

- **A tenant repo cannot author the worker's deploy file**: `.ramen/` in a checkout is never uploaded or downloaded;
  keys, tool access and roles come only from the console.
- **A group's `repo_url` cannot run code in the console**: network git URLs only, no remote helpers.

| Threat | Adversary | Defence | Where |
|---|---|---|---|
| Calling a worker without a credential | anyone | every `Mcp/Call` and `POST /mcp` needs a bearer; an empty key set denies everyone | `node-rs/src/auth.rs`, `grpc.rs::guard` |
| Guessing or timing a key | anyone | constant-time comparison over every key; 12-character minimum with four character classes | `auth.rs::check_key`, `console/security.py` |
| Reaching a worker from an address the group did not allow | anyone, a credential holder | `RAMEN_ALLOWED_CIDRS` at the node, against the hop the load balancer appended (fails closed on a wrong count), plus Cloud Armor / WAF / Envoy at the edge | `auth.rs::client_ip`, `cloud/*` |
| A stolen session id or token used elsewhere | a credential holder | sessions are HMACs over the credential; tokens are bound to audience, scope (`mcp:<group>:<zone>`) and issuer, live one hour, refresh rotation is bound to the user's session epoch; `requestState` and tasks are bound to the caller | `session.rs`, `token.rs`, `oauth_server.py` |
| A web page driving a victim's browser | a malicious page | `Origin` refused unless allow-listed; CSRF tokens on console forms and cookie-authenticated API mutations; console CSP, `X-Frame-Options: DENY`, HSTS | `http.rs`, `console/auth/csrf.py`, `security.py` |
| A tool author reading another group's data | a tenant | per zone: namespace, pods, cloud identity limited to the group's prefix and secrets (GCP IAM condition, AWS tag-keyed boundary, Azure ABAC condition) | `cloud/*` |
| A group's build overwriting platform or other groups' images | a tenant | per-group repository and build identity; platform repositories unwritable by builds; deploys pin the built image by digest; pip option/URL/VCS lines refused unless allowed | `imagebuild.py`, `deploy/terraform/*` |
| A group's evals config running code with platform rights | a tenant | promptfoo provider allowlist at sync; the Job runs as `ramen-evals` (logging only, no API token); inline assertions may not read the environment | `evals.py`, chart |
| An LLM provider key leaking to tenants or laptops | a tenant, a member | keys stay in the console; workers and the bridge call the judge proxy with a zone token or the person's token; model allowlist, per-group rate limit and token cap | `llm_proxy.py` (§38.1) |
| A group restarting other tenants' workers | a group admin | group settings push only to that group's zones | `deploy.push_config` (F4) |
| Reading another group's deploy log or scan report | a member | job records and the report page check the job's group | `web/api.py`, `web/pages.py` (S1) |
| A hostile config fragment, finding text or app | a tenant | autoescaped templates; app preview in `sandbox="allow-scripts" credentialless` under the app's validated CSP | `templates/`, `apps.py` |
| Spoofed or oversized client telemetry | a credential holder | `/otlp` stamps `ramen.group` / `ramen.user` from the credential (spoofed attributes are overwritten), 4 MiB cap incl. after gzip, 60 requests/min per token | `web/otlp_routes.py` |
| The console fetching the cloud metadata service through a provider URL | a group admin | link-local and metadata hosts refused at save and after DNS resolution | `llm_providers.py`, `llm_models.py` (F7) |
| Tool code crashing the process that holds the keys | a tenant | separate runtime process with call timeouts and idle restart | `sidecar.rs` |
| A revoked user keeping access | a credential holder | sessions and refresh tokens carry a per-user epoch bumped on password, role, group, delete and restore | `accounts.py` |
| Flooding a zone | anyone | in-flight cap, 4 MiB messages, Redis throttles by address and token, autoscaling to the zone's max | `grpc.rs`, `autoscale.py` |
| Prompt injection through a tool | anyone | opt-in rails in the worker before the call and after the result, fail-closed; LLM judges read arguments only inside escaped `<<<ARGS … ARGS>>>` / `<<<DATA … DATA>>>` blocks and a static `judge_format` rail refuses judge-control lines — this reduces injection, it cannot eliminate it | `guardrails.py`, `guardrails_llm.py` |

## What Ramen does not defend against

- **An LLM that never calls Ramen's workers and never reads the files the bridge writes cannot be forced** to report
  telemetry, run evals, scan its skills or keep the group's configs. Enforcement is the worker gate on every Ramen call
  plus the client files; anything outside both is out of reach.
- **LLM rails are not an injection boundary.** The final pass showed verdict-injection text passing input rails on a
  small model before the D85 hardening; the hardening narrows it, it does not close it. Use deterministic rails for
  anything that must hold.
- **A holder of a valid `rmk_` key is that group** until the key is revoked and the next deploy reaches the workers.
- **An OAuth access token cannot be revoked before it expires** (one hour): workers check tokens without calling the
  console.
- **Tool code holds its zone's judge token** (`RAMEN_LLM_API_KEY` in its environment) and can call the judge proxy
  within the group's allowed models and rate limit. It never holds a provider key.
- **A compromised console is a compromised platform.** It holds the Fernet key, mints tokens and binds cloud IAM; see
  the residuals below.
- **The optional engines have no authentication.** Phoenix, Jaeger and promptfoo run in `ramen-system` without auth
  and without an edge route, for trying things out; every group's evals runs are shared in one promptfoo server.
- **TLS that proves the install, not the zone** (D86, CONTRACTS §39): every in-cluster hop is TLS with Ramen's own CA
  and plaintext is refused on a cloud install, but Google's front ends, the AWS ALB and kubelet probes encrypt to the
  pod without verifying its certificate, and every worker leaf carries the same name (`worker.ramen.internal`), so a
  verified peer is "a worker of this install". Tool code runs as the node's user and can read its zone's TLS key from
  the mounted Secret, as it can the node's environment. The CA key lives only in the console (its secret store); a
  compromised console can issue any leaf.
- **One console replica.** Single-use codes, refresh rotation and membership writes are atomic within one process;
  run the chart's default of one replica. The judge proxy's and the OTLP ingest's rate limits are per replica too.
- **The cloud provider and Kubernetes itself.**

## Findings and their status

Two independent passes probed the live GKE stack read-only: §33.4 (S1–S14, console 1.0.0b21) and the final pass
(F1–F9, console 1.0.0b27). Statuses as built at the end of 1.0.0c (CONTRACTS §33.4a, §38):

| id | Finding | Status |
|---|---|---|
| F1 | the org's LLM key reached every member's token answer, the worker env and the evals Job | **fixed** (D85): the console's judge proxy; no provider key in a deploy config or a token answer. Evals and scan Jobs still get the resolved provider env for their run (they run as `ramen-evals`, and inline assertions may not read the env) |
| F2 | the shared builder could overwrite the console and every group's image | **fixed** (D85): per-group repositories and build identities, platform images by digest. Residual: on GCP the console's repository rights are project-wide (Artifact Registry ignores `resource.name` conditions) and on Azure its RBAC administrator role could grant itself registry write — a console compromise, not group code. On AWS the console's ECR rights stop at `ramen/groups/*` |
| F3 | throttling silently off (egress selector vs a hand-applied Redis) | **fixed** (D85): the chart's password-protected Redis, an egress rule for both labels, `throttling off: Redis unreachable` on the group page and in the deploy log |
| F4 | a group's settings push rolled every group's workers | **fixed**: the push reaches only that group's zones. Residual: a group's own push during its own running deploy can write the previous evals key |
| F5 | verdict injection passed LLM input rails | **mitigated** (D85): escaped delimited blocks, a static `judge_format` rail; documented as best-effort |
| F6 | a final judge line with two verdicts passed on the first | **fixed**: disagreeing verdicts block |
| F7 | SSRF from the provider model listing | **fixed** for the metadata service. Residual: other in-cluster hosts remain reachable as a host/port oracle (only model ids are ever reflected) |
| F8 | the Kubernetes client resends a create after a dropped connection | holds: client-chosen names make a replay `409` |
| F9 | runtime concurrency (D83) | holds: no cross-request leakage; a slow synchronous `policy.py` hook stalls every concurrent call's rails — they block with `fail: closed` and **pass** with `fail: open` |
| S1 | job records readable by any signed-in viewer | **fixed** |
| S2 | chunked OTLP body buffered before the size check | **fixed** |
| S3 | evals Job ran group configs as the builder | **partial**: own identity, allowlist and scrubbing done; the allowlist checks top-level `providers` only (`defaultTest.provider`, per-test `provider`, `prompts: file://…` are not refused) |
| S4 | worker identities could read every group's bucket objects | **fixed** (names stay listable on GCP) |
| S5 | GKE nodes as the default compute SA (`roles/editor`); console `serviceAccountUser` | **partial**: new clusters run as `ramen-nodes@`, `serviceAccountUser` removed; a pre-1.0.0c cluster keeps the default account until re-created, and `ramenConsoleSaIam` carries project-wide `setIamPolicy` on service accounts |
| S6 | no NetworkPolicies in `ramen-system` | **partial**: policies written; `console.sourceCidrs` (a pod range) is folded into the `ramen-system` edge rules, which admits every pod on a CNI that matches pod IPs by `ipBlock`; on EKS the edge CIDRs are the ALB's public subnets since D89, no longer the pod range |
| S6b | NetworkPolicies need an enforcing engine | **fixed on EKS (D89; live re-check of the pod-subnet layout pending), fixed in Terraform on AKS (live check pending)**: GKE Autopilot enforces them; EKS (home, satellite, CloudFormation) installs the managed `vpc-cni` addon with `enableNetworkPolicy` — enforced live on 1.0.0b35 (an unrelated pod is dropped at Redis), but the worker policy admitted the whole VPC for the ALB while nodes, ALB and pods shared the public subnets, so a pod in `default` reached a worker pod (200). D89: pods take their own subnets through VPC CNI custom networking (`10.42.64.0/20`, `10.42.80.0/20`; no NAT), and `network_set` / `RAMEN_AWS_ALB_CIDRS` admit only the public subnets. Residual: whatever speaks with a node's address (`hostNetwork` pods; on a satellite, the home cluster's pods) is still admitted on 8080, past the WAF but not past the worker's own gates (keys, sign-in, the node's IP rules). AKS (home, satellite) sets `network_policy = "calico"`; an existing AKS cluster may be replaced by the change |
| S7 | test runs leave users, keys and clients behind | **open** (operator hygiene) |
| S8 | app preview broke on a stale stored key | **fixed** (reads with the deploy's evals key + admin key) |
| S9 | `/.well-known/ramen-policy` answers 200/404 by group and zone | **open, accepted** (public by design) |
| S10 | the rendered evals config sits in a ConfigMap during the run | **open** |
| S11 | public GKE control plane, no CMEK, Firestore PITR off | **open** (Terraform knobs for a production profile) |
| S12 | a user with no membership gets the MCP-user refusal text | **open** (wording) |
| S13 | old local bridge without the configs version | **fixed** (operator's bridge updated) |
| S14 | console CSP allows `'unsafe-inline'` scripts | **open** |

## What is verified, and what is not

The 1.0.0c final runs passed on GKE, AKS and EKS (smoke, the policy, guardrail, access and security suites), and the
two passes above probed the live GKE stack. Not probed live: the bridge's local rails, a hostile repo against the
cloud (the passes do not own the demo repo; unit tests cover hostile fragments, findings, apps and size bombs),
`-32029` through the edge during the final pass, and the satellite clusters' policies. There has been no third-party
penetration test. Per-transport detail: [Transport → What is verified](wiki/transport.md#what-is-verified-and-what-is-not).

## Reporting

Report a vulnerability to the maintainer through a private GitHub security advisory on the repository: what you
found, how to reproduce it, which release. Expect an acknowledgement, a fix in a patch release, and credit in the
changelog if you want it.
