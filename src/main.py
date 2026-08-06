import contextvars
import base64
import hashlib
import json
import os
from typing import Literal
from urllib.parse import parse_qsl, urlparse

from fastmcp import FastMCP
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.tools.tool import ToolResult
import httpx
from mcp.types import ResourceLink, TextContent
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .utils import events

BRANDFETCH_API_BASE = os.environ.get(
    "BRANDFETCH_API_BASE_URL", "https://api.brandfetch.io/v2"
)
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.brandfetch.io")
OAUTH_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
OAUTH_WELL_KNOWN_URL = f"{MCP_BASE_URL}{OAUTH_PROTECTED_RESOURCE_PATH}"
OAUTH_SERVER_URL = os.environ.get(
    "OAUTH_SERVER_URL", "https://developers.brandfetch.com"
)


mcp = FastMCP(
    "brandfetch-mcp-server",
    instructions=(
        "Brand data and assets (logos, icons, colors, fonts) from Brandfetch.\n\n"
        "When a task needs actual image bytes, prefer fetching the "
        "credentialed asset 'src' URLs from get_brand directly (curl, "
        "requests, fetch) whenever your execution environment can reach "
        "cdn.brandfetch.io — that keeps image bytes out of the conversation. "
        "Route bytes through this server (the bf://asset/{domain}/{type} "
        "resource, or get_asset_base64 as last resort) only when the CDN is "
        "unreachable from your environment. On claude.ai, direct access "
        "requires adding *.brandfetch.io to the sandbox domain allowlist "
        "(Settings > Capabilities); suggest that to users who embed brand "
        "assets regularly. Each tool documents its own usage details."
    ),
)

ASSET_URL_ALLOWED_HOSTS = {"cdn.brandfetch.io"}
# Where the CDN 302s programmatic fetches of URLs lacking a valid ?c= clientId
# (hotlinking policy: tokenless URLs may only be displayed in web pages).
ASSET_HOTLINK_REDIRECT_HOST = "docs.brandfetch.com"
ASSET_URL_ALLOWED_TYPES = {"icon", "logo", "symbol"}
ASSET_MAX_DIMENSION = 2048
ASSET_MAX_BYTES = 5 * 1024 * 1024
ASSET_FETCH_TIMEOUT_SECONDS = 5.0
BRAND_API_TIMEOUT_SECONDS = 35.0
ASSET_FETCH_USER_AGENT = "Brandfetch-MCP/1.0 (asset-base64-tool)"
ASSET_FETCH_CHUNK_SIZE = 64 * 1024
# MIME-style wrap width for base64 tool output. Clients that must reproduce
# the payload by hand (claude.ai code-mode retypes it into sandbox scripts)
# drop characters inside long identical-character runs; short newline-anchored
# lines make such corruption detectable and repairable line-by-line.
ASSET_BASE64_LINE_WIDTH = 76
ASSET_ALLOWED_MEDIA_TYPES = {
    "image/svg+xml",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
# Maps Brandfetch logo `format` strings to media types, used only to hint the
# mimeType on resource_link blocks (the actual blob mime is taken from the CDN
# response Content-Type at read time).
ASSET_FORMAT_MEDIA_TYPES = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "webp": "image/webp",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
}

FEEDBACK_SLACK_TIMEOUT_SECONDS = 5.0
# Slack rejects section blocks whose text exceeds 3000 characters; keep
# headroom for the truncation marker appended after escaping.
FEEDBACK_MAX_CHARS = 2900


# Per-request context for credentials, set by CredentialsMiddleware.
_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("api_key")
_client_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("client_id")
_org_urn_var: contextvars.ContextVar[str] = contextvars.ContextVar("org_urn")

# Version prefix for composite bearer tokens issued by the OAuth token endpoint.
# Format: "bf1." + base64url(JSON({"k": <apiKey>, "c": <clientId>})).
_COMPOSITE_TOKEN_PREFIX = "bf1."


def _decode_bearer_token(token: str) -> tuple[str, str, str]:
    """Decode a bf1 bearer token into (api_key, client_id, org_urn)."""
    if not token.startswith(_COMPOSITE_TOKEN_PREFIX):
        return token, "", ""

    encoded = token[len(_COMPOSITE_TOKEN_PREFIX) :]
    try:
        # Frontend emits unpadded base64url; re-add padding before decoding.
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return payload.get("k", ""), payload.get("c", ""), payload.get("o", "")
    except Exception:
        return token, "", ""


class CredentialsMiddleware(BaseHTTPMiddleware):
    """Extract API credentials and store them in per-request context vars.

    priority:
      1. Authorization: Bearer <token>  (OAuth / header-based — preferred)
      2. ?apiKey= query parameter       (legacy method, kept for backward compatibility)
    """

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            api_key, token_client_id, org_urn = _decode_bearer_token(
                auth_header[7:].strip()
            )
        else:
            api_key, token_client_id, org_urn = (
                request.query_params.get("apiKey", ""),
                "",
                "",
            )

        client_id = token_client_id or request.query_params.get("clientId", "")
        _api_key_var.set(api_key)
        _client_id_var.set(client_id)
        _org_urn_var.set(org_urn)

        # If no credentials are present at all (a clientId alone is enough for
        # the keyless tools) start the OAuth flow using the well known.
        if not api_key and not client_id and request.url.path.rstrip("/") == "/mcp":
            return JSONResponse(
                {"error": "unauthorized", "message": MISSING_CREDENTIALS_MESSAGE},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="{MCP_BASE_URL}", '
                        f'resource_metadata="{OAUTH_WELL_KNOWN_URL}"'
                    )
                },
            )

        return await call_next(request)


