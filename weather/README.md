# weather — NWS reference MCP server

A minimal MCP server that exposes the US National Weather Service
(NWS) API to an MCP client over **stdio** transport. Kept in this
repo as a reference implementation and Claude Desktop demo; it
predates the production servers (`mcp-k8s`, `mcp-external`) and
shows the simplest possible FastMCP surface.

## Tools

| Tool | Signature | Purpose |
|---|---|---|
| `get_alerts` | `get_alerts(state: str) -> str` | Active weather alerts for a US state (two-letter code, e.g. `"CA"`) |
| `get_forecast` | `get_forecast(latitude: float, longitude: float) -> str` | 5-period forecast for a coordinate pair |

Both tools return already-formatted text (not structured objects)
since the intended consumer is a chat UI.

## Scope limits

- **US only.** The NWS API does not cover locations outside the
  United States, so calls against non-US coordinates return
  empty / error responses.
- **Read-only.** No alerting, subscription, or write behavior.
- **No caching.** Every call hits the NWS API directly.

## Run locally

```bash
cd weather
uv sync            # first time only
uv run weather.py  # starts stdio-transport MCP server on the current terminal
```

The server is meant to be spawned by an MCP client (Claude Desktop,
MCP Inspector, etc.) rather than run standalone.

## Claude Desktop config

See the root [`README.md`](../README.md#claude-desktop-stdio--weather)
for the `claude_desktop_config.json` snippet.

## MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv run weather/weather.py
```

Or via the FastMCP CLI:

```bash
cd weather
fastmcp dev weather.py
```

## Technology

- [FastMCP](https://gofastmcp.com) — MCP server framework
- [httpx](https://www.python-httpx.org) — async HTTP client for the NWS API
- Python 3.12+ (pinned via `.python-version`)

## Relationship to the rest of the toolbox

This server is intentionally kept small and unchanged — it's the
"hello world" example for anyone exploring how an MCP server is
structured. The real work in this repo lives under
[`k8s/`](../k8s/README.md) and [`external/`](../external/README.md);
those are the servers consumed by production Persona experts.

## Credits

Weather data courtesy of the
[US National Weather Service](https://www.weather.gov/documentation/services-web-api).
