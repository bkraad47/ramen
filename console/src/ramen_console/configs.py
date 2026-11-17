"""§31 initial LLM configs from the group repo: `mcp/llm-config/manifest.yaml` + fragments, validated at the deploy's
sync step (a violation names the key), versioned (sha12 over the sorted files + the manifest), handed to clients in the
token answer, named in the policy document, accepted per user and version (`users/<id>.accepted_configs`) and gated by
the workers (`RAMEN_CONFIGS_VERSION` / `_REQUIRED`)."""

import hashlib
import json
import re
import tomllib

import yaml

from .errors import ApiError, conflict, invalid, not_found
from .util import now

DIR = "mcp/llm-config"
MANIFEST = "manifest.yaml"
FORMATS = ("json", "toml", "yaml", "text", "env")
MERGES = {"deep": ("json", "toml", "yaml"), "block": ("text",), "replace": FORMATS, "keys": ("env",)}
MAX_TEXT, MAX_DIR = 32 * 1024, 256 * 1024
CLAUDE_SETTINGS = ".claude/settings.json"
CLAUDE_ALLOWED = {"hooks", "env", "permissions", "model"}
CLAUDE_FORBIDDEN = {"apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "forceLoginMethod"}
HOOK_CMD = "ramen-mcp-bridge"
SECRET_KEY = re.compile(r"_API_KEY|_TOKEN", re.I)  # `*_API_KEY*` / `*_TOKEN*`, case-insensitive
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]")


class ConfigsError(ValueError):
    pass


class NotAccepted(ApiError):
    """403 `configs not accepted`: `data` carries the version and the page where the person accepts."""

    def __init__(self, group: str, version: str, url: str):
        super().__init__(403, "configs not accepted")
        # the contract's literal phrase (§31.1), not sentence-cased: the bridge and the harness match on it
        self.detail = f"configs not accepted: version {version} of group {group}'s LLM configs; open {url}"
        self.data = {"version": version, "url": url}


