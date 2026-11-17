"""§30 NVIDIA SkillSpector scans: the group repo's `mcp/skills/*` (every immediate subdirectory with a SKILL.md) plus a
skill synthesized from what the canary serves, scanned as a Job in ramen-system after a zone's evals (report first,
gate per group); the policy clients are told (`version`, `required`, `llm`, `baseline`) and workers gate on
(`RAMEN_SKILLSPECTOR_VERSION` / `_REQUIRED`).

Job: `ramen-scan-<group>-<zone>-<rand>`, the evals ServiceAccount (§33.4a, `RAMEN_EVALS_SA`). The scan directory
travels as one ConfigMap: `files.json` = {relative path: base64} (one key per file would need `/` encoded), the
`ramen_scan.py` helper unpacks it into /scan (an emptyDir) and, for a baseline run, merges the per-skill reports into
the recursive shape; the report is echoed between markers into the pod log and read back from there.
Verified against v2.13.4: the recursive report carries `max_risk_score` (not `risk_score`), there is no `--max-skills`
flag (upstream caps at 32), issues are `{id, severity, location: {file, start_line}, explanation, finding}`.
"""

import base64
import hashlib
import json
import os
import secrets

from .evals import JOB_LABEL, service_account

PATH = "mcp/skills"
BASELINE = "mcp/skillspector-baseline.yaml"
NS = "ramen-system"
IMAGE_ENV, TIMEOUT_ENV = "RAMEN_SKILLSPECTOR_IMAGE", "RAMEN_SKILLSPECTOR_TIMEOUT_SECS"
BEGIN, END = "RAMEN_SCAN_RESULTS_BEGIN", "RAMEN_SCAN_RESULTS_END"
MAX_DIR, MAX_REPORT, MAX_BASELINE = 900 * 1024, 256 * 1024, 64 * 1024
MAX_LINES = 20
CM, SCAN, OUT = "/cm", "/scan", "/out"
DEFAULT_TIMEOUT = 600
# a group's `llm` → SKILLSPECTOR_PROVIDER; None = configured in Ramen but no such provider upstream (D73: Vertex ADC)
PROVIDER = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure_openai": "azure_openai",
    "foundry": "azure_openai",
    "bedrock": "bedrock",
    "openai_compatible": "openai_compatible",
    "vertex": None,
}

# The python the Job runs inside the image (python 3.12 of the venv): `unpack <files.json> <dir>` writes the bundled
# files (relative paths only), `merge <dir> <out>` folds per-skill reports into the recursive report shape.
HELPER = r"""
import base64, json, os, sys
def unpack(src, dst):
    for rel, b64 in json.load(open(src)).items():
        parts = rel.split("/")
        if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
            continue
        p = os.path.join(dst, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(base64.b64decode(b64))
def rec(score):
    return "SAFE" if score <= 20 else ("CAUTION" if score <= 50 else "DO_NOT_INSTALL")
def merge(src, out):
    rows, failed, top = [], False, {"score": -1}
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".json") or fn == "report.json":
            continue
        name = fn[:-5]
        try:
            doc = json.load(open(os.path.join(src, fn)))
            if not isinstance(doc, dict) or "risk_assessment" not in doc:
                raise ValueError("not a skillspector report")
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "error": f"{type(e).__name__}: {e}"})
            failed = True
            continue
        ra = doc.get("risk_assessment") or {}
        score = int(ra.get("score") or 0)
        row = {"name": name, "path": name, "risk_score": score, "risk_severity": ra.get("severity", "LOW"),
               "finding_count": len(doc.get("issues") or []),
               "execution_successful": doc.get("execution_successful", True)}
        row.update(doc)
        row.update({"name": name, "path": name})
        rows.append(row)
        failed = failed or doc.get("execution_successful") is False
        if score > top["score"]:
            top = {"score": score, "severity": ra.get("severity", "LOW"),
                   "recommendation": ra.get("recommendation", rec(score))}
    score = max(top["score"], 0)
    report = {"multi_skill": True, "skill_count": len(rows), "max_risk_score": score,
              "risk_severity": top.get("severity", "LOW"), "execution_successful": not failed,
              "risk_recommendation": top.get("recommendation", rec(score)),
              "analysis_completeness": {"is_complete": True},
              "skills_scanned": sum(1 for r in rows if "error" not in r), "skills": rows}
    with open(out, "w") as f:
        json.dump(report, f, indent=1)
if sys.argv[1] == "unpack":
    unpack(sys.argv[2], sys.argv[3])
else:
    merge(sys.argv[2], sys.argv[3])
"""


