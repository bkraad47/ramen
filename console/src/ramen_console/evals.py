"""§29.2 / §29.3 evals from the group repo: `mcp/evals/promptfooconfig.yaml` runs as a promptfoo Job in ramen-system
after a zone's golden cases (report first, gate per group); the same file is what clients are told (`version`,
`required`, `config`) and what workers gate on (`RAMEN_EVALS_VERSION` / `RAMEN_EVALS_REQUIRED`).

Job: `ramen-evals-<group>-<zone>-<rand>`, ServiceAccount `RAMEN_EVALS_SA` (§33.4a: `ramen-evals`, a cloud identity
that holds nothing — never the builder's), the config from a ConfigMap at /repo/mcp/evals/promptfooconfig.yaml, the env
(RAMEN_MCP_URL/KEY/GROUP/ENV/ZONE + §29.1 provider keys) from a Secret; results.json is echoed between markers into the
pod log and read back from there. `validate` is the provider allowlist the deploy applies at sync (§33.4a).
"""

import hashlib
import hmac
import json
import os
import re
import secrets

import yaml

PATH = "mcp/evals/promptfooconfig.yaml"
NS = "ramen-system"
UPLOAD_USER = "evals"  # Secret ramen-engines `upload-username` (D92)
SA_ENV, DEFAULT_SA = "RAMEN_EVALS_SA", "ramen-evals"
JOB_LABEL = "ramen.io/job"  # the ramen-system NetworkPolicies select Job pods on it: evals | scan
IMAGE_ENV, API_ENV, UI_ENV, TIMEOUT_ENV = (
    "RAMEN_EVALS_IMAGE",
    "RAMEN_EVALS_API_URL",
    "RAMEN_EVALS_UI_URL",
    "RAMEN_EVALS_TIMEOUT_SECS",
)
BEGIN, END = "RAMEN_EVALS_RESULTS_BEGIN", "RAMEN_EVALS_RESULTS_END"
MAX_CONFIG = 64 * 1024
MAX_LINES = 20
MAX_CASES, MAX_TEXT = 200, 500  # the report page's rows per zone, and each row's input / output / reason
DEFAULT_UI = "https://localhost:3443"  # where a person opens the chart's promptfoo UI (PORT_FORWARD, TLS front)
PORT_FORWARD = "kubectl -n ramen-system port-forward deploy/promptfoo 3443:3443"
CONFIG_DIR, OUT = "/repo/mcp/evals", "/out"
DEFAULT_TIMEOUT = 600
ENV_REF = re.compile(r"\{\{\s*env\.(RAMEN_[A-Z0-9_]+)\s*\}\}")
# §33.4a provider allowlist: promptfoo id prefix → the Ramen provider(s) that must be configured for it
FAMILIES = {
    "anthropic:": ("anthropic",),
    "openai:": ("openai", "openai_compatible"),
    "azure:": ("azure_openai", "foundry"),
    "azureopenai:": ("azure_openai", "foundry"),
    "vertex:": ("vertex",),
    "bedrock:": ("bedrock",),
}
REMOTE = ("http:", "https:", "websocket:", "ws:")
CODE = ("exec:", "python:", "javascript:", "js:", "file://", "golang:", "webhook:", "sequence:", "promptfoo:")
# §38.1: inline assertion code runs in the Job next to the provider keys; it may not read the environment
CODE_ASSERTS = ("javascript", "python", "not-javascript", "not-python")
ENV_READ = re.compile(
    r"\b(?:process|Deno|Bun)\s*(?:\.\s*env\b|\[\s*['\"`]env['\"`]\s*\])|\bos\s*\.\s*environ\b|\bgetenv\b|\benviron\b"
)


def service_account() -> str:
    return os.environ.get(SA_ENV, "").strip() or DEFAULT_SA


