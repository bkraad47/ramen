# Connect a client with a password account

A password account connects through exactly the same OAuth flow as any other sign-in, because the client never
sees the password. The authorize request lands on the console's login page, the person types their email and
password, and the login page returns them to the consent page. There is nothing to configure for it.

So this page covers only what is different: the account itself. The client commands are in
[Connect a client with OAuth](connect-oauth.md).

## The account

1. A super admin creates it on the Users page with an email and a password, or a group admin adds an existing
   account to their group. The role **MCP User** in the group is enough to connect a client.
2. The person signs in once at `https://<edge>/` to check their access. An MCP user sees a single page there: the
   groups they may connect to, and the exact client command for one of them.
3. There is no password form on that page. To change the initial password they use *Forgot your password?* on the
   sign-in page, or an admin resets it on the Users page.

<figure markdown>
![The MCP user's page](../img/mcp-user.png){ .ramen-shot }
<figcaption>What an MCP user sees after signing in: their groups, and the command to point a client at one.</figcaption>
</figure>

## Then connect

Follow [What the person does](connect-oauth.md#what-the-person-does) with the client id an admin gives them. The
browser step shows the console's own login page rather than an identity provider's, and that is the only visible
difference.

Every call the client makes is logged as `user:<id>`, never as a key id. Removing the person from the group, or
disabling the account, ends their tokens at once.
