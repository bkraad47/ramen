#!/usr/bin/env python3
"""Capture the console screenshots the docs show, so they can never quietly go stale again.

    make shots            # seeds a throwaway console on :18100, captures docs/img/*.png, updates docs/img/shots.json
    make shots OUT=reports/ui-v0.4.3   # same captures into another directory (before/after sets)
    scripts/shots.py --base https://34.1.2.3 --email admin@ramen.local --password '…' --out docs/img/gke --live gke
                          # live mode (CONTRACTS §17.2): capture only, from a real console; nothing is seeded
    RAMEN_SHOTS_PASSWORD=… scripts/shots.py --base https://<edge> --only group-badges.png scan-report.png …
                          # §34.2: the engine pages (LIVE_ONLY) from a console running promptfoo and SkillSpector
    scripts/shots.py --external phoenix-demo.png=https://localhost:6006/projects/<id>/traces \
                                promptfoo-run.png='https://localhost:3443/eval/<id>'
                          # §34.2: the two UIs next to the console, through a port-forward (promptfoo:
                          # kubectl -n ramen-system port-forward deploy/promptfoo 3443:3443; <id> = the run id the
                          # console's evals page names)

The console runs with the memory store and the local cloud adapter, seeded with fictional groups, users and keys —
no cloud, no real credentials, nothing that outlives the run. `docs/img/shots.json` records which release each shot
was captured for; `console/tests/test_docs_images.py` fails when that falls behind the UI contract.
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
PORT = int(os.environ.get("RAMEN_SHOTS_PORT", "18100"))
BASE = f"http://127.0.0.1:{PORT}"
EMAIL = "admin@ramen.local"
PASSWORD = os.environ.get("RAMEN_SHOTS_PASSWORD") or secrets.token_hex(10) + "Aa1!"  # generated per run, never static

# file -> (path, what the page is, full page (True), viewport (False) or a CSS selector: just that part of the page)
ADMIN_KEY = secrets.token_hex(24)  # generated per run (no static secrets, 0.7.6)
PAGES = {
    "login.png": ("/login", "Sign-in page", False),
    "dashboard.png": ("/", "Dashboard: load per zone and group", False),
    "group.png": ("/groups/demo", "A group: environments with their zones and workers, packages, permissions", True),
    "deploy-job.png": ("/groups/demo", "A deploy job with its log", "#jobs > *:first-child"),
    "secrets.png": ("/secrets?group=demo", "Secrets: names only, never values", False),
    "logs.png": ("/logs?group=demo&zone=a", "Logs: entries on the left, the selected body on the right", False),
    "api-keys.png": ("/api-keys", "API keys: client type, the group dropdown, the listing", False),
    "audit.png": ("/audit", "Audit: newest 100, search and outcome filter", False),
    "backups.png": ("/backups", "Backups: preview, restore, restore and prune", False),
    "users.png": ("/users", "Users, permission requests and the password rule", False),
    "environments.png": (
        "/environments",
        "Environments, Zones and Workers: environments and groups, zones and environments, workers per pair",
        True,
    ),
    "config.png": (
        "/config",
        "Config: the public address (base URI), authentication, the permission catalogue, service-account rules",
        True,
    ),
    # 0.7.5: the parts of a page the identity, roles and autoscaling docs point at
    "config-roles.png": (
        "/config",
        "Config: custom roles (name, base, OAuth only)",
        ".card:has(> h2:text-is('Roles'))",
    ),
    "config-identity.png": (
        "/config",
        "Config: identity providers with the group source, and the role mapping",
        ".card:has(> h2:has-text('Identity providers'))",
    ),
    "group-autoscale.png": (
        "/groups/demo",
        "Group page: a zone's actions (count, max to autoscale up to, the last decision) under its environment",
        "h2:text-is('Environments') + .card",
    ),
    "group-signin.png": (  # a one-zone group: two zones double every box
        "/groups/analytics",
        "Group page: How MCP users sign in, generated for this install (group key, accounts, Entra ID)",
        ".signin-guide",
    ),
    # §17.2: the two moments the end-to-end guide needs that a plain page load does not show
    "key-shown.png": ("/api-keys", "The one-time display of a new agent key", False),
    "oauth-consent.png": (
        "/oauth/authorize?{consent}",
        "OAuth: the consent page a person sees when a client asks",
        False,
    ),
    # 1.0.0c (§34.2): pages that only mean something with the engines on and a real deploy behind them; taken in
    # live mode from a console running promptfoo and SkillSpector (LIVE_ONLY: skipped when seeding)
    "group-badges.png": (
        "/groups/demo",
        "Group page: a zone row with the last deploy's evals and scan badges",
        "h2:text-is('Environments') + .card",
    ),
    "group-apps-skills.png": (
        "/groups/demo",
        "Group page: the Apps and skills table of a zone, the scan verdict per skill, the install command",
        ".pkg-zone:has(h4:text-is('Apps and skills'))",
    ),
    "group-policies.png": (
        "/groups/demo",
        "Group page: the Evals, Scans and Guardrails cards (Required, Gate, provider, model, last run, policy version)",
        "#policy-evals, #policy-scans, #policy-guardrails",  # one picture spanning the three cards
    ),
    "scan-report.png": ("{scan}", "Scan report: NVIDIA SkillSpector findings per skill for one zone's deploy", False),
    "configs.png": (
        "/groups/demo/configs",
        "LLM configs: the repo's mcp/llm-config/ as every member reads and accepts it",
        False,
    ),
    "config-llm-providers.png": (
        "/config",
        "Config: LLM providers for evals and scans, set or not set, never a value",
        "#llm-providers",
    ),
    "config-evals-scans.png": ("/config", "Config: the evals and scan engines the chart runs", "#evals-scans"),
    "config-telemetry.png": (
        "/config",
        "Config: telemetry with one project per group and the OTLP ingest URL",
        "#telemetry",
    ),
    "app-preview.png": (
        "/groups/demo/apps/{app_zone}/iris_chart",
        "App preview: the iris_chart MCP App in a sandboxed frame, read through the zone's worker",
        False,
    ),
}
# live mode captures pages that need no seeded state; the two moments above are made by clicking, so they are
# taken there too, with whatever the live console holds
LIVE_SKIP = {"login.png"}
# §34.2: the engine pages are seeded too since 1.0.0c — the throwaway console's evals and scan Jobs are canned
# (`app()` below), so these need no cloud; `--base` still takes them from a live console
LIVE_ONLY: set[str] = set()
# §34.2: the two UIs next to the console, reached through a port-forward; `--external name=url`
EXTERNAL = {
    "phoenix-demo.png": "Arize Phoenix: project demo, one trace per Ramen call, ramen.user on every span",
    "promptfoo-run.png": "promptfoo: the demo repo's eval suite as run by the deploy Job",
}


def workers():
    """Two in-process gRPC workers so the dashboard, the zone rows and the logs show load instead of a connection
    error. Same fake the console's own tests use, so the shots show the real rendering path."""
    sys.path.insert(0, str(ROOT / "console"))
    from tests.fake_grpc import FakeWorker  # noqa: PLC0415 - test helper, only needed here

    cold = FakeWorker(admin_key=ADMIN_KEY, mcp_keys=None).start()
    hot = FakeWorker(admin_key=ADMIN_KEY, mcp_keys=None).start()
    cold.metrics = {"inflight": 1, "total": 128, "errors": 0, "load": "low"}
    hot.metrics = {"inflight": 28, "total": 412, "errors": 1, "load": "high", "max_inflight": 32}
    for w in (cold, hot):
        w.load_result = LOAD_RESULT
        w.resources = {APP_URI: {"mimeType": "text/html;profile=mcp-app", "text": APP_HTML}}
    return cold, hot


