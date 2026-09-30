"""v0.5.5 I17: the public skills/ package (agentskills.io format) stays structurally valid, and the two
contribution skills (test/, iterate/) exist alongside facts/STATE.md, the scratchpad they both read first."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def test_every_skill_has_valid_frontmatter_matching_its_folder():
    folders = [p for p in SKILLS.iterdir() if p.is_dir()]
    assert folders, "no skill folders found"
    for folder in folders:
        skill_md = folder / "SKILL.md"
        assert skill_md.exists(), f"{folder.name} has no SKILL.md"
        m = FRONTMATTER.match(skill_md.read_text())
        assert m, f"{folder.name}/SKILL.md has no --- frontmatter block"
        fm = m.group(1)
        name_match = re.search(r"^name:\s*(\S+)", fm, re.M)
        assert name_match, f"{folder.name}/SKILL.md frontmatter has no name"
        assert name_match.group(1) == folder.name, f"{folder.name}/SKILL.md name does not match its folder"
        assert re.search(r"^description:\s*\S+", fm, re.M), f"{folder.name}/SKILL.md frontmatter has no description"


def test_contribution_skills_exist_and_read_the_state_snapshot():
    for name in ("test", "iterate"):
        text = (SKILLS / name / "SKILL.md").read_text()
        assert "facts/STATE.md" in text, f"skills/{name}/SKILL.md does not point at the scratchpad"
    assert (ROOT / "facts" / "STATE.md").exists()


def test_skills_readme_lists_every_skill():
    readme = (SKILLS / "README.md").read_text()
    for folder in SKILLS.iterdir():
        if folder.is_dir():
            assert f"`{folder.name}/`" in readme, f"skills/README.md does not list {folder.name}/"
