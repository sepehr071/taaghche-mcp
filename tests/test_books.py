import httpx
import pytest
from conftest import fixture

pytestmark = pytest.mark.anyio


async def test_tg_search(client, api):
    api["/v3/search/filters"] = fixture("search.json")
    out = (
        await client.call_tool("tg_search", {"query": "شازده کوچولو", "format": "audiobook", "sort": "cheapest"})
    ).structured_content
    assert out["count"] == 4 and out["next_cursor"] == "12"
    first = out["items"][0]
    assert first["id"] == 72107 and first["format"] == "audiobook" and first["final_price"] == 15000
    assert first["price"] == 15000 and first["discount_pct"] == 0 and first["free"] is False
    assert first["authors"] == ["آنتوان دوسنت اگزوپری"] and first["narrators"] == ["جمعی از گویندگان"]
    assert first["translators"] == ["احمد شاملو"] and first["rating"] == 4.37 and first["ratings_count"] == 6022
    assert first["url"] == "https://taaghche.com/audiobook/72107"
    assert out["items"][2]["format"] == "ebook" and out["items"][2]["url"] == "https://taaghche.com/book/6426"
    assert api.params() == {
        "term": "شازده کوچولو",
        "offset": "null",
        "trackingData": "110160240",
        "sort": "3",
        "type": "2",
    }
    assert api.hosts() == ["get.taaghche.com"]


async def test_tg_search_flags_and_cursor(client, api):
    api["/v3/search/filters"] = fixture("search.json")
    await client.call_tool(
        "tg_search", {"query": "رمان", "discounted_only": True, "subscription_only": True, "cursor": "12"}
    )
    p = api.params()
    assert p["hasOff"] == "true" and p["inSubscription"] == "true" and p["offset"] == "12" and "sort" not in p


async def test_tg_search_price_filter_scans_pages(client, api):
    api["/v3/search/filters"] = lambda r: httpx.Response(200, json=fixture("search.json"))
    out = (await client.call_tool("tg_search", {"query": "شازده", "max_price": 20000})).structured_content
    # 15000 + 20000 per page; paging goes on while fewer than 10 match and the API has more
    assert [i["final_price"] for i in out["items"]] == [15000, 20000] * 5
    assert out["scanned"] == 20 and len(api.calls) == 5 and out["next_cursor"] == "12"
    assert all(i["final_price"] <= 20000 for i in out["items"])


async def test_tg_search_discount_and_free(client, api):
    data = fixture("search.json")
    data["tabItems"]["items"][1].update(price=0)
    data["tabItems"]["items"][0].update(price=15000, beforeOffPrice=25000)
    data["tabItems"]["hasMore"] = False
    api["/v3/search/filters"] = data
    out = (await client.call_tool("tg_search", {"query": "شازده", "free_only": True})).structured_content
    assert [i["id"] for i in out["items"]] == [65661] and out["items"][0]["free"] is True
    assert out["next_cursor"] is None
    out = (await client.call_tool("tg_search", {"query": "شازده"})).structured_content
    assert out["items"][0]["price"] == 25000 and out["items"][0]["discount_pct"] == 40


async def test_tg_search_bad_range(client, api):
    result = await client.call_tool("tg_search", {"query": "شازده", "min_price": 5, "max_price": 1})
    assert result.is_error and "above max_price" in result.content[0].text


async def test_tg_suggest(client, api):
    api["/v3/hint"] = fixture("hint.json")
    out = (await client.call_tool("tg_suggest", {"term": "شازده"})).structured_content
    assert out["suggestions"][0] == {
        "id": 72107,
        "title": "شازده کوچولو",
        "label": "شازده کوچولو (انتشارات نگاه)",
        "format": "audiobook",
        "in_subscription": None,
        "url": "https://taaghche.com/audiobook/72107",
    }
    assert out["suggestions"][2]["format"] == "ebook" and out["suggestions"][2]["in_subscription"] is True
    assert api.hosts() == ["explore.taaghche.com"]


async def test_tg_book_ebook(client, api):
    api["/v2/book/6426"] = fixture("book_ebook.json")
    out = (await client.call_tool("tg_book", {"book_id": 6426, "description_chars": 100})).structured_content
    assert out["title"] == "شازده کوچولو" and out["format"] == "ebook" and out["final_price"] == 58500
    assert out["pages"] == 192 and "duration_min" not in out and out["available"] is True
    assert out["publisher"] == "کتاب کوله پشتی" and out["publisher_slug"] == "کتاب-کوله-پشتی"
    assert out["authors"] == ["آنتوان دوسنت اگزوپری"] and out["translators"] == ["مریم صبوری"]
    assert out["author_slugs"] == ["آنتوان-دوسنت-اگزوپری"] and out["isbn"] == "9786006687780"
    assert out["rating"] == 4.41 and out["ratings_count"] == 827 and out["reviews_count"] == 388
    assert out["rating_breakdown"]["5"] == 632 and out["in_subscription"] is True
    assert out["categories"][0]["slug"] == "category/کتاب-داستان-خارجی" and out["categories"][0]["id"] == 68
    assert out["description"].endswith("…") and len(out["description"]) == 101
    assert out["url"] == "https://taaghche.com/book/6426"


async def test_tg_book_audiobook(client, api):
    api["/v2/book/72107"] = fixture("book_audio.json")
    out = (await client.call_tool("tg_book", {"book_id": 72107, "description_chars": 0})).structured_content
    assert out["format"] == "audiobook" and out["duration_min"] == 177 and "pages" not in out
    assert out["narrators"] == ["جمعی از گویندگان"] and out["translators"] == ["احمد شاملو"]
    assert out["audio_parts"][0] == {"title": "اجرای اول", "minutes": 86}
    assert out["categories"][0]["slug"].endswith("-صوتی") and out["description"] is None
    assert out["url"] == "https://taaghche.com/audiobook/72107"


async def test_tg_book_unknown(client, api):
    api["/v2/book/99999999"] = lambda r: httpx.Response(200, content=b"null")
    result = await client.call_tool("tg_book", {"book_id": 99999999})
    assert result.is_error and "No book with id 99999999" in result.content[0].text


async def test_tg_reviews(client, api):
    api["/v2/book/6426/comments"] = fixture("comments.json")
    out = (await client.call_tool("tg_reviews", {"book_id": 6426, "limit": 2, "max_chars": 100})).structured_content
    assert out["total"] == 388 and len(out["reviews"]) == 2
    r = out["reviews"][0]
    assert r["id"] == 398431 and r["rating"] == 5.0 and r["likes"] == 207 and r["replies"] == 20
    assert r["text"].startswith("شازده :آدما") and r["user"] == "_SOMEONE_"
    assert api.params() == {"trackingData": "110160240", "start": "0", "size": "2"}


async def test_tg_reviews_replies(client, api):
    api["/v2/comment/398431/replies"] = fixture("comments.json")
    out = (await client.call_tool("tg_reviews", {"book_id": 6426, "reply_to": 398431, "offset": 3})).structured_content
    assert len(out["reviews"]) == 2 and api.params()["start"] == "3"


async def test_tg_similar(client, api):
    api["/v2/book/6426"] = fixture("book_ebook.json")
    out = (await client.call_tool("tg_similar", {"book_id": 6426})).structured_content
    assert out["groups"][0]["title"] == "آثار مشابه" and len(out["groups"][0]["books"]) == 2
    assert out["groups"][0]["books"][0]["final_price"] >= 0