class NonEmptyBodyMiddleware(BaseHTTPMiddleware):
    """Replace zero-length 202 bodies with a minimal JSON body.

    The MCP streamable-http transport answers notifications (e.g.
    notifications/initialized) with `202 Accepted` and an empty body. Behind a
    Lambda Function URL in RESPONSE_STREAM mode, the aws-lambda-web-adapter
    mishandles `Content-Length: 0` responses: the terminating chunk is never
    sent.
    see https://github.com/aws/aws-lambda-web-adapter/issues/499
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if (
            response.status_code == 202
            and response.headers.get("content-length") == "0"
        ):
            headers = {
                k: v for k, v in response.headers.items() if k != "content-length"
            }
            return Response(
                content=b"{}",
                status_code=202,
                headers=headers,
                media_type="application/json",
            )
        return response


SIGNUP_URL = "https://developers.brandfetch.com/register"
MISSING_CREDENTIALS_MESSAGE = (
    "Missing API credentials. Authenticate via OAuth when adding the Brandfetch "
    "connector.\n\n"
    f"Don't have an API key yet? Sign up for free at {SIGNUP_URL} . "
    "The free plan includes 100 requests per month."
)


def _get_api_key() -> str:
    key = _api_key_var.get("")
    if not key:
        raise ValueError(MISSING_CREDENTIALS_MESSAGE)
    return key


def _get_client_id() -> str:
    return _client_id_var.get("")


def _publish_event(event_name: str, payload: dict) -> None:
    client_id = _client_id_var.get("")
    org_urn = _org_urn_var.get("")
    if org_urn:
        actor_urn, actor_type = org_urn, "organization"
    elif client_id:
        actor_urn, actor_type = f"urn:brandfetch:api-client-id:{client_id}", "client-id"
    else:
        actor_urn, actor_type = "urn:brandfetch:api-key:anonymous", "anonymous"

    session = {
        "actor": {"type": actor_type, "urn": actor_urn},
        **({"http": {"clientId": client_id}} if client_id else {}),
    }
    events.publish(
        event_name,
        actor_urn,
        {
            **payload,
            "session": session,
            **({"clientId": client_id} if client_id else {}),
        },
    )


def _error_message(status: int, body: str, context: str) -> str:
    messages = {
        401: "Unauthorized: invalid or missing API key.",
        403: "Forbidden: your API key is invalid or does not have access to this resource. Double-check the apiKey in your MCP server URL.",
        404: context,
        429: "API key quota exceeded. Please check your plan limits.",
    }
    return messages.get(status, f"Brandfetch API error ({status}): {body}")


def _tool_error(code: str, message: str) -> str:
    return json.dumps({"code": code, "message": message})


def _normalize_media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type not in ASSET_ALLOWED_MEDIA_TYPES:
        return None
    return media_type


def _validate_asset_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("missing required 'url' argument")

    normalized_url = url.strip()
    parsed = urlparse(normalized_url)

    if not parsed.scheme or not parsed.netloc:
        raise ValueError("url must be a valid absolute URL")
    if parsed.scheme.lower() != "https":
        raise ValueError("url must use https")
    if parsed.username or parsed.password:
        raise ValueError("url must not include credentials")
    if parsed.fragment:
        raise ValueError("url must not include fragment")
    if parsed.port not in (None, 443):
        raise ValueError("url port must be 443")

    host = (parsed.hostname or "").lower()
    if host not in ASSET_URL_ALLOWED_HOSTS:
        raise ValueError(
            f"url host '{host or parsed.netloc}' is not an allowed Brandfetch CDN host"
        )

    if parsed.path != "/" and parsed.path.endswith("/"):
        raise ValueError("url path must not end with '/'")

    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if not path_segments:
        raise ValueError("url must include a path with at least a brand identifier")

    # The URL is fetched exactly as given. In particular, extension URLs from
    # get_brand (.../logo.svg, .../icon.jpeg) must NOT be rewritten to the
    # extensionless /type/{type} endpoint: that endpoint lets the CDN pick the
    # format (typically WebP), which silently discards an explicitly requested
    # format — e.g. a vector SVG would come back as a rasterized WebP.

    # Validate w/h dimensions wherever they appear in the path
    i = 0
    while i < len(path_segments) - 1:
        key = path_segments[i]
        if key in ("w", "h"):
            dim_name = "width" if key == "w" else "height"
            value = path_segments[i + 1]
            try:
                dimension = int(value)
            except ValueError as exc:
                raise ValueError(f"{dim_name} must be an integer") from exc
            if dimension <= 0:
                raise ValueError(f"{dim_name} must be greater than 0")
            if dimension > ASSET_MAX_DIMENSION:
                raise ValueError(
                    f"{dim_name} {dimension} exceeds maximum {ASSET_MAX_DIMENSION}"
                )
            i += 2
        else:
            i += 1

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in query_pairs:
        if key != "c":
            raise ValueError("only the 'c' query parameter is allowed")
        if not value:
            raise ValueError("query parameter 'c' must not be empty")
    if len(query_pairs) > 1:
        raise ValueError("query parameter 'c' must not be repeated")

    return normalized_url


def _wrap_base64(encoded: str) -> str:
    """Wrap a base64 string at ASSET_BASE64_LINE_WIDTH chars per line."""
    return "\n".join(
        encoded[i : i + ASSET_BASE64_LINE_WIDTH]
        for i in range(0, len(encoded), ASSET_BASE64_LINE_WIDTH)
    )


def _redirect_host_error(final_host: str) -> str:
    """Tool error for a fetch that redirected off the allowed CDN hosts.

    A redirect to docs.brandfetch.com is the CDN's hotlinking policy kicking in:
    the URL carries no valid ?c= clientId, so it is display-only and cannot be
    fetched programmatically. Surface that specifically — the generic
    disallowed-host message reads like a missing asset and sends callers down
    the wrong debugging path.
    """
    if final_host == ASSET_HOTLINK_REDIRECT_HOST:
        return _tool_error(
            "hotlink_blocked",
            "the CDN redirected to its hotlinking policy page: this URL has no "
            "valid '?c=' clientId, so it can only be displayed in a web page, "
            "not fetched programmatically. Use the asset 'src' URLs returned by "
            "get_brand, which carry per-request credentials.",
        )
    return _tool_error(
        "fetch_failed",
        f"redirect target host '{final_host}' is not an allowed Brandfetch CDN host",
    )


async def _stream_cdn_asset(source_url: str) -> tuple[bytes, str, int, int | None]:
    """Fetch a (pre-validated) cdn.brandfetch.io asset.

    Returns (data, media_type, size_bytes, status_code). Raises
    ValueError(_tool_error(...)) on any failure. Shared by the get_asset_base64
    tool and the bf://asset resource so the allowed-host, size, and media-type
    guards stay identical across both paths.
    """
    chunks: list[bytes] = []
    total_size = 0
    media_type: str | None = None
    status_code: int | None = None

    try:
        async with httpx.AsyncClient(timeout=ASSET_FETCH_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "GET",
                source_url,
                headers={
                    "User-Agent": ASSET_FETCH_USER_AGENT,
                    "Accept": "image/*",
                },
                follow_redirects=True,
            ) as response:
                status_code = response.status_code

                final_host = (urlparse(str(response.url)).hostname or "").lower()
                if final_host not in ASSET_URL_ALLOWED_HOSTS:
                    raise ValueError(_redirect_host_error(final_host))

                if not response.is_success:
                    raise ValueError(
                        _tool_error(
                            "fetch_failed",
                            f"upstream returned HTTP {response.status_code} for '{source_url}'",
                        )
                    )

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        content_length_bytes = int(content_length)
                    except ValueError:
                        content_length_bytes = None
                    if (
                        content_length_bytes is not None
                        and content_length_bytes > ASSET_MAX_BYTES
                    ):
                        raise ValueError(
                            _tool_error(
                                "asset_too_large",
                                f"asset exceeds {ASSET_MAX_BYTES} bytes; request a smaller variant via build_logo_urls",
                            )
                        )

                media_type = _normalize_media_type(response.headers.get("Content-Type"))
                if not media_type:
                    raise ValueError(
                        _tool_error(
                            "fetch_failed",
                            "upstream returned unsupported or missing image Content-Type",
                        )
                    )

                async for chunk in response.aiter_bytes(
                    chunk_size=ASSET_FETCH_CHUNK_SIZE
                ):
                    total_size += len(chunk)
                    if total_size > ASSET_MAX_BYTES:
                        raise ValueError(
                            _tool_error(
                                "asset_too_large",
                                f"asset exceeds {ASSET_MAX_BYTES} bytes; request a smaller variant via build_logo_urls",
                            )
                        )
                    chunks.append(chunk)
    except ValueError:
        raise
    except httpx.TimeoutException as exc:
        raise ValueError(
            _tool_error(
                "fetch_failed",
                f"upstream fetch timed out after {int(ASSET_FETCH_TIMEOUT_SECONDS * 1000)}ms",
            )
        ) from exc
    except httpx.HTTPError as exc:
        raise ValueError(
            _tool_error("fetch_failed", f"upstream fetch failed: {exc}")
        ) from exc

    return b"".join(chunks), media_type, total_size, status_code


async def _get_brand_json(identifier: str) -> dict:
    """Fetch brand data as parsed JSON (used by the bf://asset resource resolver)."""
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=BRAND_API_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{BRANDFETCH_API_BASE}/brands/{identifier}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if not resp.is_success:
        raise ValueError(
            _error_message(
                resp.status_code,
                resp.text,
                f'Brand not found for identifier "{identifier}".',
            )
        )
    return resp.json()


def _resolve_asset_src(brand: dict, asset_type: str) -> str | None:
    """Return a CDN src URL (with per-request ?c= token) for the given asset type.

    The token-bearing src from the brand response is what the CDN allows to be
    fetched programmatically — a plain clientId URL would be hotlink-blocked.
    """
    for logo in (brand or {}).get("logos") or []:
        if logo.get("type") != asset_type:
            continue
        for fmt in logo.get("formats") or []:
            src = fmt.get("src")
            if src:
                return src
    return None


def _asset_resource_links(brand: dict) -> list[ResourceLink]:
    """Build resource_link blocks (one per available asset type) for a brand.

    These are cheap URIs the client can read on demand via bf://asset/{domain}/{type};
    the bytes are only materialized when the client actually reads the resource.
    """
    domain = (brand or {}).get("domain")
    if not domain:
        return []

    links: list[ResourceLink] = []
    seen: set[str] = set()
    for logo in brand.get("logos") or []:
        asset_type = logo.get("type")
        if asset_type not in ASSET_URL_ALLOWED_TYPES or asset_type in seen:
            continue
        seen.add(asset_type)
        first_format = (logo.get("formats") or [{}])[0].get("format")
        links.append(
            ResourceLink(
                type="resource_link",
                uri=f"bf://asset/{domain}/{asset_type}",
                name=f"{domain} {asset_type}",
                description=(
                    f"{asset_type} image for {domain}, served as bytes through the MCP "
                    "server. Read this resource when the CDN URL cannot be fetched "
                    "directly (sandboxed or network-restricted environments)."
                ),
                mimeType=ASSET_FORMAT_MEDIA_TYPES.get(first_format),
            )
        )
    return links


@mcp.tool(
    annotations={
        "title": "Search Brands",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def brand_search(query: str) -> str:
    """Search for brands by name using Brandfetch's search index.

    Use this when you do NOT already know the brand's domain — for example,
    when the user gives a brand name with ambiguous or unknown domain
    ("Madame Kim", "the raclette brand", "starbuks"), or
    a name that could map to multiple companies ("Delta" — airline? faucets?
    dental?).

    For raw bank or card transaction strings, use enrich_transaction instead.

    Returns a ranked list of matches. Each match contains:
    - `brandId` (str): Brandfetch's internal ID.
    - `domain` (str): The brand's primary domain. Pass this to `get_brand`
      to fetch full details.
    - `name` (str): Display name.
    - `icon` (str): CDN URL to a small representation of the brand, suitable
      for autocomplete-style UIs. Display-only: embed it as returned (e.g. in
      an `<img>` tag) — it is not programmatically fetchable, and must not be
      edited. For other asset types, sizes, or downloadable bytes, call
      `get_brand`; for display-only variants, `build_logo_urls`.
    - `claimed` (bool): Whether the brand has officially claimed their listing.
      Claimed brands generally have higher-quality, brand-approved assets.
    - `verified` (bool): Whether Brandfetch has verified the listing.
    - `qualityScore` (float, 0–1): Completeness of the brand data.
    - `_score` (float): Search relevance for this query; higher = better match.
      Use to rank results; not comparable across different queries.

    Results are already sorted by relevance; the first result is usually the
    best match for well-known brands.

    Args:
        query: Brand name query string (e.g. "Madame Kim", "Nike").
    """
    params = {}
    client_id = _get_client_id()
    if client_id:
        params["c"] = client_id
    async with httpx.AsyncClient(timeout=BRAND_API_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{BRANDFETCH_API_BASE}/search/{query.strip()}", params=params
        )
    _publish_event(
        "mcp.brand.searched",
        {"query": query, "success": resp.is_success, "statusCode": resp.status_code},
    )
    if not resp.is_success:
        raise ValueError(
            _error_message(
                resp.status_code,
                resp.text,
                f'No brand search results found for query "{query}".',
            )
        )
    return resp.text


@mcp.tool(
    annotations={
        "title": "Get Brand Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_brand(identifier: str) -> ToolResult:
    """Look up full brand data by domain, stock ticker, ISIN, or crypto symbol.

    Call this directly when you have a confident identifier — either from a
    prior `brand_search` result, or from your own knowledge for well-known
    brands (e.g. you can call `get_brand("coca-cola.com")` directly without
    searching first). If the identifier is uncertain or the brand is obscure,
    use `brand_search` first to disambiguate.

    The identifier is auto-resolved in this order: domain → ticker → ISIN →
    crypto. Examples (all Nike): "nike.com", "NKE", "US6541061031". For
    crypto: "BTC", "ETH".

    For clean brand name lookups, prefer brand_search followed by get_brand —
    those are more reliable for unambiguous queries.

    Returns a brand object containing:
    - `name`, `domain`, `description`: Core identity.
    - `logos` (list): Typed visual assets. The `type` field distinguishes:
      * `logo`: The full brand mark, typically a wordmark or wordmark+symbol
         combination intended for headers, signatures, and contexts with
         horizontal space (e.g. "Coca-Cola" in script).
      * `symbol`: The standalone graphical mark without text, used when the
        brand is already identified by context (e.g. the Nike swoosh alone).
      * `icon`: A square, compact representation optimized for small sizes —
        favicons, app icons, avatars, list items. May be the symbol, an
        initial, or a simplified mark.
      * `other`: Anything that doesn't fit the above (mascots, seals, etc.).

      Pick by use case, not by name: a "show me the logo" request from a user
      usually wants `type: "logo"` for display, but `type: "icon"` for a
      small UI element like a list row or chat avatar.
    - `colors` (list): Brand colors as `{hex, type, brightness}` where `type`
      is `primary` | `accent` | `dark` | `light` | `brand`.
    - `fonts` (list): `{name, type, origin, originId}`.
    - `links` (list): Social and web links as `{name, url}`.
    - `company`: Metadata including `industries`, `employees`, `foundedYear`,
      `location`, and `kind` (public | private | etc.).

    **IMPORTANT — use all URLs exactly as returned:**
    Every URL in the response (logo `src` fields, image URLs, etc.) must be
    used verbatim. Do not modify, rewrite, or substitute any part of them —
    including the `?c=` query parameter. That token is a per-request
    credential issued by the API specifically for this response; replacing it
    with any other client ID (including one from context or memory) will break
    the URL.

    To download asset bytes, fetch the `src` URL directly (curl/requests)
    whenever your environment can reach cdn.brandfetch.io — that keeps the
    bytes out of the conversation. Otherwise fall back to the
    `resource_link` blocks this tool returns (`bf://asset/{domain}/{type}`),
    or to `get_asset_base64` — see that tool for the claude.ai
    network-allowlist details.

    Errors:
    - 403 / "explicit deny": The brand exists in the index but is not
      accessible on the current API tier or has access restrictions. Do not
      retry — fall back to displaying the `icon` URL from `brand_search`
      (display-only, not downloadable) or inform the user.
    - 404: Identifier not found. Try `brand_search` with a name query instead.

    Args:
        identifier: A domain ("nike.com"), ticker ("NKE"), ISIN
            ("US6541061031"), or crypto symbol ("BTC").
    """
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=BRAND_API_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{BRANDFETCH_API_BASE}/brands/{identifier}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    _publish_event(
        "mcp.brand.fetched",
        {
            "identifier": identifier,
            "success": resp.is_success,
            "statusCode": resp.status_code,
        },
    )
    if not resp.is_success:
        raise ValueError(
            _error_message(
                resp.status_code,
                resp.text,
                f'Brand not found for identifier "{identifier}".',
            )
        )

    # Attach resource_link blocks (one per available asset type) so the client
    # can read the image bytes on demand via bf://asset/{domain}/{type} instead
    # of fetching the CDN directly. Links are cheap; bytes load only on read.
    try:
        links = _asset_resource_links(resp.json())
    except (json.JSONDecodeError, ValueError):
        links = []
    return ToolResult(content=[TextContent(type="text", text=resp.text), *links])


@mcp.tool(
    annotations={
        "title": "Identify Merchant from Transaction",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def enrich_transaction(transaction_label: str, country_code: str) -> str:
    """Identify a merchant brand from a credit card or bank statement string.

    Uses AI-based matching to resolve abbreviated, truncated, or cryptic
    transaction labels (e.g. "SQ *COFFEE SHOP 4412", "AMZN MKTP US") to a
    brand. Use this when the input is a raw statement line rather than a
    clean brand name.

    Returns brand data matching the shape from `get_brand` (domain, name,
    logos, colors, etc.) for the identified merchant, or an error if no
    confident match is found. URLs in the response carry per-request
    credentials — use them exactly as returned, never edited (same rule as
    `get_brand`).

    Examples:
    - transaction_label: "STARBUCKS GENEVA", country_code: "CH"
    - transaction_label: "AMZN MKTP US", country_code: "US"
    - transaction_label: "IKEA", country_code: "CH"
    - transaction_label: "SQ *BLUE BOTTLE", country_code: "US"

    Args:
        transaction_label: The raw text from a credit card or bank statement
            (e.g. "STARBUCKS GENEVA", "AMZN MKTP US").
        country_code: ISO 3166-1 alpha-2 country code where the transaction
            occurred (e.g. "CH", "US"). Use "US" as a fallback when unknown.
    """
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=BRAND_API_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{BRANDFETCH_API_BASE}/brands/transaction",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "transactionLabel": transaction_label,
                "countryCode": country_code,
            },
        )
    _publish_event(
        "mcp.transaction.enriched",
        {
            "transactionLabel": transaction_label,
            "countryCode": country_code,
            "success": resp.is_success,
            "statusCode": resp.status_code,
        },
    )
    if not resp.is_success:
        raise ValueError(
            _error_message(
                resp.status_code,
                resp.text,
                f'Could not identify a merchant for transaction "{transaction_label}".',
            )
        )
    return resp.text


@mcp.tool(
    annotations={
        "title": "Get Brand Context",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_brand_context(domain: str) -> str:
    """Get LLM-ready brand context for a known domain — voice, audience, positioning, style.

    This is the *subjective* counterpart to `get_brand`. Use the two together
    by what kind of data you need:
    - `get_brand` → objective, structured facts: logos, colors, fonts, links,
      industry, employee count, founding year. Use when rendering UI, building
      a profile card, or citing firmographics.
    - `get_brand_context` (this tool) → probabilistic, interpretive data:
      how the brand sounds, who it talks to, what it values, what it sells,
      how it feels visually. Use when generating content, reasoning about
      fit, or grounding an LLM in a brand's identity.

    Both take the same domain and can be called in parallel when you need both.

    USE THIS WHEN: writing copy in a brand's voice, generating on-brand product
    descriptions, classifying whether content fits a brand, summarizing a
    company for a pitch, picking a target audience for a campaign, or
    describing a brand's aesthetic in words.

    For obscure or ambiguous brand names, run `brand_search` first to resolve
    the canonical domain, then call this with the result.

    Returns JSON with:
    - `meta.domain`, `meta.canonical_name`, `meta.resolved_at`: the resolved
      brand and when the context was generated. If `canonical_name` differs
      from what the user said (e.g. user said "MS", canonical is "Microsoft"),
      surface that.
    - `identity.tagline`, `identity.mission`: the brand's own words — quote
      verbatim, do not paraphrase.
    - `identity.description`: a neutral 1–2 sentence summary, safe to
      paraphrase.
    - `identity.tags`: short category labels for routing or classification.
    - `positioning.value_proposition`: use as headline framing in marketing
      or pitch contexts.
    - `positioning.target_audience[]`: distinct segments, each with its own
      description. Pick the segment that matches the user's task — do not
      blend them into a generic "everyone."
    - `positioning.products_and_services[]`: each entry has `name`, `type`
      ("product" | "service"), and `description`. Use to ground specific
      claims and avoid hallucinating offerings the brand doesn't have.
    - `brand.voice.summary`: prose description of the brand's tone, suitable
      as a system-prompt preamble when generating copy.
    - `brand.voice.attributes`: positive descriptors (e.g. "warm", "witty").
    - `brand.voice.avoid`: anti-patterns. Treat as hard constraints when
      generating copy, not soft preferences — if the list says "avoid hype",
      do not write hype.
    - `brand.style`: visual/design vibe in words (e.g. "clean, dense,
      utilitarian"). Useful for image-generation prompts or describing the
      brand's aesthetic. For actual color codes, fonts, or logo files, call
      `get_brand` instead.

    Note that this is generated content — language may occasionally be
    flowery or non-English. Treat it as a strong starting point, not gospel.

    Errors:
    - 503: Service temporarily unavailable. Retry once after a brief delay.
      If it persists, fall back to `get_brand` for the objective subset and
      tell the user that subjective brand context is currently unavailable.
    - 404: No context available for this domain. Try `brand_search` to find
      a canonical domain, then retry.
    - 403: Authentication or access tier issue. Do not retry; surface to the
      user.

    Args:
        domain: The brand's exact domain, lowercase, no scheme or path
            (e.g. "microsoft.com", "digitec.ch", "fleurdepains.ch"). If you
            only have a brand name, call `brand_search` first.
    """
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=BRAND_API_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{BRANDFETCH_API_BASE}/context/{domain.strip()}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    _publish_event(
        "mcp.brand-context.fetched",
        {
            "domain": domain,
            "success": resp.is_success,
            "statusCode": resp.status_code,
        },
    )
    if not resp.is_success:
        raise ValueError(
            _error_message(
                resp.status_code,
                resp.text,
                f'Brand context not found for domain "{domain}".',
            )
        )
    return resp.text


@mcp.tool(
    annotations={
        "title": "Build Logo URLs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def build_logo_urls(
    identifiers: list[str],
    identifier_type: Literal["domain", "ticker", "isin", "crypto"] | None = None,
    type: Literal["icon", "logo", "symbol"] = "icon",
    theme: Literal["light", "dark"] | None = "dark",
    width: int | None = None,
    height: int | None = None,
    fallback: Literal["brandfetch", "transparent", "lettermark", "404"] | None = "404",
) -> list[str] | dict[str, list[str] | str]:
    """Construct Brandfetch Logo CDN URLs for one or more brands. No API call
    is made — returns ready-to-embed URL strings.

    **HOTLINKING POLICY — read before using these URLs:**
    URLs returned by this tool are subject to Brandfetch's hotlinking policy.
    They are intended for direct browser rendering only (e.g. `<img src="...">`
    in an HTML page). Programmatic fetching or downloading of these assets —
    such as HTTP GET requests from a server or script using only the clientId
    embedded in the URL — will be blocked and return an error. If the session
    has no clientId at all, the URLs are built without a `?c=` token and the
    result is `{"urls": [...], "warning": "..."}` instead of a plain list —
    those URLs cannot be fetched programmatically under any circumstances.

    If you need to actually download or process an asset (save to disk, read
    pixel data, attach to an email, etc.), use `get_brand` instead. The CDN
    URLs embedded in `get_brand` responses carry per-request credentials that
    allow programmatic access.

    Use this tool when you want to:
    - Embed brand logos directly in a web page or UI component
    - Provide a displayable image URL to the user
    - Understand the URL grammar to tune dimensions, theme, or asset type
      for logos already obtained via `get_brand`

    Accepts multiple identifiers in a single call to avoid repeated round
    trips. All identifiers share the same display options (type, theme,
    dimensions, fallback). The returned list is in the same order as the
    input.

    URL pattern:
        https://cdn.brandfetch.io/{identifier_type}/{identifier}/w/{w}/h/{h}/theme/{theme}/fallback/{fallback}/type/{type}?c={clientId}

    Identifier types:
    - "domain": e.g. "nike.com"
    - "ticker": stock/ETF ticker, e.g. "NKE"
    - "isin": e.g. "US6541061031"
    - "crypto": e.g. "BTC", "ETH"

    When `identifier_type` is omitted the CDN auto-detects (domain → ticker
    → ISIN → crypto), but explicit typing avoids collisions.

    Asset types: "icon" (square, default), "logo" (horizontal wordmark),
    "symbol" (standalone mark without text).

    Fallback: defaults to "404" so missing assets are detectable. Other
    options: "brandfetch", "transparent", "lettermark" (icon only).

    Theme & 404 handling:
    The theme refers to the asset's own color, not the background:
    theme="light" returns a light-colored asset (white/pale) meant for
    display on dark backgrounds, and theme="dark" returns a dark-colored
    asset meant for light backgrounds. Most brands only have a dark-theme
    logo. When you request theme="light" and the URL returns 404, retry
    without the theme parameter (which gives the brand's default, usually
    dark). If that also 404s, the brand has no asset of this type — fall
    back to `get_brand` to retrieve whatever assets are available, or use
    the `icon` URL from `brand_search` results.

    Args:
        identifiers: List of brand identifiers (domains, tickers, ISINs, or
            crypto symbols). All must be the same type if identifier_type is set.
        identifier_type: Explicit identifier routing. Omit for auto-detection.
        type: Asset type — "icon", "logo", or "symbol". Default "icon".
        theme: "light" or "dark" for theme-specific variants. Omit for default.
        width: Width in pixels. Aspect ratio always preserved.
        height: Height in pixels. Aspect ratio always preserved.
        fallback: CDN behavior when asset is missing. "lettermark" only valid
            with type="icon". "404" returns HTTP 404 instead of a placeholder.
    """
    client_id = _get_client_id()

    # Build the shared suffix once
    suffix_parts: list[str] = []
    if width is not None:
        suffix_parts.append(f"w/{width}")
    if height is not None:
        suffix_parts.append(f"h/{height}")
    if theme is not None:
        suffix_parts.append(f"theme/{theme}")
    if fallback is not None:
        suffix_parts.append(f"fallback/{fallback}")
    if type != "icon":
        suffix_parts.append(f"type/{type}")
    suffix = "/".join(suffix_parts)

    query = f"?c={client_id}" if client_id else ""

    urls: list[str] = []
    for ident in identifiers:
        if identifier_type is not None:
            base = f"https://cdn.brandfetch.io/{identifier_type}/{ident}"
        else:
            base = f"https://cdn.brandfetch.io/{ident}"
        url = f"{base}/{suffix}" if suffix else base
        urls.append(f"{url}{query}")

    _publish_event(
        "mcp.logo-urls.built",
        {
            "identifierType": identifier_type,
            "type": type,
            "theme": theme,
            "count": len(identifiers),
        },
    )

    if not client_id:
        return {
            "urls": urls,
            "warning": (
                "No clientId is available on this session, so these URLs carry "
                "no '?c=' token. They can only be displayed inside a web page "
                "(e.g. an <img> tag); any programmatic fetch — curl, sandboxed "
                "code, or get_asset_base64 — will be redirected to the "
                "hotlinking policy page and blocked. To download asset bytes, "
                "use the 'src' URLs returned by get_brand instead."
            ),
        }
    return urls


@mcp.tool(
    annotations={
        "title": "Fetch Asset as Base64",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_asset_base64(url: str) -> dict[str, str | int]:
    """Fetch a Brandfetch CDN asset (logo, icon, symbol, image) and return it
    as line-wrapped, checksummed base64 for embedding in generated files.

    Use this when you need to embed a brand logo or image into a file generated
    in a sandboxed or network-restricted environment where cdn.brandfetch.io is
    unreachable — for example, when creating PPTX, DOCX, PDF, or HTML artifacts
    from a code-execution sandbox. This is the right tool whenever a direct
    download or embed of a logo, icon, or picture is needed and the CDN URL
    cannot be fetched by the caller.

    Do not use this when your environment can fetch the image URL itself —
    a browser rendering the page, or sandboxed code whose network allowlist
    covers `cdn.brandfetch.io`. In those cases fetch the `get_brand` `src`
    URL directly (curl/requests): it is faster and keeps base64 out of the
    conversation. If a sandbox fetch is blocked by network policy, tell the
    user they can allow it on claude.ai under Settings > Capabilities by
    enabling network egress and adding `*.brandfetch.io` to the domain
    allowlist — then this tool is not needed at all. Base64 increases
    payload size and bypasses CDN caching.

    If your client supports MCP resource reads, prefer the
    `bf://asset/{domain}/{type}` resource links returned by `get_brand` over
    this tool — the bytes are delivered out-of-band and skip the
    conversation entirely.

    Accepts any `cdn.brandfetch.io` asset URL — logos, icons, symbols, banners,
    hero images, and other brand-record assets. Prefer the `src` URLs from
    `get_brand`, whose `?c=` token carries per-request credentials:
    - `get_brand` src fields:    .../w/800/h/111/theme/light/logo.png?c=<token>
    - Brand-record images:       .../idwlR7BDjL/idu7P6rdmK.jpeg?c=<token>
    `build_logo_urls` output is generally NOT fetchable through this tool: those
    URLs are display-only under the hotlinking policy and fail with
    `hotlink_blocked`. Only the `?c=` query parameter is allowed. Width and
    height, if present, must each be ≤ 2048.

    Format: the URL is fetched exactly as given. Extension URLs from `get_brand`
    (.../logo.svg, .../logo.png, .../icon.jpeg) return that format; extensionless
    `type/{type}` URLs from `build_logo_urls` let the CDN pick the format
    (typically WebP). Callers targeting PowerPoint, DOCX, or older clients should
    prefer an explicit .png URL, or be prepared to convert WebP → PNG (e.g. via
    Pillow). Always trust `media_type` in the response over the URL extension.

    The base64 payload is wrapped at 76 characters per line (MIME style).
    This is deliberate: if you must reproduce the payload yourself (e.g.
    writing it into a sandbox file via a heredoc), short anchored lines are
    far less error-prone than one unbroken blob — long runs of identical
    characters (AAAA…, JPEG null padding) are where characters get dropped
    in transcription. Standard decoders (`base64 -d`, Python `b64decode`)
    ignore the newlines; strip them yourself only when building a data URI.

    ALWAYS verify after writing the decoded file:

        b64 = result["base64"]
        assert len(b64.replace("\\n", "")) == result["base64_length"]
        data = base64.b64decode(b64)
        assert hashlib.sha256(data).hexdigest() == result["sha256"]
        Path("/tmp/logo.png").write_bytes(data)

    If verification fails, you corrupted the payload while reproducing it —
    the wrapping makes this repairable: every line except the last must be
    exactly 76 characters, so find the short/long line and re-copy just that
    line from the tool result. Do NOT retype the whole payload again, and do
    not try to patch the bytes. If it keeps failing, prefer a variant with
    fewer characters (the .svg format when available, or a smaller w/h
    raster), or tell the user that allowlisting `*.brandfetch.io` on
    claude.ai (Settings > Capabilities) enables direct download and avoids
    transcription entirely.

    Context window cost: each base64 asset adds roughly 50–80 KB to the
    conversation. For multi-asset workflows (logo + symbol, light + dark
    variants), decode and write each asset to disk immediately after
    fetching rather than accumulating payloads in context.

    Theme segments (`/theme/light`, `/theme/dark`) in URLs follow
    `build_logo_urls`' convention: they name the asset's own color, not the
    background it sits on.

    Errors (JSON with a `code` field):
    - `hotlink_blocked`: the URL has no valid `?c=` token (typical for
      `build_logo_urls` output). Use a `src` URL from `get_brand` instead.
    - `asset_too_large`: decoded asset exceeds 5 MB. Request a smaller
      `w`/`h` variant.
    - `invalid_input`: malformed URL, disallowed host or query params, or
      width/height above 2048.
    - `fetch_failed`: upstream CDN failure (timeout, non-2xx, unsupported
      content type). Safe to retry once; if it persists, try another format
      variant from `get_brand`.

    Returns:
        base64: Base64 payload, wrapped at 76 chars/line. For an inline data
            URI, strip newlines: `data:{media_type};base64,{joined}`.
        base64_length: Character count of the unwrapped base64 payload.
        sha256: Hex SHA-256 of the decoded bytes — verify written files.
        media_type: Normalized media type (e.g. "image/png", "image/svg+xml").
        size_bytes: Size of decoded asset bytes.
        source_url: URL fetched by this tool.
    """
    try:
        source_url = _validate_asset_url(url)
    except ValueError as exc:
        raise ValueError(_tool_error("invalid_input", str(exc))) from exc

    data, media_type, total_size, status_code = await _stream_cdn_asset(source_url)

    encoded_asset = base64.b64encode(data).decode("ascii")
    _publish_event(
        "mcp.asset-base64.fetched",
        {
            "sourceUrl": source_url,
            "success": True,
            "statusCode": status_code,
            "sizeBytes": total_size,
            "mediaType": media_type,
        },
    )
    return {
        "base64": _wrap_base64(encoded_asset),
        "base64_length": len(encoded_asset),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": media_type,
        "size_bytes": total_size,
        "source_url": source_url,
    }


@mcp.resource(
    "bf://asset/{domain}/{type}",
    name="Brand asset",
    description=(
        "Brand icon, logo, or symbol image for a domain, served as raw image "
        "bytes through the MCP server (resolved server-side from the Brandfetch "
        "CDN). Read this instead of fetching the CDN directly when the asset is "
        "needed in a sandboxed or network-restricted environment. URI shape: "
        "bf://asset/{domain}/{type} where type is icon, logo, or symbol."
    ),
)
async def asset_resource(domain: str, type: str) -> ResourceResult:
    """Serve a brand asset (icon/logo/symbol) as image bytes via a resource read.

    Resolution: looks up the brand for `domain`, picks the asset matching `type`,
    and fetches its CDN src (which carries the per-request ?c= token that the CDN
    allows for programmatic access) server-side. The blob's mime type reflects the
    actual CDN response.
    """
    if type not in ASSET_URL_ALLOWED_TYPES:
        raise ValueError(
            _tool_error(
                "invalid_input",
                f"unsupported asset type '{type}'; expected one of {sorted(ASSET_URL_ALLOWED_TYPES)}",
            )
        )

    brand = await _get_brand_json(domain)
    src = _resolve_asset_src(brand, type)
    if not src:
        raise ValueError(
            _tool_error("not_found", f"no {type} asset available for '{domain}'")
        )

    source_url = _validate_asset_url(src)
    data, media_type, total_size, status_code = await _stream_cdn_asset(source_url)

    _publish_event(
        "mcp.asset-resource.read",
        {
            "domain": domain,
            "type": type,
            "success": True,
            "statusCode": status_code,
            "sizeBytes": total_size,
            "mediaType": media_type,
        },
    )
    return ResourceResult(contents=[ResourceContent(data, mime_type=media_type)])


def _slack_escape(text: str) -> str:
    """Escape the three characters Slack treats as control sequences in mrkdwn."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@mcp.tool(
    annotations={
        "title": "Send Feedback",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_feedback(
    message: str,
    category: Literal[
        "bug", "data-quality", "feature-request", "documentation", "praise", "other"
    ] = "other",
    tool_name: str | None = None,
) -> str:
    """Send feedback about the Brandfetch MCP server to the Brandfetch team.

    Use this to report anything that would help improve this MCP server:
    - A tool call failed, timed out, or returned something inconsistent with
      its documented behavior.
    - A tool description was confusing or misled you into a wrong call.
    - Brand data was wrong, stale, or missing (wrong logo, outdated colors,
      missing company info) — include the brand's domain in the message.
    - A capability you needed was missing (e.g. a filter, a data field, a
      whole tool).
    - The user explicitly asked to send feedback, or expressed praise or
      frustration about Brandfetch that they want passed along.

    Write feedback the way you would want to receive a bug report: what you
    were trying to do, the exact tool call and arguments, what you expected,
    and what actually happened. Specific and structured beats polite and
    vague. Send one call per distinct issue rather than bundling several
    topics into one message.

    This is a one-way channel to the Brandfetch team — nobody replies through
    it. For account or billing help, direct the user to
    https://developers.brandfetch.com instead. Never include API keys, bearer
    tokens, or personal data in the message.

    Returns a JSON acknowledgment: {"status": "received", ...}.

    Args:
        message: The feedback text. Plain text or simple markdown; messages
            longer than ~2900 characters are truncated.
        category: One of "bug", "data-quality", "feature-request",
            "documentation", "praise", "other". Default "other".
        tool_name: Name of the tool the feedback concerns (e.g. "get_brand"),
            if it concerns a specific tool.
    """
    text = _slack_escape(message.strip()) if message else ""
    if not text:
        raise ValueError(_tool_error("invalid_input", "message must not be empty"))

    truncated = len(text) > FEEDBACK_MAX_CHARS
    if truncated:
        text = text[:FEEDBACK_MAX_CHARS] + "… [truncated]"

    tool = (tool_name or "").strip()
    client_id = _client_id_var.get("")
    org_urn = _org_urn_var.get("")
    stage = os.environ.get("STAGE", "local")

    context_parts = [f"category: *{category}*"]
    if tool:
        context_parts.append(f"tool: {_slack_escape(tool)}")
    if org_urn:
        context_parts.append(f"actor: {org_urn}")
    elif client_id:
        context_parts.append(f"actor: client-id {client_id}")
    else:
        context_parts.append("actor: anonymous")

    delivered = False
    webhook_url = os.environ.get("SLACK_FEEDBACK_WEBHOOK_URL", "")
    if webhook_url:
        # One webhook serves every environment, so the stage leads the
        # message, matching the `[production]` convention of the other
        # Slack notifications.
        payload = {
            "text": f"[{stage}] New MCP server feedback ({category})",
            "blocks": [
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"[{stage}]"}],
                },
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "New MCP server feedback"},
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": " | ".join(context_parts)}],
                },
            ],
        }
        try:
            async with httpx.AsyncClient(
                timeout=FEEDBACK_SLACK_TIMEOUT_SECONDS
            ) as client:
                resp = await client.post(webhook_url, json=payload)
            delivered = resp.is_success
            if not delivered:
                print(
                    f"[mcp-server] Slack feedback webhook returned "
                    f"{resp.status_code}: {resp.text}"
                )
        except httpx.HTTPError as exc:
            print(f"[mcp-server] Slack feedback webhook failed: {exc}")
    else:
        print(
            "[mcp-server] SLACK_FEEDBACK_WEBHOOK_URL is not configured; "
            "feedback recorded as event only"
        )

    _publish_event(
        "mcp.feedback.submitted",
        {
            "category": category,
            **({"toolName": tool} if tool else {}),
            "message": text,
            "delivered": delivered,
            "truncated": truncated,
        },
    )

    if webhook_url and not delivered:
        raise ValueError(
            _tool_error(
                "delivery_failed",
                "Feedback could not be delivered. Do not retry; "
                "let the user know it failed.",
            )
        )

    return json.dumps(
        {
            "status": "received",
            "message": "Thanks — your feedback has been shared with the Brandfetch team.",
        }
    )


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def oauth_protected_resource(request: Request):
    return JSONResponse(
        {
            "resource": MCP_BASE_URL,
            "authorization_servers": [OAUTH_SERVER_URL],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://developers.brandfetch.com/docs/mcp",
            "resource_metadata": OAUTH_WELL_KNOWN_URL,
        }
    )


async def oauth_protected_resource_mcp(request: Request):
    return JSONResponse(
        {
            "resource": f"{MCP_BASE_URL}/mcp",
            "authorization_servers": [OAUTH_SERVER_URL],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://developers.brandfetch.com/docs/mcp",
            "resource_metadata": f"{OAUTH_WELL_KNOWN_URL}/mcp",
        }
    )


app = mcp.http_app(
    transport="streamable-http",
    stateless_http=True,
)
app.add_middleware(CredentialsMiddleware)
app.add_middleware(NonEmptyBodyMiddleware)
app.routes.append(Route("/health", health))
app.routes.append(Route(OAUTH_PROTECTED_RESOURCE_PATH, oauth_protected_resource))
app.routes.append(
    Route(f"{OAUTH_PROTECTED_RESOURCE_PATH}/mcp", oauth_protected_resource_mcp)
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