# §34.2: what the fake workers report on reload — the demo repo's 1.0.0c surface (tools, a resource, prompts, an MCP
# App, two skills), so the Packages card's "Apps and skills" and the app preview have something real-shaped to show
APP_URI = "ui://demo/iris_chart"
TOOLS = ["demo_calculator_tool", "word_count", "iris_describe", "iris_outliers", "iris_group_means", "slow_report"]
SKILLS = {
    "noodle-math": "A small agent skill that uses the demo calculator, word_count and unit_convert tools by name.",
    "iris-analyst": "Explore the Iris data set with the iris_describe, iris_outliers and iris_group_means tools.",
}
LOAD_RESULT = {
    "tools": [{"name": n, "inputSchema": {"type": "object"}} for n in TOOLS],
    "resources": [
        {"uri": "ramen://demo/iris.csv", "name": "iris_csv", "mimeType": "text/csv"},
        {"uri": APP_URI, "name": "iris_chart", "mimeType": "text/html;profile=mcp-app"},
    ],
    "prompts": [{"name": "get_calculation_prompt"}, {"name": "iris_analysis"}],
    "apps": [
        {
            "name": "iris_chart",
            "uri": APP_URI,
            "title": "Iris group means",
            "description": "Inline SVG bar chart of the per-species means that iris_group_means returns.",
        }
    ],
    "skills": [
        {"name": n, "description": d, "version": f"{i + 1:x}" * 12, "files": ["SKILL.md"]}
        for i, (n, d) in enumerate(SKILLS.items())
    ],
    "errors": [],
}
# A stand-in for the demo repo's iris_chart app: the same chart drawn from the real Iris species means, static (the
# real app waits for a host to post it a tool result, which a preview has none of)
IRIS_MEANS = {"setosa": (5.006, 3.428, 1.462, 0.246), "versicolor": (5.936, 2.77, 4.26, 1.326),
              "virginica": (6.588, 2.974, 5.552, 2.026)}  # fmt: skip


