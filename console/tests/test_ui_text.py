"""U1, U2, U6, U10, U11 / CONTRACTS §12.1: what the console calls things, and how the chrome is laid out."""

import re
from pathlib import Path

import pytest

from ramen_console.deploy import LOADS, cell_color, cell_load
from ramen_console.rbac import ROLE_LABELS, role_label
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

TEMPLATES = sorted(Path(__file__).resolve().parents[1].joinpath("src/ramen_console/templates").rglob("*.html"))
PAGES = (
    "/",
    "/groups",
    "/groups/demo",
    "/environments",
    "/zones",
    "/secrets",
    "/users",
    "/api-keys",
    "/logs",
    "/audit",
    "/backups",
    "/config",
)
BUTTON = re.compile(r"<button[^>]*>(.*?)</button>|<a[^>]+class=\"[^\"]*btn[^\"]*\"[^>]*>(.*?)</a>", re.S)
HEADING = re.compile(r"<h([12])[^>]*>(.*?)</h\1>", re.S)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def labels(html: str) -> list[str]:
    out = [text_of(a or b) for a, b in BUTTON.findall(html)]
    out += [text_of(t) for _, t in HEADING.findall(html)]
    return [s for s in out if s]


# --- U1: the product name appears once, in the logo --------------------------
def test_no_page_title_repeats_the_product_name(demo):
    for path in PAGES:
        title = re.search(r"<title>(.*?)</title>", demo.get(path).text).group(1)
        assert "Ramen" not in title, (path, title)


def test_the_sidebar_has_no_wordmark(demo):
    html = demo.get("/").text
    assert 'class="brand"' not in html
    assert ">RAMEN<" not in html
    assert 'alt="Ramen logo"' in html or "logo.png" in html  # the logo itself still says it


def test_the_login_heading_drops_the_product_name(client):
    html = client.get("/login").text
    assert "Ramen console" not in html
    assert "<title>Sign in</title>" in html
    assert "logo.png" in html


def test_the_reset_page_drops_it_too(client):
    assert "Ramen console" not in client.get("/auth/reset").text


# --- U10: sentence case everywhere ------------------------------------------
@pytest.mark.parametrize("path", PAGES)
def test_every_button_and_heading_starts_with_a_capital(demo, path):
    bad = [s for s in labels(demo.get(path).text) if s[:1].isalpha() and not s[:1].isupper()]
    assert bad == [], (path, bad)


def test_the_login_page_too(client):
    bad = [s for s in labels(client.get("/login").text) if s[:1].isalpha() and not s[:1].isupper()]
    assert bad == []


def test_no_template_writes_a_lowercase_button(demo):
    """A static sweep, so a button behind a role or state the tests do not reach is still covered."""
    bad = []
    for f in TEMPLATES:
        for m in re.finditer(r"<button[^>]*>([^<{]+)</button>", f.read_text()):
            label = m.group(1).strip()
            if label[:1].isalpha() and not label[:1].isupper():
                bad.append(f"{f.name}: {label}")
    assert bad == []


# --- U2: "service account" is spelled out ------------------------------------
def test_no_page_abbreviates_service_account(demo):
    for path in PAGES:
        body = text_of(demo.get(path).text)
        assert not re.search(r"\bSA\b", body), path
        assert "sa_permissions" not in body


def test_no_template_says_sa(demo):
    for f in TEMPLATES:
        assert not re.search(r">\s*(Create|create) SA\s*<", f.read_text()), f.name


# --- U7: "Generate key", never "Mint key" ------------------------------------
def test_the_api_keys_page_says_generate_key(demo):
    html = demo.get("/api-keys").text
    assert "Generate key" in html
    assert "Mint" not in html


def test_the_group_page_key_form_says_generate_key(demo):
    html = demo.get("/groups/demo").text
    assert "Generate key" in html
    assert "Mint" not in html


# --- U6: the sidebar identity block ------------------------------------------
def test_role_labels_are_written_for_people():
    assert ROLE_LABELS == {"super_admin": "Super Admin", "group_admin": "Group Admin", "viewer": "Viewer"}
    assert role_label("viewer") == "Viewer"
    assert role_label("something_else") == "Something Else"


