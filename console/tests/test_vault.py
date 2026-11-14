"""1.0.0 (the user): the console's own secrets live in the cloud's secret manager, the store keeps a ref; local keeps
them Fernet-encrypted in the store; a value is never logged, shown or backed up."""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.secrets.gcp import GcpSecrets
from ramen_console.storage import make_store
from ramen_console.storage.vaulted import MARK, VaultedStore
from tests.fakes_gcp import FakeSM
from tests.test_api import cloud, login  # noqa: F401 - fixtures

SENTINEL = "Zx9-sentinel-pw-41"


class FakeVault:
    kind = "gcp"

    def __init__(self):
        self.values, self.dropped = {}, []

    async def put_value(self, name, value):
        ref = f"sm://fake/{name}"
        self.values[ref] = value
        return ref

    async def resolve(self, ref):
        return self.values[ref] if ref and ref.startswith("sm://") else ref

    async def drop_value(self, ref):
        self.dropped.append(ref)
        self.values.pop(ref, None)

    async def delete(self, doc):
        self.values.pop(doc.get("ref"), None)


def run(c):
    return asyncio.run(c)


def test_a_value_goes_to_the_vault_and_the_store_keeps_only_a_ref():
    v = FakeVault()
    s = VaultedStore(make_store(), v)
    run(s.put("config", "smtp", {"host": "h", "smtp_password": SENTINEL}))
    raw = run(s.inner.get("config", "smtp"))
    assert raw["smtp_password"].startswith(MARK) and SENTINEL not in str(raw) and raw["host"] == "h"
    assert run(s.get("config", "smtp"))["smtp_password"] == SENTINEL
    assert run(s.list("config"))[0]["smtp_password"] == SENTINEL
    ref = raw["smtp_password"]
    run(s.put("config", "smtp", {"host": "h2", "smtp_password": SENTINEL}))  # unchanged value: same secret
    assert run(s.inner.get("config", "smtp"))["smtp_password"] == ref and len(v.values) == 1
    run(s.put("config", "smtp", {"host": "h2", "smtp_password": "new"}))  # changed: new secret, old one deleted
    assert v.dropped == [ref[len(MARK) :]] and list(v.values.values()) == ["new"]
    run(s.put("config", "smtp", {"host": "h2"}))  # cleared: deleted
    assert not v.values
    run(s.put("groups", "demo", {"name": "demo", "github_token": "t", "session_secret": "ss"}))
    run(s.delete("groups", "demo"))
    assert not v.values


def test_nested_provider_secrets_are_vaulted_and_hashes_are_not():
    v = FakeVault()
    s = VaultedStore(make_store(), v)
    run(s.put("config", "oauth_providers", {"entra": {"issuer": "i", "client_secret": SENTINEL}}))
    raw = run(s.inner.get("config", "oauth_providers"))
    assert raw["entra"]["client_secret"].startswith(MARK) and raw["entra"]["issuer"] == "i"
    assert run(s.get("config", "oauth_providers"))["entra"]["client_secret"] == SENTINEL
    run(s.put("users", "u1", {"email": "a@b", "password_hash": "$argon2$x"}))
    run(s.put("secrets", "x", {"group": "demo", "value": None, "ref": "sm://projects/p/secrets/ramen-demo-x"}))
    assert len(v.values) == 1 and run(s.inner.get("users", "u1"))["password_hash"] == "$argon2$x"


def test_values_written_before_the_vault_are_moved_at_start():
    v = FakeVault()
    inner = make_store()
    run(inner.put("config", "github_app", {"app_id": "1", "github_app_private_key": SENTINEL}))
    run(inner.put("workers", "demo:a", {"group": "demo", "redis_item_url": "redis://:pw@h"}))
    s = VaultedStore(inner, v)
    assert run(s.migrate()) == 2 and run(s.migrate()) == 0
    assert run(inner.get("config", "github_app"))["github_app_private_key"].startswith(MARK)
    assert run(s.get("workers", "demo:a"))["redis_item_url"] == "redis://:pw@h"


def test_gcp_names_console_secrets_apart_from_every_group():
    sm = FakeSM()
    b = GcpSecrets("p1", sm, on_create=lambda *a: pytest.fail("no worker may be bound to a console secret"))
    ref = run(b.put_value("console-config-smtp-smtp_password-ab12", SENTINEL))
    assert ref == "sm://projects/p1/secrets/ramenc-console-config-smtp-smtp_password-ab12"
    assert sm.secrets[ref[5:]]["labels"] == {"ramen": "console"} and run(b.resolve(ref)) == SENTINEL
    run(b.drop_value(ref))
    assert not sm.secrets


def test_aws_and_azure_keep_console_secrets_outside_the_worker_boundary():
    from ramen_console.secrets.aws import AwsSecrets
    from tests.fakes_aws import FakeAwsClients, aws_env

    with aws_env():
        b = AwsSecrets("us-east-1", FakeAwsClients().secretsmanager)
        ref = run(b.put_value("console-groups-demo:x-github_token-ab", SENTINEL))
        assert ref.startswith("asm://ramen-console/") and run(b.resolve(ref)) == SENTINEL
        run(b.drop_value(ref))


