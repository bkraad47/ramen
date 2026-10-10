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
store: firestore                             # RAMEN_STORE: memory | firestore | dynamodb | postgres
secrets_backend: gcp                         # RAMEN_SECRETS_BACKEND: store | gcp | aws
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
```

What the Config page edits is stored in the console store and layered over the file: the public address (base
URI), authentication toggles, OAuth role rules, OAuth clients, SMTP and the warning-email list, the GitHub App, the
auto-rebalance scheduler, the service-account rules and the permission catalogue.

## The public address (base URI)

The **Public address (base URI)** card near the top of the Config page holds the address people and MCP clients reach the
console at: `https://ops.example.com` or, behind a proxy that serves it under a path, `https://ops.example.com/ramen`.
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
| `postgres` | Postgres, one table, created on first use | self-hosted, or any deployment without a cloud document store |
| `memory` | in process | tests and local development only |

Postgres takes `RAMEN_POSTGRES_DSN`. On Helm: `--set console.env.RAMEN_STORE=postgres --set
console.secrets.RAMEN_POSTGRES_DSN=postgresql://...`. `RAMEN_FERNET_KEY` wraps every backend the same way.

## The variables that matter

| Variable | Read by | Meaning |
|---|---|---|
| `RAMEN_PUBLIC_URL` | console, handed to workers | The address people and clients use, and the OAuth issuer the workers trust. The base URI on the Config page wins over it. Without either, OAuth is off for the workers. |
| `RAMEN_STORE`, `RAMEN_POSTGRES_DSN` | console | The state store. |
| `RAMEN_SECRETS_BACKEND` | console | Where secret values live: `store`, `gcp` or `aws`. |
| `RAMEN_FERNET_KEY` | console | Encrypts password hashes, secret values, key hashes and tokens in the store. Generate once, keep forever. |
| `RAMEN_OAUTH_ACCESS_TTL`, `RAMEN_OAUTH_REFRESH_TTL` | console | Lifetimes of the MCP access token (seconds, default 3600) and refresh token (default 30 days) the console issues (0.7.0). |
| `RAMEN_ADMIN_EMAIL`, `RAMEN_ADMIN_PASSWORD` | console | The bootstrap super admin, re-applied on every start. `RAMEN_ADMIN_FORCE_PASSWORD=1` is the break-glass. |
| `RAMEN_COOKIE_SECURE` | console | `1` on a public console: secure cookies and HSTS. Plain http gets a 301 for GET and a 403 otherwise when a proxy terminated it; the cloud edges have no port 80 at all. |
| `RAMEN_LOGIN_RATE_LIMIT` | console | Failed sign-ins per address per minute, default 20. |
| `RAMEN_OAUTH_<NAME>_*` | console | An OIDC provider: issuer, client id and secret, scopes. Since 0.7.5 a super admin saves providers on the Config page's *Identity providers* card instead (the page wins over the environment per name); `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED` is ignored: the provider's email is the account, verified flag or not. |
| `RAMEN_OAUTH_<NAME>_GROUPS` | console | `claim` (default) or `lookup`: read the person's groups from Microsoft Graph (`entra`) or Cloud Identity (`google`) at sign-in instead of the token (0.7.5). `RAMEN_OAUTH_<NAME>_GROUPS_URL` overrides the API base including its version (`https://graph.microsoft.com/v1.0`, `https://cloudidentity.googleapis.com/v1`), for tests. The Config page's *Group source* wins. |
| `RAMEN_TOOL_ACCESS`, `RAMEN_ROLES` | worker | Written by the console on deploy: per-tool list/call kinds (0.7.2) and the custom roles with their base (0.7.5), so the node can match a token's role against both. |
| `RAMEN_SMTP_*` | console | Mail for magic links and warning digests. |
| `RAMEN_MCP_KEYS`, `RAMEN_ALLOWED_CIDRS`, `RAMEN_ADMIN_KEY`, `RAMEN_BLOCKED` | worker | Written by the console on deploy. Never set by hand in the cloud. |
| `RAMEN_VERBOSE` | worker | `1` logs full request and response bodies. Toggle per environment on the group page. |
| `RAMEN_SIDECAR_IDLE_SECS` | worker | Seconds of idleness before the Python runtime stops, default 300. |
| `RAMEN_CALL_TIMEOUT_SECS`, `RAMEN_MAX_INFLIGHT` | worker | Per-call deadline and the in-flight bound. |
| `RAMEN_TRUST_PROXY_HOPS` | worker | Proxies between the client and the node: 2 on GCP, 1 on AWS, set by the charts. A wrong value denies, never admits. |
| `RAMEN_ALLOWED_ORIGINS` | worker | Browser origins allowed on `/mcp`. Empty by default, so every browser origin is refused. |
| `RAMEN_SESSION_SECRET` | console, handed to workers | Signs the MCP session ids and the OAuth tokens a worker verifies. The console generates one per group and writes it into each zone on deploy. |
| `RAMEN_SESSION_TTL_SECS` | worker | How long an MCP session id stays valid, default 1800. |
| `RAMEN_ADMIN_CIDRS` | worker | Source ranges allowed on the `Admin` gRPC service. Any by default; the admin key is the gate that always applies. |
| `RAMEN_REDIS_ITEM_URL`, `RAMEN_REDIS_SCOPE_URL` | worker | The two throttle Redis instances, written by the console from the group page. Dialed when the node starts. |
| `RAMEN_TLS_CERT`, `RAMEN_TLS_KEY` | worker | PEM paths that make the node terminate TLS itself. The console then also needs `RAMEN_WORKER_TLS=1`, or every deploy fails. |
| `RAMEN_REFLECTION` | worker | gRPC server reflection. On by default, set to `0` on deployed workers by the charts. |
| `RAMEN_BACKUP_ROOT` | console | Where a `local` backup is written. |
| `RAMEN_MIN_PASSWORD_LEN` | console | Raises the twelve-character floor; it can never lower it. |
| `RAMEN_LOG_FILE`, `RAMEN_NODE_PORT` | worker | The access log's path and the port the node listens on. |
| `RAMEN_ALB_SPLIT_DRAIN_SECS` | console, AWS | Seconds the console waits after moving the load balancer's traffic off the canary before it changes the canary's pods, default 15. |
| `RAMEN_CONFIG` | both | The yaml file above. |

The complete list is in [contract §3 and §4](../CONTRACTS.md). Precedence runs the config file, then the
environment, then the deploy file the console writes next to the group's code.

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
is changed. Nothing breaks in the meantime: the transport has been stable since 0.3.1.

Open sessions and OAuth tokens survive an upgrade, because both are signed with the per-group session secret
rather than anything version-specific. A restore into a *fresh* console is the case that invalidates them, and
the fix there is to deploy each environment again.

## Email

SMTP on the Config page or `RAMEN_SMTP_*`. It carries magic-link sign-ins and a digest of warnings and errors to
the notify list, batched every thirty seconds.

## The local stack

`deploy/local/.env` holds the compose stack's values: the admin account, the Fernet key, the local MCP and admin
keys and the image version. `make env` copies the example. The compose console serves TLS on 8443 with a
self-signed certificate; the worker on 8080 is plaintext and open, and is for a machine you control only.
