# Configuration

Everything the console and the workers read comes from `RAMEN_*` environment variables. A yaml file can hold
them too, and the environment wins. The Config page shows the effective values with secrets masked, the file in
use, a **Hot reload** button, and the settings a super admin edits in the browser.

<figure markdown>
![The config page](../img/config.png){ .ramen-shot }
</figure>

## The config file

`RAMEN_CONFIG=/path/ramen.yaml`. Top-level keys map to `RAMEN_<KEY>`; nested keys join with `_`.

```yaml title="ramen.yaml"
public_url: https://console.example.com      # RAMEN_PUBLIC_URL: links in mail and the OAuth issuer
store: firestore                             # RAMEN_STORE: memory | firestore | dynamodb | azuretables | postgres
secrets_backend: gcp                         # RAMEN_SECRETS_BACKEND: store | gcp | aws | azure
cookie_secure: "1"                           # https only and HSTS; the charts set it
auth:
  password_login: true                       # RAMEN_AUTH_PASSWORD_LOGIN
  magic_link: false                          # RAMEN_AUTH_MAGIC_LINK
  oauth:
    entra:                                   # RAMEN_AUTH_OAUTH_ENTRA_ROLE_CLAIM and _ROLE_MAP
      role_claim: groups
      role_map: "0b7f...=group_admin:demo,analytics;9c1e...=viewer:analytics"
oauth:
  entra:                                     # RAMEN_OAUTH_ENTRA_{ISSUER,CLIENT_ID,CLIENT_SECRET,SCOPES}
    issuer: https://login.microsoftonline.com/<tenant>/v2.0
    client_id: ...
    client_secret: ...
    groups: lookup                           # RAMEN_OAUTH_ENTRA_GROUPS: read groups from Graph at sign-in (0.7.5)
smtp:
  host: smtp.example.com                     # RAMEN_SMTP_{HOST,PORT,USER,PASSWORD,FROM,TLS}
  from: ramen@example.com
autoscale:                                   # 0.7.5, see Autoscaling
  target_rps: 50                             # RAMEN_AUTOSCALE_TARGET_RPS
  p50_ms: 500                                # RAMEN_AUTOSCALE_P50_MS (1.0.0c, D83)
  min_rps: 2                                 # RAMEN_AUTOSCALE_MIN_RPS
  up_cooldown_secs: 30                       # RAMEN_AUTOSCALE_UP_COOLDOWN_SECS
  cooldown_secs: 120                         # RAMEN_AUTOSCALE_COOLDOWN_SECS
```

What the Config page edits is stored in the console store and layered over the file: the public address (base
URI), authentication toggles, identity providers and their group source, custom roles, OAuth role rules, OAuth
clients, SMTP and the warning-email list, the GitHub App, the auto-rebalance scheduler, [telemetry](telemetry.md),
the service-account rules, the permission catalogue, and the regions: blocked regions per cloud and, since 1.0.0b,
the [satellite clusters](groups-zones.md#where-a-zone-can-be) (`GET|PUT /api/v1/config/regions` carries them as
`clusters`, a list of `{provider, region, name, host, project?}`; the home cluster is implicit).

## The public address (base URI)

The **Public address (base URI)** card near the top of the Config page holds the address people and MCP clients
reach the console at: `https://ops.example.com` or, behind a proxy that serves it under a path, `https://ops.example.com/ramen`.
A super admin sets it there or with `PUT /api/v1/config/base-uri {"base_uri": "..."}` (`GET` reads it). Every
change is audited as `config.base_uri`.

When it is set, every link the console generates goes through it: page links, forms and htmx calls, static
assets, redirects (sign-in, `next=`, sign-out, password reset, the https-only redirect), links in mail, the OAuth
metadata and issuer, the upstream sign-in callback, the client snippets on the group page, and the address the
console hands to workers on deploy. Requests still arrive at the console's root; only what it writes changes.

| Precedence | Source |
|---|---|
| 1 | the base URI on the Config page |
| 2 | `RAMEN_PUBLIC_URL` |
| 3 | the address the request arrived on |

The value must be an absolute `http` or `https` URL with an optional path, and no query, fragment, credentials or
spaces; a trailing slash is dropped. Anything else is a `422` with the reason, audited as `error:invalid`. An
empty value unsets it. A save applies at once on the replica that took it and within five seconds on the others.

- **Workers learn it on their next deploy.** It becomes their `RAMEN_PUBLIC_URL` and `RAMEN_OAUTH_ISSUER`, so after
  changing it, deploy every environment, or tokens minted under the new issuer are refused by workers still
  holding the old one.
- **A wrong value breaks every link**, the Config page's included. Recover from the console's root, where requests
  still land, with an API key or a session from before the change:

  ```sh
  curl -s -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' \
    -X PUT https://<console root>/api/v1/config/base-uri -d '{"base_uri":""}'
  ```

- **The Swagger page at `/api/docs` does not follow a prefix.** It fetches `/openapi.json` from the host's root,
  which a prefix proxy sends elsewhere. Open it on the console's root, or read `<base URI>/openapi.json` directly.

### Behind a reverse proxy or a path prefix

A host-only base URI (`https://ops.example.com`) needs nothing more than a proxy that forwards everything. A base
URI with a path, say `https://<host>/ramen`, needs a proxy that does four things:

