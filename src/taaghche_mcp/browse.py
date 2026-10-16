"""Browsing the catalog: categories, category lists, bestsellers, new releases, free books, authors, publishers."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from pydantic import Field

from .books import (
    Cursor,
    Format,
    MaxPrice,
    _card,
    _clip,
    _text,
)
from .http import SITE, TRACKING, ApiError, api, page
from .registry import tool

# Filter type ids of /v2/everything (the site's enum `filter-<name>`).
CATEGORY, AUTHOR, PUBLISHER, PRICE, DISCOUNT, BOOK_TYPE = 1, 2, 3, 6, 22, 21
# Order ids of the site's sort menu.
SORTS = {"hot": 1, "newest": 5, "most_downloaded": 7, "alphabetical": 2, "cheapest": 8, "most_expensive": 11}
BOOK_TYPES = {"any": None, "ebook": 0, "audiobook": 1}

Sort = Annotated[
    Literal["hot", "newest", "most_downloaded", "alphabetical", "cheapest", "most_expensive"],
    Field(description="hot = trending ranking; cheapest = ascending price."),
]
Slug = Annotated[str, Field(max_length=200)]


MAX_PAGES = 5  # pages scanned when a price ceiling is applied here


async def _list(
    filters: list[dict[str, Any]], sort: str, cursor: str | None, fmt: str = "any", max_price: int | None = None
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any], int]:
    """/v2/everything for a filter list: 16 books a page. Returns cards, next cursor, last raw reply, books scanned.

    The API ignores a price ceiling (filter 6 only knows "0" = free), so max_price is applied here over up to
    MAX_PAGES pages; sorted by cheapest the scan stops at the first book above the ceiling.
    """
    if (kind := BOOK_TYPES[fmt]) is not None:
        filters = [*filters, {"type": BOOK_TYPE, "value": kind}]
    items: list[dict[str, Any]] = []
    offset, scanned, more = cursor or "null", 0, None
    for _ in range(MAX_PAGES if max_price is not None else 1):
        data = await api(
            "/v2/everything",
            {
                "filters": json.dumps({"list": filters}, separators=(",", ":")),
                "offset": offset,
                "order": SORTS[sort],
                "trackingData": TRACKING,
            },
        )
        page_items = [_card(b) for b in (data.get("bookList") or {}).get("books") or []]
        scanned += len(page_items)
        more = data.get("nextOffset") if data.get("hasMore") else None
        if max_price is None:
            return page_items, more, data, scanned
        items += [c for c in page_items if c["final_price"] <= max_price]
        if sort == "cheapest" and page_items and page_items[-1]["final_price"] > max_price:
            more = None  # ascending prices: nothing further can fit
        if not more or len(items) >= 10:
            break
        offset = more
    return items, more, data, scanned


def _server_filters(free_only: bool, discounted_only: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if free_only:
        out.append({"type": PRICE, "value": "0"})
    if discounted_only:
        out.append({"type": DISCOUNT, "value": 1})
    return out


async def _category_id(category: int | str) -> tuple[int, str | None]:
    """A category id with its title, or a slug resolved through the category page."""
    if isinstance(category, int) or str(category).isdigit():
        return int(category), None
    slug = str(category).removeprefix("category/")
    data = await api("/v2/everything", {"slug": f"category/{slug}", "trackingData": TRACKING})
    crumb = data.get("breadcrumb") or {}
    if crumb.get("type") != CATEGORY or not crumb.get("value"):
        raise ApiError(f"Unknown category '{slug}'. List them with tg_categories.")
    return int(crumb["value"]), crumb.get("title")


async def _browse(
    category: int | str | None,
    fmt: str,
    sort: str,
    discounted_only: bool,
    free_only: bool,
    max_price: int | None,
    cursor: str | None,
) -> dict[str, Any]:
    filters, title = [], None
    if category not in (None, "", 0):
        cat_id, title = await _category_id(category)
        filters.append({"type": CATEGORY, "value": cat_id})
    filters += _server_filters(free_only, discounted_only)
    items, more, data, scanned = await _list(filters, sort, cursor, fmt, max_price)
    return {
        "category": (title or (data.get("breadcrumb") or {}).get("title")) if category else None,
        "sort": sort,
        "count": len(items),
        **({"scanned": scanned} if max_price is not None else {}),
        "items": items,
        "next_cursor": more,
    }


CategoryArg = Annotated[
    int | Slug | None,
    Field(description="Category id (e.g. 68) or slug (e.g. 'کتاب-داستان-خارجی') from tg_categories; empty = all."),
]


@tool("Browse a category")
async def tg_browse(
    category: CategoryArg = None,
    format: Format = "any",
    sort: Sort = "hot",
    discounted_only: Annotated[bool, Field(description="Only discounted books.")] = False,
    free_only: Annotated[bool, Field(description="Only free books.")] = False,
    max_price: MaxPrice = None,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """List books of a category (or the whole catalog), 16 per page, by sort, format and price ceiling.

    max_price is applied here over up to 5 pages (`scanned`); exact with sort='cheapest'. For words use tg_search.
    """
    return await _browse(category, format, sort, discounted_only, free_only, max_price, cursor)


@tool("Bestsellers")
async def tg_bestsellers(
    category: CategoryArg = None,
    format: Format = "any",
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Hottest books now (Taaghche's trending ranking; no sales numbers are public), overall or per category.

    For best-selling order of a keyword use tg_search(sort='bestselling'). Next: tg_book.
    """
    return await _browse(category, format, "hot", False, False, None, cursor)


