# Related versions: bridge and demo repo

The console release ([Versions](versions.md)) is what the charts pin. The two companion repositories version on
their own; this table says which go together.

| Ramen | `ramen-mcp-bridge` (PyPI) | `ramen-demo-mcp-group` | Notes |
|---|---|---|---|
| 0.6.0 | 0.2.0 | `main` with `mcp/env.yaml` | `mcp/env.yaml` is new and optional; the worker image installs bridge 0.2.0. |
| 0.5.95 | 0.2.0 | `main` | Per-group roles; the bridge gained `--oauth` (sign in as a person). |
| 0.5.93 | 0.1.0 | `main` | OAuth role mapping, `mcp_user`, scoped service-account permissions; RFC 9728 metadata fix found with Claude Code. |
| 0.5.8 | 0.1.0 | `main` | The published bridge server-tested on AWS and GCP. |
| 0.5.7 | 0.1.0 | `main` | The bridge moved to its own repo and package. |
| ≤ 0.5.6 | (in `ramen/runtime-py`) | `main` | |

Compatibility rules: the bridge speaks `ramen.v1.Mcp/Call`, unchanged since 0.3.1, so any bridge works against any
worker from 0.3.1 on; `--oauth` needs a console from 0.5.95 (any loopback redirect host and port is accepted). The demo repo
follows the group repo contract ([Protos](wiki/protos.md)), unchanged since 0.1.0 except for additions (`env.yaml`
in 0.6.0, optional).