def evals_key(session_secret: str, job_id: str) -> str:
    """§31.4a / §33.4a: the deploy's own worker credential, `rek_` + HMAC(group session secret, job id) — rotated per
    deploy, recomputed (never stored) by the config push and by the console's own reads (`apps.read_app`)."""
    return "rek_" + hmac.new(session_secret.encode(), f"evals:{job_id}".encode(), hashlib.sha256).hexdigest()[:32]


def _providers(doc) -> list[tuple[str, dict]]:
    """`[(id, config)]` from promptfoo's shapes: one string, a list of strings / `{id, config}` / `{<id>: {config}}`,
    or a mapping id → config."""
    raw = doc.get("providers")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, dict):
        raw = [{k: v} for k, v in raw.items()]
    out = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, str):
            out.append((p, {}))
        elif isinstance(p, dict) and isinstance(p.get("id"), str):
            out.append((p["id"], p.get("config") if isinstance(p.get("config"), dict) else {}))
        elif isinstance(p, dict) and len(p) == 1:
            k, v = next(iter(p.items()))
            out.append((str(k), v if isinstance(v, dict) else {}))
        else:
            out.append((json.dumps(p)[:60], {}))
    return out


def _provider_error(pid: str, cfg: dict, configured: set[str], base_urls: set[str]) -> str | None:
    if pid.startswith(CODE):
        return f"provider {pid!r} runs code in the Job: refused"
    if pid == "mcp" or pid.startswith(REMOTE):
        return None
    for prefix, families in FAMILIES.items():
        if not pid.startswith(prefix):
            continue
        if not any(f in configured for f in families):
            return f"provider {pid!r}: no {' / '.join(families)} key is configured (Config → LLM providers)"
        base = cfg.get("apiBaseUrl") or cfg.get("apiHost")
        if base and str(base).rstrip("/") not in base_urls:
            return f"provider {pid!r}: apiBaseUrl must be a configured base URL"
        return None
    return f"provider {pid!r} is not mcp, http(s), websocket or a configured LLM provider: refused"


def validate(config_text: str, configured_provider_ids, base_urls=()) -> list[str]:
    """§33.4a: every `providers[]` entry must be `mcp`, `http(s):`/`websocket:`/`ws:` or a configured LLM provider
    family (`openai:` with an `apiBaseUrl` only one of `base_urls`); `exec:`, `python:`, `javascript:`/`file://`,
    `golang:`, `webhook:`, `sequence:` and every `extensions` / `transform` script are refused naming the entry.
    Assertions of type javascript / python stay allowed (the Job's identity holds nothing) as long as their inline code
    never reads the environment (`process.env`, `os.environ`, `getenv`: §38.1). The errors, [] when fine."""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError as e:
        return [f"does not parse as YAML: {str(e).splitlines()[0][:120]}"]
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        return ["must be a mapping"]
    configured, urls = set(configured_provider_ids or ()), {str(u).rstrip("/") for u in base_urls if u}
    errors = []
    for pid, cfg in _providers(doc):
        if (why := _provider_error(pid, cfg, configured, urls)) is not None:
            errors.append(why)
        if cfg.get("transform"):
            errors.append(f"provider {pid!r}: config.transform runs code: refused")
    if doc.get("extensions"):
        errors.append("extensions run code: refused")
    if doc.get("transform"):
        errors.append("transform runs code: refused")
    dt = doc.get("defaultTest") if isinstance(doc.get("defaultTest"), dict) else {}
    if isinstance(dt.get("options"), dict) and dt["options"].get("transform"):
        errors.append("defaultTest.options.transform runs code: refused")
    for i, t in enumerate(doc.get("tests") if isinstance(doc.get("tests"), list) else [], 1):
        if isinstance(t, dict) and isinstance(t.get("options"), dict) and t["options"].get("transform"):
            errors.append(f"tests[{i}].options.transform runs code: refused")
    errors += [f"{where}: inline assertion code reads the environment: refused" for where in env_reads(doc)]
    return errors