def test_the_sidebar_shows_the_email_and_role_side_by_side(demo):
    html = demo.get("/").text
    block = re.search(r'<div class="who">.*?</div>\s*</nav>', html, re.S).group(0)
    assert "root@ramen.local" in block
    assert "Super Admin" in block and "super_admin" not in block
    assert 'class="identity"' in block  # the row that holds both


def test_each_role_has_its_own_colour_token():
    css = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/static/ramen.css").read_text()
    root = re.search(r":root\{(.*?)\}", css, re.S).group(1)
    tokens = {f"--role-{r.replace('_', '-')}" for r in ROLE_LABELS}
    assert tokens <= set(re.findall(r"--[a-z-]+", root))
    assert len({re.search(rf"{t}:\s*(#[0-9A-Fa-f]+)", css).group(1).lower() for t in tokens}) == 3


def test_log_out_sits_below_the_identity_row_and_is_red(demo):
    html = demo.get("/").text
    block = re.search(r'<div class="who">(.*?)</div>\s*</nav>', html, re.S).group(1)
    assert block.index("identity") < block.index("Log out")
    assert 'class="logout"' in block
    css = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/static/ramen.css").read_text()
    assert re.search(r"\.logout\{[^}]*color:var\(--red\)", css)


def test_a_group_admin_sees_their_own_role_written_out(demo, app):  # noqa: F811
    from fastapi.testclient import TestClient

    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(app) as ga:
        login(ga, "ga@x", PW)
        assert "Group Admin" in ga.get("/").text


# --- U2: one Actions section per zone ----------------------------------------
def test_each_zone_has_one_actions_section_in_a_fixed_order(demo):
    html = demo.get("/groups/demo").text
    assert html.count('class="zone-actions"') == 2  # zone-a and zone-b
    section = re.search(r'<div class="zone-actions">(.*?)</div>\s*</td>', html, re.S).group(1)
    order = [m for m in re.findall(r">(Scale workers|IP rules|Create service account|Rebalance|View logs)<", section)]
    assert order == ["Scale workers", "IP rules", "Create service account", "Rebalance", "View logs"]


def test_the_action_buttons_share_one_width_class(demo):
    html = demo.get("/groups/demo").text
    section = re.search(r'<div class="zone-actions">(.*?)</div>\s*</td>', html, re.S).group(1)
    controls = re.findall(r"<(?:button|a)[^>]*class=\"([^\"]*)\"[^>]*>", section)
    assert controls and all("act" in c for c in controls)
    css = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/static/ramen.css").read_text()
    assert re.search(r"\.act\{[^}]*width:", css)


def test_a_viewer_gets_no_action_buttons(demo, app):  # noqa: F811
    from fastapi.testclient import TestClient

    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(app) as v:
        login(v, "v@x", PW)
        assert "zone-actions" not in v.get("/groups/demo").text


# --- U11: the dashboard never names a colour ---------------------------------
def test_load_names_replace_colour_names():
    assert LOADS == ("low", "even", "high", "down")
    assert cell_load([]) == "down"
    assert cell_load([{"load": "high"}, {"load": "low"}]) == "high"
    assert cell_load([{"load": "down"}]) == "down"
    assert cell_load([{"load": "low"}, {"load": "down"}]) == "low"
    assert cell_load([{"load": "low"}, {"load": "even"}]) == "even"


def test_the_colour_field_still_exists_for_api_clients():
    assert cell_color([{"load": "high"}]) == "red"
    assert cell_color([]) == "grey"


def test_the_dashboard_text_never_names_a_colour(demo):
    for html in (demo.get("/").text, demo.get("/ui/dashboard").text):
        body = text_of(html).lower()
        for colour in ("blue", "green", "red", "grey", "gray"):
            assert not re.search(rf"\b{colour}\b", body), (colour, body[:300])


def test_every_dashboard_cell_keeps_a_text_label(demo):
    html = demo.get("/ui/dashboard").text
    cells = re.findall(r'<div class="cell[^"]*">(.*?)</div>', html, re.S)
    assert cells
    for c in cells:
        assert text_of(c), html


def test_the_legend_names_the_loads(demo):
    legend = text_of(re.search(r'<div class="legend">(.*?)</div>', demo.get("/").text, re.S).group(1))
    for word in ("Low", "Even", "High", "Down"):
        assert word in legend
