# Microsoft Entra ID sign-in (any cloud; most often wanted on Azure)

From `docs/wiki/sso.md`. Works the same on GKE and EKS.

1. **Public address first.** The console must know its https address (`RAMEN_PUBLIC_URL`, set by `helm_set` on
   Azure, or Config → Public address), or Entra gets an `http://` callback and answers `AADSTS50011`.
2. **App registration** (the human does this in the Entra admin center, or with `az ad app create`): a web app with
   redirect URI `https://<public_hostname>/auth/entra/callback`, a client secret, the `email` optional claim, and
   Microsoft Graph `User.Read` (delegated; add `GroupMember.Read.All` only if group display names are wanted or the
   tenant hides memberships). Never ask the human to paste the secret into chat: they enter it on the Config page.
3. **Provider in the console**: **Config → Identity providers** — name `entra`, issuer
   `https://login.microsoftonline.com/<tenant id>/v2.0`, client id, client secret, group source `lookup` (Graph
   `memberOf` at every sign-in; Entra sends no `email_verified`, and Ramen does not require it). The API form, if the
   human supplies the values through the environment rather than chat:
   `PUT /api/v1/config/oauth-providers/entra {"issuer": …, "client_id": …, "client_secret": …, "groups_source": "lookup"}`.
   The login page shows the button at once; the change is audited as `config.oauth_provider`.
4. **Role mapping**: **Config → OAuth role mapping**, claim `groups`, one rule per Entra group **object id** → role
   (`group_admin`, `viewer`, `mcp_user` or a custom role) in the named Ramen groups. Highest role wins per group;
   admin-set memberships on the Users page win over the provider's.
5. **Check**: a test user signs in with the Entra button; the Users page shows the membership *via entra*. A
   `502 could not read your groups from entra` is Graph refusing the token (consent or `User.Read` missing).

Cutting someone off at once: **Sign out everywhere** (`POST /api/v1/users/<id>/sign-out`) ends their sessions,
refresh tokens and live MCP access tokens on the workers (D92); the next sign-in applies their current Entra groups.
If a mapping locks everyone out: start the console with `RAMEN_ADMIN_FORCE_PASSWORD=1` and sign in as
`admin@ramen.local`.
