def test_app_loads():
    """Build the FastMCP app."""
    from src.main import app, mcp

    assert mcp.name == "brandfetch-mcp-server"
    assert app is not None
