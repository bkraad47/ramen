# ramen-bridge

`ramen-mcp-bridge` is a stdio MCP server that forwards every JSON-RPC message to a Ramen worker's
`ramen.v1.Mcp/Call` over gRPC. It exists for MCP clients that can only start a local process (stdio) —
Claude Desktop, Cursor, and the reference `mcp` SDK all speak Streamable HTTP directly and don't need it; see the
[quickstart](https://github.com/bkraad47/ramen/blob/main/docs/how-tos/local-quickstart.md) for that path instead.

Source lives in the main [`ramen`](https://github.com/bkraad47/ramen) repo, under `runtime-py/` (package
`ramen-runtime`, entry point `ramen-mcp-bridge`). This repo (`ramen-mcp-grpc`) is its public home for install/use
docs; the code itself moves here in a later release. For now, install straight from a `ramen` checkout.

## Install
```sh
git clone https://github.com/bkraad47/ramen
uv tool install './ramen/runtime-py[grpc]'      # → ~/.local/bin/ramen-mcp-bridge
```

## Use

Against a **local** worker (plaintext, no TLS):
```sh
RAMEN_MCP_KEY="$KEY" ramen-mcp-bridge --target localhost:8080 --insecure --group demo --zone local
```

Against a **cloud** deployment (GCP GKE or AWS EKS) fronted by a load balancer with a Google-managed
certificate (the default since v0.5.5 — see [`deploy/README.md`](https://github.com/bkraad47/ramen/blob/main/deploy/README.md)):
```sh
ramen-mcp-bridge --target <public-hostname>:443 --tls --key rmk_… --group demo --zone a
```
`--tls` alone verifies against your system's CA trust store — nothing to download, no `kubectl` command, because
the certificate is issued by a public CA, not self-signed. (An older cluster still using the self-signed fallback
needs the cluster's CA pulled once via `kubectl -n ramen-system get secret ramen-console-tls -o
jsonpath='{.data.tls\.crt}' | base64 -d > ramen-lb.pem` and a `--ca ramen-lb.pem` flag — avoid this if you can,
it's what v0.5.5 I11 replaced.)

Point an MCP client at it directly:
```json
{"mcpServers": {"ramen-stdio": {"command": "ramen-mcp-bridge",
  "env": {"RAMEN_BRIDGE_TARGET": "<public-hostname>:443", "RAMEN_BRIDGE_TLS": "1",
          "RAMEN_MCP_KEY": "rmk_…", "RAMEN_BRIDGE_GROUP": "demo", "RAMEN_BRIDGE_ZONE": "a"}}}}
```

Every flag has a `RAMEN_BRIDGE_*` environment variable equivalent (`--target` → `RAMEN_BRIDGE_TARGET`, `--tls` →
`RAMEN_BRIDGE_TLS=1`, `--group` → `RAMEN_BRIDGE_GROUP`, `--zone` → `RAMEN_BRIDGE_ZONE`) so the key never has to sit
in a client's config file — `RAMEN_MCP_KEY` (or `RAMEN_BRIDGE_KEY`) is enough. `--health [SERVICE]` checks
`grpc.health.v1` and exits 0 when SERVING, for a liveness probe.

License: BSD-3-Clause, same as `ramen`.
