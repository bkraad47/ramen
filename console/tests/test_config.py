import os

from ramen_console.config import apply_config, flatten, load_yaml


def test_flatten():
    assert flatten({"console_port": 9, "RAMEN_STORE": "memory", "oauth": {"oidc": {"client_id": "a"}}}) == {
        "RAMEN_CONSOLE_PORT": "9", "RAMEN_STORE": "memory", "RAMEN_OAUTH_OIDC_CLIENT_ID": "a"}


def test_load_and_apply(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("store: memory\nverbose: true\nRAMEN_LOG_ROOT: /x\n")
    assert load_yaml(str(p)) == {"store": "memory", "verbose": True, "RAMEN_LOG_ROOT": "/x"}
    assert load_yaml(None) == {}
    assert load_yaml(str(tmp_path / "missing.yaml")) == {}
    monkeypatch.setenv("RAMEN_STORE", "dynamodb")
    monkeypatch.delenv("RAMEN_VERBOSE", raising=False)
    monkeypatch.setenv("RAMEN_CONFIG", str(p))
    applied = apply_config()
    assert os.environ["RAMEN_STORE"] == "dynamodb"
    assert os.environ["RAMEN_VERBOSE"] == "True" and "RAMEN_VERBOSE" in applied and "RAMEN_STORE" not in applied
    applied2 = apply_config()
    assert "RAMEN_VERBOSE" in applied2
