"""MCP server entry point: registers every read-only Taaghche tool."""

import logging

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, books, browse  # noqa: F401  (imports register the tools)
from .registry import TOOLS

INSTRUCTIONS = """\
Unofficial, read-only access to Taaghche (taaghche.com), an Iranian store for ebooks and audiobooks (about
220,000 titles; some are included in the Binahayat / بی‌نهایت subscription). Nothing here can log in, buy,
borrow, rate, review or add to a library.

Workflow:
1. Find a book: tg_search (title / author / topic; format ebook or audiobook; sort; discount, free, price
   range; next_cursor for more) or tg_suggest (quick title matches).
2. One book: tg_book (price, discount, pages or listening time, publisher, translator, narrator, rating,
   categories, ISBN, availability), tg_reviews (reader reviews, most liked first; reply_to for replies),
   tg_similar (similar books, same author / publisher).
3. Browse without words: tg_categories (tree with ids and slugs), then tg_browse (category, format, sort,
   price ceiling, discounted, free), tg_bestsellers, tg_new_releases, tg_free_books. 16 books per page,
   next_cursor for more.
4. People and publishers: tg_author (profile + books; slug from tg_book author_slugs), tg_publisher
   (slug from tg_book publisher_slug).

Conventions: all prices are Toman (the API returns Toman, the same number the site shows). final_price is
what the customer pays, price is before discount (equal when there is none), discount_pct is an int, free is
true for price 0. rating is 0-5 (null = nobody rated), ratings_count the number of votes. in_subscription
means the book is included in the Binahayat subscription. Audiobooks have duration_min and narrators;
ebooks have pages. Dates ending _fa are Persian (Jalali). tg_search applies min_price / max_price / free_only
to the results it scans (scanned) because the site's search has no price filter; the browse tools, tg_author
and tg_publisher do the same with max_price (up to 5 pages; sort cheapest makes it exact). Persian queries match best ('شازده کوچولو', 'کتاب صوتی روانشناسی').
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

mcp = MCPServer(
    "taaghche-mcp",
    title="Taaghche",
    instructions=INSTRUCTIONS,
    version=__version__,
    website_url="https://github.com/sepehr071/taaghche-mcp",
)

for fn, title in TOOLS:
    mcp.add_tool(fn, title=title, annotations=READ_ONLY)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request floods client logs
    mcp.run()


if __name__ == "__main__":
    main()