def timeout() -> int:
    return int(os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT)) or DEFAULT_TIMEOUT)


def image() -> str:
    return os.environ.get(IMAGE_ENV, "").strip()


def recommendation(score: int) -> str:
    return "SAFE" if score <= 20 else ("CAUTION" if score <= 50 else "DO_NOT_INSTALL")


def settings(group_doc: dict) -> dict:
    s = group_doc.get("skillspector") or {}
    return {"gate": bool(s.get("gate")), "required": bool(s.get("required")), "llm": str(s.get("llm") or "")}


def version(required: bool, llm: str, baseline_text: str) -> str:
    """§30.3: first 12 hex of sha256 over `required` + `llm` + the baseline text."""
    body = (b"required" if required else b"optional") + b"\n" + llm.encode() + b"\n" + baseline_text.encode()
    return hashlib.sha256(body).hexdigest()[:12]


async def load_baseline(svc, group: str) -> str | None:
    raw = await svc.cloud.read_file(group, BASELINE)
    return raw.decode("utf-8", "replace") if raw is not None else None


def _active(group_doc: dict) -> bool:
    """The policy exists when the chart runs the engine or the group requires it (§30.3)."""
    return bool(image()) or settings(group_doc)["required"]


def worker_env(baseline: str | None, group_doc: dict) -> dict[str, str]:
    if not _active(group_doc):
        return {}
    s = settings(group_doc)
    return {
        "RAMEN_SKILLSPECTOR_VERSION": version(s["required"], s["llm"], baseline or ""),
        "RAMEN_SKILLSPECTOR_REQUIRED": "1" if s["required"] else "0",
    }


def client_info(baseline: str | None, group_doc: dict) -> dict | None:
    """The token answer's `skillspector` (§30.3): version, required, llm and the baseline text (≤ 64 KiB)."""
    if not _active(group_doc):
        return None
    s = settings(group_doc)
    out = {"version": version(s["required"], s["llm"], baseline or ""), "required": s["required"], "llm": s["llm"]}
    if len((baseline or "").encode()) > MAX_BASELINE:
        out["error"] = f"{BASELINE} exceeds 64 KiB; the bridge cannot be handed it"
    else:
        out["baseline"] = baseline or ""
    return out


def _fenced(obj) -> str:
    return "```json\n" + json.dumps(obj, indent=2, sort_keys=True) + "\n```"


def synthesize_skill(tools: list[dict], prompts: list[dict], zone: str) -> str | None:
    """`ramen-served-<zone>/SKILL.md`: what the zone really serves, so the instruction-text rules run against the
    tool and prompt descriptions. None when nothing is served."""
    if not tools and not prompts:
        return None
    out = [
        "---",
        f"name: ramen-served-{zone}",
        f"description: The {len(tools)} tools and {len(prompts)} prompts Ramen zone {zone} serves to MCP clients.",
        "---",
        f"# Ramen zone {zone}",
        "",
    ]
    for kind, items, schema_key in (("Tool", tools, "inputSchema"), ("Prompt", prompts, "arguments")):
        for it in items:
            if not isinstance(it, dict) or not it.get("name"):
                continue
            title = f" — {it['title']}" if it.get("title") else ""
            out += [f"## {kind} `{it['name']}`{title}", ""]
            if it.get("description"):
                out += [str(it["description"]), ""]
            if it.get(schema_key) is not None:
                out += [_fenced(it[schema_key]), ""]
    return "\n".join(out)


def skill_files(listing: list[str]) -> dict[str, list[str]]:
    """The repo paths of every immediate subdirectory of mcp/skills that has a SKILL.md, by skill name."""
    by: dict[str, list[str]] = {}
    for p in listing:
        rel = p[len(PATH) + 1 :] if p.startswith(PATH + "/") else None
        if not rel or "/" not in rel:
            continue
        name = rel.split("/", 1)[0]
        if name in ("", ".", "..") or ".." in rel.split("/"):
            continue
        by.setdefault(name, []).append(p)
    return {n: sorted(ps) for n, ps in sorted(by.items()) if f"{PATH}/{n}/SKILL.md" in ps}


def bundle(files: dict[str, bytes]) -> str:
    """`files.json`: relative path → base64; refuses a tree the ConfigMap cannot carry (§30.2: 900 KiB)."""
    packed = json.dumps({k: base64.b64encode(v).decode() for k, v in sorted(files.items())})
    if len(packed) > MAX_DIR:
        raise ValueError(
            f"the scan directory is {len(packed) // 1024} KiB packed ({sum(map(len, files.values())) // 1024} KiB of "
            f"files), over the {MAX_DIR // 1024} KiB a ConfigMap carries"
        )
    return packed


