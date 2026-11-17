"""CONTRACTS §24.2 (v1.0.0), runtime half: a group image built on deploy carries `RAMEN_DEPS_BAKED` = the sha256 of the
`mcp/requirements.txt` it installed. A runtime started with that value and the same file skips `pip install` (log
`deps baked`); any other value installs as before. Real sidecar process; `pip` is a recording stand-in on the
interpreter's path, so "installed" and "skipped" are facts about a subprocess, not about a mock."""

import hashlib
import json
import shutil

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.sidecar import Sidecar, runtime_importable

pytestmark = pytest.mark.conformance
REQUIREMENTS = "# pinned by the group\nramen-test-package==1.2.3\n"
IGNORE = shutil.ignore_patterns("__pycache__", ".ramen_requirements.sha256")


@pytest.fixture
def world(tmp_path):
    """A bucket with a requirements file + a `pip` package that records its argv instead of installing."""
    if not runtime_importable():
        pytest.skip("ramen_runtime not importable (uv sync runtime-py or RAMEN_RUNTIME_PYTHON)")
    bucket = tmp_path / "bucket"
    shutil.copytree(E.FIXTURES / "demo_group", bucket, ignore=IGNORE)
    (bucket / "mcp" / "requirements.txt").write_text(REQUIREMENTS)
    calls = tmp_path / "pip-calls.jsonl"
    fake = tmp_path / "fakepip" / "pip"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    (fake / "__main__.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['RAMEN_TEST_PIP_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    env = {"PYTHONPATH": str(fake.parent), "RAMEN_TEST_PIP_LOG": str(calls)}
    return bucket, env, calls


def digest(bucket) -> str:
    return hashlib.sha256((bucket / "mcp" / "requirements.txt").read_bytes()).hexdigest()


def pip_calls(path) -> list[list[str]]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def load(bucket, env) -> tuple[dict, str]:
    with Sidecar(bucket, env) as s:
        result = s.call("runtime.load", {"bucket": str(bucket)}, timeout=120)
        return result, s.stderr


def test_the_stand_in_pip_is_what_the_runtime_runs(world):
    """Without RAMEN_DEPS_BAKED the runtime installs: the control every other case is read against."""
    bucket, env, calls = world
    result, _ = load(bucket, env)
    assert result["errors"] == [], result
    ran = pip_calls(calls)
    assert len(ran) == 1 and "install" in ran[0] and str(bucket / "mcp" / "requirements.txt") in ran[0], ran


def test_a_matching_digest_skips_pip_and_says_deps_baked(world):
    bucket, env, calls = world
    result, stderr = load(bucket, {**env, "RAMEN_DEPS_BAKED": digest(bucket)})
    assert result["errors"] == [], result
    assert pip_calls(calls) == [], "the image already holds these packages"
    assert "deps baked" in stderr, stderr[-600:]
    assert {t["name"] for t in result["tools"]} >= {"demo_calculator_tool"}, "the group still loads"


def test_the_digest_is_matched_case_insensitively_and_trimmed(world):
    bucket, env, calls = world
    load(bucket, {**env, "RAMEN_DEPS_BAKED": f" {digest(bucket).upper()} "})
    assert pip_calls(calls) == []


@pytest.mark.parametrize("baked", ["0" * 64, "baked", "1", "true"], ids=["other-digest", "word", "one", "true"])
def test_any_other_value_installs_as_before(world, baked):
    bucket, env, calls = world
    result, stderr = load(bucket, {**env, "RAMEN_DEPS_BAKED": baked})
    assert result["errors"] == [], result
    assert len(pip_calls(calls)) == 1, "a digest of another file is not this file"
    assert "deps baked" not in stderr


def test_a_changed_requirements_file_is_installed_even_in_a_baked_image(world):
    """The image was built from the previous file: a deploy that edits it must not run on stale packages."""
    bucket, env, calls = world
    baked = digest(bucket)
    (bucket / "mcp" / "requirements.txt").write_text(REQUIREMENTS + "another-package==2.0\n")
    load(bucket, {**env, "RAMEN_DEPS_BAKED": baked})
    assert len(pip_calls(calls)) == 1


def test_the_digest_covers_the_raw_bytes_not_the_parsed_package_list(world):
    """§24.2 says sha256 of requirements.txt: a comment-only edit changes it, so the image is rebuilt by the console
    (new tag) and this pod — still on the old image — installs."""
    bucket, env, calls = world
    baked = digest(bucket)
    (bucket / "mcp" / "requirements.txt").write_text(REQUIREMENTS.replace("pinned", "chosen"))
    load(bucket, {**env, "RAMEN_DEPS_BAKED": baked})
    assert len(pip_calls(calls)) == 1


def test_no_requirements_file_is_unaffected(world):
    bucket, env, calls = world
    (bucket / "mcp" / "requirements.txt").unlink()
    result, stderr = load(bucket, {**env, "RAMEN_DEPS_BAKED": "0" * 64})
    assert result["errors"] == [] and pip_calls(calls) == [] and "deps baked" not in stderr


def test_a_real_node_serves_a_baked_group_without_running_pip(world):
    """The same through `ramen-node`: the variable set on the pod reaches the runtime the node starts."""
    bucket, env, calls = world
    with LocalNode(bucket=bucket, env={**env, "RAMEN_DEPS_BAKED": digest(bucket)}) as n:
        n.wait_serving()
        assert n.node.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})["isError"] is False
        assert pip_calls(calls) == []
        assert "deps baked" in n.output() or any("deps baked" in json.dumps(x) for x in n.log_lines()), n.output()