@tool("New releases")
async def tg_new_releases(
    category: CategoryArg = None,
    format: Format = "any",
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Newest books (most recently added first), overall or per category. Next: tg_book."""
    return await _browse(category, format, "newest", False, False, None, cursor)


@tool("Free books")
async def tg_free_books(
    category: CategoryArg = None,
    format: Format = "any",
    sort: Sort = "most_downloaded",
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Free books and audiobooks (price 0), most downloaded first by default. Next: tg_book."""
    return await _browse(category, format, sort, False, True, None, cursor)


def _menu(html_text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse the category menu of the home page into {format: [{title, slug?, id?, children?}]}."""
    marks = list(re.finditer(r'id="category-(?:list-(\d)|children-(\d)-(\d+))"', html_text))
    link = re.compile(r'<a[^>]*href="/category/([^"]+)"[^>]*>(.*?)</a>', re.S)
    parent = re.compile(r'aria-controls="category-children-(\d)-(\d+)"[^>]*>.*?<p>(.*?)<span', re.S)
    out: dict[str, list[dict[str, Any]]] = {"ebook": [], "audiobook": []}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for i, m in enumerate(marks):
        seg = html_text[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(html_text)]
        links = [{"title": _text(t), "slug": unquote(s)} for s, t in link.findall(seg)]
        if m.group(1):  # a top-level list: plain categories and groups that open a children list
            entries: list[dict[str, Any]] = links
            for f, pid, title in parent.findall(seg):
                groups[(f, pid)] = {"title": _text(title), "id": int(pid)}
                entries.append(groups[(f, pid)])
            out["ebook" if m.group(1) == "1" else "audiobook"] += entries
        elif (m.group(2), m.group(3)) in groups and links:  # first link = the whole group
            groups[(m.group(2), m.group(3))].update(slug=links[0]["slug"], children=links[1:])
    return out


@tool("Categories")
async def tg_categories(
    format: Format = "any",
    group: Annotated[
        int | None,
        Field(ge=1, description="Top-level group id from a previous call: only it, with sub-categories."),
    ] = None,
) -> dict[str, Any]:
    """Category tree: top-level groups (id) and standalone categories, each with a slug.

    Pass an id or slug to tg_browse and the other list tools. Ebook and audiobook slugs differ (audio ends in
    «-صوتی»). Sub-categories need group=<id> (and format).
    """
    menu = _menu(await page("/"))
    if not menu["ebook"] and not menu["audiobook"]:
        raise ApiError("Could not read the category menu from taaghche.com (the page layout may have changed).")
    menu = {k: v for k, v in menu.items() if format in ("any", k)}
    if group is not None:
        return {k: [c for c in v if c.get("id") == group] for k, v in menu.items()}
    return {k: [{f: x for f, x in c.items() if f != "children"} for c in v] for k, v in menu.items()}


async def _entity(
    prefix: str,
    filter_type: int,
    who: int | str,
    fmt: str,
    sort: str,
    max_price: int | None,
    cursor: str | None,
) -> dict[str, Any]:
    """An author / publisher: info from the slug page plus a filtered book list."""
    info: dict[str, Any] = {}
    if isinstance(who, int) or str(who).isdigit():
        who_id, slug = int(who), None
    else:
        slug = str(who).removeprefix(f"{prefix}/")
        page_data = await api("/v2/everything", {"slug": f"{prefix}/{slug}", "trackingData": TRACKING})
        crumb = page_data.get("breadcrumb") or {}
        if crumb.get("type") != filter_type or not crumb.get("value"):
            raise ApiError(f"Unknown {prefix} '{slug}'. Find the slug with tg_book ({prefix}_slug).")
        who_id = int(crumb["value"])
        bio = next(
            (
                _text(b["contentConfig"].get("content"))
                for b in page_data.get("boxes") or []
                if b.get("type") == 3 and (b.get("contentConfig") or {}).get("content")
            ),
            "",
        )
        info = {"name": page_data.get("title"), "slug": slug, "bio": _clip(bio, 1200), "url": f"{SITE}/{prefix}/{slug}"}
        person = (page_data.get("additionalData") or {}).get("authorInfo")
        if person:
            info.update(
                nationality=(person.get("nationality") or {}).get("Title"),
                born=(person.get("birthDate") or "")[:10] or None,
                died=(person.get("deathDate") or "")[:10] or None,
                image=person.get("imageUrl"),
            )
    info["id"] = who_id
    items, more, data, scanned = await _list([{"type": filter_type, "value": who_id}], sort, cursor, fmt, max_price)
    info.setdefault("name", (data.get("breadcrumb") or {}).get("title"))
    return {
        prefix: info,
        "count": len(items),
        **({"scanned": scanned} if max_price is not None else {}),
        "items": items,
        "next_cursor": more,
    }


@tool("Author and books")
async def tg_author(
    author: Annotated[
        int | Slug,
        Field(description="Author slug (e.g. 'آنتوان-دوسنت-اگزوپری', from tg_book author_slugs) or id (e.g. 2072)."),
    ],
    format: Format = "any",
    sort: Sort = "hot",
    max_price: MaxPrice = None,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """An author's profile (with a slug: nationality, life dates, bio) and their books, 16 per page.

    Sort, format and max_price work like tg_browse. Next: tg_book.
    """
    return await _entity("author", AUTHOR, author, format, sort, max_price, cursor)


@tool("Publisher and books")
async def tg_publisher(
    publisher: Annotated[
        int | Slug,
        Field(description="Publisher slug (e.g. 'کتاب-کوله-پشتی', from tg_book publisher_slug) or id (e.g. 113)."),
    ],
    format: Format = "any",
    sort: Sort = "hot",
    max_price: MaxPrice = None,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """A publisher's description and its books, 16 per page.

    Sort, format and max_price work like tg_browse. Next: tg_book.
    """
    return await _entity("publisher", PUBLISHER, publisher, format, sort, max_price, cursor)
