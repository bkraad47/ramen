# ruff: noqa: E501 - an inline-styled HTML mail template reads better unwrapped
"""Branded HTML for every console mail (0.7.5): the Ramen logo (inline, `cid:ramen-logo`, from the console's own static
files so no client has to fetch a remote image), coral on off-white, the action link as a button, and a signature
naming the Ramen server that sent it. Built from the plain-text body, which stays as the text/plain alternative."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

LOGO = Path(__file__).parent / "static" / "logo-mark.png"
LOGO_CID = "ramen-logo"
CORAL, INK, MUTED, BG, CARD, LINE = "#F26B3A", "#2b2622", "#8a7f76", "#F4F1EC", "#fffdfa", "#e6dfd5"
URL = re.compile(r"https?://[^\s<>\"']+")


def logo_bytes() -> bytes | None:
    try:
        return LOGO.read_bytes()
    except OSError:
        return None


def _paragraph(text: str, button_url: str | None) -> str:
    """One block of the text body: the action link becomes the button (rendered separately), other URLs links."""
    out = []
    for line in text.split("\n"):
        if button_url and line.strip() == button_url:
            continue  # the button carries it
        parts, last = [], 0
        for m in URL.finditer(line):
            parts.append(escape(line[last : m.start()]))
            u = escape(m.group(0))
            parts.append(f'<a href="{u}" style="color:{CORAL};word-break:break-all">{u}</a>')
            last = m.end()
        parts.append(escape(line[last:]))
        out.append("".join(parts))
    body = "<br>".join(x for x in out if x.strip())
    return f'<p style="margin:0 0 14px;line-height:1.55">{body}</p>' if body else ""


def render(subject: str, text: str, base: str = "", action: str | None = None, mono: bool = False) -> str:
    urls = URL.findall(text)
    button_url = next((u for u in urls if "/auth/" in u), None) if action else None
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    if mono:  # the warning digest: one monospace list, nothing to click
        rows = "".join(
            f'<div style="padding:6px 0;border-bottom:1px solid {LINE}">{escape(line)}</div>'
            for line in text.strip().split("\n")
        )
        content = f'<div style="font-family:Menlo,Consolas,monospace;font-size:12px">{rows}</div>'
    else:
        button = ""
        if button_url:
            u = escape(button_url)
            button = (
                f'<p style="margin:6px 0 18px"><a href="{u}" style="background:{CORAL};color:#fff;text-decoration:none;'
                f'padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block">{escape(action)}</a></p>'
                f'<p style="margin:0 0 6px;font-size:12px;color:{MUTED}">Or copy this address into your browser:</p>'
                f'<div style="margin:0 0 18px;padding:10px 12px;background:{BG};border:1px solid {LINE};border-radius:6px;'
                f"font-family:Menlo,Consolas,monospace;font-size:12px;color:{INK};word-break:break-all;"
                f'user-select:all;-webkit-user-select:all">{u}</div>'
            )
        parts, placed = [], False
        for b in blocks:  # the button goes right under the paragraph that carried the link
            parts.append(_paragraph(b, button_url))
            if button and not placed and button_url in b:
                parts.append(button)
                placed = True
        content = "".join(parts) + ("" if placed else button)
    signature = _signature(base)
    return _page(subject, content, signature)


def _signature(base: str) -> str:
    home = escape(base.rstrip("/")) if base else ""
    where = f'<a href="{home}" style="color:{MUTED}">{escape(base.split("://")[-1].rstrip("/"))}</a>' if base else ""
    return (
        f'<p style="margin:0;font-weight:600;color:{INK}">Ramen server</p>'
        f'<p style="margin:2px 0 0">Multizone MCP for your team{(" · " + where) if where else ""}</p>'
        f'<p style="margin:8px 0 0">This message was sent by the Ramen console. Replies are not read.</p>'
    )


def _page(subject: str, content: str, signature: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:{CARD};
border:1px solid {LINE};border-radius:12px;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:{INK};font-size:15px">
<tr><td style="padding:24px 28px 8px"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td><img src="cid:{LOGO_CID}" width="48" height="48" alt="Ramen" style="display:block;border-radius:10px"></td>
<td style="padding-left:12px;font-size:18px;font-weight:700;color:{INK}">Ramen</td></tr></table></td></tr>
<tr><td style="padding:8px 28px 0"><h1 style="margin:0 0 16px;font-size:20px;color:{INK}">{escape(subject)}</h1>{content}</td></tr>
<tr><td style="padding:18px 28px 24px;border-top:1px solid {LINE};font-size:12px;color:{MUTED}">{signature}</td></tr>
</table></td></tr></table></body></html>"""


