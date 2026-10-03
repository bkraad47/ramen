# Sign in with Microsoft Entra ID or Google Workspace

!!! warning "Untested against a real tenant"
    The console speaks plain OpenID Connect (`authlib`), and the role mapping below is exercised by the test suite
    with a fake provider. Neither Entra ID nor Google Workspace has been run against a live console yet (0.5.95).
    If you run it, the facts in `ramen-master` want to know.

Both providers are configured the same way: an OIDC client on the console, then rules on the Config page that map
a claim in the ID token to a Ramen role **per group** (D41 — one person may be a Group Admin of one group and an
MCP User of another).

## 1. Register the console as an OIDC client

| | Microsoft Entra ID | Google Workspace |
|---|---|---|
| Where | Entra admin center → App registrations → New registration (web) | Google Cloud console → APIs & Services → Credentials → OAuth client (web) |
| Redirect URI | `https://<console>/auth/entra/callback` | `https://<console>/auth/google/callback` |
| Issuer | `https://login.microsoftonline.com/<tenant id>/v2.0` | `https://accounts.google.com` |
| Role claim | `groups` (enable *Token configuration → Add groups claim*, or `roles` from *App roles*) | `hd` (the hosted domain) — Google does not put group membership in the ID token; use the domain, or map on `email` |

Console environment (or `RAMEN_CONFIG` yaml) for each provider `<NAME>` (`entra`, `google`):

```
RAMEN_OAUTH_<NAME>_ISSUER=<issuer>
RAMEN_OAUTH_<NAME>_CLIENT_ID=<client id>
RAMEN_OAUTH_<NAME>_CLIENT_SECRET=<secret>
RAMEN_OAUTH_<NAME>_SCOPES=openid email profile        # Entra ID: add the groups claim in the app registration
```

The login page shows one button per configured provider. A person's email must be verified by the provider
(`email_verified`); `RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED=1` relaxes that for providers that never set it.

## 2. Map claim values to roles per group (Config → OAuth role mapping)

Name the claim (`groups`, `roles`, `hd`, …), then add rules — a claim value → a Ramen role + the groups it applies
to. Each matching rule adds its groups at its role; the highest role wins per group; a `super_admin` rule makes the
person a super admin. Once a claim is named the rules are **authoritative**: re-applied at every login, and a person
whose values match no rule becomes a viewer of nothing (the console refuses them everywhere but their own page).

Example — Entra ID group object ids:

| Claim value | Role | Groups |
|---|---|---|
| `0b7f…-platform-admins` | `group_admin` | `demo, analytics` |
| `9c1e…-analysts` | `viewer` | `analytics` |
| `2d44…-agents` | `mcp_user` | `demo` |

Example — Google Workspace: claim `hd`, value `example.com` → `mcp_user` in `demo` (everyone in the domain may
connect an MCP client to `demo`); hand out Group Admin by hand on the Users page.

The same rules can be given as environment: `RAMEN_AUTH_OAUTH_<NAME>_ROLE_CLAIM` and
`RAMEN_AUTH_OAUTH_<NAME>_ROLE_MAP="<value>=<role>:<group>,<group>;…"`; rules on the Config page win per value.

## 3. What the person gets

An MCP User signs in (through the provider) only to approve an OAuth client for a zone of their groups; Claude
Code, Claude Desktop and Cursor do that flow by themselves (see the group page's "How MCP users sign in"), and so
does the stdio bridge since `ramen-mcp-bridge` 0.2.0: `ramen-mcp-bridge --oauth https://<console> --client-id <id>
--group <g> --zone <z>` opens the browser on the console's sign-in page, which hands off to the provider.
