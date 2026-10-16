import re
from pathlib import Path

import httpx
import pytest
from conftest import fixture

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
    assert len(api.calls) == 2


async def test_timeout_is_not_retried(client, api):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    api[HINT] = slow
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert result.is_error and "did not answer in time" in result.content[0].text and len(api.calls) == 1


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


async def test_browser_user_agent_and_no_env_proxy(client, api, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")  # must be ignored (trust_env=False)
    api[HINT] = fixture("hint.json")
    result = await client.call_tool("tg_suggest", {"term": "شازده"})
    assert not result.is_error and "Chrome" in api.calls[0].headers["User-Agent"]
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
