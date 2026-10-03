"""N9: secrets are never stored or shown unencrypted. Systematic re-check of the SENSITIVE encryption
registry, the `public()` masking tuple, and `masked_env()`'s RAMEN_* env-var masking, after this round added
several new secret-shaped fields (N2 smtp_password, N5 github_app_private_key, N7 redis URLs)."""

from tests.test_api import app, client, cloud, demo, root  # noqa: F401 - fixtures


def test_postgres_dsn_is_masked_in_config_env(demo, monkeypatch):
    """RAMEN_POSTGRES_DSN commonly embeds a password (postgresql://user:pass@host/db); masked_env()'s
    substring list (SECRET/PASSWORD/KEY/TOKEN) missed it entirely before this fix."""
    monkeypatch.setenv("RAMEN_POSTGRES_DSN", "postgresql://u:s3cr3t-pw@db.internal:5432/ramen")
    assert "s3cr3t-pw" not in demo.get("/api/v1/config").text


def test_sensitive_fields_are_encrypted_and_masked_consistently():
    """Every field Fernet-encrypted at rest for a collection that goes through the generic `util.public()`
    masking must also be in its hidden tuple, or encryption is pointless: the plaintext (decrypted on read)
    would leak straight back out the first API response that returns the doc. `config` docs are exempt —
    they go through dedicated `public_smtp_config`/`public_app_config`-style helpers instead, checked by
    their own tests (test_notify.py, test_github_app.py)."""
    from ramen_console.storage.encrypted import SENSITIVE
    from ramen_console.util import public

    hidden = public.__defaults__[0]  # the `hidden` tuple's default value
    for collection, fields in SENSITIVE.items():
        if collection == "config":
            continue
        for f in fields:
            assert f in hidden, f"{collection}.{f} is encrypted at rest but not masked by util.public()"
