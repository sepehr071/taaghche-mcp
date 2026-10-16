import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def call(client, name, args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_tg_categories(client):
    data = await call(client, "tg_categories", {})
    fiction = next(c for c in data["ebook"] if c["title"] == "داستان و رمان")
    assert fiction["id"] == 58 and "children" not in fiction
    assert data["audiobook"] and any(c.get("slug") for c in data["audiobook"])
    assert (await call(client, "tg_categories", {"format": "ebook", "group": 58}))["ebook"][0]["children"]


async def test_tg_browse(client):
    data = await call(client, "tg_browse", {"category": "کتاب-داستان-خارجی", "format": "audiobook", "sort": "cheapest"})
    assert data["category"] == "داستان خارجی" and len(data["items"]) == 16 and data["next_cursor"]
    assert all(i["format"] == "audiobook" for i in data["items"])
    prices = [i["final_price"] for i in data["items"]]
    assert prices == sorted(prices)
    page2 = await call(
        client,
        "tg_browse",
        {"category": 68, "format": "audiobook", "sort": "cheapest", "cursor": data["next_cursor"]},
    )
    assert page2["items"] and not {i["id"] for i in page2["items"]} & {i["id"] for i in data["items"]}
    capped = await call(client, "tg_browse", {"category": 68, "max_price": 30000, "format": "ebook"})
    assert capped["items"] and all(i["final_price"] <= 30000 and i["format"] == "ebook" for i in capped["items"])


async def test_tg_bestsellers(client):
    data = await call(client, "tg_bestsellers", {"category": 68})
    assert len(data["items"]) == 16 and data["sort"] == "hot"


async def test_tg_new_releases(client):
    data = await call(client, "tg_new_releases", {"format": "ebook"})
    assert len(data["items"]) == 16 and all(i["format"] == "ebook" for i in data["items"])


async def test_tg_free_books(client):
    data = await call(client, "tg_free_books", {"format": "audiobook"})
    assert len(data["items"]) == 16 and all(i["free"] and i["format"] == "audiobook" for i in data["items"])


async def test_tg_author(client):
    data = await call(client, "tg_author", {"author": "آنتوان-دوسنت-اگزوپری"})
    a = data["author"]
    assert a["id"] == 2072 and a["nationality"] == "فرانسوی" and a["bio"]
    assert any(i["title"] == "شازده کوچولو" for i in data["items"])
    by_id = await call(client, "tg_author", {"author": 2072, "format": "audiobook"})
    assert by_id["items"] and all(i["format"] == "audiobook" for i in by_id["items"])


async def test_tg_publisher(client):
    data = await call(client, "tg_publisher", {"publisher": "کتاب-کوله-پشتی"})
    assert data["publisher"]["id"] == 113 and data["publisher"]["name"] == "کتاب کوله پشتی" and data["items"]
    assert all(i["publisher"] == "کتاب کوله پشتی" for i in data["items"])
