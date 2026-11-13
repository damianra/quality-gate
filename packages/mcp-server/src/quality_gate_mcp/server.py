"""MCP server exposing the quality-gate tools.

Default transport is stdio (for IDE clients / subprocess spawning);
``--transport sse`` exposes an HTTP endpoint for the compose stack or
manual debugging.

Usage:
    quality-gate-mcp                          # stdio
    python -m quality_gate_mcp.server --transport sse --port 8000
"""

from __future__ import annotations

import argparse
import logging

from mcp.server.mcpserver import MCPServer

from . import tools

logger = logging.getLogger("quality-gate-mcp")

mcp = MCPServer("quality-gate-mcp")

# Register every tool function. Registration is explicit so `tools.py`
# remains importable (and unit-testable) without the MCP runtime.
for _fn in (
    tools.list_prs,
    tools.get_pr_diff,
    tools.get_file_diff,
    tools.analyze_pr_signals,
    tools.search_code,
    tools.spawn_process,
    tools.post_verdict,
    tools.create_pr,
):
    mcp.tool()(_fn)


def main() -> None:
    parser = argparse.ArgumentParser(prog="quality-gate-mcp")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level)
    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
