"""D47 (0.7.5, CONTRACTS §21.3): a person's groups read from the identity provider at sign-in, for providers whose
ID token does not carry them. `entra` → Microsoft Graph `/me/memberOf` (every `id` and `displayName`, all pages);
`google` → Cloud Identity `searchDirectGroups` by the person's email (each group email, `group` resource name and
display name, all pages). The values replace the `groups` claim the role mapping reads. Any failure is a
`LookupError` (→ 502): nobody is signed in with stale or missing groups."""

import httpx

BASE = {
    "entra": "https://graph.microsoft.com/v1.0",
    "google": "https://cloudidentity.googleapis.com/v1",
}
SCOPES = {
    "entra": "User.Read",  # Graph lists it as sufficient for /me/memberOf; tenants may want GroupMember.Read.All
    "google": "https://www.googleapis.com/auth/cloud-identity.groups.readonly",
}
TIMEOUT = 10.0


class LookupError(Exception):  # noqa: A001 - the name says what the caller handles
    pass


def supports(provider: str) -> bool:
    return provider in BASE


async def lookup(provider: str, token: str, email: str, base_url: str | None, transport=None) -> list[str]:
    if not supports(provider):
        raise ValueError(f"group lookup is only available for {', '.join(BASE)}, not {provider!r}")
    base = (base_url or BASE[provider]).rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport, headers=headers) as http:
            return await (_entra(http, base) if provider == "entra" else _google(http, base, email))
    except LookupError:
        raise
    except Exception as e:  # noqa: BLE001 - network, JSON or shape: all the same to the person signing in
        raise LookupError(f"could not read your groups from {provider}: {type(e).__name__}") from e


async def _entra(http: httpx.AsyncClient, base: str) -> list[str]:
    out: list[str] = []
    url: str | None = f"{base}/me/memberOf?$select=id,displayName"
    while url:
        r = await http.get(url)
        if r.status_code != 200:
            raise LookupError(f"could not read your groups from entra: Graph answered {r.status_code}")
        body = r.json()
        for item in body.get("value") or []:
            for k in ("id", "displayName"):
                if item.get(k):
                    out.append(str(item[k]))
        url = body.get("@odata.nextLink")
    return out


async def _google(http: httpx.AsyncClient, base: str, email: str) -> list[str]:
    out: list[str] = []
    params = {"query": f"member_key_id=='{email}'"}
    while True:
        r = await http.get(f"{base}/groups/-/memberships:searchDirectGroups", params=params)
        if r.status_code != 200:
            raise LookupError(f"could not read your groups from google: Cloud Identity answered {r.status_code}")
        body = r.json()
        for m in body.get("memberships") or []:
            for v in ((m.get("groupKey") or {}).get("id"), m.get("group"), m.get("displayName")):
                if v:
                    out.append(str(v))
        if not body.get("nextPageToken"):
            return out
        params = {**params, "pageToken": body["nextPageToken"]}
