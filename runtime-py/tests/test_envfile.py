"""0.6.0: mcp/env.yaml (or .env) rendered from secrets into the runtime's environment at load."""

import json
import os

import pytest

from ramen_runtime import envfile, secrets
from ramen_runtime.rpc import Server as Runtime


def write(tmp_path, name, text):
    (tmp_path / "mcp").mkdir(exist_ok=True)
    (tmp_path / "mcp" / name).write_text(text)
    return tmp_path


def test_parse_yaml_and_dotenv_shapes(tmp_path):
    p = write(
        tmp_path, "env.yaml", "# comment\nDB_URL: \"postgres://x\"  # trailing\nMODE: demo\nEMPTY:\nQUOTED: 'a: b'\n"
    )
    assert envfile.parse(p / "mcp" / "env.yaml") == {
        "DB_URL": "postgres://x",
        "MODE": "demo",
        "EMPTY": "",
        "QUOTED": "a: b",
    }
    p = write(tmp_path, ".env", "export TOKEN=abc\nURL=http://h:1/p?q=1\n")
    assert envfile.parse(p / "mcp" / ".env") == {"TOKEN": "abc", "URL": "http://h:1/p?q=1"}
    with pytest.raises(ValueError, match="not an environment variable name"):
        envfile.parse(write(tmp_path, "env.yml", "bad-key: 1\n") / "mcp" / "env.yml")
    with pytest.raises(ValueError, match="expected KEY"):
        envfile.parse(write(tmp_path, "env.yml", "nocolon\n") / "mcp" / "env.yml")
    assert envfile.find(tmp_path).name == "env.yaml"  # env.yaml wins over .env when both exist


def test_apply_renders_secrets_skips_missing_and_exports(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(secrets.env_name("demo", "DB_PASS"), "s3cret")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)
    write(
        tmp_path,
        "env.yaml",
        "DEMO_MODE: demo\nDB_URL: postgres://u:{{$demo.DB_PASS}}@db/app\nAPI_KEY: {{$demo.NOPE}}\n",
    )
    got = envfile.apply(tmp_path)
    assert got == {"DEMO_MODE": "demo", "DB_URL": "postgres://u:s3cret@db/app"}
    assert os.environ["DB_URL"] == "postgres://u:s3cret@db/app" and os.environ["DEMO_MODE"] == "demo"
    assert "API_KEY" not in os.environ
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert any(
        x.get("msg") == "envfile" and x.get("key") == "API_KEY" and "NOPE" in x.get("skipped", "") for x in lines
    )
    assert "s3cret" not in capsys.readouterr().err
    assert envfile.rendered_values() == ["postgres://u:s3cret@db/app"]  # literals are not secrets
    assert secrets.redact("failed: postgres://u:s3cret@db/app", envfile.rendered_values()) == "failed: ***"
    assert envfile.apply(tmp_path / "nowhere") == {} and envfile.rendered_values() == []


def test_runtime_load_applies_the_file_and_lists_its_keys(tmp_path, monkeypatch):
    monkeypatch.setenv(secrets.env_name("demo", "TOKEN"), "tok-123")
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    write(tmp_path, "env.yaml", "DEMO_TOKEN: {{$demo.TOKEN}}\n")
    (tmp_path / "mcp" / "tools" / "echo_env").mkdir(parents=True)
    (tmp_path / "mcp" / "tools" / "echo_env" / "echo_env.py").write_text(
        "import os\n\ndef run():\n    return os.environ['DEMO_TOKEN']\n"
    )
    (tmp_path / "mcp" / "tools" / "echo_env" / "echo_env.json").write_text(
        json.dumps(
            {
                "type": "tool",
                "name": "echo_env",
                "description": "the rendered DEMO_TOKEN",
                "callable": "run",
                "input": {},
                "output": {"type": "string"},
                "error": {"type": "string"},
            }
        )
    )
    rt = Runtime(tmp_path)
    d = rt.load({})
    assert d["env"] == ["DEMO_TOKEN"] and [t["name"] for t in d["tools"]] == ["echo_env"]
    r = rt.call_tool({"name": "echo_env", "arguments": {}})
    assert r["content"][0]["text"] == "tok-123"
