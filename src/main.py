import contextvars
import base64
import json
import os
from typing import Literal
from urllib.parse import parse_qsl, urlparse, urlunparse

from fastmcp import FastMCP
import httpx
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


mcp = FastMCP("brandfetch-mcp-server")

ASSET_URL_ALLOWED_HOSTS = {"cdn.brandfetch.io"}
ASSET_URL_ALLOWED_TYPES = {"icon", "logo", "symbol"}
ASSET_MAX_DIMENSION = 2048
ASSET_MAX_BYTES = 5 * 1024 * 1024
ASSET_FETCH_TIMEOUT_SECONDS = 5.0
BRAND_API_TIMEOUT_SECONDS = 35.0
ASSET_FETCH_USER_AGENT = "Brandfetch-MCP/1.0 (asset-base64-tool)"
ASSET_FETCH_CHUNK_SIZE = 64 * 1024
ASSET_ALLOWED_MEDIA_TYPES = {
    "image/svg+xml",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


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

    # Normalize get_brand URL shape: trailing /{type}.{ext} → /type/{type}
    # e.g. .../logo.png → .../type/logo. Other file segments (e.g. banner IDs
    # like "idu7P6rdmK.jpeg") pass through unmodified.
    last_segment = path_segments[-1]
    if "." in last_segment:
        stem, _, _ = last_segment.rpartition(".")
        if stem in ASSET_URL_ALLOWED_TYPES:
            path_segments = path_segments[:-1] + ["type", stem]
            normalized_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    "/" + "/".join(path_segments),
                    parsed.params,
                    parsed.query,
                    "",
                )
            )

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
      for autocomplete-style UIs. The CDN supports asset-type substitution:
      replacing the `/icon/` segment in the URL path with `/logo/` returns the
      brand's logo at the same size; `/symbol/` returns the symbol. Dimensions
      can also be modified via the `/w/{width}/h/{height}/` segments. See the
      Logo API tool for the full URL-construction grammar.
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
async def get_brand(identifier: str) -> str:
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

    If your environment cannot fetch these URLs directly (sandboxed code
    execution, restricted networks), use `get_asset_base64` to retrieve the
    asset bytes through the MCP instead.

    Errors:
    - 403 / "explicit deny": The brand exists in the index but is not
      accessible on the current API tier or has access restrictions. Do not
      retry — fall back to the `icon` from `brand_search` or inform the user.
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
    return resp.text


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
    confident match is found.

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
) -> list[str]:
    """Construct Brandfetch Logo CDN URLs for one or more brands. No API call
    is made — returns ready-to-embed URL strings.

    **HOTLINKING POLICY — read before using these URLs:**
    URLs returned by this tool are subject to Brandfetch's hotlinking policy.
    They are intended for direct browser rendering only (e.g. `<img src="...">`
    in an HTML page). Programmatic fetching or downloading of these assets —
    such as HTTP GET requests from a server or script using only the clientId
    embedded in the URL — will be blocked and return an error.

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
    as a base64 data URI for direct embedding.

    Use this when you need to embed a brand logo or image into a file generated
    in a sandboxed or network-restricted environment where cdn.brandfetch.io is
    unreachable — for example, when creating PPTX, DOCX, PDF, or HTML artifacts
    from a code-execution sandbox. This is the right tool whenever a direct
    download or embed of a logo, icon, or picture is needed and the CDN URL
    cannot be fetched by the caller.

    Do not use this when a normal browser can fetch the image URL directly.
    Base64 increases payload size and bypasses CDN caching.

    Accepts any `cdn.brandfetch.io` asset URL — logos, icons, symbols, banners,
    hero images, and other brand-record assets. Examples:
    - `build_logo_urls` output:  .../w/400/h/400/theme/dark/type/icon?c=<token>
    - `get_brand` src fields:    .../w/800/h/111/theme/light/logo.png?c=<token>
    - Brand-record images:       .../idwlR7BDjL/idu7P6rdmK.jpeg?c=<token>
    Only the `?c=` query parameter is allowed. Width and height, if present,
    must each be ≤ 2048.

    Format: The CDN typically returns WebP for raster assets and SVG for vector
    assets. Callers targeting PowerPoint, DOCX, or older clients should be
    prepared to convert WebP → PNG if needed (e.g. via Pillow).

    Context window cost: each base64 asset adds roughly 50–80 KB to the
    conversation. For multi-asset workflows (logo + symbol, light + dark
    variants), decode and write each asset to disk immediately after fetching
    rather than accumulating data URIs in context:

        data = base64.b64decode(result["data_uri"].split(",", 1)[1])
        Path("/tmp/logo.png").write_bytes(data)

    Theme convention (same as `build_logo_urls`):
    - theme=light → light-colored asset (white/pale), for display on dark backgrounds
    - theme=dark  → dark-colored asset, for display on light backgrounds
    The parameter names the asset's own color, not the background.

    Returns:
        data_uri: Full `data:image/...;base64,...` payload ready for embedding.
        media_type: Normalized media type (e.g. "image/webp", "image/svg+xml").
        size_bytes: Size of decoded asset bytes.
        source_url: URL fetched by this tool.
    """
    try:
        source_url = _validate_asset_url(url)
    except ValueError as exc:
        raise ValueError(_tool_error("invalid_input", str(exc))) from exc

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
                    raise ValueError(
                        _tool_error(
                            "fetch_failed",
                            f"redirect target host '{final_host}' is not an allowed Brandfetch CDN host",
                        )
                    )

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

    encoded_asset = base64.b64encode(b"".join(chunks)).decode("ascii")
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
        "data_uri": f"data:{media_type};base64,{encoded_asset}",
        "media_type": media_type,
        "size_bytes": total_size,
        "source_url": source_url,
    }


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
