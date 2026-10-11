"""0.7.5 (the user): every console mail is branded HTML — Ramen logo inline, the link as a button plus a copyable
address, a Ramen server signature — with the plain text kept; the warning digest gets the same look, one row per
alert with a level badge, a readable source, its time and the places it names in bold."""

import email
import email.policy
import time

from ramen_console import mail_html
from ramen_console.mail import Mailer, magic_mail, reset_mail


def sent(tmp_path, **kw):
    m = Mailer(f"file://{tmp_path}", from_="ramen@console.test")
    import asyncio

    r = asyncio.run(m.send("p@x.test", **kw))
    assert r["ok"], r
    path = sorted(tmp_path.glob("*.eml"))[-1]
    return email.message_from_bytes(path.read_bytes(), policy=email.policy.default)


def test_a_link_mail_has_html_with_logo_button_copy_box_and_signature(tmp_path):
    link = "https://console.test/auth/reset/abc.def"
    subject, body, action = reset_mail("https://console.test", link)
    msg = sent(tmp_path, subject=subject, body=body, action=action, base="https://console.test")
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert link in text and "Ramen server · https://console.test" in text
    assert 'src="cid:ramen-logo"' in html and ">Reset password</a>" in html and f'href="{link}"' in html
    assert "Or copy this address into your browser" in html and "user-select:all" in html
    assert "Ramen server" in html and "console.test" in html
    assert html.index(">Reset password</a>") < html.index("Otherwise ignore")  # button under its sentence
    logo = [p for p in msg.walk() if p.get("Content-ID") == "<ramen-logo>"]
    assert logo and logo[0].get_content_type() == "image/png" and len(logo[0].get_content()) > 1000


def test_every_link_mail_names_its_action():
    assert reset_mail("b", "l")[2] == "Reset password" and magic_mail("b", "l")[2] == "Sign in to Ramen"


def test_the_warning_digest_is_rendered_per_alert(tmp_path):
    alerts = [
        {
            "level": "WARNING",
            "logger": "ramen.deploy",
            "message": "deploy demo/dev: zone a refused, region us-central1 is blocked",
            "at": time.time(),
        },
        {
            "level": "ERROR",
            "logger": "ramen.autoscale",
            "message": "demo/a at max_count 3 under high load",
            "at": time.time(),
        },
        {"level": "WARNING", "logger": "ramen.custom_thing", "message": "<script>x</script>"},
    ]
    body = "\n".join(f"[{a['level']}] {a['logger']}: {a['message']}" for a in alerts)
    msg = sent(
        tmp_path, subject="Ramen console: 3 server warning(s)", body=body, alerts=alerts, base="https://console.test"
    )
    html = msg.get_body(("html",)).get_content()
    assert "2 warnings, 1 error" in html and ">Warning</span>" in html and ">Error</span>" in html
    assert ">Deploy</span>" in html and ">Autoscaling</span>" in html and ">Custom Thing</span>" in html
    assert "<b" in html and ">demo/dev</b>" in html and ">zone a</b>" in html
    assert "&lt;script&gt;" in html and "<script>x" not in html  # messages are escaped
    assert "/logs" in html and "Open the console logs" in html and 'src="cid:ramen-logo"' in html
    assert mail_html.digest_subject(alerts) == "2 warnings, 1 error on the Ramen console"
    assert "[WARNING] ramen.deploy" in msg.get_body(("plain",)).get_content()  # text twin unchanged


def test_source_labels_fall_back_to_a_readable_name():
    assert mail_html.source_label("ramen.deploy") == "Deploy" and mail_html.source_label("ramen.foo.bar") == "Foo · Bar"