def _app_html() -> str:
    cols, colors = ("sepal_length", "sepal_width", "petal_length", "petal_width"), ("#F26B3A", "#3A7CA5", "#5B9A5B")
    bars, x = [], 40
    for c, col in enumerate(cols):
        for s, (name, means) in enumerate(IRIS_MEANS.items()):
            h = means[c] / 7 * 200
            bars.append(f'<rect x="{x + s * 30}" y="{220 - h:.1f}" width="26" height="{h:.1f}" fill="{colors[s]}">'
                        f"<title>{name} {col} {means[c]}</title></rect>")  # fmt: skip
        bars.append(f'<text x="{x + 42}" y="240" font-size="12" text-anchor="middle">{col}</text>')
        x += 140
    legend = " ".join(f'<span style="color:{colors[i]}">&#9632; {n}</span>' for i, n in enumerate(IRIS_MEANS))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Iris group means</title></head>'
        '<body style="font:14px system-ui;margin:16px;background:#F4F1EC;color:#1F1B18">'
        f"<h1 style='font-size:16px'>Iris group means</h1><p>{legend}</p>"
        f'<svg viewBox="0 0 600 250" width="600">{"".join(bars)}</svg></body></html>'
    )


APP_HTML = _app_html()

# Files added to the seeded demo repo (paths under the repo root): the shapes of ramen-demo-mcp-group's 1.0.0c files
REPO_EXTRAS = {
    "mcp/evals/promptfooconfig.yaml": """description: the demo tools answer through the deployed worker
providers:
  - id: mcp
    config:
      enabled: true
      server:
        url: "{{env.RAMEN_MCP_URL}}"
        headers: {Authorization: "Bearer {{env.RAMEN_MCP_KEY}}", ramen-group: "{{env.RAMEN_GROUP}}"}
prompts: ["{{prompt}}"]
tests:
  - description: calculator adds two numbers
    vars: {prompt: '{"tool": "demo_calculator_tool", "args": {"var1": 2, "var2": 3, "func": "add"}}'}
    assert: [{type: contains, value: "5"}]
""",
    "mcp/skills/noodle-math/SKILL.md": f"---\nname: noodle-math\ndescription: {SKILLS['noodle-math']}\n---\n"
    "# Noodle math\n\nScale a recipe with `demo_calculator_tool`, one operation per call.\n",
    "mcp/skills/iris-analyst/SKILL.md": f"---\nname: iris-analyst\ndescription: {SKILLS['iris-analyst']}\n---\n"
    "# Iris analyst\n\nQuote numbers exactly as the tools return them.\n",
    "mcp/llm-config/manifest.yaml": """version: 1
description: How the demo group's LLM clients should use its tools — a policy hook, a deny rule, two lines of guidance.
targets:
  - {client: claude-code, file: claude-code.settings.json, path: ~/.claude/settings.json, format: json, merge: deep}
  - {client: claude-code, file: CLAUDE.md, path: ~/.claude/CLAUDE.md, format: text, merge: block}
""",
    "mcp/llm-config/claude-code.settings.json": json.dumps(
        {
            "env": {"RAMEN_GROUP": "${RAMEN_GROUP}"},
            "permissions": {"deny": ["Bash(curl *)"]},
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "ramen-mcp-bridge policy check --target ${RAMEN_HOST} "
                                "--group ${RAMEN_GROUP}",
                            }
                        ]
                    }
                ]
            },
        },
        indent=2,
    )
    + "\n",
    "mcp/llm-config/CLAUDE.md": "Use the Ramen group's tools for what they do — `demo_calculator_tool` for arithmetic "
    "— instead of working it out yourself.\n",
    "mcp/guardrails.yaml": "engine: nemo\nconfig: mcp/guardrails\nfail: closed\ntimeout_s: 10\n"
    "tools:\n  demo_calculator_tool: {pre: true}\n",
}


def _evals_log(group: str, zone: str) -> str:
    rows = [{"success": True, "testCase": {"description": f"case {i + 1}"}} for i in range(5)]
    doc = {
        "evalId": f"eval-{group}-{zone}",
        "results": {"results": rows, "stats": {"successes": 5, "failures": 0, "errors": 0}},
        "shareableUrl": f"https://localhost:3443/eval/?evalId=eval-{group}-{zone}",
    }
    return f"promptfoo eval\nRAMEN_EVALS_RESULTS_BEGIN\n{json.dumps(doc)}\nRAMEN_EVALS_RESULTS_END\n"


