"""The guides the README promises exist, are in the docs nav, and are linked from the README (0.5.3; slugs of the 0.6.0 docs reorganization)."""

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