def env_reads(node, where: str = "") -> list[str]:
    """Where an inline javascript / python assertion (anywhere in the file: tests, defaultTest, assert-sets) reads
    the environment."""
    out = []
    if isinstance(node, dict):
        value = node.get("value")
        if node.get("type") in CODE_ASSERTS and isinstance(value, str) and ENV_READ.search(value):
            out.append(where or "assert")
        for k, v in node.items():
            out += env_reads(v, f"{where}.{k}" if where else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += env_reads(v, f"{where}[{i}]")
    return out


def render(config_text: str, env: dict[str, str]) -> str:
    """`{{env.RAMEN_*}}` filled in by the console: promptfoo renders env refs in prompts and vars but not inside a
    provider's `server.url` / headers. Provider keys (OPENAI_API_KEY …) stay env vars promptfoo reads itself;
    an unknown RAMEN_ name is left as written."""
    return ENV_REF.sub(lambda m: env.get(m.group(1), m.group(0)), config_text)


def timeout() -> int:
    return int(os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT)) or DEFAULT_TIMEOUT)


def image() -> str:
    return os.environ.get(IMAGE_ENV, "").strip()


def api_url() -> str:
    return os.environ.get(API_ENV, "").strip().rstrip("/")


def ui_url() -> str:
    """Where a person opens the promptfoo UI: `RAMEN_EVALS_UI_URL`, else the port-forward's address when the chart runs
    the server. Never the in-cluster `RAMEN_EVALS_API_URL` — no browser resolves `promptfoo.ramen-system`."""
    ui = os.environ.get(UI_ENV, "").strip().rstrip("/")
    if ui and ui != api_url():  # a chart before the run page set the UI to the in-cluster API address
        return ui
    return DEFAULT_UI if api_url() else ""


def run_ui(run_id: str | None) -> str | None:
    """One run in promptfoo's UI (its own route, `/eval/<id>`), None without a server or a run id."""
    from urllib.parse import quote

    return f"{ui_url()}/eval/{quote(run_id, safe='')}" if run_id and ui_url() else None


async def load(svc, group: str) -> str | None:
    """The synced repo's config text, None when the group repo has no such file."""
    raw = await svc.cloud.read_file(group, PATH)
    return raw.decode("utf-8", "replace") if raw is not None else None


def version(config_text: str, required: bool) -> str:
    """§29.3: first 12 hex of sha256 over the config text and the group's `required`."""
    body = config_text.encode() + b"\n" + (b"required" if required else b"optional")
    return hashlib.sha256(body).hexdigest()[:12]


def settings(group_doc: dict) -> dict:
    e = group_doc.get("evals") or {}
    return {"gate": bool(e.get("gate")), "required": bool(e.get("required"))}


def worker_env(config_text: str | None, group_doc: dict) -> dict[str, str]:
    """RAMEN_EVALS_VERSION / RAMEN_EVALS_REQUIRED for a zone's workers; nothing without the file (a worker told no
    version accepts any)."""
    if config_text is None:
        return {}
    required = settings(group_doc)["required"]
    return {"RAMEN_EVALS_VERSION": version(config_text, required), "RAMEN_EVALS_REQUIRED": "1" if required else "0"}


def client_info(config_text: str | None, group_doc: dict) -> dict | None:
    """The token answer's `evals` (§29.3): version, required and the config text (≤ 64 KiB; a larger file is named
    by version only)."""
    if config_text is None:
        return None
    required = settings(group_doc)["required"]
    out = {"version": version(config_text, required), "required": required}
    if len(config_text.encode()) > MAX_CONFIG:
        out["error"] = f"{PATH} exceeds 64 KiB; the bridge cannot be handed it"
    else:
        out["config"] = config_text
    return out


def extract(log: str) -> str | None:
    """results.json as the Job echoed it between the markers; None when the run never got there."""
    text = log or ""
    a, b = text.find(BEGIN), text.rfind(END)
    if a < 0 or b < 0 or b <= a:
        return None
    body = text[a + len(BEGIN) : b].strip()
    return body or None


