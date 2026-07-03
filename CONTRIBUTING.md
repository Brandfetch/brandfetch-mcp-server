# Contributing

Thanks for your interest in the Brandfetch MCP server!

## How this repository works

This repository is a **generated, public mirror**. The canonical source lives in
Brandfetch's internal monorepo, and the code here is regenerated from it. That
means:

- Pull requests are very welcome, but they may be **replayed into the internal
  source** rather than merged directly — your change will still land, just
  through our sync process, and you'll be credited.
- Some files you'd expect (deployment/infra configs) are intentionally not
  published.

If you're planning a larger change, please **open an issue first** so we can make
sure it fits before you invest the work.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11.

```bash
uv sync                 # install dependencies
uv run pytest           # run the tests
uv run ruff check .     # lint
uv run ruff format .    # format
```

Please make sure `uv run ruff check .` and `uv run pytest` pass before opening a
pull request. New tools or behavior changes should come with tests.

## Reporting issues

Open a GitHub issue with a clear description and, where relevant, a minimal
reproduction (the tool call and arguments you used, and what you expected).
