# Contributing, modifying and using Ramen

Ramen is BSD-3-Clause licensed and built in the open. You can use it, change it and build on it; this page says what
is asked in return.

## Use and modify
- **Run it, fork it, adapt it** to your organization: different clouds, your own console pages, your own policy
  catalogue. The group repo contract ([the MCP repo](wiki/mcp-repo.md)) and the [interface contracts](CONTRACTS.md) are the
  stable surfaces to build against.
- **Keep the reference.** The license requires the copyright notice to stay with the code; beyond the letter of
  the license, the request is simple: say where it came from. A fork that keeps "based on Project Ramen" in its README and docs
  is welcome.
- **Not acceptable**: taking the project, renaming it, and presenting it as a new commercial product of your own
  with the origin removed. That is both against the spirit of this project and, where the notice is stripped,
  against the license.

## Contribute
1. Open an issue first for anything beyond a fix — the [version history](versions.md) and the
   [architecture decisions](architecture/index.md#decisions) explain why things are the way they are.
2. Tests first. Every component has a suite (`console/`, `runtime-py/`, `node-rs/`, `tests/` conformance) and CI
   runs them on Linux and Windows plus an end-to-end compose stack; the coverage gate is 90% per component.
3. Keep the contracts honest: a change to a surface in [CONTRACTS.md](CONTRACTS.md) updates the document in the
   same change, and a release note in `CHANGELOG.md` says what a user will notice.
4. No secrets in logs, no `unsafe-eval` in the console, no new dependency without a reason in the PR.
5. Small PRs merge; large ones get a design note first.

## Related repositories
| Repo | What | Versioning |
|---|---|---|
| [ramen](https://github.com/bkraad47/ramen) | the console, node, runtime, charts, docs | `v0.x.y` tags, releases built by CI |
| [ramen-mcp-bridge](https://github.com/bkraad47/ramen-mcp-bridge) | the stdio bridge, [on PyPI](https://pypi.org/project/ramen-mcp-bridge/) | its own `v0.x.y`; the worker image pins one |
| [ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group) | the demo group repo every guide deploys | `main`; environments pin a ref |

[Releases](release.md) tracks which versions go together.

## Reporting a security issue
Open a private GitHub security advisory on the repository rather than a public issue. The
[threat model](threat-model.md#reporting) says what is in scope and what to expect.
