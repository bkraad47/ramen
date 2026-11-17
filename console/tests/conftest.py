import os

os.environ.setdefault("RAMEN_GCP_FRESH_HTTP", "0")  # fakes never need a real transport; avoids ADC probing

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("RAMEN_FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("RAMEN_SESSION_SECRET", "test-session-secret")


@pytest.fixture(autouse=True)
def _otel_no_retry_wait(monkeypatch):
    """The deploy span's retries (b35) wait up to ~95 s for a restarting collector; tests that want them set delays."""
    from ramen_console import otel

    monkeypatch.setattr(otel, "RETRY_DELAYS", ())


@pytest.fixture
def fernet_key():
    return os.environ["RAMEN_FERNET_KEY"]


# CSRF (CONTRACTS §9): cookie-authenticated /api mutations need X-Ramen-CSRF = ramen_csrf cookie. Browsers get it from
# base.html; the test client mirrors that so the existing API tests stay browser-faithful. test_csrf.py opts out.
import fastapi.testclient as _tc

_orig_init = _tc.TestClient.__init__


def _csrf_init(self, *a, **kw):
    _orig_init(self, *a, **kw)

    def hook(request):
        t = self.cookies.get("ramen_csrf")
        if t and "X-Ramen-CSRF" not in request.headers:
            request.headers["X-Ramen-CSRF"] = t

    self.event_hooks = {"request": [hook]}


_tc.TestClient.__init__ = _csrf_init


APP_HTML = '<!doctype html><html><head><title>hi</title></head><body><script>document.cookie</script>"&</body></html>'
APP = {
    "uri": "ui://demo/hello",
    "name": "hello",
    "mimeType": "text/html;profile=mcp-app",
    "_meta": {"ui/csp": {"default-src": ["'none'"], "script-src": ["'unsafe-inline'"], "img-src": ["data:"]}},
}
APP_CSP = "default-src 'none'; script-src 'unsafe-inline'; img-src data:"  # APP["_meta"]["ui/csp"] rendered
SKILL_MD = {"uri": "skill://noodle-math/SKILL.md", "name": "noodle-math", "mimeType": "text/markdown"}
SKILL = {"name": "noodle-math", "description": "adds noodles", "version": "a1b2c3d4e5f6", "files": ["SKILL.md"]}
LOAD = {
    "tools": [{"name": "calc"}],
    "resources": [APP, SKILL_MD],
    "prompts": [],
    "errors": [],
    "apps": [{"name": "hello", "uri": APP["uri"], "title": "Hello", "description": "says hi"}],
    "skills": [SKILL],
}


@pytest.fixture
def workers():
    """Two in-process gRPC workers for the local adapter fixtures: `cold` (load low) and `hot` (load high)."""
    from tests.fake_grpc import FakeWorker

    cold, hot = FakeWorker(admin_key="adm").start(), FakeWorker(admin_key="adm").start()
    cold.metrics = {"inflight": 1, "total": 5, "errors": 0, "load": "low"}
    hot.metrics = {"inflight": 4, "total": 5, "errors": 0, "load": "high"}
    # §33.1/§33.2: every fake zone serves one MCP app and one skill next to its tool
    for w in (cold, hot):
        w.load_result = dict(LOAD)
        w.resources = {
            APP["uri"]: {"mimeType": APP["mimeType"], "text": APP_HTML, "_meta": APP["_meta"]},
            SKILL_MD["uri"]: {"mimeType": "text/markdown", "text": "---\nname: noodle-math\ndescription: d\n---\n"},
        }
    yield {"cold": cold, "hot": hot}
    cold.stop()
    hot.stop()
