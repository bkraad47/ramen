import sys

from ramen_runtime import deps


def test_no_requirements_is_noop(tmp_path):
    assert deps.install(tmp_path) == {"installed": False, "reason": "no requirements"}


def test_comment_only_requirements_is_noop(demo_bucket):
    assert deps.install(demo_bucket)["installed"] is False


def test_installs_and_is_idempotent(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "requirements.txt").write_text("# c\nrequests==2.32.3\n")
    monkeypatch.setattr(deps.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    assert deps.install(tmp_path) == {"installed": True, "packages": 1}
    assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]
    assert deps.install(tmp_path) == {"installed": False, "reason": "unchanged"}
    (tmp_path / "mcp" / "requirements.txt").write_text("requests==2.32.4\n")
    assert deps.install(tmp_path)["installed"] is True and len(calls) == 2


def test_install_failure_raises(tmp_path, monkeypatch):
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "requirements.txt").write_text("nonexistent-pkg-xyz\n")
    monkeypatch.setattr(deps.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "ERROR: no match"})())
    try:
        deps.install(tmp_path)
    except deps.DepsError as e:
        assert "no match" in str(e)
    else:
        raise AssertionError
