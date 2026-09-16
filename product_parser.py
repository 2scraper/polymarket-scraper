"""
product_parser.py
------------------
Everything this repo knows about Polymarket. The engines, the writers, the
proxy pool and the solver are family code and know nothing about this site;
if you are writing site knowledge anywhere else, it belongs here (§1).

What Polymarket is, in the two sentences a parser needs
------------------------------------------------------
An EVENT is a folder — "Fed Decision in September?" — and a MARKET is one
binary question inside it — "Will the Fed increase interest rates by 25 bps
after the September 2026 meeting?" — priced between 0 and 1. One event holds
one to five markets on the captures measured here. A row of this scraper's
output is a MARKET, because that is the thing with a price, an id and an
order book; the event travels with it in the `event_*` columns.

Three views of the same markets, in this order
----------------------------------------------
1. **The inlined Next.js payload (primary).** Every page ships its data in
   `self.__next_f.push([1, "…"])` chunks — a React Server Components stream
   whose text, once the chunk strings are JSON-decoded and concatenated,
   contains the site's own market objects verbatim. This is where the exact
   numbers are: `volumeNum` 19154686.147784997 against the tile's rounded
   "$19M". Measured 2026-09-16: 20 events / 94 markets on `/predictions`,
   20 / 70 on `/predictions/crypto`, 1 / 5 on the Fed event page.

   Counted FIRST, before writing any of this, as §15 says to: there ARE
   `application/ld+json` blocks here (4 to 6 per page), which on most sites
   would make them the primary path. They are not, and the reason is in
   the next paragraph.

2. **JSON-LD (secondary, and a confirmation rather than a source).** A
   listing publishes `CollectionPage → ItemList` with twenty `Event` nodes,
   each carrying ONE price — the event's first market — plus the currency.
   So it names 20 of the page's 94 markets. It is read for two things: the
   currency, which is a fact and is not otherwise published, and a check on
   the price the payload gave, recorded as `data_source="flight+jsonld"`.

   An EVENT page publishes a different `@type` — a single `Event`, no
   `ItemList` — and its `offers.price` is **"0"** on every capture taken,
   for markets trading at 0.07 and 0.88. That is a placeholder, not a price,
   and a parser that ported the listing's JSON-LD read to detail pages would
   write zero into every row while every other column looked right (§20).
   `_jsonld_prices` therefore only reads ItemList nodes.

3. **The rendered DOM (fallback only).** Anchored on the URL pattern
   `/event/{slug}`, never on a class: the class names here are Tailwind
   soup. A tile shows a rounded volume ("$199M Vol.") and one percentage,
   and it does not publish a market id at all — so a DOM-only row is an
   EVENT's headline market keyed by the event slug, and says so in
   `data_source`. It exists for the one failure a payload-only parser cannot
   report honestly: a page that rendered its tiles and shipped a payload
   shape this file cannot read.

Pagination: there is none, and that is measured
-----------------------------------------------
A listing URL renders exactly twenty events. The page's own dehydrated query
state says `"totalCount":21511,"hasNextPage":true` and carries an opaque
cursor — and nothing in the UI ever asks for the next page. Measured
2026-09-16, all three ways:

    ?page=2 / ?_p=2 / ?offset=20   each answered with the same twenty events
    12 scroll rounds to the bottom  0 new events, document height unchanged
    "Show more markets" clicked     +118 characters of text, 0 new events
                                    (it expands one card's sub-markets)
    headless vs headful             20 events both ways

So `page_url()` returns None and `--concurrency` above 1 is refused in the
listing modes, with the reason. `--mode events` is how this repo gets depth:
page 1 is the listing, and pages 2..N are the event pages page 1 named — real
independent addresses, which is exactly the shape §7 describes when it says
page 1 is fetched alone because its content decides whether the rest can be
addressed.

Eighteen locales, one host
--------------------------
`/es/predictions`, `/zh/event/{slug}` and sixteen more, from the site's own
hreflang set. The payload is TRANSLATED under them: same `sku`, same id, same
price, a Spanish question. One trap came out of running the second locale, and
it is the reason `price` is read by index and never by label:

    listing page, /es/    "outcomes":["Yes","No"]
    event   page, /es/    "outcomes":["Sí","No"]

Two page kinds of one site localising differently. A parser keyed on the word
"Yes" is correct on the listing and silently empty on the event page.
"""
import html as html_module
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from output_writer import Market

logger = logging.getLogger("product_parser")


# ===========================================================================
# Hosts, locales and URLs
# ===========================================================================
# One host. Taken from the site's own `<link rel="alternate" hreflang=…>`
# set rather than guessed (§5) — which is what showed that the eighteen
# locales are PATHS (`https://polymarket.com/es/predictions`) and not hosts,
# so there is no per-country host table here to get wrong.
HOSTS = ("polymarket.com", "www.polymarket.com")

# The locale prefixes the site's own hreflang set names, 2026-09-16. `en` is
# not among them: English is the unprefixed path, and `x-default` points at
# it too.
LOCALES = ("bn", "de", "es", "fr", "hi", "id", "it", "ja", "pl", "pt", "ru",
           "th", "tl", "uk", "vi", "zh", "zh-hant")

# polymarket.us is a DIFFERENT venue — QCX LLC, a CFTC-regulated contract
# market — with its own markets, its own prices and its own site. It is not
# a locale of this one and this parser does not read it. Named here so that
# `unsupported_reason` can say WHY rather than "not a Polymarket site",
# which is false and sends the reader looking for a typo (§5).
SIBLING_VENUE_HOSTS = ("polymarket.us", "www.polymarket.us")


def site_host(url: str) -> Optional[str]:
    host = (urlsplit(url or "").netloc or "").lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    return host.split(":")[0] or None


def is_supported_host(url: str) -> bool:
    return site_host(url) in HOSTS


def source_of(url: str) -> str:
    """The `source` column: the bare host, without `www.`."""
    host = site_host(url) or ""
    return host[4:] if host.startswith("www.") else (host or "polymarket.com")


# Query parameters that say something about the CLICK and nothing about the
# page. `tid` is Polymarket's own: a sub-market link inside a tile carries
# `?tid=1758…`, a millisecond timestamp, so two runs an hour apart would
# otherwise disagree about the URL of a market that never moved.
TRACKING_PARAMS = frozenset("""
tid utm_source utm_medium utm_campaign utm_term utm_content gclid fbclid
_branch_match_id _branch_referrer ref referrer via
""".split())


def strip_tracking(url: str) -> str:
    parts = urlsplit(url or "")
    keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(keep), ""))


