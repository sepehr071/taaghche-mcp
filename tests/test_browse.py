import json
from pathlib import Path

import httpx
import pytest
from conftest import fixture

pytestmark = pytest.mark.anyio

EVERYTHING = "/v2/everything"


def filters(api, i=-1):
    return json.loads(api.params(i)["filters"])["list"]


def by_slug(slug_fixture, list_fixture="listing.json"):
    """/v2/everything answers the slug page for `slug=` and the book list for `filters=`."""

    def handler(request):
        return httpx.Response(200, json=fixture(slug_fixture if "slug" in request.url.params else list_fixture))

    return handler


async def test_tg_browse_by_id(client, api):
    api[EVERYTHING] = fixture("listing.json")
    args = {"category": 68, "format": "audiobook", "sort": "cheapest", "max_price": 30000, "discounted_only": True}
    out = (await client.call_tool("tg_browse", args)).structured_content
    # max_price 30000 is applied here: none of the 3 fixture books costs that little, and sort=cheapest
    # (ascending) stops the scan after the first page
    assert out["category"] == "داستان خارجی" and out["count"] == 0 and out["scanned"] == 3
    assert out["next_cursor"] is None and len(api.calls) == 1
    assert filters(api) == [{"type": 1, "value": 68}, {"type": 22, "value": 1}, {"type": 21, "value": 1}]
    p = api.params()
    assert p["order"] == "8" and p["offset"] == "null" and p["trackingData"] == "110160240"


async def test_tg_browse_price_ceiling_scans_pages(client, api):
    api[EVERYTHING] = fixture("listing.json")
    out = (await client.call_tool("tg_browse", {"category": 68, "max_price": 100000})).structured_content
    # hot order: every page is scanned (5 pages, 3 fixture books each); the 2 cheap ones per page are kept
    assert len(api.calls) == 5 and out["scanned"] == 15 and out["count"] == 10
    assert all(i["final_price"] <= 100000 for i in out["items"]) and out["next_cursor"] == "0-0-16-16"
    assert [json.loads(c.url.params["filters"])["list"] for c in api.calls[:1]] == [[{"type": 1, "value": 68}]]


async def test_tg_browse_slug_and_cursor(client, api):
    api[EVERYTHING] = by_slug("slug_category.json", "listing_p2.json")
    args = {"category": "کتاب-داستان-خارجی", "cursor": "0-0-16-16", "format": "ebook"}
    out = (await client.call_tool("tg_browse", args)).structured_content
    assert out["count"] == 3 and api.params(0)["slug"] == "category/کتاب-داستان-خارجی"
    assert filters(api) == [{"type": 1, "value": 68}, {"type": 21, "value": 0}]
    assert api.params()["offset"] == "0-0-16-16" and api.params()["order"] == "1"


async def test_tg_browse_unknown_category(client, api):
    api[EVERYTHING] = {"title": "x", "boxes": [], "breadcrumb": {"type": 0, "value": 0}}
    result = await client.call_tool("tg_browse", {"category": "نامعلوم"})
    assert result.is_error and "Unknown category" in result.content[0].text


async def test_tg_bestsellers_new_free(client, api):
    api[EVERYTHING] = fixture("listing.json")
    await client.call_tool("tg_bestsellers", {})
    assert api.params()["order"] == "1" and filters(api) == []
    await client.call_tool("tg_new_releases", {"category": 68})
    assert api.params()["order"] == "5" and filters(api) == [{"type": 1, "value": 68}]
    await client.call_tool("tg_free_books", {"format": "audiobook"})
    assert api.params()["order"] == "7" and filters(api) == [{"type": 6, "value": "0"}, {"type": 21, "value": 1}]


async def test_tg_categories(client, api):
    api["/"] = (Path(__file__).parent / "fixtures" / "home_nav.html").read_text(encoding="utf-8")
    out = (await client.call_tool("tg_categories", {})).structured_content
    assert api.hosts() == ["taaghche.com"]
    ebook = {c["title"]: c for c in out["ebook"]}
    assert ebook["کتاب رایگان"]["slug"] == "کتاب-رایگان"
    fiction = ebook["داستان و رمان"]
    assert fiction["id"] == 58 and fiction["slug"] == "کتاب-داستان-و-رمان"
    assert {"title": "داستان خارجی", "slug": "کتاب-داستان-خارجی"} in fiction["children"]
    audio = {c["title"]: c for c in out["audiobook"]}
    assert audio["ادبیات"]["children"] and all(c["slug"].endswith("صوتی") for c in audio["ادبیات"]["children"])
    out = (await client.call_tool("tg_categories", {"format": "audiobook"})).structured_content
    assert list(out) == ["audiobook"]


async def test_tg_categories_layout_changed(client, api):
    api["/"] = "<html>nothing</html>"
    result = await client.call_tool("tg_categories", {})
    assert result.is_error and "page layout" in result.content[0].text


async def test_tg_author_by_slug(client, api):
    api[EVERYTHING] = by_slug("slug_author.json")
    out = (await client.call_tool("tg_author", {"author": "آنتوان-دوسنت-اگزوپری"})).structured_content
    a = out["author"]
    assert a["id"] == 2072 and a["name"] == "آنتوان دوسنت اگزوپری" and a["nationality"] == "فرانسوی"
    assert a["born"] == "1900-06-29" and a["died"] == "1944-07-31" and a["bio"].startswith("آنتوان دوسنت")
    assert a["url"] == "https://taaghche.com/author/آنتوان-دوسنت-اگزوپری"
    assert out["count"] == 3 and filters(api) == [{"type": 2, "value": 2072}]


async def test_tg_author_by_id_and_wrong_slug(client, api):
    api[EVERYTHING] = fixture("listing.json")
    out = (await client.call_tool("tg_author", {"author": 2072})).structured_content
    assert out["author"]["id"] == 2072 and len(api.calls) == 1
    api[EVERYTHING] = fixture("slug_publisher.json")  # a publisher page is not an author
    result = await client.call_tool("tg_author", {"author": "کتاب-کوله-پشتی"})
    assert result.is_error and "Unknown author" in result.content[0].text


async def test_tg_publisher(client, api):
    api[EVERYTHING] = by_slug("slug_publisher.json")
    out = (
        await client.call_tool("tg_publisher", {"publisher": "کتاب-کوله-پشتی", "format": "ebook"})
    ).structured_content
    p = out["publisher"]
    assert p["id"] == 113 and p["name"] == "کتاب کوله پشتی" and p["bio"].startswith("نشر کتاب کوله پشتی")
    assert api.params(0)["slug"] == "publisher/کتاب-کوله-پشتی"
    assert filters(api) == [{"type": 3, "value": 113}, {"type": 21, "value": 0}]