def version(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(n for n in files if n != MANIFEST):
        h.update(f"{name}\n{files[name]}\n".encode())
    h.update(files.get(MANIFEST, "").encode())
    return h.hexdigest()[:12]


def _secret_keys(obj, where: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if SECRET_KEY.search(str(k)):
                raise ConfigsError(f"{where}: key {k!r} looks like a secret (*_API_KEY* / *_TOKEN* are not allowed)")
            _secret_keys(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _secret_keys(v, where)


def _claude(doc, where: str) -> None:
    if not isinstance(doc, dict):
        raise ConfigsError(f"{where}: a Claude Code settings fragment must be a JSON object")
    if extra := sorted(set(doc) - CLAUDE_ALLOWED):
        raise ConfigsError(f"{where}: key {extra[0]!r} is not allowed (only {', '.join(sorted(CLAUDE_ALLOWED))})")

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in CLAUDE_FORBIDDEN:
                    raise ConfigsError(f"{where}: key {k!r} is forbidden")
                if k == "command" and not (isinstance(v, str) and v.split() and v.split()[0] == HOOK_CMD):
                    raise ConfigsError(f"{where}: hook command {v!r} must start with {HOOK_CMD}")
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)


def _parse_fragment(name: str, text: str, fmt: str, merge: str):
    if fmt == "json":
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ConfigsError(f"{name}: not valid JSON ({e})") from None
    elif fmt == "toml":
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigsError(f"{name}: not valid TOML ({e})") from None
    elif fmt == "yaml":
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ConfigsError(f"{name}: not valid YAML ({e})") from None
    elif fmt == "env":
        doc = {}
        for ln in text.splitlines():
            if ln.strip() and not ln.lstrip().startswith("#"):
                if (m := ENV_LINE.match(ln)) is None or "=" not in ln:
                    raise ConfigsError(f"{name}: env fragments hold KEY=value lines, not {ln.strip()[:40]!r}")
                doc[m.group(1)] = ln.split("=", 1)[1]
    else:
        if len(text.encode()) > MAX_TEXT:
            raise ConfigsError(f"{name}: text fragments are limited to 32 KiB")
        doc = {m.group(1): "" for ln in text.splitlines() if (m := ENV_LINE.match(ln))}
    if merge == "deep" and not isinstance(doc, dict):
        raise ConfigsError(f"{name}: must be a {fmt.upper()} object for merge deep")
    return doc


def _target(t, i: int, files: dict[str, str]) -> dict:
    where = f"{MANIFEST}: targets[{i}]"
    if not isinstance(t, dict):
        raise ConfigsError(f"{where} must be a mapping")
    client, file, path = (str(t.get(k) or "").strip() for k in ("client", "file", "path"))
    fmt, merge = str(t.get("format") or ""), str(t.get("merge") or "")
    if not client:
        raise ConfigsError(f"{where}: client must be a non-empty label")
    if not file or "/" in file or file.startswith("."):
        raise ConfigsError(f"{where}: file must name a file in {DIR} (no directories)")
    if file == MANIFEST:
        raise ConfigsError(f"{where}: {MANIFEST} cannot be a target")
    if file not in files:
        raise ConfigsError(f"{where}: {file} is not in {DIR}")
    if not (path == "~" or path.startswith("~/")) or ".." in path.split("/"):
        raise ConfigsError(f"{where}: path {path!r} must be under ~ (no ..)")
    if fmt not in FORMATS:
        raise ConfigsError(f"{where}: format must be one of {', '.join(FORMATS)}, not {fmt!r}")
    if merge not in MERGES or fmt not in MERGES[merge]:
        raise ConfigsError(f"{where}: merge {merge!r} is not valid for format {fmt}")
    return {"client": client, "file": file, "path": path, "format": fmt, "merge": merge}


def parse(files: dict[str, str]) -> dict:
    """`{version, manifest, files}` of a validated directory (`files` = the fragments, the manifest apart);
    ConfigsError names the first violation."""
    if sum(len(v.encode()) for v in files.values()) > MAX_DIR:
        raise ConfigsError(f"{DIR} is over 256 KiB")
    if MANIFEST not in files:
        raise ConfigsError(f"{MANIFEST} is missing")
    try:
        m = yaml.safe_load(files[MANIFEST])
    except yaml.YAMLError as e:
        raise ConfigsError(f"{MANIFEST}: not valid YAML ({e})") from None
    if not isinstance(m, dict) or m.get("version") != 1:
        raise ConfigsError(f"{MANIFEST}: version must be 1")
    if not isinstance(m.get("targets"), list) or not m["targets"]:
        raise ConfigsError(f"{MANIFEST}: targets must be a non-empty list")
    targets = [_target(t, i, files) for i, t in enumerate(m["targets"])]
    for t in targets:
        doc = _parse_fragment(t["file"], files[t["file"]], t["format"], t["merge"])
        if t["path"].endswith(CLAUDE_SETTINGS):
            _claude(doc, t["file"])
        _secret_keys(doc, t["file"])
    manifest = {"version": 1, "description": str(m.get("description") or ""), "targets": targets}
    return {
        "version": version(files),
        "manifest": manifest,
        "manifest_text": files[MANIFEST],
        "files": {k: v for k, v in files.items() if k != MANIFEST},
    }


async def load(svc, group: str) -> dict | None:
    """The synced repo's directory: `parse()`'s answer, or `{version, manifest: {}, files: {}, error}` when it does
    not validate (the deploy fails at sync; until the next good sync nobody can accept it). None without it."""
    paths = await svc.cloud.list_files(group, DIR + "/")
    files = {}
    for p in paths:
        rel = p[len(DIR) + 1 :]
        if rel and "/" not in rel and (raw := await svc.cloud.read_file(group, p)) is not None:
            files[rel] = raw.decode("utf-8", "replace")
    if not files:
        return None
    try:
        return parse(files)
    except ConfigsError as e:
        return {"version": version(files), "manifest": {}, "manifest_text": "", "files": {}, "error": str(e)}


def worker_env(loaded: dict | None) -> dict[str, str]:
    if loaded is None:
        return {}
    return {"RAMEN_CONFIGS_VERSION": loaded["version"], "RAMEN_CONFIGS_REQUIRED": "1"}


def client_info(loaded: dict | None, accepted: bool) -> dict | None:
    """The token answer's `configs` (§31.1): `manifest` is the YAML text (the bridge parses it), `files` the fragments
    by name; an invalid directory names its error and hands out no files."""
    if loaded is None:
        return None
    out = {"version": loaded["version"], "required": True, "accepted": bool(accepted) and not loaded.get("error")}
    if loaded.get("error"):
        out["error"] = loaded["error"]
    else:
        out.update(manifest=loaded["manifest_text"], files=loaded["files"])
    return out


def policy(loaded: dict | None) -> dict | None:
    return None if loaded is None else {"version": loaded["version"], "required": True}


def accepted_version(user: dict, group: str) -> str | None:
    rec = (user.get("accepted_configs") or {}).get(group) or {}
    return rec.get("version") or None


def is_accepted(user: dict, group: str, version_: str) -> bool:
    return accepted_version(user, group) == version_


async def accept(svc, user_id: str, group: str, version_: str) -> dict:
    """Record `users/<id>.accepted_configs[group] = {version, at}`; 404 without configs, 409 on a stale version."""
    loaded = await load(svc, group)
    if loaded is None:
        raise not_found("LLM configs for this group")
    if loaded.get("error"):
        raise invalid(f"{DIR} does not validate ({loaded['error']}); fix the repo and deploy")
    if version_ != loaded["version"]:
        raise conflict(f"configs version {version_} is stale; the current version is {loaded['version']}")
    user = await svc.store.get("users", user_id)
    if not user:
        raise not_found("user")
    acc = dict(user.get("accepted_configs") or {})
    acc[group] = {"version": version_, "at": now()}
    user["accepted_configs"] = acc
    await svc.store.put("users", user_id, user)
    return {"group": group, **acc[group]}


def page_url(base: str, group: str) -> str:
    return f"{base}/groups/{group}/configs"


async def require_accepted(svc, group: str, p, base: str) -> str | None:
    """The version a key minted now records, None without configs; NotAccepted (403) until the person accepted the
    current version — an API key is not a person and cannot accept."""
    loaded = await load(svc, group)
    if loaded is None:
        return None
    user = await svc.store.get("users", p.id) if p.kind == "user" else None
    if loaded.get("error") or not user or not is_accepted(user, group, loaded["version"]):
        raise NotAccepted(group, loaded["version"], page_url(base, group))
    return loaded["version"]


async def signin_target(svc, user: dict, next_: str) -> str:
    """Where a fresh sign-in lands: the configs page of the first group with unaccepted configs, else `next_`."""
    from urllib.parse import quote

    from .rbac import memberships_of

    for g in sorted(memberships_of(user)):
        loaded = await load(svc, g)
        if loaded is not None and not is_accepted(user, g, loaded["version"]):
            return f"/groups/{g}/configs?next={quote(next_, safe='')}"
    return next_


async def acceptance(accounts, svc, group: str, loaded: dict) -> list[dict]:
    """The group page's admin view: every member with the version they accepted (None = not yet)."""
    out = []
    for m in await accounts.list_members(group):
        u = await svc.store.get("users", m["id"]) or {}
        v = accepted_version(u, group)
        out.append({**m, "version": v, "current": v == loaded["version"]})
    return out