def normalize_url(url: str) -> str:
    """An absolute, fragment-free, tracking-free address for comparison.

    A relative href is resolved against `polymarket.com` and a bare host gets
    `https://`. Trailing slashes are dropped from the path because the site
    serves `/predictions` and `/predictions/` as the same page and a run
    should not report them as two.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://polymarket.com" + url
    elif not re.match(r"^https?://", url):
        url = "https://" + url
    parts = urlsplit(strip_tracking(url))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme or "https", parts.netloc, path,
                       parts.query, ""))


def locale_of(url: str) -> Optional[str]:
    """The locale prefix this URL carries, or None for the English default."""
    segments = [s for s in urlsplit(url or "").path.split("/") if s]
    if segments and segments[0].lower() in LOCALES:
        return segments[0].lower()
    return None


def strip_locale(path: str) -> str:
    """`/es/event/fed-decision` -> `/event/fed-decision`."""
    segments = [s for s in (path or "").split("/") if s]
    if segments and segments[0].lower() in LOCALES:
        segments = segments[1:]
    return "/" + "/".join(segments)


# A market's own address, where the site gives it one:
#     /event/{event-slug}/{market-slug}
# An event holding exactly one market has no second address for it, and the
# event URL is the market's URL. Both spellings appear in listing markup —
# 34 sub-market links on one `/politics` capture, none on `/predictions`.
_EVENT_PATH_RE = re.compile(r"^/event/(?P<event>[^/?#]+)"
                            r"(?:/(?P<market>[^/?#]+))?/?$")

# Sports events live under their league as well as under `/event/`:
# `/sports/laliga/lal-bar-rrc-2026-09-16` and
# `/event/lal-bar-rrc-2026-09-16` are the same page. The second segment is
# the league and the third is the event slug.
_SPORTS_EVENT_PATH_RE = re.compile(r"^/sports/(?P<league>[^/?#]+)/"
                                   r"(?P<event>[^/?#]+)/?$")

# Id recovery from a URL, which is what `_SKU_IN_URL_RE` is for elsewhere in
# this family. Here the id IS the last slug: Polymarket puts no numeric id in
# any URL it publishes, so a row built from the DOM alone can name a market
# but cannot name `market_id` or `condition_id`. That is recorded in
# `data_source` rather than papered over.
_SKU_IN_URL_RE = re.compile(r"^/event/(?:[^/?#]+)/(?P<sku>[^/?#]+)/?$")


def market_slug_from_url(url: str) -> Optional[str]:
    path = strip_locale(urlsplit(url or "").path)
    match = _SKU_IN_URL_RE.match(path)
    return match.group("sku") if match else None


def event_slug_from_url(url: str) -> Optional[str]:
    path = strip_locale(urlsplit(url or "").path)
    match = _EVENT_PATH_RE.match(path) or _SPORTS_EVENT_PATH_RE.match(path)
    return match.group("event") if match else None


def event_url_for(slug: str, locale: Optional[str] = None) -> str:
    prefix = f"/{locale}" if locale else ""
    return f"https://polymarket.com{prefix}/event/{slug}"


def market_url_for(event_slug: str, market_slug: Optional[str] = None,
                   locale: Optional[str] = None) -> str:
    if market_slug and market_slug != event_slug:
        prefix = f"/{locale}" if locale else ""
        return f"https://polymarket.com{prefix}/event/{event_slug}/{market_slug}"
    return event_url_for(event_slug, locale)


# Top-level routes that are the site's own furniture rather than a listing of
# markets. `/predictions/{tag}` is a listing; `/leaderboard` is not, and a run
# pointed at one should be told which rather than reporting zero markets.
_NOT_A_LISTING = frozenset("""
about accuracy activity api brand careers combos contact docs help
institutional leaderboard learn legal login markets-api mentions news
notifications perps portfolio press privacy profile rewards settings signup
sitemaps support terms transparency wallet watchlist
""".split())

# Routes that ARE listings without a `/predictions/` prefix. Taken from the
# site's own primary navigation on 2026-09-16, which is the only place they
# are enumerated.
_TOP_LEVEL_LISTINGS = frozenset("""
breaking crypto earnings economy elections esports finance geopolitics iran
new politics pop-culture sports tech weather world
""".split())


def page_kind(url: str) -> str:
    """What kind of Polymarket page this URL addresses.

        listing   a page of event tiles: `/predictions`, `/predictions/{tag}`,
                  `/{category}`, or any of those with `?q={query}`
        event     one event's own page, under `/event/` or `/sports/{league}/`
        other     the site's furniture, or a path this parser does not read

    Used to choose the parse, to decide whether `--concurrency` means
    anything, and to refuse a URL with a reason rather than with zero rows.
    """
    if not is_supported_host(url):
        return "other"
    path = strip_locale(urlsplit(url or "").path)
    if _EVENT_PATH_RE.match(path) or _SPORTS_EVENT_PATH_RE.match(path):
        return "event"
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "listing"                     # the homepage lists events too
    head = segments[0].lower()
    if head == "predictions":
        return "listing"
    if head == "sports":
        # `/sports`, `/sports/live`, `/sports/nfl` — a listing. A third
        # segment is an event and was matched above.
        return "listing"
    if head in _TOP_LEVEL_LISTINGS:
        return "listing"
    return "other"


def category_from_url(url: str) -> Optional[str]:
    """The listing's own name, for the `--category` flag and the sidecar.

    `/predictions/crypto` -> "crypto", `/politics` -> "politics",
    `/predictions?q=bitcoin` -> "q:bitcoin", `/predictions` -> None. The
    query form is prefixed because "bitcoin" as a search term and "bitcoin"
    as a tag are different listings — `/predictions?q=bitcoin` returned 554
    events' worth of `totalCount` and `/predictions/bitcoin` a different set.
    """
    if page_kind(url) != "listing":
        return None
    parts = urlsplit(url or "")
    query = dict(parse_qsl(parts.query))
    if query.get("q"):
        return "q:" + query["q"]
    segments = [s for s in strip_locale(parts.path).split("/") if s]
    if not segments:
        return None
    if segments[0].lower() == "predictions":
        return segments[1].lower() if len(segments) > 1 else None
    return "/".join(s.lower() for s in segments)


def listing_url_for(category: Optional[str],
                    locale: Optional[str] = None) -> str:
    """The URL `--category` names. `None` is the site's popular listing."""
    prefix = f"/{locale}" if locale else ""
    if not category:
        return f"https://polymarket.com{prefix}/predictions"
    if category.startswith("q:"):
        return (f"https://polymarket.com{prefix}/predictions?"
                + urlencode({"q": category[2:]}))
    return f"https://polymarket.com{prefix}/predictions/{category.strip('/')}"


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be scraped, or None if it can.

    Says WHICH thing is wrong. "polymarket.us is a different venue" sends a
    reader to the right place; "not a Polymarket site" sends them looking for
    a typo in a URL that has none (§5).
    """
    if not url:
        return "no URL given"
    host = site_host(url)
    if host in SIBLING_VENUE_HOSTS:
        return ("polymarket.us is a SEPARATE venue (QCX LLC, a CFTC-regulated "
                "designated contract market) with its own markets and its own "
                "prices, not a locale of polymarket.com. This scraper reads "
                "polymarket.com only")
    if host not in HOSTS:
        return f"{host or url!r} is not a polymarket.com address"
    kind = page_kind(url)
    if kind == "other":
        path = strip_locale(urlsplit(url).path)
        head = ([s for s in path.split("/") if s] or [""])[0].lower()
        if head in _NOT_A_LISTING:
            return (f"/{head} is one of the site's own pages rather than a "
                    "listing of markets — try /predictions, /predictions/"
                    "{tag} or an /event/{slug} URL")
        return (f"{path!r} is neither a listing nor an event page — try "
                "/predictions, /predictions/{tag} or /event/{slug}")
    return None


def redirected_away(requested: str, final: str) -> Optional[str]:
    """A sentence naming a redirect that changed what was asked for.

    Polymarket has three that matter, and only the third is a problem:

      `/markets`            -> `/predictions`   a rename, same page
      `/search?q=x`         -> `/predictions?q=x`  the same search
      `/predictions?_q=x`   -> `/predictions/x` a SEARCH turned into a TAG
                              listing, which is a different set of events

    The third is the one this reports. It is the shape §7 warns about: page 1
    answered a different address than the one asked for, so any conclusion
    drawn about the requested listing is about some other listing.
    """
    if not requested or not final:
        return None
    a, b = normalize_url(requested), normalize_url(final)
    if a == b:
        return None
    a_parts, b_parts = urlsplit(a), urlsplit(b)
    if dict(parse_qsl(a_parts.query)).get("q") and \
            not dict(parse_qsl(b_parts.query)).get("q"):
        return (f"a search for {dict(parse_qsl(a_parts.query))['q']!r} was "
                f"redirected to the listing {b_parts.path!r}, which is a "
                "different set of events")
    if page_kind(a) != page_kind(b):
        return (f"{a_parts.path!r} was redirected to {b_parts.path!r}, which "
                f"is a {page_kind(b)} page and not a {page_kind(a)} one")
    return None


# ===========================================================================
# Pagination and concurrency — the policy, as data
# ===========================================================================
# See the module docstring for the three measurements behind this. A listing
# URL is one page; `--mode events` turns the event URLs page 1 names into
# pages 2..N, and those are real addresses.
PAGINATES_BY_URL = False

PAGE_URL_REASON = (
    "a Polymarket listing has no address for its second page. ?page=2, "
    "?_p=2 and ?offset=20 each answer with the same twenty events, scrolling "
    "adds none, and the site's own next-page cursor is opaque. --mode events "
    "walks the EVENT pages that listing names instead, and those are real "
    "addresses"
)

# One listing plus its twenty event pages is 21 fetches; forty leaves room
# for a listing that grows without letting a typo ask for a thousand.
PAGE_CAP = 40


def paginates_by_url(url: str = "") -> bool:
    """False for every URL on this site. Kept as a function because the rest
    of the family calls it as one and a constant would drift out of sight."""
    return PAGINATES_BY_URL


def page_url(url: str, page: int) -> Optional[str]:
    """None for page 2 and beyond, always.

    Returning a constructed `?page=2` would be worse than returning nothing:
    the site answers it with HTTP 200 and page 1, so a run that trusted it
    would find no new sku, conclude the listing was exhausted, and report a
    COMPLETE run holding one page (§18). `--mode events` plans its pages from
    page 1's own data instead, in `event_urls_from`.
    """
    return url if page <= 1 else None


def event_urls_from(rows: Iterable[Market], limit: Optional[int] = None) -> List[str]:
    """The event pages this listing named, in listing order, deduplicated.

    This is `--mode events`' page plan, and it is the reason §7's "page 1 is
    always fetched alone" is literally true here rather than a convention:
    page 5's address is not merely unknown until page 4 loads, it does not
    exist until page 1 is parsed.
    """
    seen, out = set(), []
    for row in rows:
        url = row.event_url or (event_url_for(row.event_slug)
                                if row.event_slug else None)
        if not url:
            continue
        url = normalize_url(url)
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if limit is not None and len(out) >= limit:
            break
    return out


CONCURRENCY_REASON = (
    "a listing page and an event page are each ONE fetch with no second "
    "address to hand a worker. --mode events fetches the event pages a "
    "listing named and does support --concurrency"
)


def concurrency_limit(url: str = "", mode: str = "") -> Optional[int]:
    """The highest `--concurrency` this run can use, or None for no limit."""
    return None if mode == "events" else 1


def concurrency_refusal(url: str = "", mode: str = "") -> Optional[str]:
    return None if mode == "events" else CONCURRENCY_REASON


# ===========================================================================
# Selectors — the DOM fallback only
# ===========================================================================
# The payload is read before any of this. These exist for the case where a
# page rendered its tiles and shipped a payload shape this parser could not
# read, which is the one failure mode a payload-only parser cannot report
# honestly (§20: a page that was SERVED, links to N markets and parses to
# zero rows is our bug, not an empty category).
#
# Anchored on the URL PATTERN and not on a class. Polymarket's classes are
# Tailwind utilities plus build-time tokens (`bg-pk-hover-overlay-darker`,
# `text-body-base`); the `/event/{slug}` href is a contract with search
# engines and with its own sitemap.
SELECTORS: Dict[str, str] = {
    # Any link to an event or to a market under one.
    "item_link": 'a[href*="/event/"]',
    # What a readiness wait watches. Deliberately the same thing: there is no
    # stable tile class to wait for, and the links are what the grid is made
    # of.
    "item_card": 'a[href*="/event/"]',
    # A tile's own accessible name carries the event title — the tile's
    # visible heading is split across nodes, and this one is a single string.
    "tile_title": "a[aria-label]",
    # The category / subcategory crumbs inside a tile.
    "tile_category": 'a[href*="/predictions/"]',
}

# How many event links mean "this page has rendered". Must be > 1: waiting
# for one match resolves on the site's own navigation long before the grid
# paints, and this site's navigation links five listings above the fold (§5).
MIN_CARD_MATCHES = 2

# Deliberately empty, and this is the measured version of §7's warning rather
# than an omission: Polymarket publishes no `link[rel=next]`, no numbered
# anchor and no next control anywhere. A selector here would be a third dead
# `NEXT_PAGE_SELECTOR` of the kind that let a sibling repo fetch one page of
# three and exit 0.
NEXT_PAGE_SELECTOR = ""

# How far the DOM fallback may widen from a link to its tile. A malformed
# document must not let the walk reach <body> and read the whole page as one
# tile (§4).
_WIDEN_CAP = 8


def _event_links(soup: BeautifulSoup) -> List[Any]:
    out = []
    for a in soup.select(SELECTORS["item_link"]):
        href = a.get("href") or ""
        if event_slug_from_url(href if href.startswith("http")
                               else "https://polymarket.com" + href):
            out.append(a)
    return out


def count_cards(html: Optional[str]) -> int:
    """Distinct EVENTS linked in the rendered document.

    Distinct, not links: a tile links its event two or three times (an
    invisible overlay anchor, the title, the comment count), so counting
    links reports a twenty-event page as thirty-two and a readiness wait for
    thirty-two never resolves (§4).
    """
    if not html:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    slugs = set()
    for a in _event_links(soup):
        href = a.get("href") or ""
        slug = event_slug_from_url(href if href.startswith("http")
                                   else "https://polymarket.com" + href)
        if slug:
            slugs.add(slug)
    return len(slugs)


# ===========================================================================
# The inlined payload
# ===========================================================================
_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[\s*\d+\s*,\s*')
_DECODER = json.JSONDecoder()


def flight_payload(html: Optional[str]) -> str:
    """The React Server Components stream, decoded and concatenated.

    Each chunk is a JSON string literal inside
    `self.__next_f.push([1, "…"])`, and the interesting objects straddle
    chunk boundaries — one capture split its event list across three pushes
    of 292 KB, 68 KB and 7 KB — so they are joined before anything is read
    out of them.

    Decoded with `json.JSONDecoder().raw_decode` at the opening quote rather
    than with a regex for the closing one: the chunks contain escaped quotes
    by the thousand, and a non-greedy `"(.*?)"` stops at the first of them.
    That cost a debugging session in the recon for this repo and would have
    silently truncated the payload rather than failing.

    Returns "" when there is no payload, which is what a Chromium error page,
    a challenge interstitial and a 404 all look like here.
    """
    if not html:
        return ""
    parts = []
    for match in _FLIGHT_PUSH_RE.finditer(html):
        index = match.end()
        if index >= len(html) or html[index] != '"':
            continue
        try:
            chunk, _ = _DECODER.raw_decode(html, index)
        except ValueError:
            continue
        if isinstance(chunk, str):
            parts.append(chunk)
    return "".join(parts)


# The keys that mark an object worth decoding. `outcomePrices` is on every
# market and on nothing else; `markets":[` is on every event.
_OBJECT_MARKERS = ('"outcomePrices"', '"markets":[')


# How far back from a key the object holding it may begin, and how many
# candidate `{` to try. A listing's largest event object measured 21 KB; the
# cap is an order of magnitude above that so a legitimate object is never
# missed, and it exists so a malformed payload cannot turn one lookup into a
# scan of the whole stream.
_MAX_OBJECT_BYTES = 400_000
_MAX_CANDIDATES = 4_000


def _enclosing_object(payload: str, key_pos: int) -> Optional[Tuple[int, int, dict]]:
    """The innermost JSON object that contains the key at `key_pos`.

    Found by walking BACKWARD over candidate `{` positions, nearest first,
    and DECODING each one until a decode succeeds and its span covers the
    key. The decode is what makes the answer right rather than likely: a
    candidate that is not the enclosing object either fails to parse or ends
    before the key, and both are detected rather than assumed.

    This replaces a single forward pass that kept a stack of open braces,
    which was faster, worked on twelve captures, and was WRONG on the
    thirteenth. The RSC stream is not JSON: Next.js inlines the site's own
    bootstrap JavaScript into it, and that script contains braces inside
    single-quoted strings. A JSON scanner tracks double-quoted strings only,
    so one `'}'` in that script popped the stack to empty — and from there
    every object start was lost. The symptom was an event page parsing to
    zero rows while the market it wanted sat in plain text in the payload,
    and the run reported it as a served page with no markets on it.

    Nothing about that is visible to a smaller test: the desync happens in a
    script tag hundreds of kilobytes before the data. What catches it is
    running the thing (§15) and keeping a fixture of the page that broke it.
    """
    lowest = max(0, key_pos - _MAX_OBJECT_BYTES)
    cursor = key_pos
    for _ in range(_MAX_CANDIDATES):
        cursor = payload.rfind("{", lowest, cursor)
        if cursor < 0:
            return None
        try:
            obj, end = _DECODER.raw_decode(payload, cursor)
        except ValueError:
            continue
        if end > key_pos and isinstance(obj, dict):
            return cursor, end, obj
    return None


def payload_objects(payload: str,
                    markers: Tuple[str, ...] = _OBJECT_MARKERS) -> List[dict]:
    """Every JSON object in the payload that carries one of these keys.

    Deliberately SHAPE-AGNOSTIC. The listing routes measured here ship their
    markets three different ways: `/predictions` and `/predictions/{tag}`
    inside a `results` array, `/politics` as free-standing market objects
    with no such array, and an event page as the markets of a single event —
    and one event page shipped its event object with no `results`, no
    `events` back-reference and nothing else to key on. Keying on a container
    path would have to be rewritten for each of those, and again the next
    time Next.js restructures the stream; keying on the fields the site's own
    data model has been publishing for years does not.
    """
    found: Dict[int, dict] = {}
    for marker in markers:
        position = payload.find(marker)
        while position != -1:
            if position not in found:
                hit = _enclosing_object(payload, position)
                if hit:
                    start, _end, obj = hit
                    # Keep the FIRST object found for a start offset: two
                    # markers inside one object (an event with `markets` and
                    # a market price in the same node) describe one thing.
                    found.setdefault(start, obj)
            position = payload.find(marker, position + 1)
    return [found[key] for key in sorted(found)]


def _results_arrays(payload: str) -> List[List[dict]]:
    """The listing's own grid, where the page ships one.

    `"results":[…]` is the array behind the tiles — the page's dehydrated
    infinite query. Preferred over every event object found loose in the
    payload because a listing page also ships the trending rail and the
    related-events strip, and a run for `/politics` that emitted those would
    be reporting a sidebar as part of the listing. That is §4's junk-link
    data theft, seen from the payload side: 41 event objects on that capture
    against the 20 the grid actually holds.
    """
    out = []
    for match in re.finditer(r'"results"\s*:\s*\[', payload):
        start = payload.index("[", match.end() - 1)
        try:
            arr, _ = _DECODER.raw_decode(payload, start)
        except ValueError:
            continue
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) \
                and "markets" in arr[0]:
            out.append([x for x in arr if isinstance(x, dict)])
    return out


def total_events(html: Optional[str]) -> Optional[int]:
    """The site's own count of everything the listing could show.

    21,511 for `/predictions` against the 20 it ships. NOT a per-page gap and
    never used as one (§8's rank arithmetic does not apply here — this is the
    whole catalogue, not this page's share of it). It goes in the sidecar
    beside what the run actually read, which is what makes the difference
    visible rather than assumed.
    """
    payload = flight_payload(html)
    if not _results_arrays(payload):
        # An event page ships a `totalCount` too — 372 on one capture — and
        # it belongs to the rail of related events beside the market, not to
        # anything this run asked for. A number read off the wrong container
        # is worse than no number (§13).
        return None
    match = re.search(r'"totalCount"\s*:\s*(\d+)', payload)
    return int(match.group(1)) if match else None


def has_next_page_flag(html: Optional[str]) -> Optional[bool]:
    """The listing's own `hasNextPage`, purely as evidence for the README.

    True on every listing captured, and acted on by nothing: the cursor
    beside it is opaque and the UI never spends it. Read only so the smoke
    suite can pin the claim that this repo does not paginate a listing
    BECAUSE it cannot, rather than because nobody looked.
    """
    payload = flight_payload(html)
    match = re.search(r'"hasNextPage"\s*:\s*(true|false)', payload)
    return match.group(1) == "true" if match else None


# ===========================================================================
# Small conversions
# ===========================================================================
def _as_float(value: Any) -> Optional[float]:
    """A number from a payload that types them inconsistently.

    `outcomePrices` is a list of STRINGS ("0.12"), `bestAsk` a float,
    `liquidity` a string on a market and a float on an event. All three are
    the same kind of number and all three end up in the same column.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _as_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _as_list(value: Any) -> Optional[List[Any]]:
    """`outcomes` arrives as a list on some pages and as a JSON STRING on
    others — `'["Yes","No"]'`. Both are the site's, and both must read the
    same or `outcomes[0]` would be the character `[` on half the rows."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return None
            if isinstance(parsed, list):
                return parsed
    return None


def _iso(value: Any) -> Optional[str]:
    """The site's own timestamps, normalised to a trailing `Z`.

    It ships three spellings of the same instant — `2026-09-16T00:00:00Z`,
    `2026-09-16T00:00:00.000Z` and `2026-05-13T21:23:09.737806Z` — and a
    consumer sorting them as strings needs them to at least agree on the
    suffix.
    """
    text = _as_str(value)
    if not text:
        return None
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


# Prices here are probabilities, and the site renders them as integer
# percentages: "88%". Not a currency amount, so none of the family's
# thousands-separator machinery applies — but the percent sign does have to
# come off before the number is divided, and a bare `\d+` would read the "24"
# in "$24M Vol." as a price.
_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*%")

# "$199M Vol.", "$26M today", "$13M Liq." — the tile's rounded numbers.
_MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([KMB])?", re.I)
_MONEY_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}


def percent_to_price(text: Optional[str]) -> Optional[float]:
    """"88%" -> 0.88. None where the tile shows no percentage at all, which
    is 6 of 20 tiles on one capture — a multi-outcome event renders a list of
    labels instead, and an absent number must not read as 0."""
    if not text:
        return None
    match = _PERCENT_RE.search(text)
    if not match:
        return None
    value = int(match.group(1))
    return value / 100.0 if 0 <= value <= 100 else None


def money_to_float(text: Optional[str]) -> Optional[float]:
    """"$199M" -> 199000000.0.

    ROUNDED, and the column says so: the payload's own `volumeNum` for that
    same event was 199,171,278.99. A row that carries this number carries
    `data_source="dom"` beside it, so a consumer can tell a rounded volume
    from an exact one rather than seeing a $171k move that never happened.
    """
    if not text:
        return None
    match = _MONEY_RE.search(text)
    if not match:
        return None
    number = _as_float(match.group(1))
    if number is None:
        return None
    scale = _MONEY_SCALE.get((match.group(2) or "").lower(), 1.0)
    return number * scale


# ===========================================================================
# Rows from the payload — the primary path
# ===========================================================================
# Which columns a market object can fill depends on which page shipped it,
# and the difference is large enough to be worth stating as a number: a
# listing's market objects carry 11 to 12 keys, an event page's carry 111 to
# 115. `--mode markets` is therefore genuinely shallower than `--mode event`,
# and `data_source` plus `mode` in the sidecar are how a consumer knows which
# they are holding.
def _event_context(event: Optional[dict]) -> dict:
    """The `event_*`, `category` and `tags` columns, from an event object."""
    if not isinstance(event, dict):
        return {}
    crumbs = event.get("breadcrumbs") or {}
    category = (crumbs.get("category") or {}) if isinstance(crumbs, dict) else {}
    subcategory = (crumbs.get("subcategory") or {}) if isinstance(crumbs, dict) else {}
    tags = [t.get("label") for t in (event.get("tags") or [])
            if isinstance(t, dict) and t.get("label")]
    slug = _as_str(event.get("slug"))
    return {
        "event_id": _as_str(event.get("id")),
        "event_slug": slug,
        "event_title": _as_str(event.get("title")),
        "event_url": event_url_for(slug) if slug else None,
        "category": _as_str(category.get("label")) if isinstance(category, dict) else None,
        "subcategory": _as_str(subcategory.get("label")) if isinstance(subcategory, dict) else None,
        "tags": tags or None,
        # The EVENT's volume, which is the only one a listing ships. An event
        # page ships the market's own and overwrites these three.
        "volume": _as_float(event.get("volume")),
        "volume_24h": _as_float(event.get("volume24hr")),
        "liquidity": _as_float(event.get("liquidity")),
        "volume_scope": "event",
        "end_date": _iso(event.get("endDate")),
    }


def _market_row(market: dict, event: Optional[dict], base_url: str) -> Optional[Market]:
    """One market object -> one row, with whatever the page could fill.

    Returns None for an object that named no market: the scanner keys on
    `outcomePrices`, and a malformed or partial node with that key and no
    slug is not a row, it is a read that failed.
    """
    slug = _as_str(market.get("slug"))
    if not slug:
        return None

    context = _event_context(event)
    if not context and isinstance(market.get("events"), list) and market["events"]:
        # An event page's market carries its own event back-reference, with
        # fewer fields than a listing's event object (id, slug, title,
        # series). Used only when there is no enclosing event to read.
        context = _event_context(market["events"][0])

    outcomes = _as_list(market.get("outcomes")) or []
    raw_prices = _as_list(market.get("outcomePrices")) or []
    prices = [p for p in (_as_float(x) for x in raw_prices) if p is not None]

    # By INDEX. See the module docstring: an event page under a locale path
    # ships `["Sí","No"]` where the listing ships `["Yes","No"]`, so matching
    # the word is correct on one page kind and empty on the other.
    price = prices[0] if prices else None

    event_slug = context.get("event_slug") or event_slug_from_url(base_url)
    locale = locale_of(base_url)
    url = (market_url_for(event_slug, slug, locale) if event_slug
           else normalize_url(base_url))

    row = Market(
        source=source_of(base_url) or "polymarket.com",
        url=url,
        sku=slug,
        title=_as_str(market.get("question")),
        price=price,
        outcomes=[str(o) for o in outcomes] or None,
        outcome_prices=prices or None,
        group_item_title=_as_str(market.get("groupItemTitle")),
        best_bid=_as_float(market.get("bestBid")),
        best_ask=_as_float(market.get("bestAsk")),
        spread=_as_float(market.get("spread")),
        last_trade_price=_as_float(market.get("lastTradePrice")),
        price_change_24h=_as_float(market.get("oneDayPriceChange")),
        price_change_1w=_as_float(market.get("oneWeekPriceChange")),
        active=_as_bool(market.get("active")),
        closed=_as_bool(market.get("closed")),
        accepting_orders=_as_bool(market.get("acceptingOrders")),
        market_id=_as_str(market.get("id")),
        condition_id=_as_str(market.get("conditionId")),
        clob_token_ids=[str(t) for t in (_as_list(market.get("clobTokenIds")) or [])] or None,
        neg_risk=_as_bool(market.get("negRisk")),
        start_date=_iso(market.get("startDate")),
        data_source="flight",
    )
    for key, value in context.items():
        if getattr(row, key, None) in (None, [],) and value is not None:
            setattr(row, key, value)

    # A market object that carries its OWN volume — an event page's does,
    # a listing's does not — describes this market rather than its event, and
    # that is the more precise answer. `volume_scope` records which one won,
    # because 199,171,278 for an event and 19,154,686 for one of its five
    # markets are both correct and are not comparable.
    own_volume = _as_float(market.get("volumeNum"))
    if own_volume is None:
        own_volume = _as_float(market.get("volume"))
    if own_volume is not None:
        row.volume = own_volume
        row.volume_24h = _as_float(market.get("volume24hr"))
        row.volume_1w = _as_float(market.get("volume1wk"))
        liquidity = _as_float(market.get("liquidityNum"))
        if liquidity is None:
            liquidity = _as_float(market.get("liquidity"))
        row.liquidity = liquidity
        row.volume_scope = "market"
    if row.end_date is None:
        row.end_date = _iso(market.get("endDate"))
    return row


def markets_from_flight(html: Optional[str], base_url: str = "") -> List[Market]:
    """Every market the page's own payload names, in the page's own order.

    On an EVENT page the rows are scoped to the event the URL asked for. That
    is not a detail: an event page ships the markets of its own event AND of
    everything in its rails — the Clarity Act page, a single-market event,
    carries twenty-one market objects, and the Fed page twenty-five for its
    five markets. Emitting those would be §4's junk-link data theft with no
    junk link involved: a run for one event would report four other events'
    prices as if they were its own.

    Order matters and is not incidental: it is the listing's ranking, which
    becomes `position`, and a set would lose it. Deduplicated on the market
    slug, first occurrence winning — a listing ships each market once inside
    its event, but `/politics` ships some of them twice (once in the grid and
    once in a rail) and the first is the one the grid showed.
    """
    payload = flight_payload(html)
    if not payload:
        return []

    rows: List[Market] = []
    seen = set()

    def add(market: dict, event: Optional[dict]) -> None:
        row = _market_row(market, event, base_url)
        if row and row.sku not in seen:
            seen.add(row.sku)
            rows.append(row)

    # The grid first, where the page ships one. Everything else in the
    # payload is the page's furniture (see `_results_arrays`).
    wanted = event_slug_from_url(base_url) if page_kind(base_url) == "event" else None

    grids = _results_arrays(payload)
    for grid in grids:
        for event in grid:
            if wanted and _as_str(event.get("slug")) != wanted:
                continue
            for market in (event.get("markets") or []):
                if isinstance(market, dict):
                    add(market, event)

    if rows:
        return rows

    # No grid: an event page, or a dashboard route like `/politics` that
    # ships its events loose. Scan for objects instead.
    objects = payload_objects(payload)
    events = [o for o in objects
              if isinstance(o.get("markets"), list) and o.get("slug")]
    claimed = set()
    for event in events:
        if wanted and _as_str(event.get("slug")) != wanted:
            continue
        for market in event["markets"]:
            if isinstance(market, dict) and market.get("slug"):
                claimed.add(market["slug"])
                add(market, event)
    for obj in objects:
        if obj.get("outcomePrices") is None or not obj.get("slug"):
            continue
        if obj["slug"] in claimed:
            continue
        if wanted and _market_event_slug(obj) != wanted:
            continue
        # A market with no event object anywhere in the payload. Its own
        # `events` back-reference is read inside `_market_row`.
        add(obj, None)
    return rows


def _market_event_slug(market: dict) -> Optional[str]:
    """The slug of the event a loose market object belongs to, where its own
    back-reference names one. An event page's market objects carry `events`;
    a listing's do not, and there the enclosing event is what names it."""
    events = market.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and _as_str(event.get("slug")):
                return _as_str(event.get("slug"))
    return None


# ===========================================================================
# JSON-LD — the confirmation
# ===========================================================================
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)


