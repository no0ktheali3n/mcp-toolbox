# MCP Toolbox

A collection of Model Context Protocol (MCP) servers that extend agentic AI
runtimes with specialized tools. Each server is self-contained in its own
directory, versioned independently, and deployable on its own.

## Overview

MCP Toolbox hosts the servers the [Persona](https://gitea.apexanalytix.app/analytics/apex-ai-experts-persona)
expert agents consume (Kubernetes operations + external-systems access),
plus a reference weather server kept from the repo's original template.
Each server evolves on its own tag cadence so a breaking change in one
doesn't drag the others along.

## Project Structure

```
mcp-toolbox/
├── k8s/        # mcp-k8s — Kubernetes operations MCP server (consumed by Persona K8s SRE expert)
├── external/   # mcp-external — Gitea + OCI registry MCP server (Persona add-on A)
├── weather/    # reference National Weather Service MCP server (kept from template)
└── README.md   # this file
```

## Servers at a glance

| Server | Path | Transport | Port | Tool count | Latest tag |
|---|---|---|---|---|---|
| **mcp-k8s** | [`k8s/`](k8s/README.md) | streamable-http | 8000 | 23 (discovery / diagnosis / remediation / logs) | `v2.3.0` |
| **mcp-external** | [`external/`](external/README.md) | streamable-http | 8002 | 9 (4 OCI registry + 5 Gitea) | `external-v0.2.1` |
| Weather | [`weather/`](weather/) | stdio | — | 2 (alerts, forecast) | — (reference) |

**Tag conventions:**
- `v*` (e.g. `v2.3.0`) → mcp-k8s milestones
- `external-v*` (e.g. `external-v0.2.1`) → mcp-external milestones
- Active development branch: `v2.0-dev`. Milestones are tagged off this branch.

### mcp-k8s (`k8s/`)

Kubernetes operations MCP server. Backs Eidikos, the Persona K8s SRE
expert, via Agent Zero. 23 tools span:

- **Discovery** — `get_cluster_summary`, `search_resources`, `list_*`
- **Diagnosis** — event + log access (`get_events`, `get_pod_logs`,
  `get_resource_yaml`), pod-detail / container-state inspection
- **Remediation (guarded)** — `patch_resource_limits`,
  `restart_deployment`, `scale_deployment`, `rollback_deployment`
- **Log query (Loki)** — `query_loki`, `search_logs` via the
  Kubernetes API service-proxy (no direct network path required)

All mutation tools run through a configurable denylist
(`MCP_K8S_DENYLIST`) so operators can restrict what the agent is
allowed to change per environment.

See [`k8s/README.md`](k8s/README.md) for the full catalog, dev
workflow, kubeconfig setup, and the mutation-denylist contract.

### mcp-external (`external/`)

External-systems MCP server. Lets the agent cross out of Kubernetes
to diagnose cross-system failures (image tag-format mismatches,
invalid Helm values per schema, etc.). Currently read-only; write
tools are scoped for post-PoC.

- **OCI registry** — `harbor_list_tags`, `harbor_check_tag`,
  `harbor_list_projects`, `harbor_get_repository_info`
- **Gitea** — `gitea_get_file_content`, `gitea_get_chart_metadata`,
  `gitea_get_recent_commits`, `gitea_list_branches`, `gitea_search_code`

Validated end-to-end against Scenario 1 (image tag-format mismatch
between registry and Helm chart) at `external-v0.2.1`.

See [`external/README.md`](external/README.md) for env-var
configuration, the Gitea token bootstrap sequence, and the
streamable-http / dual-repo workflow.

### Weather (`weather/`)

Reference server kept from the repo's original template. Integrates
with the US National Weather Service API — `get_alerts(state)` and
`get_forecast(latitude, longitude)`. Useful as a minimal MCP
example and for Claude Desktop demos over stdio transport.
NWS-only, so US locations only.

## Installation

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for mcp-k8s and mcp-external — both run containerized)
- A running Kubernetes cluster for mcp-k8s (k3d recommended for dev)
- Running Gitea + OCI registry for mcp-external (a lightweight
  `registry:2` + `joxit/docker-registry-ui` pairing is documented
  in the Persona stack's `k8/docker/registry/` compose file)
- [Claude Desktop](https://claude.ai/download) (only for stdio/weather)

### Setup

```bash
git clone https://github.com/no0ktheali3n/mcp-toolbox.git
cd mcp-toolbox
```

Each server has its own build/run instructions — see the subfolder READMEs.

## Transport notes

- **mcp-k8s** and **mcp-external** use **streamable-http** transport and
  run inside Docker. Consumed by Agent Zero (or any streamable-http-capable
  MCP client) over `http://<host>:<port>/mcp`.
- **Weather** uses **stdio** transport and is consumed by Claude Desktop
  via the `claude_desktop_config.json` pattern below.

### Claude Desktop (stdio) — weather

Add to `claude_desktop_config.json`
(on Windows at `%AppData%\Roaming\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "weather": {
      "command": "uv",
      "args": [
        "--directory",
        "C:/absolute/path/to/mcp-toolbox/weather",
        "run",
        "weather.py"
      ]
    }
  }
}
```

Restart Claude Desktop, confirm `weather` appears under connectors, and
verify that `get_alerts` + `get_forecast` are enabled.

### Agent Zero (streamable-http) — mcp-k8s + mcp-external

Agent Zero discovers MCP servers over HTTP. Typical container-to-container
URLs on the shared `traefik-proxy` Docker network used by the Persona stack:

```
http://mcp-k8s:8000/mcp
http://mcp-external:8002/mcp
```

Subfolder READMEs cover the full Agent Zero config block.

## Development

### Testing with MCP Inspector

```bash
# Weather (stdio)
npx @modelcontextprotocol/inspector uv run weather/weather.py

# mcp-k8s (HTTP)
npx @modelcontextprotocol/inspector --transport sse http://localhost:8000/mcp

# mcp-external (HTTP)
npx @modelcontextprotocol/inspector --transport sse http://localhost:8002/mcp
```

FastMCP CLI for iterative dev:

```bash
cd weather && fastmcp dev weather.py
```

### Adding a new server

1. Create a new directory: `mcp-toolbox/<server-name>/`
2. Implement the server using FastMCP (tool decorators + transport choice)
3. Add a subfolder `README.md` covering purpose, tool surface,
   env-var config, and dev loop
4. Add the server to this README's "Servers at a glance" table
5. Test with MCP Inspector before integrating with any agent runtime

### Dual-repo workflow (mcp-k8s / mcp-external)

The Persona stack ships a dev copy of each server inside the persona
repo (`persona/k8/docker/mcp-k8s/`, `persona/k8/docker/mcp-external/`)
so the MCP server runs on the same Docker network as Agent Zero and the
detector during integrated testing. **This repo is the authoritative
source** — Persona-side edits get mirrored back here at each milestone.
Don't let the two copies drift. See `external/README.md` "Dual-repo
note" and `k8s/README.md` for the exact mirror commands.

## Technology stack

- **MCP SDK** — Model Context Protocol implementation
- **FastMCP** — high-level Python framework for MCP servers
- **Transports** — stdio (weather / Claude Desktop) and streamable-http
  (mcp-k8s / mcp-external / Agent Zero)
- **Package management** — uv
- **Python** — 3.12+

## Consumers

- **[Persona — Eidikos (K8s SRE expert)](https://gitea.apexanalytix.app/analytics/apex-ai-experts-persona)**
  consumes `mcp-k8s` + `mcp-external` through Agent Zero. Other Persona
  experts (Hecate, Argus, Archon, Hephaestus) will land their own
  MCP surfaces here as they come online.

## Resources

- [Model Context Protocol Documentation](https://modelcontextprotocol.io)
- [FastMCP Documentation](https://gofastmcp.com)
- [National Weather Service API](https://www.weather.gov/documentation/services-web-api)

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

- Built on [FastMCP](https://github.com/jlowin/fastmcp)
- Weather data from the [US National Weather Service](https://www.weather.gov)
- Follows the [Model Context Protocol](https://modelcontextprotocol.io) specification

---

**Last updated:** 2026-04-23
**Latest tags:** `v2.3.0` (mcp-k8s) · `external-v0.2.1` (mcp-external)