def _scan_log(group: str, zone: str) -> str:
    """What the live GKE run reported (test-status-v1.0.0c Phase 7): SAFE, score 7, three skills, one MEDIUM finding
    on the skill synthesized from the served tools."""

    def row(name, score, issues=()):
        sev = "LOW" if score <= 20 else "MEDIUM"
        return {
            "name": name,
            "path": name,
            "risk_score": score,
            "risk_severity": sev,
            "finding_count": len(issues),
            "execution_successful": True,
            "risk_assessment": {"score": score, "severity": sev, "recommendation": "SAFE"},
            "issues": list(issues),
        }

    finding = {
        "id": "EA2",
        "severity": "MEDIUM",
        "location": {"file": "SKILL.md", "start_line": 12},
        "explanation": "autonomous high-impact decisions without human-in-the-loop",
    }
    skills = [row("noodle-math", 0), row("iris-analyst", 0), row(f"ramen-served-{zone}", 7, [finding])]
    doc = {
        "multi_skill": True,
        "skill_count": 3,
        "max_risk_score": 7,
        "risk_severity": "LOW",
        "execution_successful": True,
        "risk_recommendation": "SAFE",
        "analysis_completeness": {"is_complete": True},
        "skills_scanned": 3,
        "skills": skills,
    }
    return f"Scanning\nRAMEN_SCAN_RESULTS_BEGIN\n{json.dumps(doc)}\nRAMEN_SCAN_RESULTS_END\n"


def app():
    """`uvicorn shots:app --factory`: the console with canned evals and scan Jobs (the local adapter runs no Job), so
    a seeded deploy carries the badges, the run link and a scan report like a cloud deploy with both engines on."""
    from ramen_console.app import create_app  # noqa: PLC0415 - imported in the console's environment only

    async def evals_runner(group, zone, spec):
        return {"exit_code": 0, "log": _evals_log(group, zone)}

    async def scan_runner(group, zone, spec):
        return {"exit_code": 0, "log": _scan_log(group, zone)}

    a = create_app()
    a.state.cloud.evals_runner, a.state.cloud.scan_runner = evals_runner, scan_runner
    return a


def free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def local_demo_repo() -> str:
    """A git repository of the demo group on disk, so the seeded deploy clones and succeeds without GitHub (a laptop
    with a stale keychain token made the docs show "Authentication failed" on every deploy picture, 0.5.4)."""
    import shutil

    repo = Path("/tmp/ramen-shots/ramen-demo-mcp-group")
    shutil.rmtree(repo, ignore_errors=True)
    shutil.copytree(ROOT / "tests/fixtures/demo_group", repo, ignore=shutil.ignore_patterns("__pycache__"))
    # §34.2: the 1.0.0c files the engine pages read (evals, skills, LLM configs, guardrails), as in the demo repo
    shutil.copytree(
        ROOT / "tests/fixtures/guarded_group/mcp/guardrails",
        repo / "mcp/guardrails",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for rel, text in REPO_EXTRAS.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "shots",
        "GIT_AUTHOR_EMAIL": "shots@ramen.local",
        "GIT_COMMITTER_NAME": "shots",
        "GIT_COMMITTER_EMAIL": "shots@ramen.local",
    }
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "demo"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
    return f"file://{repo}"


