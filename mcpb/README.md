# Ramen MCPB bundle

Attached to each GitHub release as `ramen-<version>.mcpb` (local/stdio; open it in Claude Desktop). It runs `ramen-mcp-bridge` (pinned in `pyproject.toml`)
against your own Ramen deployment; `manifest.json` `user_config` maps to the bridge's `RAMEN_BRIDGE_*` /
`RAMEN_MCP_KEY` environment. Needs `uv` on the user's machine.

Release: bump `version` in `manifest.json` and `pyproject.toml`, then
`npx @anthropic-ai/mcpb validate manifest.json && npx @anthropic-ai/mcpb pack . ramen-<version>.mcpb` (the release
workflow packs it). Smithery shut down its API and CLI on 2026-10-31, so the bundle is no longer published there; the
`bkraad47/ramen` Smithery listing is not updated.
