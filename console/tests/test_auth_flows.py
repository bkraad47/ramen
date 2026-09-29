"""CONTRACTS §9: password-login toggle + break-glass, invite / reset / magic-link email via the file:// backend."""

import email as emaillib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console.mail import Mailer
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

FIRST, SECOND, THIRD = "F1rst-Passw0rd!", "S3cond-Passw0rd!", "Th1rd-Passw0rd!"


@pytest.fixture
def maildir(tmp_path, monkeypatch):
    d = tmp_path / "mail"
    monkeypatch.setenv("RAMEN_SMTP_HOST", f"file://{d}")
    monkeypatch.setenv("RAMEN_SMTP_FROM", "console@ramen.test")
    monkeypatch.setenv("RAMEN_PUBLIC_URL", "https://console.test/")
    return d


def mails(d: Path) -> list[emaillib.message.EmailMessage]:
    return (
        [emaillib.message_from_bytes(p.read_bytes(), policy=emaillib.policy.default) for p in sorted(d.glob("*.eml"))]
        if d.exists()
        else []
    )


def link(msg, kind) -> str:
    m = re.search(rf"https://console\.test/auth/{kind}/(\S+)", msg.get_content())
    assert m, msg.get_content()
    return f"/auth/{kind}/{m.group(1)}"


def test_password_login_toggle_and_break_glass(demo, monkeypatch):
    make_user(demo, "v@x", "viewer", ["demo"])
    cfg = demo.get("/api/v1/config/auth").json()
    assert cfg["password_login"] is True and cfg["magic_link"] is False and cfg["mail"] == "off"
    # refused: nothing else could sign in
    r = demo.put("/api/v1/config/auth", json={"password_login": False})
    assert r.status_code == 422 and "break-glass" in r.json()["detail"]
    monkeypatch.setenv("RAMEN_ADMIN_FORCE_PASSWORD", "1")
    demo.app.state.auth_env = demo.app.state.auth_env.from_env()
    r = demo.put("/api/v1/config/auth", json={"password_login": False})
    assert r.status_code == 200 and r.json()["password_login"] is False and r.json()["break_glass"] is True
    with TestClient(demo.app) as v:
        r = v.post("/login", data={"email": "v@x", "password": PW}, follow_redirects=False)
        assert r.status_code == 403 and "disabled" in r.text
        page = v.get("/login").text
        assert "break-glass" in page and 'name="password"' in page
        login(v, "root@ramen.local", "rootpw")  # bootstrap admin still gets in
    audit = demo.get("/api/v1/audit").json()
    assert any(
        a["action"] == "login" and a["user"] == "v@x" and not a["ok"] and "password_login:disabled" in a["tags"]
        for a in audit
    )
    assert any(a["action"] == "config.auth" and "password_login:False" in a["tags"] for a in audit)
    monkeypatch.setenv("RAMEN_ADMIN_FORCE_PASSWORD", "0")
    demo.app.state.auth_env = demo.app.state.auth_env.from_env()
    with TestClient(demo.app) as anon:
        assert 'name="password"' not in anon.get("/login").text
        assert (
            anon.post(
                "/login", data={"email": "root@ramen.local", "password": "rootpw"}, follow_redirects=False
            ).status_code
            == 403
        )
    assert demo.put("/api/v1/config/auth", json={"password_login": True}).json()["password_login"] is True
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put("/api/v1/config/auth", json={"magic_link": True}).status_code == 403
    assert demo.get("/config").status_code == 200


