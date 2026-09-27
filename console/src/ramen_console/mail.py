"""Outbound email (CONTRACTS §9, G12): SMTP from `RAMEN_SMTP_*`; `RAMEN_SMTP_HOST=file://<dir>` writes .eml files
(dev/tests). Message bodies are never logged."""
import asyncio
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .util import now, uid

log = logging.getLogger("ramen.mail")


class Mailer:
    def __init__(self, host="", port=587, user="", password="", from_="ramen@localhost", tls="1"):
        self.host, self.port, self.user, self.password, self.from_, self.tls = host.strip(), int(port or 587), user, password, from_, str(tls)

    @classmethod
    def from_env(cls, env=None) -> "Mailer":
        env = env or os.environ
        return cls(env.get("RAMEN_SMTP_HOST", ""), env.get("RAMEN_SMTP_PORT") or 587, env.get("RAMEN_SMTP_USER", ""),
                   env.get("RAMEN_SMTP_PASSWORD", ""), env.get("RAMEN_SMTP_FROM", "ramen@localhost"), env.get("RAMEN_SMTP_TLS", "1"))

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    @property
    def backend(self) -> str:
        return "off" if not self.host else "file" if self.host.startswith("file://") else "smtp"

    def _message(self, to, subject, body) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Date"] = self.from_, to, subject, now()
        m.set_content(body)
        return m

    def _smtp(self, msg: EmailMessage) -> None:
        if self.tls.lower() == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=15)
        with server:
            if self.tls.lower() in ("1", "true", "starttls"):
                server.starttls()
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> dict:
        if not self.enabled:
            return {"ok": False, "backend": "off", "error": "mail not configured (RAMEN_SMTP_HOST)"}
        msg = self._message(to, subject, body)
        try:
            if self.backend == "file":
                d = Path(self.host[len("file://"):])
                d.mkdir(parents=True, exist_ok=True)
                p = d / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{uid()[:6]}.eml"
                p.write_bytes(bytes(msg))
                log.info("mail written to=%s subject=%r path=%s", to, subject, p)
                return {"ok": True, "backend": "file", "path": str(p)}
            await asyncio.to_thread(self._smtp, msg)
            log.info("mail sent to=%s subject=%r", to, subject)
            return {"ok": True, "backend": "smtp"}
        except (OSError, smtplib.SMTPException) as e:
            log.warning("mail failed to=%s subject=%r error=%s", to, subject, type(e).__name__)
            return {"ok": False, "backend": self.backend, "error": f"{type(e).__name__}: {e}"}


def invite_mail(base: str, email: str, link: str, by: str) -> tuple[str, str]:
    return ("You have been invited to the Ramen console",
            f"Hello {email},\n\n{by or 'An administrator'} created a Ramen console account for you.\n"
            f"Set your password here (link valid 24 hours):\n{link}\n\nThen sign in at {base}/login\n")


def reset_mail(base: str, link: str) -> tuple[str, str]:
    return ("Reset your Ramen console password",
            f"Someone asked to reset the password for this account. If that was you, open (valid 24 hours):\n{link}\n\n"
            f"Otherwise ignore this message; nothing changes until the link is used.\n")


def magic_mail(base: str, link: str) -> tuple[str, str]:
    return ("Your Ramen console sign-in link", f"Sign in with this one-time link (valid 15 minutes):\n{link}\n")