def seed(client, repo: str | None = None) -> dict:
    """Fictional material: two groups over two zones, users, keys, secrets, an image pin, a backup, a deploy.
    Returns the dynamic values page paths need (the OAuth client id for the consent page)."""

    def h():
        t = client.cookies.get("ramen_csrf")
        return {"X-Ramen-CSRF": t} if t else {}

    client.post("/login", data={"email": EMAIL, "password": PASSWORD})
    repo = repo or "https://github.com/bkraad47/ramen-demo-mcp-group"
    client.post("/api/v1/groups", json={"name": "demo", "repo_url": repo, "ref": "main"}, headers=h())
    client.post("/api/v1/groups", json={"name": "analytics", "repo_url": repo, "ref": "main"}, headers=h())
    # §27: an environment names its groups; a zone names its environments and is deployed for every derived pair
    client.post("/api/v1/environments", json={"name": "prod", "ref": "main", "groups": ["demo"]}, headers=h())
    client.post("/api/v1/environments", json={"name": "dev", "ref": "main", "groups": ["analytics"]}, headers=h())
    bind_jobs = []
    for zone, region, envs in (("a", "us-central1-a", ["prod", "dev"]), ("b", "us-central1-b", ["prod"])):
        zone_doc = {"name": zone, "provider": "gcp", "region": region, "environments": envs}
        bind_jobs += client.post("/api/v1/zones", json=zone_doc, headers=h()).json().get("jobs") or []
    for jid in bind_jobs:  # §27.3: binding deploys at once, which syncs the repo's mcp/llm-config/
        wait_job(client, jid)
    # §31.1: the admin accepts the group's LLM configs before minting keys (a key counts as accepted by its minter)
    version = ((client.get("/.well-known/ramen-policy?group=demo&zone=a").json() or {}).get("configs") or {}).get(
        "version"
    )
    if version:
        client.post("/api/v1/groups/demo/configs/accept", json={"version": version}, headers=h())
    for email, role, groups in (
        ("ada@example.com", "group_admin", ["demo"]),
        ("grace@example.com", "viewer", ["demo", "analytics"]),
    ):
        client.post(
            "/api/v1/users",
            json={"email": email, "password": "Passw0rd!-for-shots", "role": role, "groups": groups},
            headers=h(),
        )
    client.post(
        "/api/v1/api-keys",
        json={"name": "ci-deploy", "role": "group_admin", "groups": ["demo"], "client_type": "devops"},
        headers=h(),
    )
    for name in ("claude-desktop", "cursor"):
        client.post("/api/v1/groups/demo/mcp-keys", json={"name": name}, headers=h())
    for name, value, scope in (
        ("OPENAI_API_KEY", "not-a-real-key", {"env": "prod", "zone": "a"}),
        ("DATABASE_URL", "postgres://example/not-real", {"env": "prod"}),
        ("GITHUB_TOKEN", "not-a-real-token", {}),
    ):
        client.post("/api/v1/groups/demo/secrets", json={"name": name, "value": value, **scope}, headers=h())
    client.post(
        "/api/v1/groups/demo/images",
        json={"tag": "us-central1-docker.pkg.dev/ramen/ramen/worker:demo-3", "note": "pandas 2.3"},
        headers=h(),
    )
    # 0.7.5: a custom role, an Entra-shaped provider (fictional tenant, never contacted) with the groups lookup and
    # two rules, a max per zone and one rebalance, so the autoscale controls show a decision
    client.put(
        "/api/v1/config/roles/analyst", json={"base": "viewer", "label": "Analyst", "oauth_only": True}, headers=h()
    )
    tenant = "00000000-0000-0000-0000-000000000000"
    client.put(
        "/api/v1/config/oauth-providers/entra",
        json={
            "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
            "client_id": "11111111-2222-3333-4444-555555555555",
            "client_secret": "not-a-real-secret",
            "groups_source": "lookup",
        },
        headers=h(),
    )
    rmap = "/api/v1/config/auth/role-map/entra"
    client.put(rmap, json={"claim": "groups"}, headers=h())
    for value, role, groups in (
        ("6f1c0d2e-0000-4000-8000-00000000a001", "group_admin", ["demo"]),
        ("6f1c0d2e-0000-4000-8000-00000000a002", "analyst", ["demo", "analytics"]),
    ):
        client.put(rmap, json={"value": value, "role": role, "groups": groups}, headers=h())
    for zone in ("a", "b"):
        client.put(f"/api/v1/groups/demo/zones/{zone}/workers", json={"max_count": 3}, headers=h())
    client.put("/api/v1/config/scheduler", json={"enabled": True, "interval_seconds": 60}, headers=h())
    client.post("/api/v1/groups/demo/zones/b/rebalance", headers=h())
    client.put("/api/v1/config/sa-rules", json={"rules": [{"effect": "deny", "permission": "kms.*"}]}, headers=h())
    client.post("/api/v1/backups", json={"target": "local"}, headers=h())
    # §34.2: org LLM providers (placeholders, never called), telemetry to the chart's Phoenix, the group's policy cards
    client.put(
        "/api/v1/config/llm-providers",
        json={
            "openai": {"api_key": "not-a-real-key", "allowed_models": ["gpt-4o-mini"]},
            "anthropic": {"api_key": "not-a-real-key"},
        },
        headers=h(),
    )
    client.put(
        "/api/v1/config/otel",
        json={"enabled": True, "endpoint": "https://phoenix.ramen-system.svc.cluster.local:4318"},  # D86: no http://
        headers=h(),
    )
    client.put("/api/v1/groups/demo/evals", json={"gate": True, "required": False}, headers=h())
    client.put("/api/v1/groups/demo/skillspector", json={"gate": True, "required": False, "llm": ""}, headers=h())
    job = client.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True}, headers=h()).json()
    wait_job(client, job.get("id"))
    write_worker_log(client)
    for i in range(40):  # enough audit rows that the scroll frame and the filters have something to do
        zone = {"name": f"retired-{i}", "provider": "local", "region": "local"}
        client.post("/api/v1/zones", json=zone, headers=h())
        client.delete(f"/api/v1/zones/retired-{i}", headers=h())
    oc = client.post(
        "/api/v1/oauth/clients",
        json={"name": "Claude Desktop", "redirect_uris": ["http://127.0.0.1:9999/callback"]},
        headers=h(),
    )
    scan = ""
    for e in client.get("/api/v1/environments/by-name").json():
        for _z, sc in sorted((((e.get("last_deploy") or {}).get("demo") or {}).get("skillspector") or {}).items()):
            scan = scan or sc.get("url") or ""
    if not scan:
        print("seed: the deploy recorded no scan report; scan-report.png will be skipped", file=sys.stderr)
    return {"consent": consent_query(oc.json()["client_id"], "demo", "a"), "scan": scan, "app_zone": "a"}


