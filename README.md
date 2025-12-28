# MCP Toolbox

A collection of Model Context Protocol (MCP) servers that extend Agentic capabilities with specialized tools and integrations.  Tested locally with Claude Desktop via stdio transport.

## Overview

MCP Toolbox is intended as a growing library of MCP servers, each providing focused functionality for AI assistants. Each tool is self-contained in its own directory and can be used independently or as part of a larger integration.

## Project Structure

```
mcp-toolbox/
├── weather/          # National Weather Service integration
└── [future-tools]/   # Additional tools coming soon
```

## Available Tools

### Weather

**Location:** `weather/`

Integration with the National Weather Service (NWS) API for real-time weather information.

**Tools:**
- `get_alerts(state)` - Retrieve active weather alerts for a US state (two-letter code)
- `get_forecast(latitude, longitude)` - Get detailed weather forecast for specific coordinates

**Features:**
- Real-time weather alerts with severity levels and safety instructions
- Detailed 5-period forecasts including temperature, wind, and conditions
- Direct integration with official NWS API
- US locations only (NWS limitation)

**Usage Example:**
```python
# Get alerts for California
get_alerts(state="CA")

# Get forecast for Sacramento
get_forecast(latitude=38.5816, longitude=-121.4944)
```

## Installation

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Desktop](https://claude.ai/download) (for local integration)

### Setup

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd mcp-toolbox
   ```

2. **Install a specific tool:**
   ```bash
   cd weather
   uv sync
   ```

## Configuration

### For Claude Desktop (stdio)

Add to (or create) `claude_desktop_config.json` (exists in %AppData%/Roaming/Claude aka Users/user-name/AppData/Roaming/Claude in Windows):

**Windows:**
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

#### **Usage:**
After restarting Claude Desktop, verify `weather` shows up under connectors and that it has access to the tools `get_alerts()` and `get_forecast()` and that they are enabled.

When enabled in Claude Desktop:
```Claude
What is the weather in and current alerts for Sacramento, California?
```

### For HTTP Deployment

Run the server with streamable HTTP transport:

```python
# in tool logic where server is initialized
mcp.run(transport="http", host="localhost", port=8000)
```

```bash
cd weather
uv run weather.py  # Configure transport in weather.py
```

- Running this server makes endpoint accessible from http://localhost:8000/mcp

**REF: https://gofastmcp.com/deployment/http**

**NOTE:**  This format isn't supported by Claude Desktop, currently only stdio.  Need to explore sse(deprecated)/http-streamable transports for remote / enterprise access.

## Development

### Testing with MCP Inspector

Use the MCP Inspector to test servers during development:

```bash
# Direct with npx
npx @modelcontextprotocol/inspector uv run weather/weather.py

# Or with FastMCP CLI
cd weather
fastmcp dev weather.py
```

### Adding New Tools

1. Create a new directory: `mcp-toolbox/tool-name/`
2. Implement your MCP server using FastMCP or the official SDK
3. Add documentation to this README
4. Test with MCP Inspector before integrating

## Technology Stack

- **MCP SDK:** Model Context Protocol implementation
- **FastMCP:** High-level Python framework for MCP servers
- **Transport:** stdio (Claude Desktop) and Streamable HTTP (remote)
- **Package Management:** uv

## Roadmap

- [ ] Additional weather providers? (OpenWeather, Weather.gov alternatives)
- [ ] Database integration tools
- [ ] API connectivity tools
- [ ] File system operations
- [ ] Web scraping utilities

## Contributing

This is a personal toolbox project, but suggestions and ideas are welcome through issues.

## Resources

- [Model Context Protocol Documentation](https://modelcontextprotocol.io)
- [FastMCP Documentation](https://gofastmcp.com)
- [Claude Desktop](https://claude.ai/download)
- [National Weather Service API](https://www.weather.gov/documentation/services-web-api)

## License

[Add license here]

## Acknowledgments

- Built with [FastMCP](https://github.com/jlowin/fastmcp)
- Weather data from [National Weather Service](https://www.weather.gov)
- Follows the [Model Context Protocol](https://modelcontextprotocol.io) specification

---

**Current Version:** v1.0.0  
**Last Updated:** December 2025