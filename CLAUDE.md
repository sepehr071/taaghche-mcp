# taaghche-mcp

Unofficial, read-only MCP server for Taaghche (taaghche.com), an Iranian ebook and audiobook store (about 220,000 titles).
Not published yet (no git repo, no PyPI / registry entry). It is a sibling of `../shopino-mcp` and copies its layout and idioms; keep them in step.

API map this server is built on (endpoints, samples, quirks, test report): `../taaghche/`.

## Layout

- `src/taaghche_mcp/http.py`: the only HTTP code. One shared `httpx.AsyncClient` (`trust_env=False`) on a
  `httpx_curl_cffi.AsyncCurlTransport(impersonate="chrome")`, so TLS, HTTP/2 and the default headers (User-Agent,
  sec-ch-ua*) come from one Chrome profile; we send no User-Agent of our own, only `Accept-Language` and the web app's XHR
  headers (Origin / Referer / sec-fetch-*). Proxy only from `TAAGHCHE_MCP_PROXY`, a comma-separated list used one at a time
  (rotate on 403 / 429 / challenge / connect error). Concurrency 2, 150-600 ms jitter before each uncached call, up to 2
  retries with jittered backoff and `Retry-After` (cap 10 s) on timeouts, network errors, an empty 200 body, 429 and 5xx;
  403 is never retried. A bot-check page (`_is_challenge`) falls back to a headless patchright browser (optional extra
  `taaghche-mcp[browser]`, then `patchright install chromium`) whose cookies are copied into the client. Also
  `api(path, params, host=GET)` for JSON on `get.taaghche.com` (or `EXPLORE`), `page(path)` for the home page HTML,
  replies streamed and capped at 8 MB, a 5 min cache for `/v2/book/{id}`, the home page and
  `slug=` pages (key = url + all params), `ApiError(ToolError)`, `set_transport()` for tests. Always sends `trackingData` where the site does (see quirks).
- `server.py` also drops null fields from every result (except `next_cursor`), strips pydantic `title` noise from
  the input schemas, and sends one compact text block (no structuredContent / outputSchema). Keep every `cover`,
  `image` and `url` field: downstream apps read them. `tg_reviews` text is cut at `max_chars` (default 400).
- `registry.py`: `@tool("Title")` collects tools. `server.py`: registers them with read-only annotations, holds `INSTRUCTIONS`.
- Tools: `books.py` (search, suggest, book, reviews, similar; also the shared `_card` / `_prices` helpers) and
  `browse.py` (categories, browse, bestsellers, new releases, free books, author, publisher). 12 tools, all `tg_*`.
- `tests/`: `test_<module>.py` unit tests (MockTransport + trimmed real fixtures in `tests/fixtures/`),
  `test_live_<module>.py` live tests (`@pytest.mark.live`), `test_http.py` for the client and the README tool tables.

## Commands

```bash
export HTTPS_PROXY=http://127.0.0.1:10898 HTTP_PROXY=http://127.0.0.1:10898   # uv downloads only
uv sync
uv run ruff check . && uv run ruff format --check .
uv run python -m pytest -q            # unit tests, offline (not `uv run pytest`: uv trampoline error on this machine)
uv run python -m pytest -m live -q    # one live test per tool, about 10 s
```

## Rules for changes

- Read-only only. Never call login / OTP (`/v4/site/auth/*`), `/mybook/*` (library, orders, purchase), reviews or
  quote like / reply / post (`/v2/reviews*` POST, `/v1/quotes/*` POST), `/v3/click`, `/api/metrics`, profile, payment or
  gift endpoints.
- Prices: the API returns **Toman**; nothing to convert. `final_price` = API `price` (what the customer pays),
  `price` = `beforeOffPrice` when higher, `discount_pct` computed, `free` when 0. Rating 0-5, `null` with no votes.
- New tool: `@tool("Title")` async function, every param `Annotated[..., Field(...)]` with constraints and an example,
  docstring = what it returns + when to use + what to call next, compact dict output. Add a unit test with a real
  trimmed fixture, a live test, a README tool-table row, and an `INSTRUCTIONS` mention.
- Keep `README.md` tool tables equal to `list_tools()` (a unit test checks it). Deps stay `mcp`, `httpx`, `httpx-curl-cffi` + `curl-cffi`; `patchright==1.63.0` only in the optional `browser` extra.
- ZWNJ in code is written `‌`, never the invisible character.

## Taaghche quirks (keep them)

- Hosts: `get.taaghche.com` (books, comments, lists, search; same-site CORS, no auth), `explore.taaghche.com`
  (`/v3/hint` suggestions), `quote.taaghche.com` (quotes, not used), home page HTML for the category menu.
  `gw.taaghche.com` is the account / library gateway: not used.
- `/v2/everything` needs `trackingData=110160240` (the site's constant): without it the reply is the shelf layout
  (`boxes`) instead of `bookList`. With `filters=` it lists 16 books; with `slug=` it returns the page (shelves, bio,
  `breadcrumb` = type + id of the category / author / publisher). First page `offset=null`, then the reply's `nextOffset`
  (looks like `0-0-16-16`). `offset=0-0-0-0` gives an empty list.
- Filter types (`{"list":[{"type":N,"value":V}]}`): 1 category id, 2 author id, 3 publisher id, 6 price (only `"0"` = free works;
  `"-30000"` and `"10000-30000"` are accepted and ignored, so price ceilings are applied client-side, up to 5 pages), 21 book type (0 ebook, 1 audiobook), 22 discounted (value 1),
  50 target (1 = audio only, 0 = no filter), 20 collection / tag id. Several filters combine. Orders: 1 hot, 5 newest,
  7 most downloaded, 2 alphabetical, 8 cheapest, 11 most expensive.
- Search `/v3/search/filters`: `term`, `offset` (`null`, then `nextOffset` = a plain number), `sort` 0-5, `type` 0/1/2
  (all / ebook / audio), `hasOff=true`, `inSubscription=true` (`hasOff=1` is a 400). No price filter, 12 per page.
- Unknown book id: `/v2/book/{id}` answers 200 with body `null`; non-numeric id 400. Comments: `/v2/book/{id}/comments?start&size`
  (size up to 40, most liked first), replies `/v2/comment/{id}/replies`.
- The category menu exists only in the home page HTML (`id="category-list-1|2"`, `id="category-children-<fmt>-<id>"`), no JSON.
- Book detail `/v2/book/{id}` is large (about 600 KB, with related books and quotes); `/v2/book/{id}/priceinfo` is the tiny live price.
