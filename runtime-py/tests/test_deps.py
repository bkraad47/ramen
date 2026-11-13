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
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    assert deps.install(tmp_path) == {"installed": True, "packages": 1}
    assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]
    assert deps.install(tmp_path) == {"installed": False, "reason": "unchanged"}
    (tmp_path / "mcp" / "requirements.txt").write_text("requests==2.32.4\n")
    assert deps.install(tmp_path)["installed"] is True and len(calls) == 2


def test_install_failure_raises(tmp_path, monkeypatch):
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "requirements.txt").write_text("nonexistent-pkg-xyz\n")
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda cmd, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "ERROR: no match"})(),
    )
    try:
        deps.install(tmp_path)
    except deps.DepsError as e:
        assert "no match" in str(e)
    else:
        raise AssertionError


def _fail_pip(cmd, **kw):
    raise AssertionError("pip must not run")


def test_baked_requirements_skip_the_install(tmp_path, monkeypatch, capsys):
    """§24.2: the image was built from exactly this file (`RAMEN_DEPS_BAKED` = sha256 of its bytes)."""
    import hashlib

    (tmp_path / "mcp").mkdir()
    req = tmp_path / "mcp" / "requirements.txt"
    req.write_bytes(b"# c\nrequests==2.32.3\n")
    monkeypatch.setenv("RAMEN_DEPS_BAKED", hashlib.sha256(req.read_bytes()).hexdigest().upper() + " ")
    monkeypatch.setattr(deps.subprocess, "run", _fail_pip)
    assert deps.install(tmp_path) == {"installed": False, "reason": "baked"}
    assert "deps baked" in capsys.readouterr().err
    assert not (tmp_path / "mcp" / ".ramen_requirements.sha256").exists()


def test_a_stale_or_empty_baked_hash_installs_as_before(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "requirements.txt").write_text("requests==2.32.4\n")
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setenv("RAMEN_DEPS_BAKED", "0" * 64)  # the image was built from another requirements.txt
    assert deps.install(tmp_path) == {"installed": True, "packages": 1}
    (tmp_path / "mcp" / ".ramen_requirements.sha256").unlink()
    monkeypatch.setenv("RAMEN_DEPS_BAKED", "")
    assert deps.install(tmp_path) == {"installed": True, "packages": 1} and len(calls) == 2
