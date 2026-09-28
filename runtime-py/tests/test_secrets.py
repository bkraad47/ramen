import pytest

from ramen_runtime import secrets


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("RAMEN_SECRET_DEMO__API_KEY", "s3cr3t")
    monkeypatch.setenv("RAMEN_SECRET_MY_GROUP__TOKEN", "tok")


def test_resolve_substitutes():
    assert secrets.resolve("key={{$demo.api_key}}") == "key=s3cr3t"
    assert secrets.resolve("{{ $demo.API_KEY }}/{{$my-group.token}}") == "s3cr3t/tok"


def test_unknown_secret_raises_without_leaking():
    with pytest.raises(secrets.SecretError, match=r"demo\.MISSING"):
        secrets.resolve("{{$demo.missing}}")


def test_plain_text_untouched():
    assert secrets.resolve("no secrets {{param}} here") == "no secrets {{param}} here"


def test_substitute_args_only_strings():
    out = secrets.substitute_args(
        {"a": "{{$demo.api_key}}", "b": 3, "c": ["{{$demo.api_key}}"], "d": {"x": "{{$demo.api_key}}"}}
    )
    assert out == {"a": "s3cr3t", "b": 3, "c": ["s3cr3t"], "d": {"x": "s3cr3t"}}
