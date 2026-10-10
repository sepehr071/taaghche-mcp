import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def call(client, name, args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_tg_search(client):
    data = await call(client, "tg_search", {"query": "شازده کوچولو"})
    assert data["count"] >= 5 and data["next_cursor"]
    assert any(i["format"] == "audiobook" for i in data["items"]) and any(i["format"] == "ebook" for i in data["items"])
    assert all(i["title"] and i["final_price"] >= 0 for i in data["items"])
    audio = await call(client, "tg_search", {"query": "شازده کوچولو", "format": "audiobook", "sort": "cheapest"})
    assert audio["items"] and all(i["format"] == "audiobook" for i in audio["items"])
    prices = [i["final_price"] for i in audio["items"]]
    assert prices == sorted(prices)
    cheap = await call(client, "tg_search", {"query": "شازده کوچولو", "max_price": 30000})
    assert cheap["items"] and all(i["final_price"] <= 30000 for i in cheap["items"])


async def test_tg_suggest(client):
    data = await call(client, "tg_suggest", {"term": "شازده"})
    assert data["suggestions"] and "شازده" in data["suggestions"][0]["title"]


async def test_tg_book(client):
    ebook = await call(client, "tg_book", {"book_id": 6426})
    assert ebook["title"] == "شازده کوچولو" and ebook["format"] == "ebook" and ebook["pages"] == 192
    assert ebook["final_price"] > 0 and ebook["rating"] and ebook["reviews_count"] > 100
    assert ebook["author_slugs"] and ebook["publisher_slug"] and ebook["categories"]
    audio = await call(client, "tg_book", {"book_id": 72107})
    assert audio["format"] == "audiobook" and audio["duration_min"] > 60 and audio["narrators"]


async def test_tg_book_unknown(client):
    result = await client.call_tool("tg_book", {"book_id": 99999999})
    assert result.is_error and "No book with id" in result.content[0].text


async def test_tg_reviews(client):
    data = await call(client, "tg_reviews", {"book_id": 6426, "limit": 5})
    assert data["total"] > 100 and len(data["reviews"]) == 5
    assert data["reviews"][0]["likes"] >= data["reviews"][1]["likes"]
    commented = next(r for r in data["reviews"] if r["replies"])
    replies = await call(client, "tg_reviews", {"book_id": 6426, "reply_to": commented["id"], "limit": 3})
    assert replies["reviews"]


async def test_tg_similar(client):
    data = await call(client, "tg_similar", {"book_id": 6426})
    assert data["groups"] and data["groups"][0]["books"]
    assert all(b["title"] for g in data["groups"] for b in g["books"])
