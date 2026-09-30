#!/usr/bin/env python3
"""v0.5.5 I6: prove a live console's RAMEN_SMTP_* config actually sends mail, driven the way an admin
action would trigger it, not by calling smtplib directly.

    scripts/mail_smoke.py https://<console> <admin-email> <admin-password> <target-email> [--group demo]

The public /auth/reset route always answers 200 (CONTRACTS §9 — no account-enumeration), so it can't prove
delivery. Creating a user instead (POST /api/v1/users) is the one admin action whose response surfaces the
mailer's own {"ok": ..., "error": ...} result (api_admin.py's `doc["invite"]`), so this creates a throwaway
viewer account at <target-email> and prints that result. The created user is left in place — delete it from
the Users page (or `DELETE /api/v1/users/<id>`, printed below) once you've confirmed the email arrived.
Nothing about the SMTP password is read, printed, or touched here — it only exercises whatever the console
process already has configured via its own environment. Needs httpx (the console's venv has it: `cd console
&& uv run python ../scripts/mail_smoke.py …`).
"""

import argparse
import secrets
import sys

import httpx


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("console")
    ap.add_argument("admin_email")
    ap.add_argument("admin_password")
    ap.add_argument("target_email")
    ap.add_argument("--group", default=None, help="an existing group id to attach the throwaway user to")
    ap.add_argument("--ca", help="CA bundle for a self-signed console cert")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.console, verify=args.ca or True, follow_redirects=False)
    r = c.post("/login", data={"email": args.admin_email, "password": args.admin_password})
    step("login", r.status_code == 303, f"{r.status_code}")
    c.headers["X-Ramen-CSRF"] = c.cookies.get("ramen_csrf", "")

    body = {
        "email": args.target_email,
        "password": f"Aa1!{secrets.token_urlsafe(16)}",  # meets the strength rule: upper, lower, digit, special
        "role": "viewer",
        "groups": [args.group] if args.group else [],
    }
    r = c.post("/api/v1/users", json=body)
    step("create throwaway user", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    doc = r.json()
    invite = doc.get("invite")
    step("mail sent", bool(invite and invite.get("ok")), str(invite))
    print(f"Check {args.target_email}'s inbox for the invite. Clean up with: DELETE /api/v1/users/{doc['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
