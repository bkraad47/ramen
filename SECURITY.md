# Security policy

## Reporting a vulnerability
Open a **private security advisory** on this repository (Security → Report a vulnerability). Say what you found, how
to reproduce it and which release. Expect an acknowledgement within a few days, a fix in a patch release and credit in
the changelog if you want it. Please do not open a public issue for a vulnerability.

## Supported versions
The latest release on `main` receives fixes. Pre-1.0: there is no long-term branch.

## What the console reads from its environment
Two variables are secrets and are only ever read from the process environment, never written anywhere:

| Variable | Purpose |
|---|---|
| `RAMEN_FERNET_KEY` | encrypts stored secrets, session secrets and tokens at rest |
| `RAMEN_ADMIN_PASSWORD` | the bootstrap super admin's first password (the deploy prompts for it) |

They are sent nowhere. Every other `RAMEN_*` variable is configuration (store, cloud project, public URL, log root);
the Config page shows them with secret values masked. The per-call threat model, what is verified and what is not, is
in [docs/threat-model.md](docs/threat-model.md).

## Readable code
Everything in this repository is source. The one vendored third-party file, the console's htmx, is shipped unminified
with its version and checksum in `console/src/ramen_console/static/VENDOR.md` so a reviewer can read what runs in the
browser.
