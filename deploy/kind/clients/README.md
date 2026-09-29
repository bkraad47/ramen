# Independent MCP clients against the kind cluster (U24 / D23)

Two configs, both verified by hand against `make kind-up` (zone `b`, `localhost:18082`). Substitute your own
`rmk_` key — mint one on the group page or with
`POST /api/v1/groups/demo/mcp-keys {"name":"laptop"}` — and **deploy** afterwards: a key only reaches the workers
once a deploy has written it into the zone's key set.

| File | Client | Where it goes |
|---|---|---|
| `claude-code.mcp.json` | Claude Code | `.mcp.json` in the project root, or `claude mcp add` (below) |
| `cursor.mcp.json` | Cursor | `~/.cursor/mcp.json` (global) or `<project>/.cursor/mcp.json` |

Both are plaintext h2c to localhost. Through a cloud load balancer, replace `--insecure` with
`--tls` (and `--ca <pem>` while the certificate is self-signed) and point `--target` at `<lb-host>:443` —
see `deploy/local/mcp-client-config.example.json`'s `_DEPLOYED` block.

## Claude Code, without editing a file

```sh
claude mcp add ramen --scope project -- \
  /path/to/ramen/runtime-py/.venv/bin/ramen-mcp-bridge \
  --target localhost:18082 --key 'rmk_…' --group demo --zone b --insecure
claude mcp list          # ramen: … - ✓ Connected
claude -p "Use the ramen MCP server to multiply 6 by 7 with demo_calculator_tool."
```

`ramen-mcp-bridge` comes from `pip install 'ramen-runtime[grpc]'` or `runtime-py/.venv/bin/`.