def jsonld_blocks(html: Optional[str]) -> List[Any]:
    """Every parseable JSON-LD block. 4 to 6 per page on every capture."""
    if not html:
        return []
    out = []
    for raw in _JSONLD_RE.findall(html):
        text = raw.strip()
        if not text:
            continue
        try:
            out.append(json.loads(text))
        except ValueError:
            # A block this parser cannot read is not an error worth raising:
            # the payload is the source and this is the confirmation.
            logger.debug("skipping an unparseable ld+json block (%d bytes)",
                         len(text))
    return out


def _jsonld_nodes(blocks: Iterable[Any]) -> List[dict]:
    """Flatten `@graph`, top-level lists and single objects into one list.

    `@graph` is here because §4 names it as the shape that returns zero
    products in silence when it is not handled. Polymarket does not use it
    today — five of five blocks on every capture are plain objects — and the
    four lines that would find it if it appeared cost nothing.
    """
    out = []
    stack = list(blocks)
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            stack = list(node) + stack
        elif isinstance(node, dict):
            out.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                stack = list(graph) + stack
    return out


def jsonld_prices(html: Optional[str]) -> Dict[str, Tuple[Optional[float], Optional[str]]]:
    """`{event_slug: (price, currency)}` from a listing's ItemList.

    ITEMLIST ONLY. An event page's own `Event` node publishes
    `"offers": {"price": "0"}` — zero, on every capture taken, for markets
    trading at 0.07 and 0.88. Reading it would write a placeholder into the
    `price` of every row of every event run while the rest of the row looked
    correct, which is the exact shape of §20's detail-page finding.

    The price it does give is the event's FIRST market, which is what the
    caller matches it against.
    """
    out: Dict[str, Tuple[Optional[float], Optional[str]]] = {}
    for node in _jsonld_nodes(jsonld_blocks(html)):
        if node.get("@type") != "CollectionPage":
            continue
        main = node.get("mainEntity")
        if not isinstance(main, dict) or main.get("@type") != "ItemList":
            continue
        for item in (main.get("itemListElement") or []):
            if not isinstance(item, dict):
                continue
            entity = item.get("item")
            if not isinstance(entity, dict):
                continue
            slug = event_slug_from_url(entity.get("url") or "")
            if not slug:
                continue
            offers = entity.get("offers")
            # `offers` is legal as null, as a dict and as a list, and a
            # `.get()` default does not save you from the first of those
            # (§4). All three are handled rather than assumed away.
            if isinstance(offers, list):
                offers = next((o for o in offers if isinstance(o, dict)), None)
            if not isinstance(offers, dict):
                out.setdefault(slug, (None, None))
                continue
            out[slug] = (_as_float(offers.get("price")),
                         _as_str(offers.get("priceCurrency")))
    return out


