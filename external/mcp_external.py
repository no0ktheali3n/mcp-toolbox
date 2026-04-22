"""mcp-external FastMCP server for Gitea + Harbor (M12 scaffolding).

This is the M12 skeleton. Only a placeholder health_check MCP tool and
a plain /health HTTP route are wired up at this stage. Actual Gitea
(gitea_*) and Harbor (harbor_*) tools land in follow-on streams per
plans/ADD_ON_A_EXTERNAL_SYSTEMS.md / POC_ROADMAP M12.

Conventions (mirror mcp-k8s):
- FastMCP, transport: streamable-http, port 8002
- All tools are async def decorated with @mcp.tool(...)
- Return dict/JSON, never raw YAML
- Tool names prefixed by domain (gitea_*, harbor_*) per the
  project-wide MCP tool namespacing rule (see persona/CLAUDE.md).
"""

import logging
import os

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-external")

MCP_SAFETY_MODE = os.environ.get("MCP_SAFETY_MODE", "full")

WRITE_TOOLS: set[str] = set()
DESTRUCTIVE_TOOLS: set[str] = set()

GITEA_URL = os.environ.get("GITEA_URL", "http://gitea:3000")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")
HARBOR_URL = os.environ.get("HARBOR_URL", "http://registry:5000")
HARBOR_USER = os.environ.get("HARBOR_USER", "")
HARBOR_PASSWORD = os.environ.get("HARBOR_PASSWORD", "")

mcp = FastMCP("mcp-external", host="0.0.0.0", port=8002)


def _status_payload() -> dict:
    return {
        "status": "ok",
        "server": "mcp-external",
        "version": "0.1.0-scaffold",
        "safety_mode": MCP_SAFETY_MODE,
        "endpoints": {
            "gitea": {
                "url": GITEA_URL,
                "configured": bool(GITEA_URL),
                "authenticated": bool(GITEA_TOKEN),
            },
            "harbor": {
                "url": HARBOR_URL,
                "configured": bool(HARBOR_URL),
                "authenticated": bool(HARBOR_USER and HARBOR_PASSWORD),
            },
        },
        "tools_planned": {
            "gitea": [
                "gitea_get_file_content",
                "gitea_get_chart_metadata",
                "gitea_get_recent_commits",
                "gitea_list_branches",
                "gitea_search_code",
            ],
            "harbor": [
                "harbor_check_tag",
                "harbor_list_tags",
                "harbor_get_repository_info",
                "harbor_list_projects",
            ],
        },
    }


def apply_safety_mode():
    """Remove tools based on MCP_SAFETY_MODE environment variable."""
    mode = os.environ.get("MCP_SAFETY_MODE", "full")

    if mode == "read-only":
        for tool_name in WRITE_TOOLS:
            if tool_name in mcp._tool_manager._tools:
                del mcp._tool_manager._tools[tool_name]
    elif mode == "non-destructive":
        for tool_name in DESTRUCTIVE_TOOLS:
            if tool_name in mcp._tool_manager._tools:
                del mcp._tool_manager._tools[tool_name]


@mcp.custom_route("/health", methods=["GET"])
async def http_health(request: Request) -> JSONResponse:
    return JSONResponse(_status_payload())


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def health_check() -> dict:
    """Return server health and configured external endpoints.

    Placeholder tool used to verify mcp-external is reachable from
    Agent Zero during M12 scaffolding. Reports which external systems
    are configured (URL set). It does NOT probe them yet.
    """
    return _status_payload()


# TODO (M12 follow-on)
# Gitea tools: gitea_get_file_content, gitea_get_chart_metadata,
#              gitea_get_recent_commits, gitea_list_branches,
#              gitea_search_code
# Harbor tools: harbor_check_tag, harbor_list_tags,
#               harbor_get_repository_info, harbor_list_projects
# See plans/ADD_ON_A_EXTERNAL_SYSTEMS.md for full contracts.


if __name__ == "__main__":
    apply_safety_mode()
    logger.info("mcp-external starting on 0.0.0.0:8002 (streamable-http)")
    logger.info("GITEA_URL=%s  HARBOR_URL=%s", GITEA_URL, HARBOR_URL)
    mcp.run(transport="streamable-http")
