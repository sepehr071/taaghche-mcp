"""Finding and reading books: search, suggestions, one book, its reviews and similar books."""

from __future__ import annotations

import html
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import EXPLORE, SITE, TRACKING, ApiError, api
from .registry import tool

BookId = Annotated[int, Field(ge=1, le=100_000_000, description="Book id (from tg_search), e.g. 6426 or 72107.")]
MaxPrice = Annotated[int | None, Field(ge=0, description="Maximum price in Toman, e.g. 100000.")]
MinPrice = Annotated[int | None, Field(ge=0, description="Minimum price in Toman, e.g. 20000.")]
Format = Annotated[
    Literal["any", "ebook", "audiobook"],
    Field(description="ebook (الکترونیکی), audiobook (صوتی) or any."),
]
Cursor = Annotated[
    str | None,
    Field(
        pattern=r"^[0-9-]{1,40}$",
        description="next_cursor of the previous reply, for the next page (keep the other arguments the same).",
    ),
]

# The site's sort ids of /v3/search/filters (`sortOptions`).
SEARCH_SORTS = {"relevance": 0, "bestselling": 1, "shortest": 2, "cheapest": 3, "newest": 4, "popular": 5}
SEARCH_FORMAT = {"any": None, "ebook": 1, "audiobook": 2}
# The site's role ids of authors[].type.
ROLES = {1: "authors", 2: "translators", 4: "narrators"}
MAX_SEARCH_REQUESTS = 5  # pages scanned when a price filter is applied here (the API has none on search)


def _text(fragment: str | None) -> str:
    """HTML to plain text."""
    s = re.sub(r"</p>|<br\s*/?>|</li>|</h\d>", "\n", fragment or "")
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return "\n".join(line for line in (re.sub(r"[ \t\xa0]+", " ", x).strip() for x in s.splitlines()) if line)


def _clip(text: str | None, n: int) -> str | None:
    text = text or ""
    return (text[:n] + "…" if len(text) > n else text) or None


def _name(person: dict[str, Any]) -> str:
    return " ".join(x for x in (person.get("firstName"), person.get("lastName")) if x).strip()


def _people(book: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"authors": [], "translators": [], "narrators": []}
    for p in book.get("authors") or []:
        key = ROLES.get(p.get("type"))
        if key and (n := _name(p)) and n not in out[key]:
            out[key].append(n)
    return out


def _format(book: dict[str, Any]) -> str | None:
    kind = book.get("type")
    if kind == "Audio":
        return "audiobook"
    if kind == "Text":
        return "ebook"
    if kind:
        return str(kind).lower()
    first = (book.get("types") or [{}])[0].get("id")  # search results have no `type`
    return {1: "ebook", 3: "audiobook"}.get(first)


def _book_url(book_id: int, fmt: str | None) -> str:
    return f"{SITE}/{'audiobook' if fmt == 'audiobook' else 'book'}/{book_id}"


def _prices(book: dict[str, Any]) -> dict[str, Any]:
    """The API price is Toman (the page shows the same number); `price` is what the customer pays."""
    final = int(book.get("price") or 0)
    before = int(book.get("beforeOffPrice") or 0)
    if before <= final:
        before = final
    return {
        "final_price": final,
        "price": before,
        "discount_pct": round(100 * (before - final) / before) if before else 0,
        "free": final == 0,
    }


def _rating(book: dict[str, Any]) -> dict[str, Any]:
    count = sum(int(r.get("count") or 0) for r in book.get("rates") or [])
    return {"rating": round(float(book["rating"]), 2) if count and book.get("rating") else None, "ratings_count": count}


def _card(book: dict[str, Any]) -> dict[str, Any]:
    """One compact book row built from a list / search item."""
    fmt = _format(book)
    people = _people(book)
    out: dict[str, Any] = {"id": book.get("id"), "title": book.get("title"), "format": fmt, **_prices(book)}
    out["authors"] = people["authors"]
    if people["translators"]:
        out["translators"] = people["translators"]
    if people["narrators"]:
        out["narrators"] = people["narrators"]
    out["publisher"] = book.get("publisher")
    out.update(_rating(book))
    if book.get("numberOfPages"):
        out["pages"] = book["numberOfPages"]
    if book.get("sumDurationSeconds"):
        out["duration_min"] = round(book["sumDurationSeconds"] / 60)
    if book.get("subscriptionAvailable"):
        out["in_subscription"] = True  # included in the "Binahayat" (بی‌نهایت) subscription
    out["url"] = _book_url(book["id"], fmt)
    out["cover"] = book.get("coverUri")
    return out