def jsonld_currency(html: Optional[str]) -> Optional[str]:
    """The currency the page states, from whichever node states one.

    Read from the EVENT node too, whose `offers.price` is a useless "0" —
    the currency beside it is not useless and is not otherwise published
    anywhere on the page. Taking one field of a node and refusing another is
    deliberate: `price` there is a placeholder and `priceCurrency` is a fact,
    and the way to know which is which is to have looked at both (§19's rule
    about re-reading what a payload actually returns).

    Returns None rather than a defaulted "USD" when the page states nothing
    (§8: never present a guess as a fact).
    """
    found = set()
    for node in _jsonld_nodes(jsonld_blocks(html)):
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = next((o for o in offers if isinstance(o, dict)), None)
        if isinstance(offers, dict) and _as_str(offers.get("priceCurrency")):
            found.add(_as_str(offers.get("priceCurrency")))
        main = node.get("mainEntity")
        if isinstance(main, dict) and main.get("@type") == "ItemList":
            for item in (main.get("itemListElement") or []):
                entity = item.get("item") if isinstance(item, dict) else None
                if not isinstance(entity, dict):
                    continue
                sub_offers = entity.get("offers")
                if isinstance(sub_offers, list):
                    sub_offers = next((o for o in sub_offers
                                       if isinstance(o, dict)), None)
                if isinstance(sub_offers, dict) and _as_str(sub_offers.get("priceCurrency")):
                    found.add(_as_str(sub_offers.get("priceCurrency")))
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        logger.warning("This page states %d different currencies (%s); "
                       "leaving the column null rather than picking one.",
                       len(found), ", ".join(sorted(found)))
    return None


