"""R4 (0.7.0): the console ships the readable htmx build, and VENDOR.md says exactly which bytes."""

import hashlib
import re
from pathlib import Path

from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - pytest fixtures

STATIC = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/static")


def test_htmx_is_the_unminified_2_0_4_build_and_vendor_md_matches():
    js = (STATIC / "htmx.js").read_bytes()
    assert not (STATIC / "htmx.min.js").exists()
    assert b"version: '2.0.4'" in js and js.count(b"\n") > 3000  # readable, not a one-liner
    row = next(x for x in (STATIC / "VENDOR.md").read_text().splitlines() if x.startswith("| `htmx.js`"))
    assert "| 2.0.4 |" in row and "github.com/bigskysoftware/htmx/releases/download/v2.0.4/htmx.js" in row
    assert re.search(r"`([0-9a-f]{64})`", row).group(1) == hashlib.sha256(js).hexdigest()


def test_pages_load_the_readable_build_and_the_json_extension_still_works(demo):
    page = demo.get("/config").text
    assert '/static/htmx.js?v=' in page and "htmx.min.js" not in page
    assert demo.get("/static/htmx.js").status_code == 200
    assert demo.get("/static/htmx.min.js").status_code == 404
    assert "htmx.defineExtension('json'" in page  # base.html's extension rides on the public htmx API
    # the extension's hooks exist in this build under the names base.html uses
    js = (STATIC / "htmx.js").read_text()
    for name in ("defineExtension", "getExpressionVars", "htmx:configRequest"):
        assert name in js, name
