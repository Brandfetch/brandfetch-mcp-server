import asyncio


def test_app_loads():
    """Build the FastMCP app."""
    from src.main import app, mcp

    assert mcp.name == "brandfetch-mcp-server"
    assert app is not None
    # Server instructions steer clients to direct CDN fetch when egress allows
    assert "*.brandfetch.io" in (mcp.instructions or "")


def test_asset_resource_template_registered():
    """The bf://asset/{domain}/{type} resource template is exposed."""
    from src.main import mcp

    templates = asyncio.run(mcp.list_resource_templates())
    uris = {t.uri_template for t in templates}
    assert "bf://asset/{domain}/{type}" in uris


def test_resolve_asset_src_picks_matching_type():
    from src.main import _resolve_asset_src

    brand = {
        "domain": "nike.com",
        "logos": [
            {"type": "icon", "formats": [{"src": "https://cdn/icon.webp"}]},
            {
                "type": "logo",
                "formats": [
                    {"src": "https://cdn/logo.svg", "format": "svg"},
                    {"src": "https://cdn/logo.png", "format": "png"},
                ],
            },
        ],
    }

    assert _resolve_asset_src(brand, "logo") == "https://cdn/logo.svg"
    assert _resolve_asset_src(brand, "icon") == "https://cdn/icon.webp"
    assert _resolve_asset_src(brand, "symbol") is None
    assert _resolve_asset_src({}, "logo") is None


def test_asset_resource_links_builds_unique_typed_links():
    from src.main import _asset_resource_links

    brand = {
        "domain": "nike.com",
        "logos": [
            {"type": "icon", "formats": [{"src": "x", "format": "png"}]},
            {"type": "logo", "formats": [{"src": "y", "format": "svg"}]},
            # duplicate type (e.g. light/dark) should not produce a second link
            {"type": "logo", "formats": [{"src": "z", "format": "png"}]},
            # "other" is not an exposed asset type
            {"type": "other", "formats": [{"src": "w", "format": "png"}]},
        ],
    }

    links = _asset_resource_links(brand)
    by_uri = {str(link.uri): link for link in links}

    assert set(by_uri) == {
        "bf://asset/nike.com/icon",
        "bf://asset/nike.com/logo",
    }
    assert all(link.type == "resource_link" for link in links)
    assert by_uri["bf://asset/nike.com/icon"].mimeType == "image/png"
    assert by_uri["bf://asset/nike.com/logo"].mimeType == "image/svg+xml"


def test_asset_resource_links_no_domain():
    from src.main import _asset_resource_links

    assert _asset_resource_links({}) == []
    assert _asset_resource_links({"logos": [{"type": "icon"}]}) == []


def test_validate_asset_url_preserves_format_extension():
    """Extension URLs from get_brand must be fetched as given — rewriting them
    to the extensionless type/{type} endpoint lets the CDN pick the format
    (WebP), silently replacing an explicitly requested SVG/PNG/JPEG."""
    from src.main import _validate_asset_url

    for url in (
        "https://cdn.brandfetch.io/id_0dwKPKT/theme/light/logo.svg?c=tok",
        "https://cdn.brandfetch.io/id_0dwKPKT/w/399/h/399/theme/dark/icon.jpeg?c=tok",
        "https://cdn.brandfetch.io/id_0dwKPKT/w/800/h/278/theme/light/logo.png?c=tok",
        "https://cdn.brandfetch.io/nike.com/w/400/h/400/theme/dark/type/icon?c=tok",
    ):
        assert _validate_asset_url(url) == url


def test_redirect_host_error_flags_hotlink_policy():
    import json

    from src.main import _redirect_host_error

    hotlink = json.loads(_redirect_host_error("docs.brandfetch.com"))
    assert hotlink["code"] == "hotlink_blocked"
    assert "?c=" in hotlink["message"]

    other = json.loads(_redirect_host_error("evil.example.com"))
    assert other["code"] == "fetch_failed"
    assert "evil.example.com" in other["message"]


def test_wrap_base64_lines_are_anchored_and_lossless():
    import base64

    from src.main import ASSET_BASE64_LINE_WIDTH, _wrap_base64

    encoded = base64.b64encode(bytes(range(256)) * 20).decode("ascii")
    wrapped = _wrap_base64(encoded)

    lines = wrapped.split("\n")
    assert all(len(line) == ASSET_BASE64_LINE_WIDTH for line in lines[:-1])
    assert 0 < len(lines[-1]) <= ASSET_BASE64_LINE_WIDTH
    # decoders ignore the newlines; joining restores the original exactly
    assert wrapped.replace("\n", "") == encoded
    assert base64.b64decode(wrapped) == base64.b64decode(encoded)

    assert _wrap_base64("") == ""
    assert _wrap_base64("abcd") == "abcd"


def test_build_logo_urls_without_client_id_warns():
    from src.main import _client_id_var, build_logo_urls

    token = _client_id_var.set("")
    try:
        result = asyncio.run(build_logo_urls(identifiers=["nike.com"]))
    finally:
        _client_id_var.reset(token)

    assert set(result) == {"urls", "warning"}
    assert result["urls"] == [
        "https://cdn.brandfetch.io/nike.com/theme/dark/fallback/404"
    ]
    assert "?c=" not in result["urls"][0]
    assert "hotlinking" in result["warning"]


def test_build_logo_urls_with_client_id_returns_plain_list():
    from src.main import _client_id_var, build_logo_urls

    token = _client_id_var.set("myClientId")
    try:
        result = asyncio.run(
            build_logo_urls(identifiers=["nike.com"], type="logo", width=400)
        )
    finally:
        _client_id_var.reset(token)

    assert result == [
        "https://cdn.brandfetch.io/nike.com/w/400/theme/dark/fallback/404/type/logo?c=myClientId"
    ]
