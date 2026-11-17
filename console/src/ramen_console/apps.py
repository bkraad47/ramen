"""MCP Apps and Skills over MCP on the console (1.0.0c Phase 7, CONTRACTS §33.1 / §33.2).

The runtime serves a group repo's `mcp/apps/<name>/index.html` as the resource `ui://<group>/<name>` (mime
`text/html;profile=mcp-app`, `_meta["ui/csp"]` optional) and `mcp/skills/<name>/` as `skill://<name>/<file>`. The
Packages card lists both per zone from the reload manifest; the preview page reads the app's HTML through the zone's
worker with the console's own credential and shows it in a sandboxed iframe under the app's CSP, never with the
viewer's cookies.
"""

import re

from . import configs, evals, guardrails, otel
from . import skillspector as sk

APP_MIME = "text/html;profile=mcp-app"
DEFAULT_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"
MAX_HTML = 512 * 1024  # §33.1: the runtime caps an app at 512 KiB; a larger answer is not an app
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # the runtime's rule for app and skill names
DIRECTIVE_RE = re.compile(r"^[a-z][a-z-]{1,40}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9.:/'*_%+=-]{1,256}$")  # `'self'`, `data:`, `https://cdn.x/`, `*.x`, a hash
HEAD_RE = re.compile(r"<head\b[^>]*>", re.I)
SKILL_RE = re.compile(r"^skill://([^/]+)/SKILL\.md$")
META_FRAME = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "frame-src 'self' about:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name))


def app_name(uri) -> str | None:
    """`ui://<group>/<name>` → `<name>`; None for anything else."""
    if not isinstance(uri, str) or not uri.startswith("ui://"):
        return None
    parts = uri[5:].split("/")
    return parts[1] if len(parts) == 2 and valid_name(parts[1]) else None


def clean_csp(value) -> str:
    """The app's `_meta["ui/csp"]` — a mapping directive → list of sources (`app.yaml` `csp`) — rendered as one
    policy; the strict default when it is absent or any part of it is not a plain CSP token."""
    if not isinstance(value, dict) or not value:
        return DEFAULT_CSP
    parts = []
    for d, sources in value.items():
        ok = isinstance(d, str) and DIRECTIVE_RE.match(d) and isinstance(sources, list)
        if not ok or not all(isinstance(s, str) and SOURCE_RE.match(s) for s in sources):
            return DEFAULT_CSP
        parts.append(" ".join([d, *sources]))
    return "; ".join(parts)


def with_csp(html: str, csp: str) -> str:
    """The CSP meta first in `<head>` (before any script), or first of all when the document has no head."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
    m = HEAD_RE.search(html)
    return html[: m.end()] + meta + html[m.end() :] if m else meta + html


def apps_of(manifest) -> list[dict]:
    """`[{name, uri, title, description, csp}]`: the manifest's `apps` list when the runtime reports one, else its
    resources whose mime type carries the MCP Apps profile; `csp` from the resource entry's `_meta["ui/csp"]`."""
    m = manifest or {}
    by_uri = {r.get("uri"): r for r in m.get("resources") or [] if isinstance(r, dict)}
    listed = m.get("apps") if isinstance(m.get("apps"), list) else None
    rows = (
        listed
        if listed is not None
        else [r for r in by_uri.values() if str(r.get("mimeType") or "").lower() == APP_MIME]
    )
    out = []
    for a in rows:
        if not isinstance(a, dict) or not (name := app_name(a.get("uri"))):
            continue
        r = by_uri.get(a["uri"]) or {}
        csp = clean_csp((r.get("_meta") or {}).get("ui/csp"))
        out.append(
            {
                "name": name,
                "uri": a["uri"],
                "title": a.get("title") or "",
                "description": a.get("description") or "",
                "csp": csp,
            }
        )
    return out


def skills_of(manifest, scan: dict | None) -> list[dict]:
    """`[{name, uri, description, version, files, scan}]` from the manifest's `skills` list (the `skills/list` shape)
    when it reports one, else from the `skill://<name>/SKILL.md` resources; `scan` is the last deploy's
    recommendation for that skill (`reports[zone].skillspector.skills[].name`), None when it was not scanned."""
    verdicts = {
        str(s.get("name")): s.get("recommendation") or sk.recommendation(int(s.get("score") or 0))
        for s in (scan or {}).get("skills") or []
        if isinstance(s, dict) and "error" not in s
    }
    m = manifest or {}
    rows = []
    if isinstance(m.get("skills"), list):
        for s in m["skills"]:
            if isinstance(s, dict) and valid_name(s.get("name")):
                files = s.get("files")
                n = len(files) if isinstance(files, list) else None
                rows.append(
                    (s["name"], f"skill://{s['name']}/SKILL.md", s.get("description") or "", s.get("version"), n)
                )
    else:
        for r in m.get("resources") or []:
            if isinstance(r, dict) and (mm := SKILL_RE.match(str(r.get("uri") or ""))) and valid_name(mm.group(1)):
                rows.append((mm.group(1), r["uri"], r.get("description") or "", None, None))
    return [
        {"name": n, "uri": u, "description": d, "version": v, "files": f, "scan": verdicts.get(n)}
        for n, u, d, v, f in rows
    ]


