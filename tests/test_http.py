import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest
from conftest import fixture

from taaghche_mcp import http

pytestmark = pytest.mark.anyio

HINT = "/v3/hint"


async def test_all_tools_are_read_only(client):
    tools = (await client.list_tools()).tools
    assert len(tools) == 12
    for t in tools:
        assert t.name.startswith("tg_"), t.name
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False, t.name
        assert t.description and t.title, t.name


async def test_readme_tool_tables_match_the_server(client):
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\| `(tg_\w+)` \|", readme, re.M)
    assert sorted(listed) == sorted(t.name for t in (await client.list_tools()).tools)


async def test_dropped_connection_is_retried_once(client, api):
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return httpx.Response(200, json=fixture("hint.json"))

    api[HINT] = flaky
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert not result.is_error and len(attempts) == 2


async def test_empty_200_body_is_retried_once(client, api):
    replies = iter([httpx.Response(200), httpx.Response(200, json=fixture("hint.json"))])
    api[HINT] = lambda r: next(replies)
    out = (await client.call_tool("tg_suggest", {"term": "شازده"})).structured_content
    assert out["suggestions"][0]["id"] == 72107 and len(api.calls) == 2


async def test_network_error_after_retry(client, api):
    def down(request):
        raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)

    api[HINT] = down
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and "Could not reach taaghche.com (ConnectError)" in result.content[0].text
    assert len(api.calls) == 3  # retried twice


async def test_timeout_is_retried_twice(client, api):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    api[HINT] = slow
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and "did not answer in time" in result.content[0].text and len(api.calls) == 3


@pytest.mark.parametrize(
    ("status", "text"),
    [(404, "HTTP 404"), (429, "rate limiting"), (500, "server error (HTTP 500)"), (403, "blocked"), (401, "logged-in")],
)
async def test_http_errors_are_actionable(client, api, status, text):
    api[HINT] = lambda r: httpx.Response(status)
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and text in result.content[0].text


async def test_validation_error_detail_is_passed_on(client, api):
    body = {"errors": {"id": ["The value 'abc' is not valid."]}, "status": 400}
    api[HINT] = lambda r: httpx.Response(400, json=body)
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and "The value 'abc' is not valid." in result.content[0].text


async def test_non_json_reply(client, api):
    api[HINT] = lambda r: httpx.Response(200, text="<html>oops</html>")
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and "non-JSON" in result.content[0].text


