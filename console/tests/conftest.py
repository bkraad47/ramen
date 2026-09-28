import os

os.environ.setdefault("RAMEN_GCP_FRESH_HTTP", "0")  # fakes never need a real transport; avoids ADC probing

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("RAMEN_FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("RAMEN_SESSION_SECRET", "test-session-secret")


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
