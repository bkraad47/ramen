"""§36.2 / §36.3 guardrails as a client policy, console side (mirrors `evals.py`): the group repo's
`mcp/guardrails.yaml` and its `mcp/guardrails/` dir are packed (≤ 64 KiB, base64 gzip tar) and handed to every client
that signs in (token answer `guardrails {version, required, config, client_llm?}`), named by version in
`/.well-known/ramen-policy`, and gated by the workers (`RAMEN_GUARDRAILS_VERSION` / `_REQUIRED`, `-32029`). When the
yaml names an LLM rail the deploy resolves the group's or the org's LLM provider (`llm_scope`) and hands the worker
`RAMEN_LLM_*` pointing at the console's judge proxy (§38.1); no provider = the deploy fails at the sync step."""

import base64
import gzip
import hashlib
import io
import tarfile

import yaml

from . import llm_providers as lp
from . import llm_proxy, llm_scope

PATH = "mcp/guardrails.yaml"
DIR = "mcp/guardrails"
MAX_PACK = 64 * 1024
# §36.1: the rails that need a model; the runtime maps them to NeMo flows
LLM_RAILS = frozenset(
    {
        "self_check_input",
        "self_check_output",
        "jailbreak_detection",
        "fact_checking",
        "hallucination",
        "topical",
        "sensitive_data",
    }
)
STAGES = ("pre", "post")
SKIP_DIRS, SKIP_SUFFIXES = ("__pycache__",), (".pyc", ".pyo")  # a worker's bytecode in the bucket copy is not policy


def packable(rel: str) -> bool:
    """A file under `mcp/guardrails/` that belongs in the pack (and the version): no `..`, no `__pycache__/`, no
    compiled Python — the local worker writes those into the bucket copy after its first call (a version race)."""
    parts = rel.split("/")
    skipped = ".." in parts or bool(set(parts[:-1]) & set(SKIP_DIRS)) or rel.endswith(SKIP_SUFFIXES)
    return bool(rel) and not skipped


def _names(value) -> set[str]:
    """The rail names of one `pre` / `post` value: a list of names (or `{name: options}` items) or a mapping."""
    items = value.keys() if isinstance(value, dict) else (value if isinstance(value, list) else [])
    out = set()
    for it in items:
        if isinstance(it, str):
            out.add(it)
        elif isinstance(it, dict):
            out.update(k for k in it if isinstance(k, str))
    return out


def llm_rails(text: str) -> dict[str, list[str]]:
    """tool → the LLM-backed rails its `pre`/`post` name (`__default__` for top-level `pre`/`post`); {} when the file
    names none (or does not parse: the runtime reports that on the deploy job)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, list[str]] = {}
    specs = {"__default__": doc, **{str(k): v for k, v in (doc.get("tools") or {}).items() if isinstance(v, dict)}}
    for tool, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        found = {n for s in STAGES for n in _names(spec.get(s)) if n in LLM_RAILS}
        if found:
            out[tool] = sorted(found)
    return out


def engine(text: str) -> str:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return ""
    return str(doc.get("engine") or "") if isinstance(doc, dict) else ""


def pack(text: str, files: dict[str, bytes]) -> str:
    """`config`: a gzip tar (`guardrails.yaml` + `guardrails/<rel>`, sorted, zeroed metadata so the version is
    stable), base64. Raises ValueError over 64 KiB."""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz,  # gzip's own header carries a time
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        entries = {"guardrails.yaml": text.encode()} | {f"guardrails/{rel}": data for rel, data in files.items()}
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.uid, info.gid, info.mode = len(entries[name]), 0, 0, 0, 0o644
            tar.addfile(info, io.BytesIO(entries[name]))
    packed = base64.b64encode(buf.getvalue()).decode()
    if len(packed) > MAX_PACK:
        raise ValueError(f"{PATH} + {DIR}/ pack to {len(packed) // 1024} KiB, over the 64 KiB a client can be handed")
    return packed


async def load(svc, group: str) -> dict | None:
    """`{text, files, pack | None, error?, llm_rails, engine}` from the synced repo; None without the yaml."""
    raw = await svc.cloud.read_file(group, PATH)
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    files = {}
    for p in await svc.cloud.list_files(group, DIR + "/"):
        rel = p[len(DIR) + 1 :]
        if packable(rel) and (data := await svc.cloud.read_file(group, p)) is not None:
            files[rel] = data
    out = {"text": text, "files": files, "pack": None, "llm_rails": llm_rails(text), "engine": engine(text)}
    try:
        out["pack"] = pack(text, files)
    except ValueError as e:
        out["error"] = str(e)
    return out


def settings(group_doc: dict) -> dict:
    s = group_doc.get("guardrails") or {}
    return {"llm": str(s.get("llm") or ""), "model": str(s.get("model") or ""), "required": bool(s.get("required"))}


def version(loaded: dict, group_doc: dict) -> str:
    """§36.3: first 12 hex of sha256 over the pack (the raw files when too large) + `required` + `llm` + `model`."""
    s = settings(group_doc)
    h = hashlib.sha256()
    if loaded.get("pack"):
        h.update(loaded["pack"].encode())
    else:
        h.update(loaded["text"].encode())
        for rel, data in sorted(loaded["files"].items()):
            h.update(rel.encode() + b"\0" + data)
    tail = (b"required" if s["required"] else b"optional") + b"\n" + s["llm"].encode() + b"\n" + s["model"].encode()
    h.update(b"\n" + tail)
    return h.hexdigest()[:12]


def needs_llm(loaded: dict | None) -> bool:
    return bool(loaded and loaded["llm_rails"])


def worker_env(loaded: dict | None, group_doc: dict) -> dict[str, str]:
    """RAMEN_GUARDRAILS_VERSION / RAMEN_GUARDRAILS_REQUIRED for a zone's workers; nothing without the file."""
    if loaded is None:
        return {}
    s = settings(group_doc)
    return {
        "RAMEN_GUARDRAILS_VERSION": version(loaded, group_doc),
        "RAMEN_GUARDRAILS_REQUIRED": "1" if s["required"] else "0",
    }