def wait_job(client, jid, secs: int = 120) -> None:
    """Until the job leaves `running` (the canned evals and scan Jobs answer at once; the canary steps take seconds)."""
    for _ in range(secs):
        if not jid or (client.get(f"/api/v1/jobs/{jid}").json() or {}).get("status") != "running":
            return
        time.sleep(1)


def consent_query(client_id: str, group: str, zone: str) -> str:
    """The authorize query a real client sends (PKCE challenge is a fixed, valid-shaped value: nothing is exchanged)."""
    from urllib.parse import urlencode

    return urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:9999/callback",
            "scope": f"mcp:{group}:{zone}",
            "state": "shots",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
        }
    )


def write_worker_log(client) -> None:
    """The Logs page reads what a worker wrote. The local adapter reads `<log root>/<group>/<zone>/worker.log`, so a
    plausible run of calls is written there — including one failure, so the outcome column shows both states."""
    keys = {k["name"]: k.get("key_id") for k in client.get("/api/v1/groups/demo/mcp-keys").json()}
    claude, cursor = keys.get("claude-desktop"), keys.get("cursor")
    base = datetime(2026, 9, 29, 10, 41, 0, tzinfo=UTC)
    calls = [
        (claude, "tools/list", None, "ok", 0, 8),
        (claude, "tools/call", "demo_calculator_tool", "ok", 0, 41),
        (cursor, "tools/list", None, "ok", 0, 7),
        (cursor, "tools/call", "demo_calculator_tool", "ok", 0, 38),
        (claude, "resources/list", None, "ok", 0, 5),
        (cursor, "tools/call", "demo_calculator_tool", "error", 13, 120),
        (claude, "tools/call", "demo_calculator_tool", "ok", 0, 44),
        (claude, "prompts/list", None, "ok", 0, 6),
    ]
    lines = [
        json.dumps(
            {
                "ts": (base + timedelta(seconds=17 * i)).isoformat(timespec="seconds"),
                "level": "info",
                "msg": "mcp call",
                "ip": "10.8.0.31",
                "group": "demo",
                "zone": "a",
                "env": "prod",
                "method": method,
                "name": name,
                "status": status,
                "grpc_code": code,
                "ms": ms,
                "key_id": kid,
            }
        )
        for i, (kid, method, name, status, code, ms) in enumerate(calls)
    ]
    loaded = {"ts": base.isoformat(timespec="seconds"), "level": "info", "msg": "loaded 1 tool from the bucket"}
    lines.insert(0, json.dumps(loaded))
    log = Path(os.environ.get("RAMEN_LOG_ROOT", "/tmp/ramen-shots/logs")) / "demo" / "a" / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(lines) + "\n")