def job_spec(
    group: str,
    zone: str,
    files: dict[str, bytes],
    env: dict[str, str],
    *,
    image: str,
    llm: str | None = None,
    baseline: str | None = None,
):
    """`{name, configmap, secret, job}`. Without a baseline one recursive scan; with one, skill by skill (upstream
    refuses `--baseline` with `--recursive`) merged by the helper. `--no-llm` unless the group picked an `llm`, whose
    SKILLSPECTOR_PROVIDER travels in the Secret with the provider env. The scan's exit code is the Job's (1 = score
    over 50, 2 = error)."""
    name = f"ramen-scan-{group}-{zone}"[: 63 - 7] + "-" + secrets.token_hex(3)
    labels = {"app.kubernetes.io/name": "ramen-scan", JOB_LABEL: "scan", "ramen.io/group": group, "ramen.io/zone": zone}
    meta = {"name": name, "namespace": NS, "labels": labels}
    data = {"files.json": bundle(files), "ramen_scan.py": HELPER}
    env = dict(env)
    llm_flag = " --no-llm"
    if llm and PROVIDER.get(llm):
        env["SKILLSPECTOR_PROVIDER"] = PROVIDER[llm]
        llm_flag = ""
    common = f"--format json{llm_flag}"
    if baseline is not None:
        data["baseline.yaml"] = baseline
        scan = (
            f'rc=0; for d in {SCAN}/*/; do n=$(basename "$d"); '
            f'skillspector scan "$d" --baseline {CM}/baseline.yaml {common} -o {OUT}/"$n".json; '
            f"r=$?; [ $r -gt $rc ] && rc=$r; done; python {CM}/ramen_scan.py merge {OUT} {OUT}/report.json"
        )
    else:
        scan = f"skillspector scan {SCAN} --recursive {common} -o {OUT}/report.json; rc=$?"
    script = (
        f"python {CM}/ramen_scan.py unpack {CM}/files.json {SCAN} && {scan}; "
        f"echo {BEGIN}; cat {OUT}/report.json; echo; echo {END}; exit $rc"
    )
    container = {
        "name": "scan",
        "image": image,
        "command": ["sh", "-c", script],
        "env": [{"name": "HOME", "value": OUT}, {"name": "SKILLSPECTOR_LOG_LEVEL", "value": "WARNING"}],
        "envFrom": [{"secretRef": {"name": name}}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": 1000,
        },
        "resources": {
            "requests": {
                "cpu": os.environ.get("RAMEN_SKILLSPECTOR_CPU", "250m"),
                "memory": os.environ.get("RAMEN_SKILLSPECTOR_MEMORY", "512Mi"),
            },
            "limits": {"memory": os.environ.get("RAMEN_SKILLSPECTOR_MEMORY_LIMIT", "1Gi")},
        },
        "volumeMounts": [
            {"name": "cm", "mountPath": CM, "readOnly": True},
            {"name": "scan", "mountPath": SCAN},
            {"name": "out", "mountPath": OUT},
        ],
    }
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
                "spec": {
                    "serviceAccountName": service_account(),
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "containers": [container],
                    "volumes": [
                        {"name": "cm", "configMap": {"name": name}},
                        {"name": "scan", "emptyDir": {}},
                        {"name": "out", "emptyDir": {}},
                    ],
                },
            },
        },
    }
    return {
        "name": name,
        "configmap": {"apiVersion": "v1", "kind": "ConfigMap", "metadata": dict(meta), "data": data},
        "secret": {"apiVersion": "v1", "kind": "Secret", "metadata": dict(meta), "type": "Opaque", "stringData": env},
        "job": job,
    }


def extract(log: str) -> str | None:
    text = log or ""
    a, b = text.find(BEGIN), text.rfind(END)
    if a < 0 or b < 0 or b <= a:
        return None
    return text[a + len(BEGIN) : b].strip() or None


def _finding(i: dict) -> dict:
    loc = i.get("location") if isinstance(i.get("location"), dict) else {}
    return {
        "rule": str(i.get("id") or i.get("rule_id") or "?"),
        "severity": str(i.get("severity") or "LOW"),
        "file": str(loc.get("file") or i.get("file") or "SKILL.md"),
        "line": int(loc.get("start_line") or i.get("start_line") or i.get("line") or 0),
        "message": str(i.get("explanation") or i.get("message") or i.get("finding") or ""),
    }


