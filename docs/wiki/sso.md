# Sign in with Microsoft Entra ID or Google Workspace

The console speaks plain OpenID Connect. Since 0.7.5 it can also **read the person's groups from the provider**
at sign-in (Microsoft Graph for Entra ID, Cloud Identity for Google Workspace), map them to a role per Ramen
group, and keep the memberships an admin set by hand next to the ones the provider decides. Roles can be your own
(0.7.5 [custom roles](users-access.md#custom-roles)), and a role can insist on the provider: people who hold it
cannot sign in with a password.

!!! info "What has been run against real providers (0.7.5)"
    **Microsoft Entra ID**, on GKE and on EKS, through a throwaway app registration and two test groups with the
    groups lookup on: sign-in mapped group ids to Group Admin and to an OAuth-only custom role; a person removed from a
    group lost that membership at the next sign-in and got it back when re-added; the bridge then signed the same
    person in (`--oauth`), and the worker listed and refused tools by that role. **Google** sign-in, on GKE, with the
    provider added on the Config page: mapped on `email`, same account as Entra, and the bridge's tool list followed
    the Google role. **Google Workspace groups** (the Cloud Identity lookup) are covered by tests against a fake
    endpoint only: the maintainer's Google account has no Workspace. If you run it, please open an issue.

Nothing here depends on the cloud the console runs on: Entra ID works on GKE, Workspace on EKS, both at once
anywhere. The provider is a few environment variables on the console.

## 1. Register the console as an OIDC client

| | Microsoft Entra ID | Google Workspace |
|---|---|---|
| Where | Entra admin center → App registrations → New registration (web) | Google Cloud console → APIs & Services → Credentials → OAuth client (web) |
| Redirect URI | `https://<console>/auth/entra/callback` | `https://<console>/auth/google/callback` |
| Issuer | `https://login.microsoftonline.com/<tenant id>/v2.0` | `https://accounts.google.com` |
| Groups | `groups` claim (Token configuration → add groups claim), **or** the lookup below | the lookup below (Google puts no groups in the ID token); or scope on `hd`, the hosted domain |

Then, as a super admin, **Config → Identity providers (Entra ID, Google Workspace)**: pick the name (`entra`,
`google`, or your own for any other OIDC provider), paste the issuer, client id and client secret, choose the group
source, save. The secret is encrypted in the store and never shown again; the login page shows the new button at
once, no restart. `GET|PUT|DELETE /api/v1/config/oauth-providers/{name}` is the same thing for scripts; every change
is audited as `config.oauth_provider`.

The environment still works, for installs that keep secrets out of the store, and shows on the card as source `env`
(a provider saved on the page wins over the environment of the same name):

```sh
RAMEN_OAUTH_<NAME>_ISSUER=<issuer>
RAMEN_OAUTH_<NAME>_CLIENT_ID=<client id>
RAMEN_OAUTH_<NAME>_CLIENT_SECRET=<secret>
RAMEN_OAUTH_<NAME>_SCOPES=openid email profile       # optional; the lookup adds what it needs
RAMEN_OAUTH_<NAME>_GROUPS=lookup                     # 0.7.5: claim (default) | lookup
```

**Who the person is.** The account is the email the provider returns (`email`, or a `preferred_username` that is one);
every MCP call then carries that account in the worker's log line, which is the accountability Ramen needs. Ramen
does not ask the provider to swear the email is verified: Entra ID never sends `email_verified`, and 0.7.5's first
live sign-in was refused for exactly that until the rule changed. A provider that returns no email at all gets an
account keyed on its stable id (Entra `oid` in the tenant, Google `sub`), shown as `<provider>:<id>` until an email
arrives. The console's public address must be the https one people use (Config → Public address, or
`RAMEN_PUBLIC_URL`), or the provider is sent an `http://` callback and refuses it (Entra: `AADSTS50011`).

## 2. Where the groups come from

**`claim`** (the default, and the only way before 0.7.5): the console reads the claim you name on the Config page
from the ID token or userinfo. Entra ID can emit group ids in `groups` (up to 200; beyond that the token carries an
overage marker and you want the lookup). Google emits no groups; `hd` is the domain.

**`lookup`**: after the sign-in the console calls the provider with the person's access token and injects the
result as claim `groups`, replacing whatever the token said.

| Provider | Call | Values you can map | Scope added |
|---|---|---|---|
| `entra` | Microsoft Graph `GET /v1.0/me/memberOf` (paged) | every group's **object id**; display names too, but only when the app has `GroupMember.Read.All` — with `User.Read` alone Graph returns ids only (verified live) | `User.Read` |
| `google` | Cloud Identity `groups/-/memberships:searchDirectGroups` for the person's email | every group's email, its `groups/<id>` resource name and its display name | `cloud-identity.groups.readonly` |

Set `RAMEN_OAUTH_<NAME>_GROUPS=lookup` or choose **Group source** per provider on the Config page. If the lookup
fails (the API said no, or did not answer in ten seconds) the sign-in fails with `502 could not read your groups
from <provider>`: nobody is signed in with stale groups. For Entra ID the app registration needs Graph's
`User.Read` delegated permission, which every registration has; if your tenant hides group membership from
members, grant `GroupMember.Read.All` and add it to `RAMEN_OAUTH_ENTRA_SCOPES`. For Google, enable the Cloud
Identity API on the OAuth client's project.

## 3. Map groups to roles per Ramen group

**Config → OAuth role mapping**: name the claim (`groups`), then add rules. Each rule is a claim value, a role and
the Ramen groups it applies to. Several rules may hit one person; the highest role wins per group. A `super_admin`
rule makes the person a super admin. Custom roles appear in the role list after the built-ins.

| Claim value | Role | Groups |
|---|---|---|
| `9edc3dc1-…` (an Entra group's object id; the display name works only with `GroupMember.Read.All`) | `group_admin` | `demo, analytics` |
| `de27d6f1-…` | `analyst` (a custom role) | `analytics` |
| `mcp-users@example.com` (a Google group) | `mcp_user` | `demo` |

Scoping Google Workspace without groups: claim `hd`, value `example.com`, role `mcp_user` in `demo` lets everyone in
the domain connect an MCP client to `demo`; group admins are then set by hand on the Users page.

The same rules can come from the environment as `RAMEN_AUTH_OAUTH_<NAME>_ROLE_CLAIM` and
`RAMEN_AUTH_OAUTH_<NAME>_ROLE_MAP`; rules on the Config page win per value.

## 4. Membership from either side

Once a claim is named, the provider's rules are re-applied at **every** sign-in, and they decide the memberships
that came from the provider. They do not touch the memberships an admin set on the Users page:

- A person in the Entra group `AD-Analysts` gets `analyst` in `analytics` from the provider. A group admin who
  also makes them `viewer` in `demo` by hand keeps that: the next sign-in leaves `demo` alone.
- An admin entry for a group the provider also maps **wins**: set a person to `group_admin` in `analytics` and they
  stay group admin there, whatever the provider says. Remove the admin entry and the provider's role stands again.
- Removed from the Entra group: the provider's membership is gone at the next sign-in, sessions and refresh tokens
  end at once (epoch bump), and the person keeps only what an admin set by hand. Ramen learns of the removal only
  at a sign-in: until the person signs in again, an MCP client's refresh token keeps renewing for up to
  `RAMEN_OAUTH_REFRESH_TTL` (30 days by default). To cut someone off now, delete the account on the Users page (the
  next sign-in recreates it from the provider's current groups), or shorten that TTL.

The Users page shows the source next to each membership: *via entra*, *set here*. People whose claim values match
no rule and have no admin entry see only their own account page.

## 5. Roles that require the provider

A custom role with **OAuth only** switched on (Config → Roles) refuses password and magic-link sign-in for anyone
holding it in any group: `/login` answers `403 This account signs in with entra`. MCP clients that reach the
console's sign-in step land on the provider too. The bootstrap super admin is exempt, as it is from the mappings.
The built-in roles cannot be made OAuth-only; make a custom one on the base you want.

## 6. If it does not work

| Symptom | Likely cause |
|---|---|
| The provider's button does not appear on the login page | The provider was not saved on the Config page, or the `RAMEN_OAUTH_<NAME>_*` variables are not all set |
| Microsoft says `AADSTS50011`, redirect URI `http://…` does not match | The console does not know its public https address: set Config → Public address, or `RAMEN_PUBLIC_URL`, to the URL people use, and register that `https://<console>/auth/entra/callback` on the app |
| Sign-in works but the account shows as `entra:<id>` instead of an email | The provider returned no email claim; add the `email` optional claim on the app registration (Entra) and the account adopts it at the next sign-in |
| `502 could not read your groups from entra` | Graph refused the token: the app registration lacks `User.Read`, or admin consent is required in your tenant. Check the audit line's tags |
| Sign-in succeeds but the person sees only their own account page | Their groups matched no rule (ids and display names both work with the lookup; check the spelling) |
| A rule demoted everyone, including you | Start the console with `RAMEN_ADMIN_FORCE_PASSWORD=1`, sign in as the bootstrap super admin, fix the rules |
| An existing password account signs in through the provider | It is the same account, matched on the email the provider returns (0.7.5: verified or not); the mapping decides the provider-side memberships, the admin entries stay |
| A person with a custom OAuth-only role cannot sign in with a password | By design; they must use the provider button |
| The provider does not serve the standard discovery path | Point `RAMEN_OAUTH_<NAME>_ISSUER` at the issuer whose `.well-known/openid-configuration` resolves |

Signing out of Ramen does not sign the person out of the provider; the next sign-in may be silent.

## 7. What the person gets

An MCP user signs in through the provider only to approve an OAuth client for a zone of their groups. Claude Code,
Claude Desktop, Cursor and the bridge do that flow by themselves; the console's login page hands off to the
provider. The token the client receives carries the person's role in the group, custom roles included, so the
worker can apply [per-tool access](groups-zones.md) to it. [Connect with OAuth](connect-oauth.md) has the client side.