def before_markers(log: str, lines: int = 3) -> str:
    """The last non-empty lines promptfoo printed before the markers: what went wrong when the file is not JSON."""
    head = (log or "").split(BEGIN, 1)[0]
    kept = [
        ln.strip() for ln in head.splitlines() if ln.strip() and not ln.strip().startswith("{") and ln.strip() != "}"
    ]
    return " | ".join(ln[:120] for ln in kept[-lines:])


def _name(row: dict, i: int) -> str:
    tc = row.get("testCase") if isinstance(row.get("testCase"), dict) else {}
    desc = tc.get("description") or row.get("description")
    if desc:
        return str(desc)
    vars_ = tc.get("vars") or row.get("vars") or {}
    if isinstance(vars_, dict) and vars_:
        return ",".join(f"{k}={v}" for k, v in sorted(vars_.items()))[:80]
    return f"test {i}"


def _reason(row: dict) -> str:
    err = row.get("error")
    if isinstance(err, str) and err:
        return err
    gr = row.get("gradingResult")
    if isinstance(gr, dict) and gr.get("reason"):
        return str(gr["reason"])
    fr = row.get("failureReason")
    return fr if isinstance(fr, str) and fr else "failed"


def _text(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + "…"


def _status(row: dict) -> str:
    """pass / fail / error: promptfoo's `failureReason` (0 none, 1 an assertion, 2 the provider), else `error`."""
    if row.get("success"):
        return "pass"
    fr = row.get("failureReason")
    if isinstance(fr, int) and fr in (1, 2):
        return "error" if fr == 2 else "fail"
    return "error" if isinstance(row.get("error"), str) and row["error"] else "fail"


def _case(row: dict, i: int) -> dict:
    """One row of the report page: the test, what was sent, what came back, why it failed."""
    out = {"name": _name(row, i)[:200], "status": _status(row)}
    prompt = row.get("prompt") if isinstance(row.get("prompt"), dict) else {}
    if prompt.get("raw"):
        out["input"] = _text(prompt["raw"])
    resp = row.get("response") if isinstance(row.get("response"), dict) else {}
    if resp.get("output") is not None:
        out["output"] = _text(resp["output"])
    elif resp.get("error"):
        out["output"] = _text(resp["error"])
    if out["status"] != "pass":
        out["reason"] = _text(_reason(row))
    if isinstance(row.get("latencyMs"), int):
        out["latency_ms"] = row["latencyMs"]
    return out


def parse_results(json_text: str) -> dict:
    """`{passed, failed, errors, failures: [(name, reason)], cases, id?, url?}` from promptfoo's output file: the stats
    and rows live under `results` (or at the top level); counts fall back to the rows when there are no stats."""
    try:
        doc = json.loads(json_text)
    except ValueError as e:
        raise ValueError(f"results.json is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise ValueError("results.json is not an object")
    inner = doc.get("results") if isinstance(doc.get("results"), dict) else doc
    rows = inner.get("results") if isinstance(inner.get("results"), list) else []
    stats = inner.get("stats") if isinstance(inner.get("stats"), dict) else doc.get("stats") or {}
    failures = []
    for i, row in enumerate(rows, 1):
        if isinstance(row, dict) and not row.get("success", True):
            failures.append((_name(row, i), _reason(row)))
    errored = sum(
        1 for r in rows if isinstance(r, dict) and not r.get("success", True) and isinstance(r.get("error"), str)
    )
    out = {
        "passed": int(stats.get("successes", sum(1 for r in rows if isinstance(r, dict) and r.get("success")))),
        "failed": int(stats.get("failures", len(failures) - errored)),
        "errors": int(stats.get("errors", errored)),
        "failures": failures,
        "cases": [_case(r, i) for i, r in enumerate(rows, 1) if isinstance(r, dict)],
    }
    if doc.get("evalId"):
        out["id"] = str(doc["evalId"])
    if isinstance(doc.get("shareableUrl"), str) and doc["shareableUrl"]:
        out["url"] = doc["shareableUrl"]
        # `--share` stores the run under a new id on the server; the local evalId never reaches it (GKE 1.0.0b37:
        # the run page's link answered "404 Eval not found"), so the id that names the run is the share URL's
        if shared := _shared_id(doc["shareableUrl"]):
            out["id"] = shared
    return out


def _shared_id(url: str) -> str | None:
    """The eval id in a promptfoo share URL: `…/eval/<id>` or `…/eval/?evalId=<id>`."""
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    if ids := parse_qs(parts.query).get("evalId"):
        return ids[0] or None
    tail = parts.path.rstrip("/").rsplit("/", 1)
    return tail[1] if len(tail) == 2 and tail[0].endswith("/eval") and tail[1] else None


def result_of(parsed: dict, url: str) -> dict:
    """`job.result[zone].evals` (§29.2): the counts, `url` = the console's report page of this run (the group page's
    badge opens it) and `run` = promptfoo's eval id. promptfoo's own share link is not kept: it names the in-cluster
    server, which no browser reaches."""
    out = {k: parsed[k] for k in ("passed", "failed", "errors")}
    out["url"] = url
    if parsed.get("id"):
        out["run"] = parsed["id"]
    return out


def stored_report(parsed: dict) -> dict:
    """`job.reports[zone].evals`: the counts, promptfoo's run id and the first MAX_CASES cases, scrubbed (§24.5) — a
    tool's answer or a failure reason may quote anything the run saw."""
    from .security import scrub

    def clean(c: dict) -> dict:
        return {k: scrub(v) if isinstance(v, str) else v for k, v in c.items()}

    out = {k: parsed[k] for k in ("passed", "failed", "errors")}
    if parsed.get("id"):
        out["id"] = parsed["id"]
    out["cases"] = [clean(c) for c in parsed.get("cases", [])[:MAX_CASES]]
    if len(parsed.get("cases", [])) > MAX_CASES:
        out["truncated"] = True
    return out


def mcp_url(target: str, group: str, zone: str) -> str:
    """The canary's Streamable HTTP URL for the Job: `https://<group>.<zone>.worker.ramen.internal:8080/mcp` on a TLS
    install (D86 §39.3, D92 §39.2a: the zone's own name, aliased to the pod IP by `job_spec`), else the pod address."""
    from .tls import require_tls, worker_name

    if require_tls():
        return f"https://{worker_name(group, zone)}:{target.rsplit(':', 1)[-1]}/mcp"
    return f"http://{target}/mcp"


def job_spec(
    group: str,
    zone: str,
    config_text: str,
    env: dict[str, str],
    *,
    image: str,
    api_url: str | None = None,
    worker_ip: str | None = None,
):
    """`{name, configmap, secret, job}`: the three objects of one run. `--share` (and the promptfoo server's URLs)
    when the chart runs the server, else `--no-write`; the eval's exit code is the Job's (100 = failures)."""
    name = f"ramen-evals-{group}-{zone}"[: 63 - 7] + "-" + secrets.token_hex(3)
    labels = {
        "app.kubernetes.io/name": "ramen-evals",
        JOB_LABEL: "evals",
        "ramen.io/group": group,
        "ramen.io/zone": zone,
    }  # noqa: E501
    meta = {"name": name, "namespace": NS, "labels": labels}
    flags = "--no-cache --no-progress-bar --no-table " + ("--share" if api_url else "--no-write")
    script = (
        f"promptfoo eval -c {CONFIG_DIR}/promptfooconfig.yaml -o {OUT}/results.json {flags}; rc=$?; "
        f"echo {BEGIN}; cat {OUT}/results.json; echo; echo {END}; exit $rc"
    )
    pod_env = [
        {"name": "HOME", "value": OUT},
        {"name": "PROMPTFOO_CONFIG_DIR", "value": f"{OUT}/.promptfoo"},
        {"name": "PROMPTFOO_CACHE_PATH", "value": f"{OUT}/cache"},
        {"name": "PROMPTFOO_DISABLE_TELEMETRY", "value": "1"},
        {"name": "PROMPTFOO_DISABLE_UPDATE", "value": "1"},
    ]
    if api_url:  # the upload goes to the server; promptfoo's own share link names where a person opens the UI
        if secret := os.environ.get("RAMEN_ENGINES_SECRET", "").strip():
            # D92 §39.7: the server's TLS front takes the Jobs' upload-only credential (a share upload, never a read);
            # promptfoo turns URL userinfo into Basic auth and strips it from the request and the printed link
            ref = {"secretKeyRef": {"name": secret, "key": "upload-password"}}
            pod_env.append({"name": "RAMEN_PROMPTFOO_UPLOAD_PASSWORD", "valueFrom": ref})
            scheme, _, rest = api_url.partition("://")
            api_url = f"{scheme}://{UPLOAD_USER}:$(RAMEN_PROMPTFOO_UPLOAD_PASSWORD)@{rest}"
        pod_env += [
            {"name": "PROMPTFOO_REMOTE_API_BASE_URL", "value": api_url},
            {"name": "PROMPTFOO_REMOTE_APP_BASE_URL", "value": ui_url() or DEFAULT_UI},
        ]
    container = {
        "name": "evals",
        "image": image,
        "command": ["sh", "-c", script],
        "env": pod_env,
        "envFrom": [{"secretRef": {"name": name}}],
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "resources": {
            "requests": {
                "cpu": os.environ.get("RAMEN_EVALS_CPU", "250m"),
                "memory": os.environ.get("RAMEN_EVALS_MEMORY", "512Mi"),
            },
            "limits": {"memory": os.environ.get("RAMEN_EVALS_MEMORY_LIMIT", "1Gi")},
        },
        "volumeMounts": [
            {"name": "config", "mountPath": CONFIG_DIR, "readOnly": True},
            {"name": "out", "mountPath": OUT},
        ],
    }
    pod = {
        "serviceAccountName": service_account(),
        "automountServiceAccountToken": False,  # §33.4a: the Job never talks to the API server
        "restartPolicy": "Never",
        "containers": [container],
        "volumes": [{"name": "config", "configMap": {"name": name}}, {"name": "out", "emptyDir": {}}],
    }
    if worker_ip:  # D86 §39.3 / D92: the canary by its zone's name, the one its leaf carries
        from .tls import worker_name

        pod["hostAliases"] = [{"ip": worker_ip, "hostnames": [worker_name(group, zone)]}]
    if worker_ip or (api_url or "").startswith("https://"):  # the canary's and the server's leaves: the install CA
        pod["volumes"].append({"name": "ramen-ca", "configMap": {"name": "ramen-ca"}})
        container["volumeMounts"].append({"name": "ramen-ca", "mountPath": "/etc/ramen/ca", "readOnly": True})
        container["env"].append({"name": "NODE_EXTRA_CA_CERTS", "value": "/etc/ramen/ca/ca.crt"})
        # D92: promptfoo's own fetch skips certificate checks unless told (PROMPTFOO_INSECURE_SSL defaults to true)
        container["env"].append({"name": "PROMPTFOO_INSECURE_SSL", "value": "false"})
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": dict(meta),
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout(),
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {
                    "labels": dict(labels),
                    "annotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"},
                },
                "spec": pod,
            },
        },
    }
    return {
        "name": name,
        "configmap": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": dict(meta),
            "data": {"promptfooconfig.yaml": render(config_text, env)},
        },
        "secret": {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": dict(meta),
            "type": "Opaque",
            "stringData": dict(env),
        },
        "job": job,
    }


def log_lines(parsed: dict) -> list[str]:
    """The failed tests for the job log, at most MAX_LINES of them."""
    out = [f"evals {name}: FAILED {why}" for name, why in parsed["failures"][:MAX_LINES]]
    if len(parsed["failures"]) > MAX_LINES:
        out.append(f"evals: {len(parsed['failures']) - MAX_LINES} more failed tests not listed")
    return out


def summary(parsed: dict) -> str:
    return f"{parsed['passed']} passed, {parsed['failed']} failed, {parsed['errors']} errors"
