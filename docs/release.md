# Releases

| | Where | Latest |
|---|---|---|
| **Ramen** | [GitHub releases](https://github.com/bkraad47/ramen/releases), tagged `v<version>` with zips built by CI | [v0.6.0](https://github.com/bkraad47/ramen/releases/tag/v0.6.0) |
| **ramen-mcp-bridge** | [PyPI](https://pypi.org/project/ramen-mcp-bridge/) and [GitHub](https://github.com/bkraad47/ramen-mcp-bridge) | [0.2.2](https://pypi.org/project/ramen-mcp-bridge/0.2.2/) |
| **ramen-demo-mcp** | [GitHub](https://github.com/bkraad47/ramen-demo-mcp-group), the demo group repo | `main` |

```sh
pip install ramen-mcp-bridge==0.2.2
```

## Which versions go together

| Ramen | Bridge on PyPI | Demo repo | Notes |
|---|---|---|---|
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

## Every release

The [version tracker](versions.md) is generated from the changelog with a date and a release link per version.
The [architecture](architecture/index.md) page keeps a snapshot per minor release.
