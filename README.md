<!-- mcp-name: io.github.sepehr071/taaghche-mcp -->

# taaghche-mcp

**Let your AI agent search Taaghche, the Iranian ebook and audiobook store: compare prices and formats, read reviews, browse bestsellers, new and free books.**

Unofficial and read-only. Covers about 220,000 Persian and translated ebooks and audiobooks on
[taaghche.com](https://taaghche.com), prices in Toman. No API key, no account.

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
claude mcp add taaghche -- uvx taaghche-mcp
```

Any other MCP client (Claude Desktop, Cursor, VS Code): run `uvx taaghche-mcp` as a stdio server, e.g.

```json
{
  "mcpServers": {
    "taaghche": { "command": "uvx", "args": ["taaghche-mcp"] }
  }
}
```

Then ask, for example:

- "Is there an audiobook of شازده کوچولو, and which edition is the cheapest?"
- "Free audiobooks about history, most downloaded first."
- "What do readers say about book 6426? Show the most liked reviews."
- <span dir="rtl">پرفروش&zwnj;ترین کتاب&zwnj;های روان&zwnj;شناسی زیر ۱۰۰ هزار تومان کدامند؟</span>

## How it works

`taaghche-mcp` runs locally and calls the same public JSON endpoints the taaghche.com website uses
(`get.taaghche.com` for books, reviews, lists and search, `explore.taaghche.com` for suggestions; the category menu is
read from the home page). No hosted server in between. API map and quirks: `../taaghche/`.

## Tools

All 12 tools are annotated `readOnlyHint: true` and return compact structured JSON.

**Find and read a book**

| Tool | What it does |
|---|---|
| `tg_search` | Search by title, author or topic; format ebook / audiobook; sort (relevance, bestselling, cheapest, newest, ...); discounted, free, subscription, price range |
| `tg_suggest` | Typeahead: matching titles with their publisher |
| `tg_book` | Price and discount, format, pages or listening time, publisher, translator, narrator, rating breakdown, review count, categories, ISBN, availability |
| `tg_reviews` | Reader reviews (most liked first) with stars and likes; replies to one review |
| `tg_similar` | Similar books, other books of the same author and publisher |

**Browse**

| Tool | What it does |
|---|---|
| `tg_categories` | The category tree for ebooks and audiobooks with ids and slugs |
| `tg_browse` | A category or the whole catalog: format, sort, price ceiling, discounted, free; 16 per page |
| `tg_bestsellers` | The hottest books («داغ‌ترین»), overall or per category and format |
| `tg_new_releases` | Newest books, overall or per category and format |
| `tg_free_books` | Free books and audiobooks, most downloaded first |
| `tg_author` | An author's profile (nationality, life dates, biography) and books |
| `tg_publisher` | A publisher's description and books |

## Good to know

- **Prices are in Toman.** The API returns Toman (book 6426 is `58500` in the API and `۵۸,۵۰۰ تومان` on the page). `final_price` is what you pay, `price` is before discount, `discount_pct` a whole percent, `free` is true for 0.
- **The site cannot filter by price, so the price range is applied here.** `tg_search` applies `min_price`, `max_price` and `free_only` to the results it scans (up to 5 pages of 12); the browse tools, `tg_author` and `tg_publisher` do the same with `max_price` (up to 5 pages of 16). `scanned` tells how many books were looked at; with `sort="cheapest"` the ceiling is exact and the scan stops early. Only free and discounted lists are filtered by the site.
- **Paging:** `tg_search` returns 12 books per page, the browse tools and `tg_author` / `tg_publisher` 16. Pass `next_cursor` back as `cursor` with the same other arguments.
- **«Bestsellers» is the site's «hot» ranking.** Taaghche publishes no sales numbers; for the true best-selling order of a keyword use `tg_search(sort="bestselling")`.
- **Binahayat (بی‌نهایت) subscription:** `in_subscription` marks books included in it; the price is for buying the book permanently.
- **Dates ending `_fa` are Persian (Jalali).**
- Persian queries match best.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TAAGHCHE_MCP_PROXY` | unset | HTTP proxy for every request, e.g. `http://user:pass@host:port` (system proxy variables are ignored) |

## Development

```bash
export HTTPS_PROXY=http://127.0.0.1:10898 HTTP_PROXY=http://127.0.0.1:10898   # uv downloads only
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q            # offline, on recorded fixtures
uv run pytest -m live -q    # real API
```

## License

MIT
