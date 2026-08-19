# Brandfetch MCP Server

The official [Model Context Protocol](https://modelcontextprotocol.io) server for
[Brandfetch](https://brandfetch.com). It gives AI assistants access to brand
search, company data, logos and design assets, and LLM-ready brand context from
the Brandfetch API.

Full documentation: **https://docs.brandfetch.com/mcp/overview**

## Tools

| Tool                 | Description                                                                     |
| -------------------- | ------------------------------------------------------------------------------- |
| `brand_search`       | Search for brands by name using Brandfetch's search index.                      |
| `get_brand`          | Look up full brand data by domain, stock ticker, ISIN, or crypto symbol.        |
| `enrich_transaction` | Identify a merchant brand from a credit-card or bank-statement string.          |
| `get_brand_context`  | Get LLM-ready brand context for a domain — voice, audience, positioning, style. |
| `build_logo_urls`    | Construct Brandfetch Logo CDN URLs for one or more brands (no API call).        |
| `get_asset_base64`   | Fetch a Brandfetch CDN asset (logo, icon, symbol, image) as base64.             |
| `send_feedback`      | Report bugs, wrong or stale brand data, or missing capabilities to Brandfetch.  |

## Quick start (hosted server)

The easiest way to use this server is to connect to the Brandfetch-hosted
endpoint — no install, no infrastructure:

```
https://mcp.brandfetch.io/mcp
```

Authentication uses a Brandfetch MCP token (a `bf1.` bearer token). Generate one
from the **Keys and MCP** page in the
[Brandfetch dashboard](https://developers.brandfetch.com), or connect via OAuth
from a client that supports it.

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "brandfetch": {
      "type": "http",
      "url": "https://mcp.brandfetch.io/mcp",
      "headers": {
        "Authorization": "Bearer bf1.YOUR_TOKEN"
      }
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json` (or the project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "brandfetch": {
      "url": "https://mcp.brandfetch.io/mcp",
      "headers": {
        "Authorization": "Bearer bf1.YOUR_TOKEN"
      }
    }
  }
}
```

Clients that support OAuth can instead point at `https://mcp.brandfetch.io/mcp`
with no token and complete the authorization flow in the browser.

## Self-hosting

This is an HTTP (streamable-http) MCP server built with
[FastMCP](https://github.com/jlowin/fastmcp). You can run your own instance with
Docker.

```bash
docker build -t brandfetch-mcp-server .
docker run --rm -p 8080:8080 brandfetch-mcp-server
```

The server then listens on `http://localhost:8080/mcp`. Point your MCP client at
that URL and send your Brandfetch credentials as a `Bearer` token.

> The `Dockerfile` bundles the [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)
> and a few `AWS_LWA_*` environment variables. These are inert outside of AWS
> Lambda and can be ignored (or removed) for a plain container/host deployment.

### Running without Docker

```bash
uv sync
uv run uvicorn src.main:app --host 0.0.0.0 --port 8080
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11.

```bash
uv sync                 # install dependencies
uv run pytest           # run the tests
uv run ruff check .     # lint
uv run ruff format .    # format
```

## License

[MIT](./LICENSE)
