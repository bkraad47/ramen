"""CONTRACTS §23.7: the official `@modelcontextprotocol/conformance` suite, both wires, against a real worker.

Not in the default `testpaths` (it downloads the suite with npx): `uv run pytest conformance_suite`. Skips when
node >= 20.18.1 / npx, the node binary or the runtime is missing. `run.py` does the work; this asserts its verdict and
that the baselines are honest (every entry has a reason, nothing is excused on the modern wire without one)."""

import re
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run  # noqa: E402

ENTRY = re.compile(r"^\s*-\s+(\S+)\s+# reason: \S.{10,}$")


@pytest.mark.parametrize("wire", sorted(run.WIRES))
def test_every_baseline_entry_has_a_reason(wire):
    text = run.baseline(wire).read_text()
    entries = (yaml.safe_load(text) or {}).get("server") or []
    lines = [x for x in text.splitlines() if x.strip().startswith("- ")]
    assert len(lines) == len(entries), "one entry per line"
    for line, entry in zip(lines, entries, strict=True):
        m = ENTRY.match(line)
        assert m and m.group(1) == entry, f"{wire}: `{line.strip()}` needs `# reason: <why Ramen does not do this>`"
    assert set(yaml.safe_load(text) or {}) <= {"server"}, "server mode only"


def test_the_suite_version_is_pinned():
    assert re.fullmatch(r"\d+\.\d+\.\d+(-alpha\.\d+)?", run.VERSION), run.VERSION


@pytest.mark.parametrize("wire", sorted(run.WIRES))
def test_official_suite(wire):
    if why := run.missing():
        pytest.skip(why)
    code = run.main(["--wire", wire])
    log = (run.RESULTS / f"{wire}.log").read_text()
    assert code == 0, f"official suite, wire {wire}: unexpected failures or stale baseline entries\n{log[-6000:]}"