async def test_frontend_headers_and_no_env_proxy(client, api, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")  # must be ignored (trust_env=False)
    api[HINT] = fixture("hint.json")
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    h = api.calls[0].headers
    assert not result.is_error and h["Origin"] == "https://taaghche.com" and h["sec-fetch-mode"] == "cors"
    assert api.hosts() == ["explore.taaghche.com"]


async def test_book_detail_is_cached_but_null_is_not(client, api):
    api["/v2/book/6426"] = fixture("book_ebook.json")
    api["/v2/book/1"] = lambda request: httpx.Response(200, text="null")
    for _ in range(2):
        await client.call_tool("tg_book", {"book_id": 6426})
        await client.call_tool("tg_book", {"book_id": 1})
    assert [c.url.path for c in api.calls].count("/v2/book/6426") == 1
    assert [c.url.path for c in api.calls].count("/v2/book/1") == 2


async def test_raw_client_one_text_block_no_structured_content_no_output_schema(api):
    from mcp import Client

    from taaghche_mcp.server import mcp

    api[HINT] = fixture("hint.json")
    async with Client(mcp) as raw:  # the real client, without the conftest patch
        assert all(t.output_schema is None for t in (await raw.list_tools()).tools)
        result = await raw.call_tool("tg_suggest", {"term": "شازده"})
    assert len(result.content) == 1 and result.content[0].type == "text"
    assert result.structured_content is None
    assert "\n" not in result.content[0].text and "null" not in result.content[0].text


async def test_input_schemas_have_no_title_noise(client):
    for t in (await client.list_tools()).tools:
        assert "title" not in t.input_schema
        assert all("title" not in p for p in t.input_schema["properties"].values()), t.name


@pytest.mark.parametrize("status", [429, 503])
async def test_busy_status_is_retried_once(client, api, status):
    replies = iter([httpx.Response(status), httpx.Response(200, json=fixture("hint.json"))])
    api[HINT] = lambda r: next(replies)
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert not result.is_error and len(api.calls) == 2


async def test_oversized_reply_is_refused(client, api, monkeypatch):
    monkeypatch.setattr("taaghche_mcp.http.MAX_BYTES", 100)
    api[HINT] = lambda r: httpx.Response(200, content=b"[" + b"1," * 100 + b"1]")
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and "too large" in result.content[0].text


async def test_slug_pages_are_cached_per_params(client, api):
    api["/v2/everything"] = fixture("slug_category.json")
    for slug in ("a", "a", "b"):
        await client.call_tool("tg_browse", {"category": slug})
    assert [c.url.params.get("slug") for c in api.calls if c.url.params.get("slug")] == ["category/a", "category/b"]


def test_is_challenge():
    html = {"content-type": "text/html"}
    assert http._is_challenge(httpx.Response(403, text="<html>Just a moment...</html>", headers=html))
    assert http._is_challenge(httpx.Response(503, text="<!doctype html><title>ArvanCloud</title>"))
    assert not http._is_challenge(httpx.Response(403, text="<html>Access Denied</html>", headers=html))
    assert not http._is_challenge(httpx.Response(403, json={"status": 403}))
    assert not http._is_challenge(httpx.Response(200, text="<html></html>", headers=html))


async def test_proxy_list_rotates_on_connect_error(monkeypatch, api):
    monkeypatch.setenv("TAAGHCHE_MCP_PROXY", "http://p1:1, http://p2:2")
    assert http._proxies() == ["http://p1:1", "http://p2:2"]

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    api[HINT] = boom
    http._get_client()
    with pytest.raises(http.ApiError):
        await http.api(HINT, host=http.EXPLORE)
    assert http._proxy_index >= 1 and len(api.calls) == 3


async def test_stale_client_does_not_rotate(monkeypatch, api):
    monkeypatch.setenv("TAAGHCHE_MCP_PROXY", "http://p1:1, http://p2:2")
    old, _ = http._get_client()
    http._next_proxy(old)
    new, _ = http._get_client()
    assert http._proxy_index == 1 and new is not old
    http._next_proxy(old)  # a late failure on the retired client must not skip the proxy now in use
    assert http._proxy_index == 1 and http._client is new


async def test_challenge_cleared_by_browser_once(monkeypatch, api):
    runs = []

    async def fake_browser(proxy):
        runs.append(proxy)
        await asyncio.sleep(0.01)
        return [{"name": "clearance", "value": "ok", "domain": ".taaghche.com", "path": "/"}]

    monkeypatch.setattr(http, "_browser_cookies", fake_browser)

    def guarded(request):
        if "clearance=ok" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text="<html>Just a moment</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, json=[])

    api[HINT] = guarded
    await asyncio.gather(http.api(HINT, {"a": 1}), http.api(HINT, {"a": 2}))
    assert len(runs) == 1  # the second caller reused the cookies the first one fetched


async def test_challenge_without_patchright(monkeypatch, api):
    monkeypatch.setitem(sys.modules, "patchright.async_api", None)  # makes the import fail
    api[HINT] = lambda r: httpx.Response(403, text="<html>Just a moment</html>")
    with pytest.raises(http.ApiError, match=r"taaghche-mcp\[browser\].*patchright install chromium"):
        await http.api(HINT)


async def test_retry_after_is_honoured_and_capped(monkeypatch, client, api):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)
    hits = []

    def limited(request):
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if len(hits) == 2:
            return httpx.Response(503, headers={"Retry-After": "999"})
        return httpx.Response(200, json=fixture("hint.json"))

    api[HINT] = limited
    assert not (await client.call_tool("tg_suggest", {"term": "شازده"})).is_error
    assert len(hits) == 3 and [w for w in waits if w] == [3, http.MAX_RETRY_WAIT]


async def test_home_page_is_a_navigation_request(api):
    api["/"] = "<html></html>"
    await http.page()
    h = api.calls[0].headers
    assert h["sec-fetch-mode"] == "navigate" and "origin" not in h