1. **`/ramen/*` → the console, prefix stripped**: `/ramen/login` arrives as `/login`.
2. **`/ramen/*` with the `ramen-group` and `ramen-zone` headers → that zone's workers, prefix stripped.** The
   worker's resource is `<base URI>/mcp` and its metadata `<base URI>/.well-known/oauth-protected-resource`, so
   `/ramen/mcp` and `/ramen/.well-known/oauth-protected-resource` must reach the worker as `/mcp` and
   `/.well-known/oauth-protected-resource`, headers intact.
3. **`/.well-known/oauth-authorization-server/ramen` → the console's `/.well-known/oauth-authorization-server`.**
   This is RFC 8414 path insertion, at the host's root and outside the prefix; spec-following MCP clients look
   there. The bridge asks `<base URI>/.well-known/oauth-authorization-server` instead, which rule 1 covers.
4. **Leave cookies alone and pass `X-Forwarded-Proto`.** The console sets its cookies with `Path=/`, which covers
   any prefix.

On GKE this was run with HTTPRoutes on the existing Gateway: rules 1 and 3 on the console Service, rule 2 in each
zone namespace, each with a `URLRewrite` filter of type `ReplacePrefixMatch` and one `PathPrefix` per rule. GKE
rejects `ReplaceFullPath` (`GWCER104`), and because the Gateway runs without error isolation, one rejected route
stops every route on it from reconciling, the console-managed worker routes included, until it is fixed. New
routes took about six minutes, and for a while after that a few prefixed requests still reached the old URL map
and got the console's `404`.

The AWS load balancer cannot rewrite paths, so on AWS a prefix needs a proxy of your own in front of it. A proxy
in front of the edge is also one more hop in `x-forwarded-for`: raise `RAMEN_TRUST_PROXY_HOPS` by one, or IP rules
see the proxy's address ([Transport](transport.md)). That combination has not been run.

## The state store

| `RAMEN_STORE` | Backend | When |
|---|---|---|
| `firestore` | Firestore Native | the GCP default |
| `dynamodb` | DynamoDB, one table | the AWS default |
| `azuretables` | Azure Table Storage, one table (`RAMEN_AZURE_STORAGE_ACCOUNT`, `RAMEN_AZURE_TABLE`, default `ramen`) | the Azure default (0.7.5) |
| `postgres` | Postgres, one table, created on first use | self-hosted, or any deployment without a cloud document store |
| `memory` | in process | tests and local development only |

Postgres takes `RAMEN_POSTGRES_DSN`. On Helm: `--set console.env.RAMEN_STORE=postgres --set
console.secrets.RAMEN_POSTGRES_DSN=postgresql://...`. `RAMEN_FERNET_KEY` wraps every backend the same way.

## The variables that matter

