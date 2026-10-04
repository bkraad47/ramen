#!/usr/bin/env python3
"""Capture the console screenshots the docs show, so they can never quietly go stale again.

    make shots            # seeds a throwaway console on :18100, captures docs/img/*.png, updates docs/img/shots.json
    make shots OUT=reports/ui-v0.4.3   # same captures into another directory (before/after sets)
    scripts/shots.py --base https://34.1.2.3 --email admin@ramen.local --password '…' --out docs/img/gke --live gke
                          # live mode (CONTRACTS §17.2): capture only, from a real console; nothing is seeded

The console runs with the memory store and the local cloud adapter, seeded with fictional groups, users and keys —
no cloud, no real credentials, nothing that outlives the run. `docs/img/shots.json` records which release each shot
was captured for; `console/tests/test_docs_images.py` fails when that falls behind the UI contract.
"""

import argparse
import json
import os
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
EMAIL, PASSWORD = "admin@ramen.local", "Shots-Console-1!"

# file -> (path, what the page is, full page or viewport)
PAGES = {
    "login.png": ("/login", "Sign-in page", False),
    "dashboard.png": ("/", "Dashboard: load per zone and group", False),
    "group.png": ("/groups/demo", "A group: environments, zones, packages, permissions, worker image", True),
    "deploy-job.png": ("/groups/demo#jobs", "A deploy job with its log", True),
    "secrets.png": ("/secrets?group=demo", "Secrets: names only, never values", False),
    "logs.png": ("/logs?group=demo&zone=a", "Logs: entries on the left, the selected body on the right", False),
    "api-keys.png": ("/api-keys", "API keys: client type, the group dropdown, the listing", False),
    "audit.png": ("/audit", "Audit: newest 100, search and outcome filter", False),
    "backups.png": ("/backups", "Backups: preview, restore, restore and prune", False),
    "users.png": ("/users", "Users, permission requests and the password rule", False),
    "environments.png": ("/environments", "Environments with the last deploy flattened", False),
    "config.png": (
        "/config",
        "Config: the public address (base URI), authentication, the permission catalogue, service-account rules",
        True,
    ),
    # §17.2: the two moments the end-to-end guide needs that a plain page load does not show
    "key-shown.png": ("/api-keys", "The one-time display of a new agent key", False),
    "oauth-consent.png": (
        "/oauth/authorize?{consent}",
        "OAuth: the consent page a person sees when a client asks",
        False,
    ),
}
# live mode captures pages that need no seeded state; the two moments above are made by clicking, so they are
# taken there too, with whatever the live console holds
LIVE_SKIP = {"login.png"}


def workers():
    """Two in-process gRPC workers so the dashboard, the zone rows and the logs show load instead of a connection
    error. Same fake the console's own tests use, so the shots show the real rendering path."""
    sys.path.insert(0, str(ROOT / "console"))
    from tests.fake_grpc import FakeWorker  # noqa: PLC0415 - test helper, only needed here

    cold = FakeWorker(admin_key="shots", mcp_keys=None).start()
    hot = FakeWorker(admin_key="shots", mcp_keys=None).start()
    cold.metrics = {"inflight": 1, "total": 128, "errors": 0, "load": "low"}
    hot.metrics = {"inflight": 6, "total": 412, "errors": 1, "load": "even"}
    for w in (cold, hot):
        w.load_result = {
            "tools": [{"name": "demo_calculator_tool", "inputSchema": {"type": "object"}}],
            "resources": [],
            "prompts": [],
            "errors": [],
        }
    return cold, hot


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
    for zone, region in (("a", "us-central1-a"), ("b", "us-central1-b")):
        client.post("/api/v1/zones", json={"name": zone, "provider": "gcp", "region": region}, headers=h())
    repo = repo or "https://github.com/bkraad47/ramen-demo-mcp-group"
    client.post("/api/v1/groups", json={"name": "demo", "repo_url": repo, "ref": "main"}, headers=h())
    client.post("/api/v1/groups", json={"name": "analytics", "repo_url": repo, "ref": "main"}, headers=h())
    envs = "/api/v1/groups/{}/environments"
    client.post(envs.format("demo"), json={"name": "prod", "ref": "main", "zones": ["a", "b"]}, headers=h())
    client.post(envs.format("analytics"), json={"name": "dev", "ref": "main", "zones": ["a"]}, headers=h())
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
    client.put("/api/v1/config/sa-rules", json={"rules": [{"effect": "deny", "permission": "kms.*"}]}, headers=h())
    client.post("/api/v1/backups", json={"target": "local"}, headers=h())
    client.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True}, headers=h())
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
    return {"consent": consent_query(oc.json()["client_id"], "demo", "a")}


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
            if only and name not in only or live and name in LIVE_SKIP:
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
                page.select_option("#group-pick", "demo")  # an agent key names its groups (§14 W1)
                page.get_by_role("button", name="Add").click()
                page.get_by_role("button", name="Generate key").click()
                try:
                    page.wait_for_function("document.querySelector('#once').innerText.trim().length > 0", timeout=8000)
                except Exception:  # noqa: BLE001 - say what the API answered instead of a blank picture
                    print(f"key-shown: no one-time display; api replies {replies}", file=sys.stderr)
                page.wait_for_timeout(300)
            page.screenshot(path=str(out / name), full_page=full)
            taken.append(name)
            print(f"captured {name} <- {path}")
        browser.close()
    return taken


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
    if live:  # §17.2: a cloud set is recorded under its own key with the run's date; no staleness gate
        doc.setdefault("live", {})[live] = {
            "captured_for": VERSION,
            "date": datetime.now(UTC).date().isoformat(),
            "screenshots": {name: PAGES[name][1] for name in taken},
        }
        f.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        print(f"recorded {len(taken)} live shots ({live}) in {f.relative_to(ROOT)}")
        return
    doc["ui_contract"] = contract
    for name in taken:
        doc["screenshots"][name] = {"page": PAGES[name][1], "captured_for": VERSION}
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
    a = ap.parse_args()

    import httpx

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
        "RAMEN_SESSION_SECRET": "shots-only-secret",
        "RAMEN_BACKUP_ROOT": "/tmp/ramen-shots/backups",
        "RAMEN_BUCKET_ROOT": "/tmp/ramen-shots/buckets",
        "RAMEN_LOG_ROOT": "/tmp/ramen-shots/logs",
        "RAMEN_LOCAL_WORKERS": f"demo/a={cold.target},demo/b={hot.target},analytics/a={cold.target}",
        "RAMEN_WORKER_URL": cold.target,
        "RAMEN_ADMIN_KEY": "shots",
    }
    from cryptography.fernet import Fernet

    env.setdefault("RAMEN_FERNET_KEY", Fernet.generate_key().decode())
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "ramen_console.app:create_app", "--factory", "--port", str(PORT)],
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