@tool("Search books")
async def tg_search(
    query: Annotated[
        str,
        Field(
            min_length=2, max_length=100, description="Title, author or topic, Persian works best, e.g. 'شازده کوچولو'."
        ),
    ],
    format: Format = "any",
    sort: Annotated[
        Literal["relevance", "bestselling", "shortest", "cheapest", "newest", "popular"],
        Field(
            description="relevance, bestselling (پرفروش‌ترین), shortest, cheapest (ascending price), newest, popular."
        ),
    ] = "relevance",
    discounted_only: Annotated[bool, Field(description="Only books with a discount now.")] = False,
    free_only: Annotated[bool, Field(description="Only free books (price 0).")] = False,
    subscription_only: Annotated[
        bool, Field(description="Only books included in the Binahayat (بی‌نهایت) subscription.")
    ] = False,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Search Taaghche's ebooks and audiobooks by title, author or topic: price in Toman, discount, rating, format.

    Use first for "find book X" / "is there an audiobook of X" / "cheapest edition of X". Price limits and
    free_only are applied here to what the search returns (the site's search has no price filter), so
    a narrow range may need several pages: pass next_cursor back as cursor (same other arguments). Details:
    tg_book; opinions: tg_reviews; browse a category without words: tg_browse.
    """
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ApiError("min_price is above max_price.")
    local = free_only or min_price is not None or max_price is not None
    items: list[dict[str, Any]] = []
    offset, scanned, has_more = cursor or "null", 0, True
    for _ in range(MAX_SEARCH_REQUESTS if local else 1):
        data = await api(
            "/v3/search/filters",
            {
                "term": query,
                "offset": offset,
                "trackingData": TRACKING,
                "sort": SEARCH_SORTS[sort] or None,
                "type": SEARCH_FORMAT[format],
                "hasOff": "true" if discounted_only else None,
                "inSubscription": "true" if subscription_only else None,
            },
        )
        tab = data.get("tabItems") or {}
        page = [_card(b) for b in tab.get("items") or []]
        scanned += len(page)
        items += [
            c
            for c in page
            if (not free_only or c["free"])
            and (min_price is None or c["final_price"] >= min_price)
            and (max_price is None or c["final_price"] <= max_price)
        ]
        has_more, offset = bool(tab.get("hasMore")), str(tab.get("nextOffset") or "")
        if not has_more or not offset or len(items) >= 10:
            break
    return {
        "query": query,
        "count": len(items),
        "scanned": scanned,
        "items": items,
        "next_cursor": offset if has_more and offset else None,
    }


@tool("Search suggestions")
async def tg_suggest(
    term: Annotated[str, Field(min_length=3, max_length=60, description="Start of a title or author, e.g. 'شازده'.")],
) -> dict[str, Any]:
    """Typeahead of the Taaghche search box: up to about 10 book titles (with publisher) that match the text.

    Use to find the book id of a title quickly or to check how a title is spelled; for prices and filters use tg_search.
    """
    data = await api("/v3/hint", {"term": term}, host=EXPLORE)
    out = []
    for s in data.get("suggestions") or []:
        d = s.get("data") or {}
        fmt = {"book": "ebook", "audioBook": "audiobook"}.get(d.get("type"), d.get("type"))
        out.append(
            {
                "id": d.get("id"),
                "title": d.get("content"),
                "label": _text(s.get("value")),
                "format": fmt,
                "in_subscription": bool(d.get("subscription")) or None,
                "url": _book_url(d["id"], fmt) if d.get("id") else None,
            }
        )
    return {"term": term, "suggestions": out}


async def _book(book_id: int) -> dict[str, Any]:
    data = await api(f"/v2/book/{book_id}")
    if not isinstance(data, dict) or not data.get("book"):
        raise ApiError(f"No book with id {book_id} on Taaghche. Find ids with tg_search.")
    return data


@tool("Book details")
async def tg_book(
    book_id: BookId,
    description_chars: Annotated[
        int, Field(ge=0, le=5000, description="Cut the description after this many characters.")
    ] = 800,
) -> dict[str, Any]:
    """Everything about one book: price in Toman and discount, ebook or audiobook, pages or listening time, publisher,
    author / translator / narrator, rating with breakdown, number of reviews, categories, ISBN, availability.

    Use after tg_search. Reviews: tg_reviews; similar books: tg_similar; the author's other books: tg_author
    (author_slug); the publisher's: tg_publisher (publisher_slug).
    """
    data = await _book(book_id)
    b = data["book"]
    fmt = _format(b)
    parts = [f for f in b.get("files") or [] if f.get("type") == 8]  # audio parts
    out: dict[str, Any] = {"id": b.get("id"), "title": b.get("title"), "format": fmt, **_prices(b)}
    out["available"] = b.get("state") == 1
    out["in_subscription"] = bool(b.get("subscriptionAvailable"))
    out.update(_people(b))
    out["author_slugs"] = [
        p["slug"].removeprefix("author/") for p in b.get("authors") or [] if p.get("type") == 1 and p.get("slug")
    ]
    out["publisher"] = b.get("publisher")
    out["publisher_slug"] = (b.get("publisherSlug") or "").removeprefix("publisher/") or None
    out.update(_rating(b))
    out["rating_breakdown"] = {str(int(r["value"])): r["count"] for r in b.get("rates") or []}
    out["rating_aspects"] = {r["title"]: round(r["point"], 2) for r in b.get("rateDetails") or [] if r.get("title")}
    out["reviews_count"] = data.get("commentsCount")
    out["quotes_count"] = data.get("quotesCount")
    if b.get("numberOfPages"):
        out["pages"] = b["numberOfPages"]
    if b.get("sumDurationSeconds"):
        out["duration_min"] = round(b["sumDurationSeconds"] / 60)
    if parts:
        out["audio_parts"] = [
            {"title": p.get("title"), "minutes": round((p.get("duration") or 0) / 60)} for p in parts[:30]
        ]
    out["categories"] = [
        {
            "id": c.get("id"),
            "title": c.get("title"),
            "slug": (c.get("audioSlug") if fmt == "audiobook" else c.get("slug")),
        }
        for c in b.get("categories") or []
    ]
    out["tags"] = [t.get("tag") for t in b.get("labels") or [] if t.get("tag")][:10]
    out["isbn"] = b.get("ISBN")
    out["published_fa"] = b.get("publishDate") or b.get("persianShortDate")
    out["description"] = _clip(_text(b.get("description")), description_chars) if description_chars else None
    out["url"] = _book_url(b["id"], fmt)
    out["cover"] = b.get("coverUri")
    return out


@tool("Book reviews")
async def tg_reviews(
    book_id: BookId,
    offset: Annotated[int, Field(ge=0, le=100_000, description="How many reviews to skip.")] = 0,
    limit: Annotated[int, Field(ge=1, le=40, description="Reviews per call.")] = 10,
    reply_to: Annotated[
        int | None, Field(ge=1, description="A review id: return the replies to that review instead.")
    ] = None,
    max_chars: Annotated[
        int, Field(ge=100, le=3000, description="Cut each review text after this many characters.")
    ] = 500,
) -> dict[str, Any]:
    """Reader reviews of a book, most liked first: star rating, text, likes, number of replies.

    Use after tg_book to see what readers think (total count = reviews_count). Page with offset. To read
    the replies of one review pass its id as reply_to.
    """
    if reply_to:
        data = await api(f"/v2/comment/{reply_to}/replies", {"trackingData": TRACKING, "start": offset, "size": limit})
    else:
        data = await api(f"/v2/book/{book_id}/comments", {"trackingData": TRACKING, "start": offset, "size": limit})
    return {
        "book_id": book_id,
        "total": data.get("total"),
        "offset": offset,
        "reviews": [
            {
                "id": c.get("id"),
                "user": c.get("nickname"),
                "date_fa": c.get("creationDate"),
                "rating": c.get("rate") or None,
                "text": _clip(c.get("comment"), max_chars),
                "likes": c.get("likeCount"),
                "dislikes": c.get("dislikeCount"),
                "replies": c.get("repliesCount"),
            }
            for c in data.get("commentsList") or []
        ],
    }


@tool("Similar books")
async def tg_similar(book_id: BookId) -> dict[str, Any]:
    """Books Taaghche suggests next to this one (similar works, other books of the same author and publisher),
    as separate lists with price, format and rating.

    Use after tg_book to compare alternatives, e.g. another translation or the audiobook version.
    """
    data = await _book(book_id)
    groups = []
    for g in data.get("relatedBooks") or []:
        books = [_card(b) for b in (g.get("bookData") or {}).get("books") or []]
        if books:
            groups.append({"title": g.get("title"), "books": books})
    return {"book_id": book_id, "groups": groups}
