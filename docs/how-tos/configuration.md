# Configuration: the config file, environment variables, verbose logging

Everything the console and the workers read comes from `RAMEN_*` environment variables. A yaml file can hold them
too; the environment wins. The Config page shows the effective values (secrets masked), the file in use and a *Hot
reload* button.

<figure markdown>
![The config page](../img/config.png){ .ramen-shot }
</figure>

## The console's config file
`RAMEN_CONFIG=/path/ramen.yaml`. Top-level keys map to `RAMEN_<KEY>`; nested keys join with `_`:

```yaml title="ramen.yaml"
public_url: https://console.example.com      # RAMEN_PUBLIC_URL — links in mail and the OAuth issuer
store: firestore                             # RAMEN_STORE: memory | firestore | dynamodb | postgres
secrets_backend: gcp                         # RAMEN_SECRETS_BACKEND: store | gcp | aws
cookie_secure: "1"                           # https only, HSTS; set by the charts
auth:
  password_login: true                       # RAMEN_AUTH_PASSWORD_LOGIN
  magic_link: false                          # RAMEN_AUTH_MAGIC_LINK
  oauth:
    entra:                                   # RAMEN_AUTH_OAUTH_ENTRA_ROLE_CLAIM / _ROLE_MAP
      role_claim: groups
      role_map: "0b7f…=group_admin:demo,analytics;9c1e…=viewer:analytics"
oauth:
  entra:                                     # RAMEN_OAUTH_ENTRA_{ISSUER,CLIENT_ID,CLIENT_SECRET,SCOPES}
    issuer: https://login.microsoftonline.com/<tenant>/v2.0
    client_id: …
    client_secret: …
smtp:
  host: smtp.example.com                     # RAMEN_SMTP_{HOST,PORT,USER,PASSWORD,FROM,TLS}
  from: ramen@example.com
```

`POST /api/v1/config/reload` (the *Hot reload* button) re-reads the file without a restart. What the Config page
edits — authentication toggles, OAuth role rules, SMTP, warning emails, the GitHub App, the scheduler, OAuth clients,
service-account rules — is stored in the console store and layered over the file.

## The variables that matter
| Variable | Console or worker | Meaning |
|---|---|---|
| `RAMEN_PUBLIC_URL` | console (handed to workers on deploy) | The address people and clients use; the OAuth issuer workers trust. |
| `RAMEN_STORE` + `RAMEN_POSTGRES_DSN` | console | State store. Firestore/DynamoDB need no DSN; Postgres takes one. [Changing the state store](storage-backend.md). |
| `RAMEN_SECRETS_BACKEND` | console | Where secret values live. |
| `RAMEN_FERNET_KEY` | console | Encrypts values kept in the store (`store` backend, GitHub tokens, session secrets). Generate once, keep forever. |
| `RAMEN_ADMIN_EMAIL` / `RAMEN_ADMIN_PASSWORD` | console | The bootstrap super admin, re-applied on every start (break-glass with `RAMEN_ADMIN_FORCE_PASSWORD=1`). |
| `RAMEN_COOKIE_SECURE` | console | `1` on a public console: secure cookies, HSTS and https only (0.6.0). |
| `RAMEN_LOGIN_RATE_LIMIT` | console | Failed credential attempts per IP per minute (default 20). |
| `RAMEN_MCP_KEYS`, `RAMEN_ALLOWED_CIDRS`, `RAMEN_ADMIN_KEY` | worker | Written by the console on deploy; never set by hand in the cloud. |
| `RAMEN_VERBOSE` | worker | `1` logs full request and response bodies; otherwise one JSON line per call. Toggle per environment on the group page (*Verbose*). |
| `RAMEN_SIDECAR_IDLE_SECS` | worker | Seconds of idleness before the Python runtime is stopped (default 300; it restarts on the next call). |
| `RAMEN_CALL_TIMEOUT_SECS`, `RAMEN_MAX_INFLIGHT` | worker | Per-call deadline and the in-flight bound. |
| `RAMEN_TRUST_PROXY_HOPS` | worker | How many proxies sit between the client and the node. A wrong value makes the node fall back to the peer address and deny, never admit. |

The complete list, with precedence and the deploy-time file the console writes next to the group's code, is in
[contract §1](../CONTRACTS.md); the per-cloud Helm values (`console.env`, `console.secrets`, `gateway`,
`image`) are in the [GCP](gcp.md) and [AWS](aws.md) guides.

## Environments and verbose logging
An environment is where per-deployment configuration lives: its git ref, zones, blocked packages and the verbose
flag. Turn verbose on for an environment while you debug a tool (the worker then logs every request and response
body to the *Logs* page), and off again — bodies may carry data you do not want in a log store. Deploy after
changing it; the flag reaches workers with the deploy file.

## Local stack
`deploy/local/.env` holds the compose stack's values (admin account, Fernet key, MCP and admin keys, `VERSION`);
`make env` copies the example. The compose console serves TLS itself on 8443 with a self-signed certificate.