def _row(s: dict) -> dict:
    if "error" in s and "risk_score" not in s and "risk_assessment" not in s:
        return {"name": str(s.get("name") or "?"), "error": str(s["error"])}
    ra = s.get("risk_assessment") if isinstance(s.get("risk_assessment"), dict) else {}
    score = int(s.get("risk_score", ra.get("score", 0)) or 0)
    issues = s.get("issues") if isinstance(s.get("issues"), list) else s.get("findings")
    issues = issues if isinstance(issues, list) else []
    return {
        "name": str(s.get("name") or (s.get("skill") or {}).get("name") or "?"),
        "path": str(s.get("path") or s.get("name") or ""),
        "score": score,
        "severity": str(s.get("risk_severity") or ra.get("severity") or "LOW"),
        "recommendation": str(ra.get("recommendation") or recommendation(score)),
        "findings": [_finding(i) for i in issues if isinstance(i, dict)],
    }


def parse_report(json_text: str) -> dict:
    """`{score, severity, recommendation, skills_scanned, findings, skills: [row], error?}` from the recursive report
    (`max_risk_score`, `skills[]`) or a single-skill one (`risk_assessment`, `issues`); tolerant of §30's
    `risk_score`."""
    try:
        doc = json.loads(json_text)
    except ValueError as e:
        raise ValueError(f"report.json is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise ValueError("report.json is not an object")
    rows = (
        [_row(s) for s in doc["skills"] if isinstance(s, dict)] if isinstance(doc.get("skills"), list) else [_row(doc)]
    )
    ra = doc.get("risk_assessment") if isinstance(doc.get("risk_assessment"), dict) else {}
    scored = [r for r in rows if "score" in r]
    score = int(
        doc.get("max_risk_score", doc.get("risk_score", ra.get("score", max((r["score"] for r in scored), default=0))))
        or 0
    )
    out = {
        "score": score,
        "severity": str(
            doc.get("risk_severity") or ra.get("severity") or max((r["severity"] for r in scored), default="LOW")
        ),
        "recommendation": str(doc.get("risk_recommendation") or ra.get("recommendation") or recommendation(score)),
        "skills_scanned": int(doc.get("skills_scanned", len(scored)) or 0),
        "findings": sum(len(r["findings"]) for r in scored),
        "skills": rows,
    }
    if doc.get("execution_successful") is False:
        out["error"] = "the scan did not complete"
    return out


def result_of(parsed: dict, url: str) -> dict:
    """`job.result[zone].skillspector` (§30.2)."""
    return {
        "score": parsed["score"],
        "severity": parsed["severity"],
        "recommendation": parsed["recommendation"],
        "skills": parsed["skills_scanned"],
        "findings": parsed["findings"],
        "url": url,
    }


def stored_report(parsed: dict) -> dict:
    """`job.reports[zone].skillspector`: the parsed rows, findings dropped from the end until it fits 256 KiB."""
    out = {k: v for k, v in parsed.items() if k != "skills"}
    out["skills"] = [dict(r) for r in parsed["skills"]]
    while len(json.dumps(out)) > MAX_REPORT:
        out["truncated"] = True
        longest = max((r for r in out["skills"] if r.get("findings")), key=lambda r: len(r["findings"]), default=None)
        if longest is None:
            break
        longest["findings"] = longest["findings"][: max(len(longest["findings"]) // 2, 0)]
    return out


def log_lines(parsed: dict) -> list[str]:
    """Each finding for the job log (skill, rule, severity, file:line, message), at most MAX_LINES."""
    rows = [
        f"scan {r['name']} {f['rule']} {f['severity']} {f['file']}:{f['line']}: {f['message'][:160]}"
        for r in parsed["skills"]
        for f in r.get("findings") or []
    ] + [f"scan {r['name']}: error {r['error'][:160]}" for r in parsed["skills"] if "error" in r]
    out = rows[:MAX_LINES]
    if len(rows) > MAX_LINES:
        out.append(f"scan: {len(rows) - MAX_LINES} more findings not listed")
    return out


def summary(parsed: dict) -> str:
    n = parsed["skills_scanned"]
    return (
        f"{parsed['recommendation']}, score {parsed['score']}/100, {n} skill{'s' if n != 1 else ''}, "
        f"{parsed['findings']} finding{'s' if parsed['findings'] != 1 else ''}"
    )