def capture(
    out: Path, only: list[str], base: str = BASE, creds=(EMAIL, PASSWORD), values=None, live=False
) -> list[str]:
    from playwright.sync_api import sync_playwright

    email, password = creds
    values = values or {}
    out.mkdir(parents=True, exist_ok=True)
    taken = []

    def sign_in(page):
        page.goto(f"{base}/login")
        page.fill("input[name=email]", email)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2, ignore_https_errors=True
        )
        for name, (path, _, full) in PAGES.items():
            if only and name not in only or live and name in LIVE_SKIP or not live and name in LIVE_ONLY:
                continue
            if live and ("{scan}" in path or "{app_zone}" in path) and "scan" not in values:
                values.update(live_engines(page, base))
            if "{scan}" in path and not values.get("scan"):
                print(f"{name}: the console has no scan report yet; skipped", file=sys.stderr)
                continue
            if name == "login.png":  # the one page that must not be signed in
                page.goto(f"{base}/logout")
                page.goto(f"{base}/login")
            else:
                if "ramen_session" not in {c["name"] for c in page.context.cookies()}:
                    sign_in(page)
                if "{consent}" in path and "consent" not in values:
                    values["consent"] = live_consent(page, base)
                page.goto(f"{base}{path.format(**values)}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(700)  # htmx partials (worker rows, the dashboard grid) land after load
            if name == "key-shown.png":  # generate an agent key and catch the one-time display
                page.fill("form[hx-post='/api/v1/api-keys'] input[name=name]", "claude-desktop")
                page.select_option("form[hx-post='/api/v1/api-keys'] select[name=client_type]", "agent")
                replies: list[tuple[str, int]] = []

                def remember(r, replies=replies):
                    if "/api/v1/api-keys" in r.url:
                        replies.append((r.url, r.status))

                page.on("response", remember)
                page.click("#key-groups > summary")  # an agent key names its groups (§14 W1; checkbox dropdown)
                page.check("#key-groups input[value=demo]")
                page.click("#key-groups > summary")
                page.get_by_role("button", name="Generate key").click()
                try:
                    page.wait_for_function("document.querySelector('#once').innerText.trim().length > 0", timeout=8000)
                except Exception:  # noqa: BLE001 - say what the API answered instead of a blank picture
                    print(f"key-shown: no one-time display; api replies {replies}", file=sys.stderr)
                page.wait_for_timeout(300)
            if name in ("group.png", "group-autoscale.png"):  # §26.1: zone b's actions open under its environment
                page.evaluate("document.querySelectorAll('details.zone-tools').forEach((d, i) => d.open = i == 1)")
                page.wait_for_timeout(200)
            if name == "deploy-job.png":  # the newest job with its log open
                page.evaluate("(d => d && (d.open = true))(document.querySelector('#jobs > :first-child details'))")
                page.wait_for_timeout(200)
            if name == "group-signin.png":  # the Entra section closed by default; open it next to the group key
                page.evaluate("document.querySelectorAll('.signin-guide details').forEach((d, i) => d.open = i != 1)")
                page.wait_for_timeout(200)
            if name == "app-preview.png" and page.locator("iframe.app-preview").count() == 0:
                print(f"app-preview: no frame; the page says: {page.inner_text('body')[:160]!r}", file=sys.stderr)
                continue
            if isinstance(full, str) and "," in full:  # several parts of a page: the box around all of them
                page.evaluate("window.scrollTo(0, 0)")  # boxes are viewport-relative; at the top they are page ones
                boxes = [page.locator(s.strip()).first.bounding_box() for s in full.split(",")]
                boxes = [b for b in boxes if b]
                x0, y0 = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
                x1 = max(b["x"] + b["width"] for b in boxes)
                y1 = max(b["y"] + b["height"] for b in boxes)
                page.screenshot(
                    path=str(out / name), full_page=True, clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
                )
            elif isinstance(full, str):
                page.locator(full).first.screenshot(path=str(out / name))
            else:
                page.screenshot(path=str(out / name), full_page=full)
            taken.append(name)
            print(f"captured {name} <- {path}")
        browser.close()
    return taken


def capture_external(out: Path, targets: dict[str, str]) -> list[str]:
    """§34.2: Phoenix and promptfoo through a port-forward; a plain viewport capture once the app has rendered."""
    from playwright.sync_api import sync_playwright

    taken = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # D86: both forwards serve TLS from the install CA, which this throwaway browser does not trust
        page = browser.new_page(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2, ignore_https_errors=True
        )
        for name, url in targets.items():
            page.goto(url)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2500)  # both are single-page apps that fetch after load
            page.screenshot(path=str(out / name))
            taken.append(name)
            print(f"captured {name} <- {url}")
        browser.close()
    return taken


def live_engines(page, base: str) -> dict:
    """Live mode: the scan report of the demo group's last deploy and a zone that serves the app, from the API."""
    import httpx

    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    with httpx.Client(base_url=base, cookies=cookies, verify=False, timeout=30) as c:
        envs = c.get("/api/v1/environments/by-name").json()
        zones = c.get("/api/v1/groups/demo/zones").json().get("zones") or ["a"]
    scan = ""
    for e in envs:
        for _z, sc in (((e.get("last_deploy") or {}).get("demo") or {}).get("skillspector") or {}).items():
            if sc.get("url"):
                scan = sc["url"]
    return {"scan": scan, "app_zone": zones[-1]}


def live_consent(page, base: str) -> str:
    """Live mode: the consent page needs a registered client; use the first one, or register a throwaway."""
    import httpx

    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    with httpx.Client(base_url=base, cookies=cookies, verify=False, timeout=30) as c:
        h = {"X-Ramen-CSRF": cookies.get("ramen_csrf", "")}
        clients = c.get("/api/v1/oauth/clients", headers=h).json()
        if not clients:
            clients = [
                c.post(
                    "/api/v1/oauth/clients",
                    json={"name": "Claude Desktop", "redirect_uris": ["http://127.0.0.1:9999/callback"]},
                    headers=h,
                ).json()
            ]
        groups = c.get("/api/v1/groups", headers=h).json()
        g = groups[0]["id"] if groups else "demo"
        zones = c.get("/api/v1/zones", headers=h).json()
        z = zones[0]["id"] if zones else "a"
    return consent_query(clients[0]["client_id"], g, z)