def resolve(group_doc: dict, group_cfg: dict, org_cfg: dict) -> dict:
    """The LLM credential the group's rails run with (group → org; the group's `guardrails.llm` / `model` when set).
    Raises llm_scope.NoProvider / BadModel."""
    s = settings(group_doc)
    return llm_scope.resolve(group_doc, group_cfg, org_cfg, "guardrails", s["llm"] or None, s["model"] or None)


async def deploy_env(svc, group_doc: dict, loaded: dict | None, base_url: str, token: str) -> dict[str, str]:
    """§36.2 / §38.1: `RAMEN_LLM_*` for the zone's deploy config, only when the yaml names an LLM rail — the judge
    proxy at `base_url` with the zone's judge `token`. Raises NoProvider / BadModel / llm_proxy.NoUrl (the deploy fails
    at the sync step)."""
    if not needs_llm(loaded):
        return {}
    group = group_doc["name"]
    res = resolve(group_doc, await lp.load(svc.store, group), await lp.load(svc.store))
    if not base_url:
        raise llm_proxy.NoUrl()
    return llm_scope.worker_env(res, base_url, token)


def client_info(loaded: dict | None, group_doc: dict, client_llm: dict | None = None) -> dict | None:
    """The token answer's `guardrails` (§36.3): version, required, the packed config (or `error` when too large) and
    `client_llm` when given."""
    if loaded is None:
        return None
    s = settings(group_doc)
    out = {"version": version(loaded, group_doc), "required": s["required"]}
    if loaded.get("pack"):
        out["config"] = loaded["pack"]
    else:
        out["error"] = loaded.get("error") or f"{PATH} cannot be packed"
    if client_llm:
        out["client_llm"] = client_llm
    return out


def wants_client_llm(loaded: dict | None, group_doc: dict) -> bool:
    """§36.3: the bridge runs the rails locally when the group requires guardrails, or the engine is nemo with LLM
    rails; otherwise it is not told where the judge is."""
    if loaded is None:
        return False
    return settings(group_doc)["required"] or (loaded["engine"] == "nemo" and needs_llm(loaded))


async def client_llm(svc, group_doc: dict, loaded: dict | None, issuer: str = "") -> dict | None:
    """§38.1 `guardrails.client_llm`: the judge proxy at the console's public URL and the model; None when none is
    wanted, the URL is unknown or no provider resolves (a sign-in never fails on a missing provider)."""
    if not issuer or not wants_client_llm(loaded, group_doc):
        return None
    try:
        res = resolve(group_doc, await lp.load(svc.store, group_doc["name"]), await lp.load(svc.store))
    except (llm_scope.NoProvider, llm_scope.BadModel):
        return None
    return llm_scope.client_llm(res, f"{issuer.rstrip('/')}{llm_proxy.PATH}")


async def token_info(svc, group_doc: dict, issuer: str = "") -> dict | None:
    """Everything one sign-in path hands out (token answer, the console's own policy meta): `client_info` with the
    resolved `client_llm`."""
    loaded = await load(svc, group_doc["name"])
    return client_info(loaded, group_doc, await client_llm(svc, group_doc, loaded, issuer))


def policy(loaded: dict | None, group_doc: dict) -> dict | None:
    """`/.well-known/ramen-policy` → `guardrails {version, required}` (public: no config, no credential)."""
    if loaded is None:
        return None
    return {"version": version(loaded, group_doc), "required": settings(group_doc)["required"]}
