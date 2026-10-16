"""Shared async HTTP client for the Taaghche API.

Taaghche (taaghche.com) is a Next.js site whose data comes from a few public hosts: `get.taaghche.com`
(book detail, comments, listings, search), `explore.taaghche.com` (search suggestions) and `quote.taaghche.com`
(quotes). Every tool goes through `api` (JSON) or `page` (the home page HTML, only for the category menu).
`_send` caps concurrency, retries once when the server drops the connection, answers 200 with an empty body or
answers 429 / 502 / 503 (fixed short delay; GET only), caps the body size, and turns HTTP failures into `ToolError` messages the model can act on.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

GET = "https://get.taaghche.com"
EXPLORE = "https://explore.taaghche.com"
SITE = "https://taaghche.com"

# The site's own constant (`trackingData` in its config). get.taaghche.com answers /v2/everything in the
# shelf layout (`boxes`) instead of the flat list (`bookList`) when it is missing.
TRACKING = 110160240

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0",
    "Accept": "application/json",
}

MAX_CONCURRENCY = 4
MAX_BYTES = 8_000_000  # book detail is ~600 KB; anything bigger is not an API reply
RETRY_DELAY = 0.5
RETRY_STATUS = (429, 502, 503)

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None

# Short cache for the big payloads (book detail ~600 KB, home page) and the slug pages (keyed on all params);
# prices may lag by CACHE_TTL. Entries are immutable responses: every caller parses its own copy.
CACHE_TTL = 300
CACHE_MAX = 32
_CACHEABLE = re.compile(r"/v2/book/\d+$|^https://taaghche\.com/$")
_cache: dict[tuple[str, tuple], tuple[float, httpx.Response]] = {}


class ApiError(ToolError):
    """A failed upstream call."""


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client."""
    global _transport, _client, _limit
    _transport, _client, _limit = transport, None, None
    _cache.clear()


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        # Taaghche answers direct connections fine; a proxy is only used when the user opts in
        # with TAAGHCHE_MCP_PROXY.
        _client = httpx.AsyncClient(
            transport=_transport,
            headers=HEADERS,
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=8),
            follow_redirects=True,
            trust_env=False,
            proxy=os.environ.get("TAAGHCHE_MCP_PROXY") or None,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


async def api(path: str, params: dict[str, Any] | None = None, *, host: str = GET) -> Any:
    """GET a JSON path on `host` (get.taaghche.com by default) and return the parsed JSON."""
    r = await _send(f"{host}{path}", params={k: v for k, v in (params or {}).items() if v is not None})
    try:
        return r.json()
    except ValueError as e:
        raise ApiError(f"taaghche.com returned a non-JSON response for {path} (HTTP {r.status_code}).") from e


async def page(path: str = "/") -> str:
    """GET an HTML page of the site and return the text."""
    r = await _send(f"{SITE}{path}", headers={"Accept": "text/html"})
    return r.text


async def _send(url: str, **kwargs: Any) -> httpx.Response:
    params = kwargs.get("params") or {}
    key = (url, tuple(sorted(params.items())))
    cacheable = bool(_CACHEABLE.search(url)) or "slug" in params
    hit = _cache.get(key) if cacheable else None
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    r = await _fetch(url, **kwargs)
    if cacheable and r.content.strip() not in (b"", b"null"):  # never cache unknown ids / empty bodies
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), r)
    return r


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """GET with the body read in chunks and capped at MAX_BYTES."""
    async with client.stream("GET", url, **kwargs) as r:
        if int(r.headers.get("content-length") or 0) > MAX_BYTES:
            raise ApiError("taaghche.com sent a reply that is too large.")
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise ApiError("taaghche.com sent a reply that is too large.")
        headers = [(k, v) for k, v in r.headers.items() if k.lower() not in ("content-encoding", "content-length")]
        return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)


async def _fetch(url: str, **kwargs: Any) -> httpx.Response:
    client, limit = _get_client()
    for attempt in (1, 2):
        try:
            async with limit:
                r = await _get(client, url, **kwargs)
        except httpx.TimeoutException as e:
            raise ApiError("taaghche.com did not answer in time. Try again in a moment.") from e
        except httpx.TransportError as e:
            if attempt == 2:
                raise ApiError(
                    f"Could not reach taaghche.com ({type(e).__name__}). Check the internet connection, "
                    "or set TAAGHCHE_MCP_PROXY."
                ) from e
            continue
        if attempt == 1 and ((r.status_code == 200 and not r.content) or r.status_code in RETRY_STATUS):
            await asyncio.sleep(RETRY_DELAY)
            continue
        break
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                errors = body.get("errors")
                detail = (str(errors) if errors else str(body.get("title") or ""))[:300]
        except ValueError:
            pass
        raise ApiError(_status_message(r.status_code) + (f" Site says: {detail}" if detail else ""))
    return r


def _status_message(code: int) -> str:
    if code == 400:
        return "taaghche.com rejected the parameters (HTTP 400)."
    if code == 401:
        return "This needs a logged-in Taaghche account (HTTP 401); this server is read-only and has no login."
    if code == 403:
        return "taaghche.com blocked the request (HTTP 403). Turn off VPN/proxy or set TAAGHCHE_MCP_PROXY."
    if code == 404:
        return "Not found on taaghche.com (HTTP 404). Check the id or slug."
    if code == 429:
        return "taaghche.com is rate limiting requests (HTTP 429). Wait a minute before retrying."
    if code >= 500:
        return f"taaghche.com had a server error (HTTP {code}). Check the ids, or try again later."
    return f"taaghche.com rejected the request (HTTP {code})."
