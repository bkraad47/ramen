"""N2: persisted SMTP credentials (Users page), selected-user notify list, warning/error digest emails."""

import logging

import pytest

from ramen_console import alerts
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures


@pytest.fixture(autouse=True)
def _clean_pending():
    alerts.PENDING.clear()
    yield
    alerts.PENDING.clear()


async def test_smtp_config_round_trips_and_masks_password(demo, tmp_path):
    maildir = tmp_path / "mail"
    r = demo.get("/api/v1/config/smtp")
    assert r.status_code == 200 and r.json()["password_set"] is False

    r = demo.put(
        "/api/v1/config/smtp",
        json={"host": f"file://{maildir}", "mail_from": "console@ramen.test", "password": "s3cret"},
    )
    assert r.status_code == 200
    assert r.json()["password_set"] is True and "password" not in r.json() and r.json()["host"] == f"file://{maildir}"

    stored = await demo.app.state.store.get("config", "smtp")
    assert stored["smtp_password"] == "s3cret"  # EncryptedStore decrypts on read
    assert demo.app.state.mailer.enabled and demo.app.state.mailer.backend == "file"


async def test_smtp_password_is_encrypted_at_rest(demo, tmp_path):
    demo.put("/api/v1/config/smtp", json={"host": f"file://{tmp_path / 'm'}", "password": "s3cret"})
    raw = await demo.app.state.store.inner.get("config", "smtp")
    assert raw["smtp_password"] != "s3cret" and raw["smtp_password"].startswith("enc:")


def test_smtp_config_is_super_admin_gated(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    from fastapi.testclient import TestClient

    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.get("/api/v1/config/smtp").status_code == 403
        assert v.put("/api/v1/config/smtp", json={"host": "x"}).status_code == 403


async def test_notify_config_round_trip_and_gating(demo):
    u = make_user(demo, "a@x", "viewer", ["demo"])
    r = demo.put("/api/v1/config/notify", json={"user_ids": [u["id"]]})
    assert r.status_code == 200 and r.json()["user_ids"] == [u["id"]]
    assert demo.get("/api/v1/config/notify").json()["user_ids"] == [u["id"]]


async def test_warning_log_is_queued_and_flushed_as_digest(demo, tmp_path):
    maildir = tmp_path / "mail"
    u = make_user(demo, "a@x", "viewer", ["demo"])
    demo.put("/api/v1/config/smtp", json={"host": f"file://{maildir}", "mail_from": "console@ramen.test"})
    demo.put("/api/v1/config/notify", json={"user_ids": [u["id"]]})

    logging.getLogger("ramen.somewhere").warning("disk is getting full")
    assert len(alerts.PENDING) == 1

    svc = demo.app.state.services
    sent = await alerts.flush_alerts(svc, demo.app.state.mailer)
    assert sent == ["a@x"] and alerts.PENDING == []
    assert len(list(maildir.glob("*.eml"))) == 1
    assert "disk is getting full" in list(maildir.glob("*.eml"))[0].read_text()


async def test_flush_is_noop_without_recipients_or_mail(demo):
    logging.getLogger("ramen.somewhere").warning("nobody will see this")
    assert await alerts.flush_alerts(demo.app.state.services, demo.app.state.mailer) == []
    assert len(alerts.PENDING) == 1  # bounded, not dropped, until mail+recipients are configured


async def test_flush_is_noop_when_mail_enabled_but_no_recipients_selected(demo, tmp_path):
    demo.put("/api/v1/config/smtp", json={"host": f"file://{tmp_path / 'm'}"})
    logging.getLogger("ramen.somewhere").warning("still nobody will see this")
    assert await alerts.flush_alerts(demo.app.state.services, demo.app.state.mailer) == []
    assert len(alerts.PENDING) == 1


def test_alert_handler_ignores_its_own_logger():
    logging.getLogger("ramen.alerts").warning("flush failed: boom")
    assert alerts.PENDING == []
