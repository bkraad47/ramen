"""Ramen MCPB entry point: the bridge reads RAMEN_BRIDGE_* / RAMEN_MCP_KEY set from the bundle's user_config."""

import sys

from ramen_mcp_bridge.bridge import main

sys.exit(main())
