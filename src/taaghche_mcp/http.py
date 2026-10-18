"""Shared async HTTP client for the Taaghche API.

Taaghche (taaghche.com) is a Next.js site whose data comes from a few public hosts: `get.taaghche.com`
(book detail, comments, listings, search), `explore.taaghche.com` (search suggestions) and `quote.taaghche.com`
(quotes). Every tool goes through `api` (JSON) or `page` (the home page HTML, only for the category menu).
`_send` caps concurrency, retries timeouts, network errors, empty 200 bodies, 429 and 5xx (jittered backoff,
Retry-After honoured; GET only), caps the body size, and turns HTTP failures into `ToolError` messages the model can act on.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from typing import Any

import httpx
from httpx_curl_cffi import AsyncCurlTransport
from mcp.server.mcpserver.exceptions import ToolError

GET = "https://get.taaghche.com"
EXPLORE = "https://explore.taaghche.com"
SITE = "https://taaghche.com"

# The site's own constant (`trackingData` in its config). get.taaghche.com answers /v2/everything in the
# shelf layout (`boxes`) instead of the flat list (`bookList`) when it is missing.
TRACKING = 110160240

HEADERS = {
    # User-Agent, sec-ch-ua and TLS come from the impersonated Chrome profile (see _get_client).
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}
# The API hosts (get/explore/quote) are same-site XHR calls from the web app on taaghche.com.
XHR_HEADERS = {
    "Accept": "application/json",
    "Origin": SITE,
    "Referer": f"{SITE}/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "priority": "u=1, i",
}
# The home page is a plain navigation, not XHR.
DOC_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "priority": "u=0, i",
}

MAX_CONCURRENCY = 2
JITTER = (0.15, 0.6)  # seconds before each uncached call
MAX_RETRIES = 2
MAX_RETRY_WAIT = 10  # cap for Retry-After and backoff, seconds
MAX_BYTES = 8_000_000  # book detail is ~600 KB; anything bigger is not an API reply
RETRY_DELAY = 0.5

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None
_proxy_index = 0
_retired: set[asyncio.Task] = set()
_browser_lock: asyncio.Lock | None = None
_cleared: tuple[httpx.AsyncClient, float] | None = None

# Short cache for the big payloads (book detail ~600 KB, home page) and the slug pages (keyed on all params);
# prices may lag by CACHE_TTL. Entries are immutable responses: every caller parses its own copy.
CACHE_TTL = 300
CACHE_MAX = 32
_CACHEABLE = re.compile(r"/v2/book/\d+$|^https://taaghche\.com/$")
_cache: dict[tuple[str, tuple], tuple[float, httpx.Response]] = {}


class _XhrTransport(AsyncCurlTransport):
    """The Chrome profile sends navigation-only headers; a None value makes curl drop them (API calls are XHR)."""

    def _create_request_params(self, req: httpx.Request) -> dict[str, Any]:
        p = super()._create_request_params(req)
        p["headers"] = [*p["headers"], (b"upgrade-insecure-requests", None), (b"sec-fetch-user", None)]
        return p


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client."""
    global _transport, _client, _limit, _proxy_index, _browser_lock, _cleared
    _transport, _client, _limit, _proxy_index, _browser_lock, _cleared = transport, None, None, 0, None, None
    _cache.clear()


async def _same_site(r: httpx.Response) -> None:
    """Refuse a redirect off taaghche.com."""
    if r.has_redirect_location:
        host = r.url.join(r.headers["location"]).host
        if host != "taaghche.com" and not host.endswith(".taaghche.com"):
            raise ApiError("taaghche.com redirected to another host; refused.")


def _proxies() -> list[str]:
    """TAAGHCHE_MCP_PROXY: one proxy URL, or a comma-separated list used one at a time."""
    return [p.strip() for p in os.environ.get("TAAGHCHE_MCP_PROXY", "").split(",") if p.strip()]


# A bare HTML 403 is often a plain error page; only a known bot-check marker counts as a challenge.
_CHALLENGE = re.compile(r"just a moment|captcha|challenge|arvan|attention required|enable javascript", re.I)


def _is_challenge(r: httpx.Response) -> bool:
    """A bot-check page: 403/429/503 with an HTML body that carries a known challenge marker."""
    return (
        r.status_code in (403, 429, 503)
        and "html" in r.headers.get("content-type", "") + r.text[:20].lower()
        and bool(_CHALLENGE.search(r.text[:5000]))
    )


def _next_proxy(client: httpx.AsyncClient) -> None:
    """Sticky pool: move to the next proxy after a block or connect error; the next call rebuilds the client.

    Only the client that failed may rotate: a slower request that failed on an old client must not skip a good proxy.
    The old client is closed late because other requests may still be using it.
    """
    global _client, _limit, _proxy_index
    if len(_proxies()) < 2 or client is not _client:
        return
    _client, _limit, _proxy_index = None, None, _proxy_index + 1
    t = asyncio.create_task(_close_later(client))
    _retired.add(t)
    t.add_done_callback(_retired.discard)


async def _close_later(client: httpx.AsyncClient) -> None:
    await asyncio.sleep(30)  # longer than the 30 s request timeout
    await client.aclose()


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        # Taaghche answers direct connections fine; a proxy is only used when the user opts in
        # with TAAGHCHE_MCP_PROXY. curl_cffi presents Chrome's TLS/HTTP2 fingerprint and default headers;
        # cookies and redirects stay with httpx. The proxy goes to the transport (a client-level proxy
        # would be ignored once a transport is given).
        proxies = _proxies()
        _client = httpx.AsyncClient(
            transport=_transport
            or _XhrTransport(
                impersonate="chrome",
                default_headers=True,
                proxy=proxies[_proxy_index % len(proxies)] if proxies else None,
                max_connections=2 * MAX_CONCURRENCY,
            ),
            headers=HEADERS,
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=2 * MAX_CONCURRENCY),
            follow_redirects=True,
            event_hooks={"response": [_same_site]},
            trust_env=False,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


