# external — Gitea + Harbor MCP server

Standalone MCP tool server that exposes Gitea and Harbor (OCI
registry) operations to MCP clients. Companion to `../k8s/` in this
repo.

## Status

M12 scaffolding (2026-04-22). Ships with a placeholder `health_check`
MCP tool and a plain `/health` HTTP route so consumers can verify
connectivity and endpoint configuration. Real Gitea and Harbor tools
land in a follow-on release.

## Installation

```bash
cd external
docker build -t mcp-external .
docker run --rm -p 8002:8002 \
  -e GITEA_URL=https://gitea.example.com \
  -e GITEA_TOKEN=... \
  -e HARBOR_URL=https://harbor.example.com \
  -e HARBOR_USER=... \
  -e HARBOR_PASSWORD=... \
  mcp-external
```

Server listens on `0.0.0.0:8002` (streamable-http). Point your MCP
client at `http://<host>:8002/mcp`. Liveness: `GET /health`.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `GITEA_URL` | `http://gitea:3000` | Override for your Gitea instance |
| `GITEA_TOKEN` | _empty_ | Personal access token |
| `HARBOR_URL` | `http://registry:5000` | Works with Harbor or plain registry:2 |
| `HARBOR_USER` | _empty_ | Basic-auth credentials (Harbor mode) |
| `HARBOR_PASSWORD` | _empty_ | — |
| `MCP_SAFETY_MODE` | `full` | `full` / `read-only` / `non-destructive` |

## Planned tools

**Gitea (read):**
- `gitea_get_file_content`
- `gitea_get_chart_metadata` — parses `Chart.yaml` + `values.schema.json`
- `gitea_get_recent_commits`
- `gitea_list_branches`
- `gitea_search_code`

**Harbor / OCI Distribution (read):**
- `harbor_check_tag`
- `harbor_list_tags`
- `harbor_get_repository_info`
- `harbor_list_projects`

**Post-PoC write tools** (documented, not built): `gitea_create_pull_request`,
`gitea_update_file`, `harbor_retag_image`, `harbor_trigger_scan`.

## Conventions

- Python 3.12+ / FastMCP
- All tools: `@mcp.tool(...)` with `async def`
- Return dict/JSON, never raw YAML
- Tool names domain-prefixed (`gitea_*`, `harbor_*`) so multiple MCP
  servers can co-exist in a single agent runtime without collision

## Fixtures

`fixtures/` contains shell scripts that seed defective workloads used
to validate end-to-end scenarios. Placeholder today; wired up
alongside the real tools.