def zone_manifest(env: dict, zone: str) -> dict | None:
    """The reload manifest one worker of `zone` reported at the environment's last deploy (every worker serves the
    same set, §30.2)."""
    for key, m in ((env.get("last_deploy") or {}).get("manifest") or {}).items():
        if key.split(" ")[0] == zone and isinstance(m, dict):
            return m
    return None


async def zone_scan(jobs, env: dict, zone: str) -> dict | None:
    """The stored SkillSpector report of the last deploy's scan of `zone` (`job.reports[zone].skillspector`)."""
    url = (((env.get("last_deploy") or {}).get("skillspector") or {}).get(zone) or {}).get("url") or ""
    parts = url.split("/")  # /groups/{g}/scans/{jid}/{zone}
    if len(parts) != 6 or parts[3] != "scans":
        return None
    job = await jobs.get(parts[4])
    return ((job or {}).get("reports") or {}).get(zone, {}).get("skillspector") if job else None


async def per_zone(jobs, env: dict) -> dict[str, dict]:
    """The Packages card's "Apps and skills" per zone of one environment."""
    out = {}
    for z in env.get("zones") or []:
        m = zone_manifest(env, z)
        if m is not None:
            out[z] = {"apps": apps_of(m), "skills": skills_of(m, await zone_scan(jobs, env, z))}
    return out


async def policy_meta(svc, group: str, g: dict) -> dict:
    """What the console puts under `params._meta` so its own read passes the worker's gates (§26.3, §29.3, §30.3,
    §31.2) — the versions it hands every client, and an empty telemetry report."""
    meta = {}
    cfg = await otel.get_config(svc.store)
    if cfg.get("enabled"):
        meta["io.ramen/telemetry"] = {"version": otel.telemetry_version(cfg), "seq": 1, "events": []}
    if ev := evals.client_info(await evals.load(svc, group), g):
        meta["io.ramen/evals"] = {"version": ev["version"]}
    if scan := sk.client_info(await sk.load_baseline(svc, group), g):
        meta["io.ramen/skillspector"] = {"version": scan["version"]}
    if lc := configs.policy(await configs.load(svc, group)):
        meta["io.ramen/configs"] = {"version": lc["version"]}
    if gr := guardrails.policy(await guardrails.load(svc, group), g):
        meta["io.ramen/guardrails"] = {"version": gr["version"]}  # §36.3
    return meta


class AppError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


async def read_app(svc, jobs, group: str, g: dict, zone: str, name: str) -> tuple[str, str]:
    """`(html with the CSP meta, csp)` of app `name` on `zone`: 404 when the zone or app is unknown to the last
    deploy, 502 when the worker cannot answer or answers something else. §33.4a (S8): the read is the console's own —
    the last deploy's evals key (every worker of the zone holds it, §31.4a) next to the admin key the client adds —
    never a stored group key, which a worker deployed earlier may not know."""
    if not valid_name(name) or zone not in await svc.group_zones(group):
        raise AppError(404, "app not found")
    env = next((e for e in await svc.environments(group) if zone in (e.get("zones") or [])), None)
    m = zone_manifest(env, zone) if env else None
    app = next((a for a in apps_of(m) if a["name"] == name), None)
    if app is None:
        raise AppError(404, "app not found")
    key = evals.evals_key(await svc.session_secret(group), env["last_deploy"]["job"])
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "resources/read",
        "params": {"uri": app["uri"], "_meta": await policy_meta(svc, group, g)},
    }
    try:
        reply = await svc.cloud.mcp_call(group, zone, key, msg)
    except Exception as e:  # noqa: BLE001 - the worker's failure is the answer, never a stack trace
        raise AppError(502, f"the zone's worker did not answer: {type(e).__name__}: {str(e)[:200]}") from None
    if err := reply.get("error"):
        code = err.get("code") if isinstance(err, dict) else None
        raise AppError(404 if code == -32002 else 502, f"the worker refused: {str(err)[:200]}")
    contents = (reply.get("result") or {}).get("contents") or []
    c = contents[0] if contents and isinstance(contents[0], dict) else {}
    html = c.get("text")
    if str(c.get("mimeType") or "").lower() != APP_MIME or not isinstance(html, str):
        raise AppError(404, "the resource is not an MCP app")
    if len(html.encode()) > MAX_HTML:
        raise AppError(502, "the app is larger than 512 KiB")
    csp = clean_csp((c.get("_meta") or {}).get("ui/csp")) if (c.get("_meta") or {}).get("ui/csp") else app["csp"]
    return with_csp(html, csp), csp
