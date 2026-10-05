# Ramen MCPB bundle

Published on Smithery as `bkraad47/ramen` (local/stdio). It runs `ramen-mcp-bridge` (pinned in `pyproject.toml`)
against your own Ramen deployment; `manifest.json` `user_config` maps to the bridge's `RAMEN_BRIDGE_*` /
`RAMEN_MCP_KEY` environment. Needs `uv` on the user's machine.

Release: bump `version` in `manifest.json` and `pyproject.toml`, then
`npx @anthropic-ai/mcpb validate manifest.json && npx @anthropic-ai/mcpb pack . ramen-<version>.mcpb` and
`npx @smithery/cli mcp publish ./ramen-<version>.mcpb -n bkraad47/ramen` (Smithery CLI v4.11 rejects
`server.type: "uv"`, hence `python` + `uv run`).
