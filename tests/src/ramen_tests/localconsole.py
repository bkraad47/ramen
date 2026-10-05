"""Launch a real console (uvicorn, memory store, local cloud adapter) on a free port, so the OAuth 2.1 server
(§16.3) can be driven end to end against a `LocalNode` that shares its session secret. Skips unless the console
venv exists: RAMEN_CONSOLE_PYTHON, else ../console/.venv.

`RAMEN_LOCAL_SESSION_SECRET` pins the group secret the console mints tokens with (the compose stack does the same),
so a node started with `RAMEN_SESSION_SECRET` = that value and `RAMEN_OAUTH_ISSUER` = this console's URL verifies
every token it issues."""

import base64
import hashlib
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from . import env as E
from .console import Console
from .localnode import free_port
from .sidecar import venv_bin

OAUTH_SERVER = E.RAMEN_DIR / "console" / "src" / "ramen_console" / "oauth_server.py"
REDIRECT = "http://127.0.0.1:9999/callback"


def console_python() -> str | None:
    p = E.env("RAMEN_CONSOLE_PYTHON")
    if p:
        return p
    found = venv_bin(E.RAMEN_DIR / "console" / ".venv", "python")
    return str(found) if found else None


def require_console() -> str:
    py = console_python()
    if not py:
        pytest.skip("console venv not found (uv sync in console, or RAMEN_CONSOLE_PYTHON)")
    try:
        r = subprocess.run([py, "-c", "import ramen_console, uvicorn"], capture_output=True, timeout=60)
    except OSError, subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0:
        pytest.skip("ramen_console/uvicorn not importable from the console venv")
    return py


def supports_access_ttl() -> bool:
    """C8 (ui-agent): `oauth_server.py` reads RAMEN_OAUTH_ACCESS_TTL. Until it does, a short-TTL run is xfail."""
    return OAUTH_SERVER.exists() and "RAMEN_OAUTH_ACCESS_TTL" in OAUTH_SERVER.read_text()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


class LocalConsole:
    """Context manager: a console process; `.url`, `.admin` (logged-in super admin), OAuth helpers."""

    def __init__(
        self,
        env: dict | None = None,
        session_secret: str = "conformance-secret",
        admin: tuple[str, str] = ("admin@ramen.local", "ramen-admin"),
    ):
        self.python = require_console()
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp(prefix="ramen-console-"))
        self.url = f"http://127.0.0.1:{self.port}"
        self.creds, self.session_secret = admin, session_secret
        self.env = {
            **os.environ,
            "RAMEN_STORE": "memory",
            "RAMEN_CLOUD": "local",
            "RAMEN_BUCKET_ROOT": str(self.dir / "buckets"),
            "RAMEN_LOG_ROOT": str(self.dir / "logs"),
            "RAMEN_ADMIN_EMAIL": admin[0],
            "RAMEN_ADMIN_PASSWORD": admin[1],
            "RAMEN_PUBLIC_URL": self.url,  # the OAuth issuer (§16.3) = what the node's RAMEN_OAUTH_ISSUER must equal
            "RAMEN_LOCAL_SESSION_SECRET": session_secret,
            "RAMEN_SESSION_SECRET": "console-cookie-signing",
            "RAMEN_SMTP_HOST": f"file://{self.dir / 'mail'}",
            "RAMEN_LOGIN_RATE_LIMIT": "0",
            "RAMEN_LOG_LEVEL": "WARNING",
        }
        for k in (
            "RAMEN_TLS",
            "RAMEN_COOKIE_SECURE",
            "RAMEN_FERNET_KEY",
            "RAMEN_CONFIG",
            "RAMEN_DOTENV",
            "RAMEN_WORKER_URL",
            "RAMEN_OAUTH_ACCESS_TTL",
            "RAMEN_OAUTH_REFRESH_TTL",
            "FIRESTORE_EMULATOR_HOST",
        ):
            self.env.pop(k, None)
        self.env.update(env or {})
        self.proc: subprocess.Popen | None = None
        self.stdout = self.dir / "stdout.log"
        self.admin: Console | None = None

    def __enter__(self):
        cmd = [self.python, "-m", "uvicorn", "ramen_console.app:app", "--host", "127.0.0.1", "--port", str(self.port)]
        self.proc = subprocess.Popen(  # noqa: S603
            cmd, env=self.env, stdout=open(self.stdout, "ab"), stderr=subprocess.STDOUT, cwd=self.dir
        )
        self.wait_ready()
        self.admin = Console(self.url)
        r = self.admin.login(*self.creds)
        assert r.status_code in (200, 303), f"bootstrap login failed: {r.status_code} {r.text[:300]}"
        return self

    def __exit__(self, *_):
        if self.admin:
            self.admin.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def output(self) -> str:
        return self.stdout.read_text(errors="replace")[-4000:] if self.stdout.exists() else ""

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise AssertionError(f"console exited {self.proc.returncode}:\n{self.output()}")
            try:
                if httpx.get(f"{self.url}/healthz", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise AssertionError(f"console not ready after {timeout}s:\n{self.output()}")

    # --- world ---------------------------------------------------------------------------------------------------
    def seed_group(self, group: str = "demo", zone: str = "local", env: str = "dev") -> None:
        """What a token's scope `mcp:<group>:<zone>` needs on the console side: the zone, the group, an environment
        binding them (nothing is deployed — the node under test is a LocalNode)."""
        a = self.admin
        assert a.create_zone(zone, provider="local", region="local").status_code in (201, 409)
        assert a.create_group(group, E.DEMO_REPO).status_code in (201, 409)
        assert a.create_environment(group, env, [zone]).status_code in (201, 409)

    # --- OAuth 2.1 (§16.3) -------------------------------------------------------------------------------------
    def register_client(self, name: str = "ramen-tests", redirect_uris: list[str] | None = None) -> str:
        r = self.admin.http.post(
            f"{self.url}/api/v1/oauth/clients", json={"name": name, "redirect_uris": redirect_uris or [REDIRECT]}
        )
        assert r.status_code == 201, r.text
        return r.json()["client_id"]

    def mint(self, client_id: str, group: str = "demo", zone: str = "local", redirect: str = REDIRECT) -> dict:
        """The full authorization-code + PKCE flow as a signed-in user (the console's admin): consent → code →
        `/oauth/token` → `{access_token, refresh_token, expires_in, ...}`."""
        verifier, challenge = pkce()
        scope = f"mcp:{group}:{zone}"
        q = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": scope,
            "state": "s1",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": scope,
        }
        a = self.admin
        page = a.http.get(f"{self.url}/oauth/authorize", params=q, follow_redirects=False)
        assert page.status_code == 200, f"consent page: {page.status_code} {page.text[:300]}"
        r = a.http.post(
            f"{self.url}/oauth/authorize",
            data={**q, "decision": "allow", "csrf_token": a.http.cookies.get("ramen_csrf", "")},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        return self.exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_verifier": verifier,
            }
        )

    def token_response(self, form: dict) -> httpx.Response:
        return httpx.post(f"{self.url}/oauth/token", data=form, timeout=30)

    def exchange(self, form: dict) -> dict:
        r = self.token_response(form)
        assert r.status_code == 200, f"/oauth/token {r.status_code}: {r.text[:300]}"
        return r.json()

    def refresh(self, refresh_token: str, client_id: str) -> dict:
        return self.exchange({"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id})
