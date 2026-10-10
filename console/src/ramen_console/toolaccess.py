"""C10 (0.7.2, A8): per-tool list/call permissions by caller kind, stored on the environment as
`tool_access: {tool: {"list": [kind...], "call": [kind...]}}` and handed to every worker as `RAMEN_TOOL_ACCESS`."""

import json

from . import rbac

# `key` = a group MCP key; the rest = the person's role in THIS group (the token's `role` claim). Super admins are
# always allowed and never appear in a list.
KINDS = ("key", "group_admin", "viewer", "mcp_user")
MODES = ("list", "call")


def kinds() -> tuple[str, ...]:
    """The built-in kinds, then every custom role (D48): a custom name in a list matches callers holding that role."""
    return KINDS + tuple(n for n in rbac.custom_roles() if n not in KINDS)


def _kinds(tool: str, raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    known = kinds()
    out = []
    for k in raw or []:
        k = str(k).strip()
        if not k:
            continue
        if k not in known:
            raise ValueError(f"{tool}: unknown kind {k!r} (one of {', '.join(known)})")
        if k not in out:
            out.append(k)
    return sorted(out, key=known.index)


def clean(raw) -> dict[str, dict[str, list[str]]]:
    """The map as stored: kinds validated and in a fixed order, `call` ⊆ `list` enforced (422 upstream), and an
    entry that lets every kind list and call dropped — it is the same as no entry. Unknown tool names are kept."""
    if not isinstance(raw, dict):
        raise ValueError("tool_access must be a map of tool name to {list, call}")
    out = {}
    for tool, entry in raw.items():
        tool = str(tool).strip()
        if not tool:
            raise ValueError("Tool name must not be empty")
        if not isinstance(entry, dict):
            raise ValueError(f'{tool}: expected {{"list": [...], "call": [...]}}')
        lists = {mode: _kinds(tool, entry.get(mode)) for mode in MODES}
        missing = [k for k in lists["call"] if k not in lists["list"]]
        if missing:
            raise ValueError(f"{', '.join(missing)} may call {tool} but not list it; a caller must list to call")
        if all(len(lists[mode]) == len(kinds()) for mode in MODES):
            continue
        out[tool] = lists
    return out


def compact(tool_access: dict | None) -> str:
    """`RAMEN_TOOL_ACCESS`: canonical JSON, no spaces, keys sorted — `{}` when nothing is restricted."""
    return json.dumps(tool_access or {}, separators=(",", ":"), sort_keys=True)


def known_tools(env: dict) -> list[str]:
    """Tool names the zones reported on the last deploy (what the group page offers)."""
    packages = (env.get("last_deploy") or {}).get("packages") or {}
    return sorted({t["name"] for p in packages.values() for t in p.get("tools", []) if t.get("name")})


def rows(env: dict) -> list[str]:
    """The Tool access table: known tools plus any restricted name not (or no longer) deployed."""
    return sorted(set(known_tools(env)) | set(env.get("tool_access") or {}))
