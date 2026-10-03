# Sign in with Microsoft Entra ID or Google Workspace

!!! warning "Untested against a real tenant"
    The console speaks plain OpenID Connect, and the role mapping below is covered by tests with a fake provider.
    Neither Entra ID nor Google Workspace has been run against a live console as of 0.6.0. If you run it, please
    open an issue with what worked and what did not.

Both providers are set up the same way: an OIDC client on the console, then rules on the Config page that map a
claim in the ID token to a role per group.

## 1. Register the console as an OIDC client

| | Microsoft Entra ID | Google Workspace |
|---|---|---|
| Where | Entra admin center → App registrations → New registration (web) | Google Cloud console → APIs & Services → Credentials → OAuth client (web) |
| Redirect URI | `https://<edge>/auth/entra/callback` | `https://<edge>/auth/google/callback` |
| Issuer | `https://login.microsoftonline.com/<tenant id>/v2.0` | `https://accounts.google.com` |
| Role claim | `groups` (add the groups claim under *Token configuration*), or `roles` from *App roles* | `hd`, the hosted domain |

Google does not put group membership in the ID token, so map on `hd` or on `email`.

Console environment, or the config file, for each provider name (`entra`, `google`):

```sh
RAMEN_OAUTH_<NAME>_ISSUER=<issuer>
RAMEN_OAUTH_<NAME>_CLIENT_ID=<client id>
RAMEN_OAUTH_<NAME>_CLIENT_SECRET=<secret>
RAMEN_OAUTH_<NAME>_SCOPES=openid email profile
```

The login page shows one button per configured provider. The provider must mark the email as verified;
`RAMEN_OAUTH_<NAME>_ALLOW_UNVERIFIED=1` relaxes that.

## 2. Map claim values to roles per group

**Config → OAuth role mapping**: name the claim, then add rules. Each rule is a claim value, a role and the
groups it applies to. The highest role wins per group. A `super_admin` rule makes the person a super admin.

| Claim value | Role | Groups |
|---|---|---|
| `0b7f...` (an Entra group id) | `group_admin` | `demo, analytics` |
| `9c1e...` | `viewer` | `analytics` |
| `2d44...` | `mcp_user` | `demo` |

For Google Workspace: claim `hd`, value `example.com`, role `mcp_user` in `demo` lets everyone in the domain
connect an MCP client to `demo`. Hand out Group Admin by hand on the Users page.

Once a claim is named the rules are **authoritative** and re-applied at every login. A person whose claim values
match no rule gets no membership and sees only their own account page. The same rules can come from the
environment as `RAMEN_AUTH_OAUTH_<NAME>_ROLE_CLAIM` and `RAMEN_AUTH_OAUTH_<NAME>_ROLE_MAP`; rules on the Config
page win per value.

## 3. If it does not work

| Symptom | Likely cause |
|---|---|
| The provider's button does not appear on the login page | The four `RAMEN_OAUTH_<NAME>_*` variables are not all set, so the provider was not configured |
| Sign-in succeeds but the person sees only their own account page | Their claim values matched no rule. Once a claim is named the mapping is authoritative, and no match means no membership |
| A rule demoted everyone, including you | Start the console with `RAMEN_ADMIN_FORCE_PASSWORD=1` and sign in as the bootstrap super admin, who is exempt from the mapping, then fix the rules |
| An existing password account signs in through the provider | It is the same account, matched on the verified email, and the mapping then decides its roles |
| The provider does not serve the standard discovery path | Point `RAMEN_OAUTH_<NAME>_ISSUER` at the issuer whose `.well-known/openid-configuration` resolves |

Signing out of Ramen does not sign the person out of the provider; the next sign-in may be silent.

## 4. What the person gets

An MCP user signs in through the provider only to approve an OAuth client for a zone of their groups. Claude Code,
Claude Desktop, Cursor and the bridge do that flow by themselves; the console's login page hands off to the
provider. [Connect with OAuth](connect-oauth.md) has the client side.