def test_invite_and_reset_flow(demo, maildir):
    demo.app.state.mailer = Mailer.from_env()
    u = demo.post("/api/v1/users", json={"email": "new@x", "password": FIRST, "role": "viewer", "groups": ["demo"]})
    assert u.status_code == 201 and u.json()["invite"] == {"ok": True, "backend": "file"}
    inv = mails(maildir)
    assert (
        len(inv) == 1
        and inv[0]["To"] == "new@x"
        and inv[0]["From"] == "console@ramen.test"
        and "invited" in inv[0]["Subject"]
    )
    assert FIRST not in inv[0].get_content()
    set_path = link(inv[0], "reset")
    with TestClient(demo.app) as anon:
        assert anon.get("/auth/reset").status_code == 200
        r = anon.post("/auth/reset", data={"email": "nobody@x"})
        assert r.status_code == 200 and "If that account exists" in r.text
        assert len(mails(maildir)) == 1  # unknown address: no mail, same answer
        assert anon.post("/auth/reset", data={"email": "new@x"}).status_code == 200
        msgs = mails(maildir)
        assert len(msgs) == 2 and "Reset" in msgs[1]["Subject"]
        reset_path = link(msgs[1], "reset")
        assert anon.get(set_path).status_code == 200 and 'name="password"' in anon.get(reset_path).text
        assert anon.get("/auth/reset/garbage").status_code == 400
        # the newer request superseded the invite nonce: the invite link is now dead
        assert anon.post(set_path, data={"password": "x" * 8}).status_code == 400
        r = anon.post(reset_path, data={"password": SECOND}, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
        assert anon.post(reset_path, data={"password": THIRD}).status_code == 400  # single use
        assert (
            anon.post("/login", data={"email": "new@x", "password": FIRST}, follow_redirects=False).status_code == 401
        )
        login(anon, "new@x", SECOND)
    audit = demo.get("/api/v1/audit").json()
    assert any(a["action"] == "password.reset" and a["user"] == "new@x" and a["ok"] for a in audit)
    assert any(a["action"] == "password.reset.request" and a["user"] == "nobody@x" for a in audit)
    assert "second-pw" not in str(demo.get("/api/v1/users").json())
    assert "nonce" not in str(demo.get("/api/v1/users").json())


def test_reset_page_without_mail(client):
    r = client.get("/auth/reset")
    assert r.status_code == 200 and "not configured" in r.text
    assert client.post("/auth/reset", data={"email": "root@ramen.local"}).status_code == 200


def test_magic_link_flow(demo, maildir):
    demo.app.state.mailer = Mailer.from_env()
    make_user(demo, "m@x", "viewer", ["demo"])
    with TestClient(demo.app) as anon:
        assert anon.post("/auth/magic", data={"email": "m@x"}).status_code == 403  # disabled by default
    assert demo.put("/api/v1/config/auth", json={"magic_link": True}).json()["magic_link"] is True
    with TestClient(demo.app) as anon:
        page = anon.get("/login").text
        assert 'action="/auth/magic"' in page
        r = anon.post("/auth/magic", data={"email": "m@x"})
        assert r.status_code == 200 and "sign-in link" in r.text
        msg = mails(maildir)[-1]
        assert msg["To"] == "m@x"
        path = link(msg, "magic")
        assert anon.get("/auth/magic/nope").status_code == 400
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303 and anon.cookies.get("ramen_session")
        assert anon.get("/api/v1/me").json()["email"] == "m@x"
        assert anon.get(path, follow_redirects=False).status_code == 400  # single use
    assert demo.put("/api/v1/config/auth", json={"magic_link": False}).status_code == 200
    audit = demo.get("/api/v1/audit").json()
    assert any(a["action"] == "login.magic" and a["user"] == "m@x" and a["ok"] for a in audit)


def test_password_login_can_go_off_when_magic_link_on(demo, maildir):
    demo.app.state.mailer = Mailer.from_env()
    assert demo.put("/api/v1/config/auth", json={"magic_link": True}).status_code == 200
    assert demo.put("/api/v1/config/auth", json={"password_login": False}).status_code == 200
    assert demo.get("/api/v1/config/auth").json()["password_login"] is False
    assert demo.put("/api/v1/config/auth", json={"password_login": True}).status_code == 200


async def test_mailer_backends(monkeypatch, tmp_path):
    off = Mailer.from_env({})
    assert not off.enabled and off.backend == "off"
    assert (await off.send("a@b", "s", "body"))["ok"] is False
    f = Mailer(f"file://{tmp_path / 'm'}", from_="x@y")
    r = await f.send("a@b", "Hi", "text")
    assert r["ok"] and Path(r["path"]).exists() and f.backend == "file"
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent.append(("starttls",))

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, msg):
            sent.append(("send", msg["To"]))

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    m = Mailer.from_env(
        {
            "RAMEN_SMTP_HOST": "smtp.test",
            "RAMEN_SMTP_PORT": "2525",
            "RAMEN_SMTP_USER": "u",
            "RAMEN_SMTP_PASSWORD": "p",
            "RAMEN_SMTP_FROM": "f@t",
            "RAMEN_SMTP_TLS": "1",
        }
    )
    assert m.backend == "smtp" and (await m.send("a@b", "s", "b")) == {"ok": True, "backend": "smtp"}
    assert sent == [("connect", "smtp.test", 2525), ("starttls",), ("login", "u"), ("send", "a@b")]
    sent.clear()
    ssl = Mailer("smtp.test", 465, tls="ssl")
    assert (await ssl.send("a@b", "s", "b"))["ok"] and sent == [("connect", "smtp.test", 465), ("send", "a@b")]

    def boom(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    r = await Mailer("smtp.test", 25, tls="0").send("a@b", "s", "b")
    assert r["ok"] is False and "OSError" in r["error"]