async def api(path: str, params: dict[str, Any] | None = None, *, host: str = GET) -> Any:
    """GET a JSON path on `host` (get.taaghche.com by default) and return the parsed JSON."""
    r = await _send(
        f"{host}{path}", params={k: v for k, v in (params or {}).items() if v is not None}, headers=XHR_HEADERS
    )
    try:
        return r.json()
    except ValueError as e:
        raise ApiError(f"taaghche.com returned a non-JSON response for {path} (HTTP {r.status_code}).") from e


async def page(path: str = "/") -> str:
    """GET an HTML page of the site and return the text."""
    r = await _send(f"{SITE}{path}", headers=DOC_HEADERS)
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
            raise ApiError("taaghche.com sent a reply that is too large.", 413)
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise ApiError("taaghche.com sent a reply that is too large.", 413)
        headers = [(k, v) for k, v in r.headers.items() if k.lower() not in ("content-encoding", "content-length")]
        return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)


async def _once(client: httpx.AsyncClient, limit: asyncio.Semaphore, url: str, **kwargs: Any) -> httpx.Response:
    """One jittered request; timeouts and network errors become ApiError (status None)."""
    try:
        async with limit:
            await asyncio.sleep(random.uniform(*JITTER))
            return await _get(client, url, **kwargs)
    except httpx.TimeoutException as e:
        raise ApiError("taaghche.com did not answer in time. Try again in a moment.") from e
    except httpx.TooManyRedirects as e:
        _next_proxy(client)
        raise ApiError("taaghche.com kept redirecting. Try again in a moment.") from e
    except httpx.TransportError as e:
        if isinstance(e, httpx.ConnectError | httpx.ProxyError):
            _next_proxy(client)
        raise ApiError(
            f"Could not reach taaghche.com ({type(e).__name__}). Check the internet connection, "
            "or set TAAGHCHE_MCP_PROXY."
        ) from e


async def _browser_cookies(proxy: str | None) -> list[dict[str, Any]]:
    """Open the home page in headless patchright, wait for the challenge to clear, return the cookies."""
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise ApiError(
            "taaghche.com served a bot-check page. Install the browser extra (`pip install taaghche-mcp[browser]`, or "
            "`uvx --with patchright==1.63.0 taaghche-mcp`) and run `patchright install chromium`.",
            403,
        ) from e
    u = httpx.URL(proxy) if proxy else None
    opts = u and {"server": f"{u.scheme}://{u.host}:{u.port}", "username": u.username, "password": u.password}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=opts)
        try:
            page = await browser.new_page()
            await page.goto(SITE, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_function(
                "!/just a moment|access denied|attention required|arvancloud/i.test(document.title)", timeout=30_000
            )
            return await browser.contexts[0].cookies()
        finally:
            await browser.close()


async def _clear_challenge(client: httpx.AsyncClient) -> None:
    """Rung 2: let a real browser pass the bot check once and share its cookies with the httpx jar."""
    global _browser_lock, _cleared
    _browser_lock = _browser_lock or asyncio.Lock()
    async with _browser_lock:  # one browser at a time; callers that waited reuse the cookies just fetched
        if _cleared and _cleared[0] is client and time.monotonic() - _cleared[1] < 30:
            return
        proxies = _proxies()
        try:
            cookies = await _browser_cookies(proxies[_proxy_index % len(proxies)] if proxies else None)
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(f"taaghche.com bot check was not cleared by the browser ({type(e).__name__}).", 403) from e
        for c in cookies:
            client.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        _cleared = (client, time.monotonic())


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After on a 429/503, else None (HTTP-date form is ignored)."""
    v = r.headers.get("retry-after", "")
    return float(v) if r.status_code in (429, 503) and v.replace(".", "", 1).isdigit() else None


async def _attempt(url: str, **kwargs: Any) -> httpx.Response:
    client, limit = _get_client()
    r = await _once(client, limit, url, **kwargs)
    if _is_challenge(r):
        _next_proxy(client)
        client, limit = _get_client()
        await _clear_challenge(client)
        r = await _once(client, limit, url, **kwargs)  # one retry; a second block is final
        if _is_challenge(r):
            _next_proxy(client)
    return r


async def _fetch(url: str, **kwargs: Any) -> httpx.Response:
    """GET with up to MAX_RETRIES retries (jittered backoff, or Retry-After) on timeout, network error, empty 200,
    429 and 5xx. Other 4xx (403 too) are never retried."""
    for attempt in range(MAX_RETRIES + 1):
        wait = RETRY_DELAY * 2**attempt * random.uniform(1, 1.5)
        try:
            r = await _attempt(url, **kwargs)
        except ApiError as e:
            if attempt == MAX_RETRIES or e.status is not None:
                raise
        else:
            if not ((r.status_code == 200 and not r.content) or r.status_code == 429 or r.status_code >= 500):
                break
            if attempt == MAX_RETRIES:
                break
            wait = _retry_after(r) or wait
        await asyncio.sleep(min(wait, MAX_RETRY_WAIT))
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                errors = body.get("errors")
                detail = (str(errors) if errors else str(body.get("title") or ""))[:300]
        except ValueError:
            pass
        raise ApiError(_status_message(r.status_code) + (f" Site says: {detail}" if detail else ""), r.status_code)
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
