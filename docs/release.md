# Releases

| | Where | Latest |
|---|---|---|
| **Ramen** | [GitHub releases](https://github.com/bkraad47/ramen/releases), tagged `v<version>` with zips built by CI | [latest](https://github.com/bkraad47/ramen/releases/latest) |
| **ramen-mcp-bridge** | [PyPI](https://pypi.org/project/ramen-mcp-bridge/) and [GitHub](https://github.com/bkraad47/ramen-mcp-bridge) | [0.2.2](https://pypi.org/project/ramen-mcp-bridge/0.2.2/) |
| **ramen-demo-mcp** | [GitHub](https://github.com/bkraad47/ramen-demo-mcp-group), the demo group repo | `main` |

```sh
pip install ramen-mcp-bridge==0.2.2
```

## Which versions go together

| Ramen | Bridge on PyPI | Demo repo | Notes |
|---|---|---|---|
| 0.6.23 | 0.2.2 | `main` with `mcp/env.yaml` and `.github/workflows/ramen-deploy.yml` | self-contained image for MCP directories (`glama/Dockerfile`); Smithery bundle `mcpb/`; no worker or bridge change |
| 0.6.21 | 0.2.2 | `main` with `mcp/env.yaml` and `.github/workflows/ramen-deploy.yml` | new MCP registry title and description; no worker or bridge change |
| 0.6.2 | 0.2.2 | `main` with `mcp/env.yaml` and `.github/workflows/ramen-deploy.yml` | listed on the official MCP registry as `io.github.bkraad47/ramen`; no worker or bridge change |
| 0.6.1 | 0.2.2 | `main` with `mcp/env.yaml` and `.github/workflows/ramen-deploy.yml` | the worker image installs bridge 0.2.2: Windows token file in `%LOCALAPPDATA%` (0.2.1), `--ca` also verifies the console during `--oauth` (0.2.2) |
| 0.6.0 | 0.2.0 | `main` with `mcp/env.yaml` | `env.yaml` is new and optional; the worker image installs bridge 0.2.0 |
| 0.5.95 | 0.2.0 | `main` | the bridge gained `--oauth` |
| 0.5.93 | 0.1.0 | `main` | OAuth role mapping, `mcp_user`, scoped permissions |
| 0.5.7 to 0.5.8 | 0.1.0 | `main` | the bridge moved to its own package and was server-tested on both clouds |
| 0.5.6 and earlier | in `ramen/runtime-py` | `main` | |

The bridge speaks `ramen.v1.Mcp/Call`, unchanged since 0.3.1, so any bridge works against any worker from 0.3.1
on. `--oauth` needs a console from 0.5.95. The demo repo follows the group repo contract, unchanged since 0.1.0
except for additions.

## Where a release is published

Pushing a `v<version>` tag runs the `release` workflow. Only the maintainer's own tags on `bkraad47/ramen` publish
beyond GitHub (`github.actor == 'bkraad47'`); forks and other pushers stop after the GitHub release.

| Channel | How it updates |
|---|---|
| GitHub release | zips, the MCPB bundle `ramen-<version>.mcpb`, and `ramen-node-linux-amd64.zip` (the latest worker, no version in the name) |
| [Official MCP registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.bkraad47/ramen) | `server.json` published with GitHub OIDC; no secret |
| [Smithery](https://smithery.ai/servers/bkraad47/ramen) | the MCPB bundle, with the `SMITHERY_API_KEY` repository secret (skipped with a warning when unset) |
| [Glama](https://glama.ai/mcp/servers/bkraad47/ramen) | rebuilds from the repository and downloads the latest release's worker; nothing to publish |
| Docker Hub `bkraad47/ramen-worker`, `bkraad47/ramen-console` and GHCR `ghcr.io/bkraad47/…` (0.7.0) | multi-arch images tagged `<version>` and `latest`; GHCR with the workflow's own token, Docker Hub with `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` (skipped with a warning when unset). Both clouds can pull them straight, without `make push` |
| Demo repo tag | `ramen-demo-mcp-group` is tagged `v<version>` with the release it was verified against (`VERSION` file in the repo) |
| PulseMCP and other directories | read the official registry |

## How a release is made (since 0.7.0)

1. Work on branch `v<version>` (ramen, and the demo repo when it changes); test locally.
2. Open a pull request to `main`; CI runs lint, versions, Python, Rust, conformance on Linux and Windows, kind.
3. Merge when green. The merge runs `ci` again and `pages` (the docs site).
4. Tag `main` with `v<version>` (the demo repo too). The tag runs `release`, which publishes everything in the table.
Nothing goes to `main` without a pull request.

`check_versions.py` fails CI when `server.json` or the bundle is not at `VERSION`, or a description is over the
registry's 100 characters, because a registry version cannot be changed once published.

## Every release

The [version tracker](versions.md) is generated from the changelog with a date and a release link per version.
The [architecture](architecture/index.md) page keeps a snapshot per minor release.
