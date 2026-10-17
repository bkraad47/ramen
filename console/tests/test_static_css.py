"""The identity pill in the sidebar: every role gets a visible background, not only the four built-in ones — a
custom role (analyst, analyst_sso, anything from the custom roles API) used to render white on white."""

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "src" / "ramen_console" / "static" / "ramen.css"


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", CSS.read_text())
    assert m, f"no rule for {selector}"
    return m.group(1)


def test_role_pill_has_a_default_background_for_custom_roles():
    base = _rule(".role")
    assert "color:#fff" in base
    assert re.search(r"background:\s*var\(--role-custom\)", base), "custom roles need a visible pill background"
    assert "--role-custom:" in _rule(":root")


def test_builtin_role_pills_keep_their_colours():
    css = CSS.read_text()
    for cls in ("super-admin", "group-admin", "viewer", "mcp-user"):
        assert re.search(rf"\.role\.{cls}\{{background:var\(--role-{cls}\)\}}", css)