def record(taken: list[str], contract: str, live: str | None = None) -> None:
    f = ROOT / "docs/img/shots.json"
    doc = json.loads(f.read_text()) if f.exists() else {"ui_contract": contract, "screenshots": {}}
    what = {**{n: p[1] for n, p in PAGES.items()}, **EXTERNAL}
    if live:  # §17.2: a cloud set is recorded under its own key with the run's date; no staleness gate
        doc.setdefault("live", {})[live] = {
            "captured_for": VERSION,
            "date": datetime.now(UTC).date().isoformat(),
            "screenshots": {name: what[name] for name in taken},
        }
        for name in taken:  # §34.2: a live shot the docs show is recorded like the seeded ones
            doc["screenshots"][name] = {"page": what[name], "captured_for": VERSION, "live": live}
        f.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        print(f"recorded {len(taken)} live shots ({live}) in {f.relative_to(ROOT)}")
        return
    doc["ui_contract"] = contract
    for name in taken:
        doc["screenshots"][name] = {"page": what[name], "captured_for": VERSION}
    f.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"recorded {len(taken)} shots in {f.relative_to(ROOT)} (ui_contract {contract})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/img", help="directory for the png files")
    ap.add_argument("--only", nargs="*", default=[], help="capture just these file names")
    ap.add_argument("--contract", default=VERSION, help="the release whose UI these shots must match")
    ap.add_argument("--no-record", action="store_true", help="do not touch docs/img/shots.json")
    ap.add_argument("--base", help="live mode: capture from this console instead of seeding one (§17.2)")
    ap.add_argument("--email", default=EMAIL)
    ap.add_argument("--password", default=os.environ.get("RAMEN_SHOTS_PASSWORD", PASSWORD))
    ap.add_argument("--live", default="gke", help="live mode: the key the set is recorded under")
    ap.add_argument(
        "--external",
        nargs="*",
        default=[],
        metavar="NAME=URL",
        help="§34.2: capture these UIs as given (phoenix-demo.png=http://localhost:6006/…, promptfoo-run.png=…)",
    )
    a = ap.parse_args()

    import httpx

    if a.external:
        targets = dict(kv.split("=", 1) for kv in a.external)
        unknown = set(targets) - set(EXTERNAL)
        if unknown:
            print(f"unknown external shots {sorted(unknown)}; known: {sorted(EXTERNAL)}", file=sys.stderr)
            return 2
        taken = capture_external(ROOT / a.out, targets)
        if not a.no_record:
            record(taken, a.contract, live=a.live)
        return 0

    if a.base:
        taken = capture(ROOT / a.out, a.only, base=a.base.rstrip("/"), creds=(a.email, a.password), live=True)
        if not a.no_record:
            record(taken, a.contract, live=a.live)
        return 0

    if not free(PORT):
        print(f"port {PORT} is busy; set RAMEN_SHOTS_PORT or stop what is there", file=sys.stderr)
        return 2
    cold, hot = workers()
    env = {
        **os.environ,
        "RAMEN_STORE": "memory",
        "RAMEN_CLOUD": "local",
        "RAMEN_ADMIN_EMAIL": EMAIL,
        "RAMEN_ADMIN_PASSWORD": PASSWORD,
        "RAMEN_SESSION_SECRET": secrets.token_hex(32),
        "RAMEN_BACKUP_ROOT": "/tmp/ramen-shots/backups",
        "RAMEN_BUCKET_ROOT": "/tmp/ramen-shots/buckets",
        "RAMEN_LOG_ROOT": "/tmp/ramen-shots/logs",
        "RAMEN_LOCAL_WORKERS": f"demo/a={cold.target},demo/b={hot.target},analytics/a={cold.target}",
        "RAMEN_WORKER_URL": cold.target,
        "RAMEN_ADMIN_KEY": ADMIN_KEY,  # the fake workers check the same key, so the shots show live workers
        # §34.2: what the chart hands the console with the three engines on (images and in-cluster URLs; nothing runs
        # them here — `app()` cans the Jobs, and the UIs are only named)
        "RAMEN_EVALS_IMAGE": "ghcr.io/promptfoo/promptfoo:0.125.1",
        "RAMEN_EVALS_API_URL": "https://promptfoo.ramen-system:3000",
        "RAMEN_EVALS_UI_URL": "https://localhost:3443",  # the chart's evals.uiUrl: the port-forward's address
        "RAMEN_SKILLSPECTOR_IMAGE": "ghcr.io/bkraad47/ramen-skillspector:2.13.4",
        "RAMEN_OTEL_UI_URL": "https://phoenix.ramen-system:6006",
    }
    from cryptography.fernet import Fernet

    env.setdefault("RAMEN_FERNET_KEY", Fernet.generate_key().decode())
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "shots:app", "--factory", "--app-dir", str(ROOT / "scripts"), "--port", str(PORT)],
        cwd=ROOT / "console",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/login", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.5)
        else:
            print("console did not start", file=sys.stderr)
            return 1
        with httpx.Client(base_url=BASE, timeout=30, follow_redirects=False) as client:
            values = seed(client, repo=local_demo_repo())
        taken = capture(ROOT / a.out, a.only, values=values)
        if not a.no_record and a.out == "docs/img":
            record(taken, a.contract)
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        for w in (cold, hot):
            w.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