@pytest.fixture
def vault_app(monkeypatch, tmp_path, cloud):  # noqa: F811
    monkeypatch.setenv("RAMEN_STORE", "memory")
    monkeypatch.setenv("RAMEN_ADMIN_EMAIL", "root@ramen.local")
    monkeypatch.setenv("RAMEN_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setenv("RAMEN_BACKUP_ROOT", str(tmp_path / "backups"))
    v = FakeVault()
    return create_app(store=make_store(), cloud=cloud, secrets=v), v


def test_on_a_cloud_no_secret_reaches_the_store_logs_api_pages_or_backups(vault_app, caplog, tmp_path):
    app, v = vault_app
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as c:
        login(c, "root@ramen.local", "rootpw")
        assert c.put("/api/v1/config/smtp", json={"host": f"file://{tmp_path}/m", "password": SENTINEL}).is_success
        r = c.put(
            "/api/v1/config/oauth-providers/entra",
            json={"issuer": "https://login.example.com/t/v2.0", "client_id": "cid", "client_secret": SENTINEL + "o"},
        )
        assert r.is_success, r.text
        assert c.put("/api/v1/config/otel", json={"headers": {"Authorization": SENTINEL + "h"}}).is_success
        assert isinstance(app.state.store, VaultedStore)
        raw = run(app.state.store.inner.list("config"))
        assert SENTINEL not in str(raw) and sum(SENTINEL in x for x in v.values.values()) == 3
        seen = "".join(
            c.get(p).text for p in ("/config", "/api/v1/config", "/api/v1/config/smtp", "/api/v1/audit", "/audit")
        )
        assert SENTINEL not in seen
        page = c.get("/config").text  # each section lists what is set; a secret only as set/not set
        assert page.count("set (hidden)") == 2 and "login.example.com" in page
        b = c.post("/api/v1/backups", json={"target": "local"}).json()
        assert SENTINEL not in open(b["path"]).read()
    assert SENTINEL not in caplog.text


def test_a_restore_keeps_the_provider_secret_the_backup_left_out(vault_app):
    app, v = vault_app
    with TestClient(app) as c:
        login(c, "root@ramen.local", "rootpw")
        body = {"issuer": "https://login.example.com/t/v2.0", "client_id": "cid", "client_secret": SENTINEL}
        assert c.put("/api/v1/config/oauth-providers/entra", json=body).is_success
        bid = c.post("/api/v1/backups", json={"target": "local"}).json()["id"]
        assert c.post(f"/api/v1/backups/{bid}/restore", json={}).is_success
        assert run(app.state.store.get("config", "oauth_providers"))["entra"]["client_secret"] == SENTINEL


def test_a_known_secret_never_reaches_a_log_line_or_a_deploy_log(vault_app, caplog, tmp_path):
    from ramen_console.security import ScrubbedLog, scrub

    app, v = vault_app
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as c:
        login(c, "root@ramen.local", "rootpw")
        assert c.put("/api/v1/config/smtp", json={"host": f"file://{tmp_path}/m", "password": SENTINEL}).is_success
        logging.getLogger("ramen.something").warning("smtp said %s", SENTINEL)
        log = ScrubbedLog()
        log.append(f"cloning https://x-access-token:{SENTINEL}@github.com/o/r and {SENTINEL}")
        assert log == ["cloning https://***@github.com/o/r and ***"]
        assert scrub("redis://default:pw@host:6379/0") == "redis://***@host:6379/0"
    assert SENTINEL not in caplog.text and "smtp said ***" in caplog.text


def test_scrubbing_keeps_the_args_uvicorns_access_formatter_unpacks(vault_app):
    """The access formatter does `(addr, method, path, version, status) = record.args`: the filter scrubs the
    strings in place instead of collapsing them (which printed a TypeError stack on every access line on kind)."""
    from ramen_console.security import _ScrubSecrets, remember_secret

    remember_secret(SENTINEL)
    args = ("10.0.0.1:1", "GET", f"/login?token={SENTINEL}", "1.1", 200)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', args, None)
    assert _ScrubSecrets().filter(rec)
    assert len(rec.args) == 5 and rec.args[4] == 200 and SENTINEL not in rec.getMessage()
    assert rec.getMessage() == '10.0.0.1:1 - "GET /login?token=*** HTTP/1.1" 200'
    plain = logging.LogRecord("x", logging.INFO, "", 0, "pw is " + SENTINEL, (), None)
    _ScrubSecrets().filter(plain)
    assert plain.getMessage() == "pw is ***"
    named = logging.LogRecord("x", logging.INFO, "", 0, "pw %(pw)s", ({"pw": SENTINEL},), None)
    _ScrubSecrets().filter(named)
    assert named.getMessage() == "pw ***"