def jsonld_event_titles(html: Optional[str]) -> Dict[str, str]:
    """`{event_slug: name}` from whichever JSON-LD node names events.

    Used only by the DOM fallback, whose own title source is a tile's
    `aria-label`.
    """
    out = {}
    for node in _jsonld_nodes(jsonld_blocks(html)):
        if node.get("@type") == "Event":
            slug = event_slug_from_url(node.get("url") or "")
            if slug and _as_str(node.get("name")):
                out[slug] = _as_str(node.get("name"))
        main = node.get("mainEntity")
        if isinstance(main, dict) and main.get("@type") == "ItemList":
            for item in (main.get("itemListElement") or []):
                entity = item.get("item") if isinstance(item, dict) else None
                if isinstance(entity, dict):
                    slug = event_slug_from_url(entity.get("url") or "")
                    if slug and _as_str(entity.get("name")):
                        out[slug] = _as_str(entity.get("name"))
    return out


# ===========================================================================
# The DOM — the fallback
# ===========================================================================
def _tile_of(anchor: Any) -> Any:
    """The outermost ancestor still covering exactly ONE event.

    Counts DISTINCT EVENT SLUGS and not links, which is the whole point: a
    Polymarket tile links its event two or three times — an invisible overlay
    anchor with the title as its `aria-label`, the title itself, and the
    comment count with a `#commentsInner` fragment — so a walk that stopped
    at "more than one event link" would never leave the anchor and would read
    no price at all (§4). Stopping one level too LATE is the worse failure:
    the tile would then report its neighbour's volume.
    """
    node, tile = anchor, anchor
    for _ in range(_WIDEN_CAP):
        node = getattr(node, "parent", None)
        if node is None:
            break
        slugs = set()
        for a in node.find_all("a", href=True):
            href = a["href"]
            slug = event_slug_from_url(href if href.startswith("http")
                                       else "https://polymarket.com" + href)
            if slug:
                slugs.add(slug)
        if len(slugs) > 1:
            break
        tile = node
    return tile