| Variable | Read by | Meaning |
|---|---|---|
| `RAMEN_PUBLIC_URL` | console, handed to workers | The address people and clients use, and the OAuth issuer the workers trust. The base URI on the Config page wins over it. Without either, OAuth is off for the workers. |
| `RAMEN_STORE`, `RAMEN_POSTGRES_DSN` | console | The state store. |
| `RAMEN_JOB_STALE_SECS` | console | 1.0.0a: deploy jobs live in the store; a job still `running` this many seconds (default 1800) after its last change is shown as `lost (console restarted)` and no longer blocks a deploy of that environment. The last 50 jobs per environment are kept. |
| `RAMEN_OTEL_UI_URL` | console | 1.0.0a: the address of the chart's trace UI (Jaeger or Phoenix), shown on the Config page's Telemetry section. The chart sets it when `otel.collector` is `jaeger` or `phoenix`. |
| `RAMEN_DEPLOY_TIMEOUT_SECS`, `RAMEN_NODE_WAIT_SECS` | console | How long a deploy waits for a rollout: 300 s, or 1200 s while a pod of it is still `Unschedulable` (a new zone's first node on GKE Autopilot takes 10–18 min; a node pool scaling up for it likewise). A rollout that is merely broken (crash loop, image pull) still fails at the first deadline. |
| `RAMEN_CLOUD` | console | The cloud adapter: `local`, `gcp`, `aws` or `azure`. Also decides which cloud's zones and regions the Zones and Config pages offer. |
| `RAMEN_SECRETS_BACKEND` | console | Where secret values live: `store`, `gcp`, `aws` or `azure` (Key Vault, `RAMEN_AZURE_KEY_VAULT`). |
| `RAMEN_AZURE_SUBSCRIPTION_ID`, `RAMEN_AZURE_RESOURCE_GROUP`, `RAMEN_AZURE_LOCATION` (default `eastus`), `RAMEN_AZURE_STORAGE_ACCOUNT`, `RAMEN_AZURE_CONTAINER` (default `groups`), `RAMEN_AZURE_TABLE` (default `ramen`), `RAMEN_AZURE_KEY_VAULT`, `RAMEN_AZURE_OIDC_ISSUER` | console, Azure | The install the Azure adapter manages, the AKS OIDC issuer the zone identities trust. Set by the chart from `terraform output helm_set`. |
| `RAMEN_AZURE_ZONAL` | console, Azure | `1` (default) pins a zone's workers to its availability zone; `0` when the AKS pool has no zones (`node_zones = []`), so a Ramen zone is a logical placement. |
| `RAMEN_AZURE_TLS` | chart, Azure | The edge certificate: Let's Encrypt (default) or `selfsigned`. |
| `RAMEN_AUTOSCALE_TARGET_RPS`, `RAMEN_AUTOSCALE_UP_COOLDOWN_SECS`, `RAMEN_AUTOSCALE_COOLDOWN_SECS` | console | [Autoscaling](autoscaling.md): calls per second per worker that scale up (default 50), seconds between steps up (30) and down (120). The bounds (1 to a max) are per group × zone on the group page. |
| `RAMEN_AUTOSCALE_P50_MS`, `RAMEN_AUTOSCALE_MIN_RPS` | console | [Latency trigger](autoscaling.md#latency-trigger-100c-d83) (1.0.0c, D83): a zone whose p50 over the last minute reaches `P50_MS` (default 500) scales up when it serves at least `MIN_RPS` (default 2) calls/s; a step down needs the p50 under half the bound. |
| `RAMEN_FERNET_KEY` | console | Encrypts password hashes, secret values, key hashes and tokens in the store. Generate once, keep forever. |
| `RAMEN_OAUTH_ACCESS_TTL`, `RAMEN_OAUTH_REFRESH_TTL` | console | Lifetimes of the MCP access token (seconds, default 3600) and refresh token (default 30 days) the console issues. |
| `RAMEN_ADMIN_EMAIL`, `RAMEN_ADMIN_PASSWORD` | console | The bootstrap super admin, re-applied on every start. `RAMEN_ADMIN_FORCE_PASSWORD=1` is the break-glass. |
| `RAMEN_COOKIE_SECURE` | console | `1` on a public console: secure cookies and HSTS. Plain http gets a 301 for GET and a 403 otherwise when a proxy terminated it; the cloud edges have no port 80 at all. |
| `RAMEN_LOGIN_RATE_LIMIT` | console | Failed sign-ins per address per minute, default 20. |
| `RAMEN_OAUTH_<NAME>_*` | console | An OIDC provider: issuer, client id and secret, scopes. Since 0.7.5 a super admin saves providers on the Config page's *Identity providers* card instead (the page wins over the environment per name); `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED` is ignored: the provider's email is the account, verified flag or not. |
| `RAMEN_OAUTH_<NAME>_GROUPS` | console | `claim` (default) or `lookup`: read the person's groups from Microsoft Graph (`entra`) or Cloud Identity (`google`) at sign-in instead of the token. `RAMEN_OAUTH_<NAME>_GROUPS_URL` overrides the API base including its version (`https://graph.microsoft.com/v1.0`, `https://cloudidentity.googleapis.com/v1`), for tests. The Config page's *Group source* wins. |
| `RAMEN_TOOL_ACCESS`, `RAMEN_ROLES` | worker | Written by the console on deploy: per-tool list/call kinds and the custom roles with their base, so the node can match a token's role against both. The roles themselves are kept in the store and edited on the Config page's *Roles* card. |
| `RAMEN_SMTP_*` | console | Mail: invitations, password resets, magic links and warning digests, as HTML with a plain-text part. The Config page's SMTP card overrides them field by field. |
| `RAMEN_MCP_KEYS`, `RAMEN_ALLOWED_CIDRS`, `RAMEN_ADMIN_KEY`, `RAMEN_BLOCKED` | worker | Written by the console on deploy. Never set by hand in the cloud. |
| `RAMEN_VERBOSE` | worker | `1` logs full request and response bodies. Toggle per environment on the group page. |
| `RAMEN_SIDECAR_IDLE_SECS` | worker | Seconds of idleness before the Python runtime stops, default 300. |
| `RAMEN_CALL_TIMEOUT_SECS`, `RAMEN_MAX_INFLIGHT` | worker | Per-call deadline and the in-flight bound. |
| `RAMEN_TRUST_PROXY_HOPS` | worker, console | Proxies between the client and the node, or the console: 2 on GCP, 1 on AWS and on AKS, set by the charts. The node denies on a wrong value, never admits; the console (0.8.0) uses it for the audit `ip` and the sign-in rate limit, falling back to the peer. |
| `RAMEN_ALLOWED_ORIGINS` | worker | Browser origins allowed on `/mcp`. Empty by default, so every browser origin is refused. |
| `RAMEN_SESSION_SECRET` | console, handed to workers | Signs the MCP session ids and the OAuth tokens a worker verifies. The console generates one per group and writes it into each zone on deploy. |
| `RAMEN_SESSION_TTL_SECS` | worker | How long an MCP session id stays valid, default 1800. |
| `RAMEN_LIST_TTL_MS` | worker | 1.0: the `ttlMs` a 2026-07-28 client is given on list results and `server/discover`, default 60000. |
| `RAMEN_MRTR_TTL_SECS` | worker | 1.0: how long a signed `requestState` of a [multi round-trip request](mrtr.md) stays valid, default 600. Not settable from the group's deploy file. |
| `RAMEN_STOP_DRAIN_SECS` | worker | 1.0: how long a worker keeps serving after SIGTERM before it closes its streams and stops, so a load balancer can take the pod out of rotation first. Default 20 in a pod, 0 elsewhere; keep it under the pod's termination grace period (30 s). |
| `RAMEN_TASKS` | worker | 1.0: `1` or `0` forces [tasks](tasks.md) on or off. Unset: on when the worker has a Redis store (the group's throttle Redis) or runs outside Kubernetes, off in a pod without one — there a task tool answers synchronously. |
| `RAMEN_WAKE_TIMEOUT_SECS` | activator | 1.0: how long a call to a [sleeping zone](sleeping-workers.md) is held while the zone wakes, before `503` with `Retry-After: 10`. Default 25, minimum 1. Read from the pod's environment only, not from the group's deploy file. |
| `RAMEN_ACTIVATOR`, `RAMEN_ACTIVATOR_UPSTREAM`, `RAMEN_CONSOLE_INTERNAL_URL` | activator | 1.0: set by the console on a sleeping zone's activator pod, never by hand. `RAMEN_ACTIVATOR=1` starts the node as an activator; the upstream is where the zone's real workers answer (default `worker-direct:8080`; `host:port`, `http://host:port` or `https://host:port`; a bare `host:port` is dialed over TLS when the node has its pair or `RAMEN_TLS_CA`, and under `RAMEN_REQUIRE_TLS` an `http://` value stops the node at start); the internal URL is where it asks the console for the wake. The console reads `RAMEN_CONSOLE_INTERNAL_URL` too, to hand it on (the chart sets it; default `https://console.ramen-system.svc.cluster.local:443` under `RAMEN_REQUIRE_TLS`, else `http://console.ramen-system.svc.cluster.local:80`). |
| `RAMEN_TASK_TTL_SECS` | worker | 1.0: how long a [task](tasks.md) and its result are kept, default 3600. |
| `RAMEN_MAX_TASKS` | worker | 1.0: tasks running on one pod at a time, default 4; the next task call is `429`. |
| `RAMEN_RUNTIME_CONCURRENCY` | worker | 1.0.0c (D83): tool, prompt and resource calls the Python runtime serves at once, on threads of one process, default 4 (at least 1). CPU-bound work still shares the interpreter; a slow tool or an LLM rail no longer blocks the other calls. A reload waits for the calls in flight (up to the guardrail timeout) and holds new ones back. See [The MCP repo](mcp-repo.md#a-tool) for what this asks of tool code. |
| `RAMEN_MAX_SUBSCRIPTIONS` | worker | 1.0: open [`subscriptions/listen`](transport.md#subscriptions) streams per pod, default 64; the next is `429`. |
| `RAMEN_ADMIN_CIDRS` | worker | Source ranges allowed on the `Admin` gRPC service. Any by default; the admin key is the gate that always applies. |
| `RAMEN_REDIS_URL` | console | 1.0.0c: the chart's Redis, `rediss://:<password>@redis.ramen-system.svc.cluster.local:6379` (TLS since D86), from Secret `ramen-redis` (`redis.enabled`). Handed to the zones of every group that names no Redis on its page as `RAMEN_REDIS_SCOPE_URL`. |
| `RAMEN_EVALS_SA`, `RAMEN_WORKER_EGRESS`, `RAMEN_CLUSTER_SERVICE_CIDR` | console | 1.0.0c: the evals/scan Jobs' ServiceAccount (`ramen-evals`), and the switch + Service range that make the console write a `worker-egress` NetworkPolicy into every zone namespace; set by the chart (`evals.serviceAccount`, `networkPolicy.workerEgress`, `cluster.serviceCidr`). |
| `RAMEN_REDIS_ITEM_URL`, `RAMEN_REDIS_SCOPE_URL` | worker | The two throttle Redis instances, written by the console from the group page. Dialed when the node starts. Since 1.0 the same Redis (scope first, else item) is the [task store](tasks.md#where-tasks-live-and-why-redis-matters), and both URLs are removed from the environment tool code sees. |
| `RAMEN_REDIS_TIMEOUT_MS` | worker | 1.0.0c: the bound on every Redis operation a call waits for (throttle, task store, consent store) and on each response; past it the throttle lets the call through, the stores answer an error, and the connection is re-dialed in the background. Default 500, 50–10000. Read from the pod's environment only (CONTRACTS §39.5a). |
| `RAMEN_OTEL_ENDPOINT`, `RAMEN_OTEL_HEADERS`, `RAMEN_OTEL_CLIENT` | worker | 1.0: [telemetry](telemetry.md), written by the console on deploy from the Config page's Telemetry card: the collector's OTLP/HTTP base URL (unset = nothing is exported), the headers Ramen sends (a JSON object of strings), and the object agents are handed on `server/discover`. The last two are removed from the environment tool code sees. |
| `RAMEN_TELEMETRY_CONSENT`, `RAMEN_TELEMETRY_NOTICE` | worker | 1.0: written by the console on deploy when the group [requires telemetry consent](telemetry.md#consent): `1`, and the collector host the prompt names. Unset by default. |
| `RAMEN_EVALS_IMAGE`, `RAMEN_EVALS_API_URL`, `RAMEN_EVALS_UI_URL`, `RAMEN_EVALS_TIMEOUT_SECS` | console | 1.0.0c: [evals](evals.md) on deploy. The promptfoo image the eval Job runs (unset = no engine, `evals: {skipped}`), the chart's promptfoo server the runs are shared into (`https://promptfoo.<ns>:3000`) and the address a person opens its UI at, which the Config page and the run page name (`evals.uiUrl`, default `https://localhost:3443` after `kubectl -n ramen-system port-forward deploy/promptfoo 3443:3443` — the TLS front; the chart sets the image from `evals.image`, which has a default, and the two URLs with `evals.engine: promptfoo`), and how long the console waits for a run (600 s). |
| `RAMEN_EVALS_VERSION`, `RAMEN_EVALS_REQUIRED` | worker | 1.0.0c: written by the console on deploy and by the config push when the repo's `mcp/evals/promptfooconfig.yaml` or the group's `evals.required` changes; with `required` the worker refuses gated calls that do not carry the version (`-32026`, [evals](evals.md#client-policy-versions-the-gate-the-bridge)). |
| `RAMEN_SKILLSPECTOR_IMAGE`, `RAMEN_SKILLSPECTOR_TIMEOUT_SECS` | console | 1.0.0c Phase 2: [SkillSpector scans](skillspector.md) on deploy. The scanner image the scan Job runs (unset = no engine; `skillspector.engine: skillspector` sets it) and how long the console waits for a run (600 s). |
| `RAMEN_SKILLSPECTOR_VERSION`, `RAMEN_SKILLSPECTOR_REQUIRED` | worker | 1.0.0c Phase 2: written by the console on deploy and by the config push; with `required` the worker refuses gated calls that do not carry the version (`-32027`, [SkillSpector](skillspector.md#client-policy-the-bridges-local-scan)). |
| `RAMEN_GUARDRAILS_VERSION`, `RAMEN_GUARDRAILS_REQUIRED` | worker | 1.0.0c Phase 2.5: written by the console on deploy and by the config push when the repo's `mcp/guardrails.yaml` / `mcp/guardrails/` or the group's `guardrails` settings change; with `required` the worker refuses gated calls that do not carry the version (`-32029`, [guardrails](guardrails.md)). |
| `RAMEN_LLM_PROVIDER`, `RAMEN_LLM_MODEL`, `RAMEN_LLM_API_KEY`, `RAMEN_LLM_BASE_URL` | worker | 1.0.0c: written by the console on deploy only when `mcp/guardrails.yaml` names an LLM-backed rail. Since D85 they name the console's [LLM judge proxy](#the-llm-judge-proxy-100c-d85): `PROVIDER=openai_compatible`, `BASE_URL=<console>/llm/v1`, `API_KEY` = the zone's judge token (`rjt_…`), `MODEL` = the group's or the org's resolved model — never a provider key. No provider anywhere fails the deploy at the sync step. A repo's `mcp/env.yaml` may not set any `RAMEN_*` name. |
| `RAMEN_LLM_PROXY_PER_MIN` | console | 1.0.0c (D85): LLM judge proxy calls per group per minute on each console replica (default 120). |
| `RAMEN_CONFIGS_VERSION`, `RAMEN_CONFIGS_REQUIRED` | worker | 1.0.0c Phase 3: written by the console on deploy and by the config push when the repo's `mcp/llm-config/` changes; `required` is `1` whenever the directory exists, and the worker then refuses gated calls that do not carry the accepted version (`-32028`, [LLM configs](llm-configs.md#the-worker-gate)). |
| `RAMEN_BUILDER`, `RAMEN_BUILD_REGISTRY` | console | 1.0: [images built on deploy](worker-images.md). `1` and the repository prefix images are pushed under; both are needed for a builder to exist, and the chart sets both when `builder.enabled` is true (Terraform's helm line sets it) or, with `builder.enabled` unset, when a legacy builder identity (`builder.gsa` / `roleArn` / `clientId`) is given. Unset by default. |
| `RAMEN_BUILDER_IMAGE`, `RAMEN_BUILD_TIMEOUT_SECS`, `RAMEN_BUILD_CPU`, `RAMEN_BUILD_MEMORY`, `RAMEN_BUILD_MEMORY_LIMIT`, `RAMEN_BUILD_ARGS`, `RAMEN_BUILD_INIT_IMAGE` | console | 1.0: the build Job: its image (default `gcr.io/kaniko-project/executor:v1.24.0`), how long the console waits for it (900 s), its CPU and memory (requests `250m` and `768Mi`, memory limit `2Gi`), extra kaniko arguments (none) and the init container's image (the build's base image). |
| `RAMEN_TLS_CERT`, `RAMEN_TLS_KEY` | worker | PEM paths the node serves TLS with (re-read when they change). The console mounts the zone's leaf (Secret `ramen-tls`) and sets them on every deploy (D86). |
| `RAMEN_TLS_CA` | worker | The install CA bundle the node and the runtime trust for their own calls (console, collector, Redis, the activator's upstream), next to the public roots. |
| `RAMEN_REQUIRE_TLS` | console, worker | `1` on every cloud install (the chart): plaintext is refused — the node will not start without its pair or with an `http://`/`redis://` peer URL; the console will not start with `RAMEN_WORKER_TLS=0`, a `redis://` `RAMEN_REDIS_URL` or an `http://` internal/evals URL, and answers 422 to `http://` telemetry and LLM base URLs (CONTRACTS §39.4). |
| `RAMEN_TLS_CA_DIR` | console | Local compose / kind: a directory with `ca.crt` (and `ca.key` to issue) used instead of the CA the console keeps in its store. |
| `RAMEN_TLS_LEAF_DAYS`, `RAMEN_TLS_RENEW_SECS` | console | Leaf lifetime (30) and how often due leaves are re-issued (3600 s); chart `tls.leafDays` / `tls.renewSecs`. |
| `RAMEN_TLS_SERVICES` | console | The chart services the console issues `ramen-tls-<svc>` for (set by the chart). |
| `RAMEN_WORKER_TLS`, `RAMEN_WORKER_TLS_NAME` | console | How the console dials workers: under `RAMEN_REQUIRE_TLS` TLS with the install CA (default `1`; `0` stops the console at start), verifying the name `worker.ramen.internal` unless overridden. The install CA itself has no page: `POST /api/v1/config/tls/rotate` (super admin, audited `config.tls.rotate`) publishes a new CA in the bundle at once and switches every leaf to it after `RAMEN_TLS_ROTATE_SETTLE_SECS` (180 s); `GET /api/v1/config/tls` shows the stage and each leaf's CA; `?retire=1` drops the old CA once every leaf is new (409 before). See [Transport](transport.md). |
| `RAMEN_REFLECTION` | worker | gRPC server reflection. On by default, set to `0` on deployed workers by the charts. |
| `RAMEN_BACKUP_ROOT` | console | Where a `local` backup is written. |
| `RAMEN_MIN_PASSWORD_LEN` | console | Raises the twelve-character floor; it can never lower it. |
| `RAMEN_LOG_FILE`, `RAMEN_NODE_PORT` | worker | The access log's path and the port the node listens on. |
| `RAMEN_ALB_SPLIT_DRAIN_SECS` | console, AWS | Seconds the console waits after moving the load balancer's traffic off the canary before it changes the canary's pods, default 15. |
| `RAMEN_CONFIG` | both | The yaml file above. |

The complete list is in [contract §3 and §4](../CONTRACTS.md). Precedence runs the config file, then the
environment, then the deploy file the console writes next to the group's code.

Chart values of 1.0.0c: `evals.engine: none | promptfoo` (default `none`) and `evals.image` — see [Evals](evals.md);
`skillspector.engine: none | skillspector` (default `none`) and `skillspector.image` — see
[SkillSpector scans](skillspector.md). Two more things live in the store, not the environment: the Config page's
**LLM providers** card (`config/llm_providers`, `GET`/`PUT /api/v1/config/llm-providers`, keys shown as set / not set;
see below) and, read-only and public, `GET /.well-known/ramen-policy?group=&zone=`
with the telemetry, evals, skillspector and configs versions a client must hold. Phase 3 adds the **OTLP ingest**,
`POST <console>/otlp/v1/{traces,metrics,logs}` (a person's token or an MCP key as bearer; forwarded to the Telemetry
card's `endpoint` with `ramen.*` and `openinference.project.name` stamped, `204` while telemetry is off); the Telemetry
section of the Config page shows its URL ([LLM configs](llm-configs.md#the-otlp-ingest)).

### One providers card and live model lists (1.0.0c)

Since 1.0.0c the Config page shows **one "LLM providers" card**: a table with a row per provider — Anthropic, OpenAI,
Azure OpenAI, Vertex AI, Bedrock, Azure AI Foundry, OpenAI-compatible — its state (`configured`, `key only`, `not set`,
with *Clear* buttons), its own fields inline (key, base URL, host, project and location, region, endpoint and
deployment, headers) and an **Allowed models** picker, under one *Save changes* button (`PUT` merges; an empty key
keeps the stored one). The group page's *LLM providers (group)* card is the same table. *Load models* asks the
provider for its model ids: `GET /api/v1/config/llm-providers/{provider}/models` (super admin) and
`GET /api/v1/groups/{group}/llm-providers/{provider}/models` (group admin) answer `{"models": [...], "source":
"live" | "cached" | "static" | "unavailable", "error"?: "..."}` — OpenAI and a compatible server `GET <base URL>/models`,
Anthropic `GET /v1/models`, Azure OpenAI and Foundry the deployments list (`/openai/deployments?api-version=2024-10-21`,
deployment names), Vertex the Gemini list when the console has `GOOGLE_API_KEY` in its environment, otherwise the
Gemini ids the console knows (`static`), Bedrock `bedrock:ListFoundationModels` with the console's own AWS credentials,
otherwise its static list. Five seconds, cached ten minutes per scope and provider, forgotten when the provider is
saved; a failure is `unavailable` with a short reason (`HTTP 401`, `ConnectTimeout`), never the key. The picker shows
the default model first and the stored ids ticked; a free-text field adds ids the list lacks (a new deployment). The
group's Guardrails card picks the **model** from a select over the allowed models of the scope that serves the chosen
provider (group, then org).

### Allowed models and group-scoped providers (1.0.0c Phase 2.5)

Every provider on the two cards takes **`allowed_models`**, a list of model ids (the form takes them comma-separated;
`PUT /api/v1/config/llm-providers {"openai": {"allowed_models": ["gpt-4o-mini", "gpt-4o"]}}`). Empty means the
provider's default only: `gpt-4o-mini` (OpenAI), `claude-haiku-4-5` (Anthropic), `gemini-2.0-flash` (Vertex),
`anthropic.claude-3-haiku-20240307-v1:0` (Bedrock), the `deployment` field (Foundry); Azure OpenAI and an
OpenAI-compatible server have no default, so a model (deployment) must be listed before the rails can use them.

A group may set **its own providers** on its page, card *LLM providers (group)* — `GET`/`PUT
/api/v1/groups/{g}/llm-providers`, group admin, the same shape and the same merge rules as the org card (`None` keeps,
`""` clears, keys write-only), stored encrypted under `config/groups/<g>/llm_providers` and audited as
`group.llm_providers` with `provider:` tags. Resolution for the group's guardrails, scans and evals is **group first,
then org, else an error** (`no LLM provider: set one on the group or ask a super admin`): the scope that configures the
wanted provider wins, its `allowed_models` bound the model, its whole provider env is what an evals or scan Job gets.
Evals never fail for want of a provider (they run LLM-free); a scan whose `llm` is configured nowhere any more is the
run's error (gated like a run error); LLM rails without a provider fail the deploy before any zone.

The group's **guardrails settings** — `PUT /api/v1/groups/{g}/guardrails {"llm": "<provider>"|"", "model":
"<id>"|"", "required": bool}` (group admin, audited `group.guardrails`) — pick the rails' provider and model (`""` =
the first configured provider and its default; a model outside the scope's `allowed_models` is a `422` naming the
list) and whether clients must carry the guardrails policy (`-32029`). Any change pushes the new version to the deployed
zones. Every sign-in path hands out the policy — the token answer's `guardrails {version, required, config,
client_llm?}` (`config` = `mcp/guardrails.yaml` + `mcp/guardrails/` as a base64 gzip tar, ≤ 64 KiB; `client_llm`
`{provider: "openai_compatible", base_url: "<console>/llm/v1", model}` only when the group requires guardrails or the
engine is `nemo` with LLM rails), `/.well-known/ramen-policy` → `guardrails {version, required}`, and a group key's
deploy carries the rails themselves.

### The LLM judge proxy (1.0.0c, D85)

**Provider keys never leave the console.** Workers and clients reach the group's model through the console:
`POST /llm/v1/chat/completions` (OpenAI-compatible, `stream` off) and `GET /llm/v1/models` (the models the caller may
name). The bearer is a zone's **judge token** (`rjt_…`, minted per deploy from the group's session secret, bound to the
group and zone, written into the zone's deploy config as `RAMEN_LLM_API_KEY`; the zone's current and previous deploy's
tokens are accepted, an older one answers `401`), or what the OTLP ingest accepts from a member: an OAuth access token,
an MCP key of the group, or a console session naming its group in `ramen-group`. Another group is `403`, nothing `401`.
The console resolves the group's provider (group, then org), refuses a `model` outside `allowed_models` (`403`), adds
the key and maps the call: OpenAI and OpenAI-compatible servers are forwarded, Azure OpenAI and Foundry go to the
deployment URL with `api-version`, Anthropic is translated to the Messages API and back, Vertex and Bedrock use their
OpenAI-compatible endpoints with the **console's** cloud identity (its GSA needs `roles/aiplatform.user`, its IAM role
`bedrock:InvokeModel` and `bedrock:CallWithBearerToken`). `max_tokens` is capped at 4096 (and set to it when absent),
`n` at 4, tools and streaming are refused or dropped, the call times out after 60 s, and an upstream failure answers
`502 {"error", "upstream_status"}`. Each group gets `RAMEN_LLM_PROXY_PER_MIN` calls a minute per console replica
(default 120, then `429`); one `llm.proxy` audit line per 100 calls per group (group, model, scope, zone). Bodies and
keys are never logged. Workers call it in-cluster at `RAMEN_CONSOLE_INTERNAL_URL` over TLS, verified against the install CA
(the worker egress policy already admits the console pods; since D86 there is no plain-http exemption); without
that variable, the base URI, else `RAMEN_PUBLIC_URL`. A repo's `mcp/env.yaml` (or `.env`) that sets any `RAMEN_*` name
fails the deploy at the sync step naming the key. Evals and scan Jobs still get the provider env (they run as
`ramen-evals`, not tenant code), but an inline `javascript` / `python` assertion that reads the environment
(`process.env`, `os.environ`, `getenv`) is refused at sync.

## Network policies and Redis (1.0.0c)

The chart writes NetworkPolicies into `ramen-system` (`networkPolicy.system: true`): the console pod accepts its port
from the edge (`networkPolicy.edgeCidrs`, default the Google front ends on GKE, the VPC on EKS; `edgeNamespaces`,
default `envoy-gateway-system` on AKS; plus `console.sourceCidrs`), the worker namespaces (their `/otlp` posts, wake
and Admin callbacks) and the evals/scan Job pods; Phoenix or Jaeger take OTLP on 4318 from the console, the workers
and the Jobs and show their UI to the edge only; promptfoo answers the console and the Jobs; Redis answers the console
and the workers. With `networkPolicy.workerEgress: true` and `cluster.serviceCidr` set (the cluster's Service range —
without it the policy is skipped and `helm` says so), the console adds `worker-egress` to every zone namespace: a
worker pod, where the group's own tool code runs, reaches DNS, the console, Redis, the collector's 4318, the zone's own
pods and the internet, never another ClusterIP of the cluster.

`redis.enabled: true` runs a Redis in `ramen-system` with `requirepass`: the password is generated into Secret
`ramen-redis` (kept across upgrades) or taken from `redis.auth.existingSecret` (keys `password` and `url`). The console
reads the URL from that Secret as `RAMEN_REDIS_URL` and hands it to every zone whose group names no Redis of its own
on the group page, so tasks, consent and the scope throttle have a store out of the box; a URL set on the group page
still wins. The password is scrubbed from logs and job records. The local stack has the same with
`make up ENGINES=redis` (`RAMEN_REDIS_PASSWORD` in the generated `.env`). A managed Redis instead: `redis.externalUrl`
(`rediss://:<key>@<host>:<port>/0`, TLS) goes into the same Secret and nothing runs in the cluster; the Azure Terraform
creates one and puts the value in `helm_set`.

## Restricting actions

- **Service-account rules** on the Config page gate what any group may request for its zone identities. Deny
  wins; with allow rules present a permission must match one. Group admins add stricter restrictions per group,
  and a clash is refused.
- **Allowed worker sizes** per zone are set by super admins in the group page's *Zone actions* card.
- **Blocked packages** per environment and zone are set on the group page.
- **Password login** can be switched off once another way in exists.
- **API keys, backups, audit and config** are super-admin pages. Group deletion is super-admin only.

## Upgrading

Push the new images, then roll the console: `make push`, then `kubectl -n ramen-system rollout restart
deploy/console`. The console reads its version from the installed package, so the sidebar tells you what is
actually running.

Workers are not rolled by that. Each group's zones pick up the new worker image on their **next deploy**, so a
release reaches a group when someone deploys it, and a group pinned to a recorded image stays there until the pin
is changed. Nothing breaks in the meantime: the transport has been stable since 0.3.1. A 1.0 worker keeps answering 2025-06-18 clients as
0.8 did, so clients do not have to change when a group's workers move to 1.0.

Open sessions and OAuth tokens survive an upgrade, because both are signed with the per-group session secret
rather than anything version-specific. A restore into a *fresh* console is the case that invalidates them, and
the fix there is to deploy each environment again.

## Email

SMTP on the Config page or `RAMEN_SMTP_*`. It carries invitations, password resets, magic-link sign-ins and a digest
of warnings and errors to the notify list, batched every thirty seconds. Every mail is branded HTML (the logo
inline, a button plus the address to copy, a signature) with the plain text kept. The digest is one row per alert
with a level badge, and its subject counts them ("2 warnings, 1 error on the Ramen console"). A zone pinned at its
autoscaling max under load is one of those warnings.

## The local stack

`deploy/local/.env` holds the compose stack's values: the admin account, the Fernet key, the local MCP and admin
keys and the image version. `make env` (run by `make up`) writes it from the example with generated secrets and rotates a stale one; nothing in it is committed. Every compose hop is TLS
(D86): `make up` generates a local CA and one leaf per service into `deploy/local/.certs` (git-ignored, kept across
runs, re-issued when due); trust `deploy/local/.certs/ca/ca.crt` on the laptop. The console serves 8443, the worker
8080 — open, and for a machine you control only.

## Upgrading the chart

A newer chart version adds values (1.0.0b: `evals.*`, `console.k8sSubjects`, `console.sourceCidrs`; 1.0.0c:
`networkPolicy.*`, `cluster.serviceCidr`, `redis.*`, `evals.serviceAccount` / `evals.gsa` / `evals.roleArn` /
`evals.clientId`). `helm upgrade
--reuse-values` keeps the previous release's values and ignores the new defaults, so a template that needs one
renders empty (seen live: the promptfoo PersistentVolumeClaim asked for `0` storage and the upgrade was refused
before anything moved). Upgrade with `--reset-then-reuse-values` (chart defaults, then your previous overrides) or
pass a values file.

**Upgrading to D86 (TLS on every hop) from 1.0.0b or an earlier 1.0.0c.** The chart adds `tls.*` and moves the
console's Service to port 443; every zone needs a redeploy to get its certificate. Exactly:

1. `helm upgrade ramen deploy/helm/ramen -n ramen-system --reset-then-reuse-values -f <your values>`. The console
   restarts on TLS, creates the install CA, writes ConfigMap `ramen-ca` and Secrets `ramen-tls-<service>`; Redis
   and the collectors restart on TLS (their pods wait for those Secrets, so give the console a minute).
2. Until step 3, existing zones keep answering clients through the edge, but the console cannot reach them
   (`Admin/*` fails), their wake, judge and telemetry calls fail, and their throttling reports Redis unreachable.
3. Redeploy every zone of every environment: the group page's **Deploy**, or
   `POST /api/v1/groups/<group>/environments/<env>/deploy`. Each deploy writes `ramen-tls` / `ramen-ca` into the
   zone, switches its probes and edge backends to TLS and rolls both tracks.
4. Check: `kubectl -n ramen-<group>-<zone> get secret ramen-tls`, the deploy log's `tls: issued ramen-<group>-<zone>
   leaf (CA <fingerprint>)`, a `tools/call` through the edge, and no `tls handshake failed` on the Logs page (a load
   balancer's TCP check that closes before any ClientHello, or gives up partway through one, is logged at `debug`, not as a failure). Point **Config → Telemetry** at `https://…:4318` if it named `http://`.
