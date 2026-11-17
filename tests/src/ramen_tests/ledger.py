"""S7: what a harness run creates on a console — users, MCP keys, API keys, OAuth clients, groups — and the temporary
branches it pushes, recorded as they are made and removed at the end of the session (`sweep`, called by the `admin`
fixture's teardown). Every `Console` records through an httpx response hook, so a raw `console.http.post(...)` counts
too. Records are keyed by the console's base URL: a `LocalConsole` that exits takes its records with it (`forget`).
`RAMEN_KEEP_RESIDUE=1` keeps everything (debugging a failed run); the run's own group (`RAMEN_E2E_GROUP`, default
`demo`) and `RAMEN_KEEP_GROUPS` are never deleted, even when this run created them."""

import json
import re
import subprocess

import httpx

from . import env as E

CREATES = (  # (POST path → kind); the answer's id field
    (re.compile(r"^/api/v1/users$"), "user", "id"),
    (re.compile(r"^/api/v1/groups/(?P<group>[^/]+)/mcp-keys$"), "mcp_key", "id"),
    (re.compile(r"^/api/v1/api-keys$"), "api_key", "id"),
    (re.compile(r"^/api/v1/oauth/clients$"), "oauth_client", "client_id"),
    (re.compile(r"^/api/v1/groups$"), "group", "name"),
)
# deletion order: what references a group goes before the group; keys before their users
ORDER = ("oauth_client", "mcp_key", "api_key", "user", "group")
DELETE = {
    "user": "/api/v1/users/{id}",
    "mcp_key": "/api/v1/groups/{group}/mcp-keys/{id}",
    "api_key": "/api/v1/api-keys/{id}",
    "oauth_client": "/api/v1/oauth/clients/{id}",
    "group": "/api/v1/groups/{id}",
}

RECORDS: list[dict] = []  # {base, kind, id, group?}
BRANCHES: list[tuple[str, str]] = []  # (remote, branch)


def base_of(url: httpx.URL) -> str:
    return f"{url.scheme}://{url.netloc.decode()}"


def kept_groups() -> set[str]:
    keep = {E.env("RAMEN_E2E_GROUP", "demo")}
    return keep | {g.strip() for g in (E.env("RAMEN_KEEP_GROUPS") or "").split(",") if g.strip()}


def observe(response: httpx.Response) -> None:
    """httpx response hook: record a `201` answer to one of `CREATES`."""
    req = response.request
    if req.method != "POST" or response.status_code != 201:
        return
    for pattern, kind, field in CREATES:
        if m := pattern.match(req.url.path):
            response.read()
            try:
                body = response.json()
            except ValueError:
                body = {}
            ident = body.get(field) if isinstance(body, dict) else None
            if ident is None and kind == "group":  # an older console answering without the name
                ident = json.loads(req.content or b"{}").get("name")
            if ident is None or (kind == "group" and ident in kept_groups()):
                return
            rec = {"base": base_of(req.url), "kind": kind, "id": str(ident), **m.groupdict()}
            if rec not in RECORDS:
                RECORDS.append(rec)
            return


def forget(base: str) -> None:
    """A console that is gone (a `LocalConsole` process) leaves nothing to delete."""
    RECORDS[:] = [r for r in RECORDS if r["base"] != base.rstrip("/")]


def track_branch(remote: str, branch: str) -> None:
    """A branch pushed to `remote` (a probe ref on the demo repo); `sweep` deletes it with `git push --delete`."""
    if (remote, branch) not in BRANCHES:
        BRANCHES.append((remote, branch))


def sweep(console, branches: bool = True) -> dict:
    """Delete this console's records (and the tracked branches) as `console` — a super admin. Answers
    `{"deleted": [...], "left": [...]}`; a 404 counts as deleted (the test removed it itself)."""
    out: dict[str, list[str]] = {"deleted": [], "left": []}
    if E.env("RAMEN_KEEP_RESIDUE") == "1":
        out["left"] = [f"{r['kind']} {r['id']} (RAMEN_KEEP_RESIDUE)" for r in RECORDS]
        return out
    base = base_of(console.http.base_url)
    mine = [r for r in RECORDS if r["base"] == base]
    for kind in ORDER:
        for r in [x for x in reversed(mine) if x["kind"] == kind]:
            label = f"{kind} {r.get('group') + '/' if r.get('group') else ''}{r['id']}"
            try:
                resp = console.http.delete(DELETE[kind].format(**r))
                code = resp.status_code
            except httpx.HTTPError as e:
                code, resp = None, e
            if code in (200, 204, 404):
                out["deleted"].append(label)
                RECORDS.remove(r)
            else:
                out["left"].append(f"{label}: {code} {getattr(resp, 'text', resp)!s:.120}")
    if branches:
        for remote, branch in list(BRANCHES):
            r = subprocess.run(["git", "push", remote, "--delete", branch], capture_output=True, text=True)
            gone = r.returncode == 0 or "remote ref does not exist" in r.stderr
            (out["deleted"] if gone else out["left"]).append(
                f"branch {branch}" + ("" if gone else f": {r.stderr[-120:]}")
            )
            if gone:
                BRANCHES.remove((remote, branch))
    return out
