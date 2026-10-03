"""The guides the README promises exist, are in the docs nav, and are linked from the README (0.5.3; slugs of the
0.6.0 docs reorganization). Site links in README / llms.txt / the landing page have a page behind them."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDES = {
    "get-started": "Get started",
    "wiki/mcp-repo": "The MCP repo",
    "wiki/connect-oauth": "Connect a client with OAuth",
}


def test_every_promised_guide_exists_is_in_the_nav_and_is_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text()
    nav = (ROOT / "mkdocs.yml").read_text()
    for slug, title in GUIDES.items():
        assert (ROOT / "docs" / f"{slug}.md").exists(), slug
        assert f"{slug}.md" in nav, f"{slug} not in the mkdocs nav"
        assert f"https://bkraad47.github.io/ramen/{slug}/" in readme, f"README does not link {slug}"
        assert title in readme, f"README does not name the guide {title!r}"


def _links():
    spec = importlib.util.spec_from_file_location("check_docs_links", ROOT / "scripts" / "check_docs_links.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_site_link_in_readme_llms_and_landing_has_a_page():
    assert _links().check(ROOT) == []


def test_a_link_to_a_deleted_page_is_caught():
    site, docs = "https://bkraad47.github.io/ramen/", ROOT / "docs"
    m = _links()
    for gone in ("features/", "related-versions/", "wiki/concepts/", "how-tos/local-quickstart/"):
        assert not m.site_page(docs, site + gone), gone
    for live in ("", "wiki/", "wiki/connect-oauth/", "CONTRACTS/", "llms.txt", "img/dashboard.png"):
        assert m.site_page(docs, site + live), live