def markets_from_dom(html: Optional[str], base_url: str = "") -> List[Market]:
    """One row per event tile, from the rendered page alone.

    A fallback and never an overlay. There is deliberately no DOM/payload
    reconciliation here, and the reason is a measurement rather than a
    preference: the tile's percentage and the payload's first price are two
    DIFFERENT MARKETS, not two views of one. The Fed tile renders "88% · 25
    bps increase" — the event's leading outcome — while the ItemList
    publishes 0.12, which is its "No change" market. Overlaying one onto the
    other would write a confident wrong price (§4).

    Every row from here carries `data_source="dom"` and is keyed by the EVENT
    slug, because the DOM publishes no market id anywhere.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    titles = jsonld_event_titles(html)
    locale = locale_of(base_url)
    rows: List[Market] = []
    seen = set()
    for anchor in _event_links(soup):
        href = anchor.get("href") or ""
        absolute = href if href.startswith("http") else "https://polymarket.com" + href
        slug = event_slug_from_url(absolute)
        if not slug or slug in seen:
            continue
        tile = _tile_of(anchor)
        text = tile.get_text(" ", strip=True) if tile is not None else ""
        # An anchor whose tile is the anchor itself carried no tile at all —
        # a navigation link, a footer link, a rail. Skipped rather than
        # emitted as a row with every column null but the URL, which is what
        # a sibling repo shipped for a week (§4).
        if tile is anchor or not text:
            continue
        seen.add(slug)

        label = None
        aria = anchor.get("aria-label")
        if aria and aria.strip():
            label = aria.strip()
        rows.append(Market(
            source=source_of(base_url) or "polymarket.com",
            url=event_url_for(slug, locale),
            sku=slug,
            title=titles.get(slug) or label,
            price=percent_to_price(text),
            volume=money_to_float(text),
            volume_scope="event",
            event_slug=slug,
            event_title=titles.get(slug) or label,
            event_url=event_url_for(slug, locale),
            category=_dom_category(tile),
            data_source="dom",
        ))
    return rows


def _dom_category(tile: Any) -> Optional[str]:
    if tile is None:
        return None
    for a in tile.select(SELECTORS["tile_category"]):
        text = a.get_text(strip=True)
        if text:
            return text
    return None


# ===========================================================================
# Bot challenges
# ===========================================================================
# Polymarket sits behind Cloudflare (`server: cloudflare`, a `cf-ray` header
# on every response) in front of Vercel. Twelve captures taken on 2026-09-16
# from a datacentre exit contained **zero** markers of any vendor: no
# reCAPTCHA, hCaptcha, Turnstile, DataDome, PerimeterX, Incapsula, Kasada or
# AWS WAF, no challenge iframe, no `data-sitekey`, and no `*_SITE_KEY` in any
# page config. So no challenge was MET — which §18 is careful to say is not
# the same as none being configured. Cloudflare's managed challenge is one
# request away at any time, it renders a Turnstile, and this repo implements
# the interception and the solve for exactly that (`captcha_solver.py`,
# `TurnstileTaskProxyless`).
#
# `cf-turnstile` is deliberately NOT in this set, and that is the third time
# this family has had to write it down: 2Captcha's own Scraping Browser
# auto-solve extension injects a `data-ts-input="cf-turnstile-response"`
# hunter into every page it loads, so the marker fires on GOOD pages fetched
# over `--cdp-endpoint` and is absent from the real challenge. What works is
# the host the challenge actually loads from.
BOT_CHALLENGE_MARKERS = {
    "cloudflare": ("challenges.cloudflare.com", "cf_chl_opt", "__cf_chl_",
                   "cf-browser-verification", "Just a moment..."),
    "recaptcha": ("google.com/recaptcha", "g-recaptcha", "grecaptcha.render"),
    "hcaptcha": ("hcaptcha.com/1/api.js", "h-captcha"),
    "datadome": ("captcha-delivery.com", "datadome"),
    "perimeterx": ("px-captcha", "perimeterx.net"),
    "awswaf": ("awswaf", "token.awswaf.com"),
}

# Script tags injected by a browser EXTENSION rather than by the site. The
# Scraping Browser API ships an auto-solve extension whose hunters appear in
# the markup of a perfectly good page; stripping them before looking for
# markers is what stopped a sibling repo reporting exit 3 on a 1.8 MB page
# holding the full catalogue.
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script[^>]+src=[\"'](?:chrome|moz)-extension://[^\"']*[\"'][^>]*>"
    r".*?</script>", re.S | re.I)

# How much of a document to unescape before matching. A refusal page is a few
# hundred bytes of furniture; a served listing is 750 KB of grid. Unescaping
# the whole of the second buys nothing and risks a market question reading as
# a marker (§20).
_MARKER_PREFIX_BYTES = 200_000


def _marker_haystack(html: str) -> str:
    """The document as a marker matcher should see it.

    Entities are unescaped over a bounded prefix because an edge can serve
    the SAME refusal page two ways — `https&#58;&#47;&#47;host` to an HTTP
    client and `https://host` to a browser — and a literal marker then
    matches the three browser engines and silently misses the HTTP one (§20).
    """
    trimmed = html[:_MARKER_PREFIX_BYTES]
    stripped = _EXTENSION_SCRIPT_RE.sub(" ", trimmed)
    return html_module.unescape(stripped).lower()


def detect_bot_challenge(html: Optional[str]) -> Optional[str]:
    """The vendor of a challenge page, or None.

    Returns the vendor NAME so a caller can say which one, rather than True.
    """
    if not html:
        return None
    haystack = _marker_haystack(html)
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        for marker in markers:
            if marker.lower() in haystack:
                return vendor
    return None


# The site's own root element, present exactly three times in every one of
# the twelve captures taken — including its 404 page, which the site really
# does serve — and absent from anything that is not Polymarket's HTML.
#
# This is the structural "was this built out of the site's own assets?" test
# §8 arrived at, and it answers the case a marker list cannot: Chromium's own
# network-error page carries the site's HOSTNAME in its `<title>`, no vendor
# marker of any kind, and `ERR_PROXY_CONNECTION_FAILED` in a div (§18). A
# title check calls that a real page; this does not.
_SITE_ROOT_MARKERS = ("__pm_html", "/_next/static/")


def served_by_polymarket(html: Optional[str]) -> bool:
    if not html:
        return False
    head = html[:_MARKER_PREFIX_BYTES]
    return any(marker in head for marker in _SITE_ROOT_MARKERS)


# The site's own "this address does not exist", in its own words. An
# UNAMBIGUOUS POSITIVE SIGNAL and therefore checked before any threshold
# (§17's classification-order trap): a real page that happened to reference
# few assets must not read as blocked, and this page is a correct answer to a
# wrong URL rather than a failure.
_NOT_FOUND_MARKERS = ("page not found", "this page could not be found")


def looks_not_found(html: Optional[str], status: Optional[int] = None) -> bool:
    if status == 404:
        return True
    if not html:
        return False
    return any(m in html[:_MARKER_PREFIX_BYTES].lower()
               for m in _NOT_FOUND_MARKERS)


# ===========================================================================
# The public parse
# ===========================================================================
# How much of a page's rows must carry a price before the read is suspect.
# Measured 2026-09-16 across five captures: 94/94, 70/70, 84/84, 5/5 and 1/1
# markets carried an `outcomePrices` pair — every market the payload named
# had a price, on every page kind. So the floor sits high, and the 5% of room
# below it is for a market the venue has just created and not yet priced.
PRICE_FLOOR_PCT = 95


# How far apart the two views of one price may be and still count as the
# same number. JSON-LD publishes two decimals and the payload three or four
# — 0.88 against 0.875, 0.0705 against 0.07 — so anything tighter than half
# a cent reports rounding as disagreement. Measured over the 20 ItemList
# entries of three listing captures: at 0.005 every genuine pair matched and
# the only rows left over were the ones described below, which are a
# different market rather than a different price.
JSONLD_PRICE_TOLERANCE = 0.005


def _confirm_with_jsonld(rows: List[Market], html: Optional[str]) -> Tuple[int, int]:
    """Cross-check the payload's price against the page's own JSON-LD.

    The ItemList publishes ONE price per event, and which of the event's
    markets it belongs to is not stated anywhere. On a two-outcome event it
    is the first; on a sports event it is the leading side — the ItemList
    said 1.0 for a settled match whose first market was 0.0, on seven events
    of one capture. So the check is the one §4 specifies for exactly this
    situation: the structured price must be AMONG the event's market prices.
    If it is, the matching row is marked `flight+jsonld`. If it is not, the
    two views disagree about which market this is, the row is left alone and
    the disagreement is logged with the sku — overwriting a correct row is
    worse than leaving one uncorrected.

    It can therefore confirm at most 20 of a listing's 94 rows, and none on
    an event page, which has no ItemList. It is not coverage and is not
    reported as such.
    """
    prices = jsonld_prices(html)
    if not prices:
        return 0, 0
    by_event: Dict[str, List[Market]] = {}
    for row in rows:
        if row.event_slug:
            by_event.setdefault(row.event_slug, []).append(row)
    confirmed = mismatched = 0
    for slug, (price, currency) in prices.items():
        siblings = by_event.get(slug) or []
        if not siblings:
            continue
        if currency:
            for row in siblings:
                row.currency = row.currency or currency
        if price is None:
            continue
        match = next((r for r in siblings if r.price is not None
                      and abs(price - r.price) <= JSONLD_PRICE_TOLERANCE), None)
        if match is not None:
            confirmed += 1
            match.data_source = "flight+jsonld"
        else:
            mismatched += 1
            logger.info(
                "JSON-LD publishes %s for event %s and none of its %d "
                "market(s) is within %s of it (%s). Leaving the payload's "
                "values in place — they are the ones with a market id "
                "attached — and not marking them confirmed.",
                price, slug, len(siblings), JSONLD_PRICE_TOLERANCE,
                ", ".join(str(r.price) for r in siblings[:5]))
    return confirmed, mismatched


def parse_markets(html: str, url: str, page: int = 1,
                  mode: str = "markets") -> List[Market]:
    """Every market this page names, as rows, in the page's own order.

    The three paths run in the order the module docstring gives, and the
    first one that produces rows wins — with one exception: JSON-LD is always
    consulted for the currency and for the price cross-check, because it is
    the only place the currency is stated at all.

    `page` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it a row from
    page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output (§18).
    """
    base = normalize_url(url)
    rows = markets_from_flight(html, base)
    source = "flight"

    if not rows:
        rows = markets_from_dom(html, base)
        source = "dom"
        if rows:
            # The page rendered its tiles and shipped a payload this parser
            # could not read. That is a bug here, not an empty listing, and
            # saying so is the difference between a reader checking the URL
            # and a reader checking this file (§20).
            logger.warning(
                "Read %d row(s) from the rendered DOM because the page's own "
                "payload produced none. These rows are EVENT-level: the DOM "
                "publishes no market id, so `sku` is the event slug, `price` "
                "is the tile's rounded percentage and `volume` its rounded "
                "total. If the site has not changed, this is a parser "
                "regression worth reporting.", len(rows))

    if rows and source == "flight":
        confirmed, mismatched = _confirm_with_jsonld(rows, html)
        if confirmed or mismatched:
            logger.info("JSON-LD confirmed the price of %d row(s) and "
                        "disagreed on %d.", confirmed, mismatched)

    # The currency, for every row and every path. An event page states it on
    # its `Event` node while stating a placeholder price beside it, so this
    # runs whichever path produced the rows.
    currency = jsonld_currency(html)
    if currency:
        for row in rows:
            row.currency = row.currency or currency

    for index, row in enumerate(rows, start=1):
        row.page = page
        row.position = index

    priced = sum(1 for r in rows if r.price is not None)
    if rows and priced * 100 < PRICE_FLOOR_PCT * len(rows):
        logger.warning(
            "Only %d of %d row(s) on page %d carry a price (floor is %d%%). "
            "Every market on every capture taken for this repo had one, so "
            "this is worth a look at the dump rather than a shrug.",
            priced, len(rows), page, PRICE_FLOOR_PCT)
    return rows


# Kept under the family's name: every engine and smoke suite in this family
# calls `parse_products`, and a reader moving between repos should not have
# to learn a new one.
parse_products = parse_markets


def page_gap(html: Optional[str], parsed: int) -> Optional[int]:
    """None, always, on this site — and the None is the point.

    §8's rank arithmetic applies where a site NUMBERS its items: 30 rows
    spanning ranks 1-50 proves 20 cards never loaded. Polymarket numbers
    nothing. Its `totalCount` is the size of the whole catalogue (21,511
    against a page of 20), so reading it as a gap would claim 21,491 missing
    rows on a page that rendered everything it was ever going to.

    An unknown gap must not read the same as a gap of zero, so this returns
    None and `total_events` goes in the sidecar beside the row count instead.
    """
    return None


# ===========================================================================
# What kind of answer did we just get?
# ===========================================================================
# Five states, and four of them want a different response. The order the
# checks run in is the whole design, and it is §17's lesson rather than a
# preference: an UNAMBIGUOUS POSITIVE SIGNAL is stronger than a threshold, so
# the site's own "Page not found" and the site's own market data are both
# read before anything that counts markers or references.
#
# The alternative — asking "does this page reference the site's assets often
# enough?" first — is how a sibling repo reported exit 3 for a correct answer
# on a minimal real page, and sent its reader hunting for a proxy problem.
PAGE_STATES = ("content", "empty", "shell", "challenge", "blocked")


def is_challenge_page(html: Optional[str]) -> bool:
    """A challenge interstitial, as opposed to a flat refusal.

    Cloudflare's managed challenge is the one this site could realistically
    serve — it sits behind Cloudflare, `cf-ray` on every response — and it is
    the one worth retrying and worth handing to a solver. See
    `captcha_solver.detect_turnstile`: a Challenge page publishes no sitekey
    in its markup at all, so the runtime interception is what makes it
    solvable rather than merely detectable.
    """
    vendor = detect_bot_challenge(html)
    return vendor in ("cloudflare", "recaptcha", "hcaptcha", "datadome",
                      "perimeterx", "awswaf")


def detect_block_marker(html: Optional[str]) -> Optional[str]:
    """The vendor name behind a refusal, for the `blocked_{vendor}` stop
    reason and for the log line a reader will paste into an issue."""
    return detect_bot_challenge(html)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "") -> str:
    """Which of the five states this response is.

    `status` is the SECOND positional argument in this family, and in this
    one it genuinely carries information: Polymarket answers a slug that does
    not exist with a real HTTP 404 and its own "Page not found" page, which
    is a correct answer to a wrong URL rather than a failure.
    """
    if not html:
        return "blocked"

    # 1. The site's own "this does not exist". Unambiguous, and checked
    #    before anything else including the status, because the 404 page is
    #    served by Polymarket and would otherwise have to survive every
    #    marker check below to be recognised.
    if looks_not_found(html, status):
        return "empty"

    # 2. The site's own data. A page that names markets IS content, whatever
    #    else is on it — and this is the check that stops a vendor marker in
    #    a script bundle from overruling a page holding the full grid (§18).
    payload = flight_payload(html)
    if payload and ('"outcomePrices"' in payload or '"markets":[' in payload):
        return "content"
    if count_cards(html) >= MIN_CARD_MATCHES:
        return "content"

    # 3. Only now, on a page that produced nothing, does a vendor marker get
    #    to say what went wrong. It REFINES the reason for a page the policy
    #    had already given up on rather than deciding for one it had not.
    vendor = detect_bot_challenge(html)
    if vendor:
        return "challenge" if is_challenge_page(html) else "blocked"

    # 4. Served by Polymarket, and nothing on it yet. Wants a WAIT rather
    #    than a refetch — which is the state §18 says a search grid needs on
    #    a site whose page kinds render differently, and the state a
    #    too-early snapshot lands in here.
    if served_by_polymarket(html):
        return "shell"

    # 5. Something answered, and it was not this site. Chromium's own
    #    network-error page lands here: it carries `<title>polymarket.com
    #    </title>` — the SITE'S OWN HOSTNAME, so a title check would call it
    #    a real page — no vendor marker at all, and `ERR_PROXY_CONNECTION_
    #    FAILED` in a div no marker list would know (§18). Asking whether the
    #    page was built out of the site's own assets is what answers it.
    if status is not None and status >= 400:
        return "blocked"
    return "blocked"