SOURCES = {
    "ramen.deploy": "Deploy",
    "ramen.drift": "Drift",
    "ramen.autoscale": "Autoscaling",
    "ramen.scheduler": "Scheduler",
    "ramen.security": "Security",
    "ramen.mail": "Mail",
    "ramen.services": "Console",
    "ramen.oauth": "Sign-in",
    "ramen.auth": "Sign-in",
}
LEVELS = {
    "WARNING": ("Warning", "#b7791f", "#fdf3e1"),
    "ERROR": ("Error", "#d64545", "#fbe9e9"),
    "CRITICAL": ("Critical", "#d64545", "#fbe9e9"),
}
SCOPE = re.compile(r"\b(?:[a-z][a-z0-9-]*/[a-z0-9][a-z0-9-]*|zone [a-z0-9][a-z0-9-]*)\b")


def digest_subject(alerts: list[dict]) -> str:
    """`2 warnings, 1 error on the Ramen console`."""
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a.get("level", "WARNING")] = counts.get(a.get("level", "WARNING"), 0) + 1
    parts = [f"{n} {LEVELS.get(k, (k.title(),))[0].lower()}{'s' if n != 1 else ''}" for k, n in counts.items()]
    return ", ".join(parts) + " on the Ramen console"


def source_label(logger: str) -> str:
    return (
        SOURCES.get(logger) or logger.removeprefix("ramen.").replace("_", " ").replace(".", " · ").title() or "Console"
    )


def _highlight(message: str) -> str:
    """Escape the message and bold what names a place — `demo/dev`, `zone a`."""
    out, last = [], 0
    for m in SCOPE.finditer(message):
        out.append(escape(message[last : m.start()]))
        out.append(f'<b style="color:{INK}">{escape(m.group(0))}</b>')
        last = m.end()
    out.append(escape(message[last:]))
    return "".join(out)


def render_alerts(subject: str, alerts: list[dict], base: str = "") -> str:
    """The warning digest: a count line, then one row per alert — level badge, source, time, the message — and a
    button to the console's logs. Same shell as every other Ramen mail."""
    from datetime import UTC, datetime

    counts: dict[str, int] = {}
    for a in alerts:
        counts[a.get("level", "WARNING")] = counts.get(a.get("level", "WARNING"), 0) + 1
    summary = ", ".join(
        f"{n} {LEVELS.get(k, (k.title(),))[0].lower()}{'s' if n != 1 else ''}" for k, n in counts.items()
    )
    rows = []
    for a in alerts:
        label, fg, bg = LEVELS.get(a.get("level", ""), (a.get("level", "").title() or "Note", MUTED, BG))
        at = a.get("at")
        when = datetime.fromtimestamp(at, UTC).strftime("%H:%M:%S UTC") if at else ""
        rows.append(
            f'<tr><td style="padding:12px 0;border-bottom:1px solid {LINE};vertical-align:top">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;letter-spacing:.02em">{escape(label)}</span>'
            f'<span style="margin-left:8px;font-size:12px;font-weight:600;color:{INK}">{escape(source_label(a.get("logger", "")))}</span>'
            f'<span style="margin-left:8px;font-size:12px;color:{MUTED}">{escape(when)}</span>'
            f'<div style="margin-top:6px;line-height:1.5">{_highlight(a.get("message", ""))}</div></td></tr>'
        )
    content = (
        f'<p style="margin:0 0 6px;line-height:1.55">The console logged <b>{escape(summary)}</b> since the last digest.</p>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 4px">{"".join(rows)}</table>'
    )
    if base:
        u = escape(base.rstrip("/") + "/logs")
        content += (
            f'<p style="margin:20px 0 8px"><a href="{u}" style="background:{CORAL};color:#fff;text-decoration:none;'
            f'padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block">Open the console logs</a></p>'
        )
    return _page(subject, content, _signature(base))


def signature_text(base: str = "") -> str:
    """The plain-text twin of the HTML signature."""
    return (
        "\n--\nRamen server"
        + (f" · {base.rstrip('/')}" if base else "")
        + "\nThis message was sent by the Ramen console.\n"
    )
