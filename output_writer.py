"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three modes, one row shape
--------------------------
    --mode markets   /predictions, /predictions/{tag}, /{category}, ?q={query}
                     one listing page, one row per MARKET on it
    --mode events    the same listing, then every event's own page — deep
                     rows for all of them
    --mode event     /event/{slug}  one event, deep rows for its markets

All three yield the SAME class, because they are three views of the same
thing. A MARKET is the leaf Polymarket prices: an event ("Fed Decision in
September?") is a folder of markets ("25 bps increase", "No change", …) and
each market is one binary contract with its own id, its own order book and
its own price. A listing page selects events; an event page opens one. So
there is one dataclass here, and `diff_runs.py` can compare a `markets` run
against an `events` run on the columns both populate.

The row is `Market` and not `Product`
-------------------------------------
Every other repo in this family names its row `Product`. Polymarket sells no
products: what it lists is a question, and what a row carries is that
question's price — a probability between 0 and 1, quoted in USD because one
share pays $1 if the outcome happens. `price` therefore keeps the family's
name and the family's meaning (what one unit costs right now) while being a
number a reader must not mistake for a retail price; `price_is_probability`
is not a column because it would be True on every row of every run, which is
exactly the column §9 says must not exist. The README says it instead.

What IS kept, byte-identical and in order, is the family prefix — `source`,
`scraped_at`, `url`, `sku`, `title` — so one column name works across the
family (§9).

Columns the family has and this repo does not, with the measurement:

    brand           no seller: every market is issued by the venue itself.
    original_price  no discount chain. A market's price moves; it is never
                    marked down FROM anything, and `price_24h_ago` (which
                    the payload does publish, as `oneDayPriceChange`) is the
                    honest version of "what it used to cost".
    discount_pct    same reason. A percentage change is `price_change_24h`.
    rating          Polymarket publishes no rating on a market.
    in_stock        an order book is not stock. `accepting_orders` and
                    `active`/`closed` say what a caller actually needs, and
                    a market with no liquidity is still open.

`sku` is the market's SLUG, not its numeric id
----------------------------------------------
Both are unique and both are in the payload. The slug wins because it is the
one id that every path can produce: the flight payload carries it, the DOM
carries it in the href (`/event/{event}/{market}`), and it survives in a
`--dump-html` capture where the numeric id may not. `market_id` carries the
numeric id beside it and `condition_id` the on-chain one, so nothing is lost.

The measurement behind that choice, 2026-09-16: on the 94 markets of one
`/predictions` capture, slug and id were both 94/94 unique and agreed
one-to-one. The slug is not stable across a market being re-worded — the
"Clarity Act signed into law in 2026" event has a 2025 image filename from
its own earlier slug — so a consumer tracking one market over months should
join on `condition_id` (event mode) or `market_id`. That sentence is in the
README too, because a reader who joins on `sku` across a re-slug gets two
rows and no warning.
"""
import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The host a row came from. Polymarket serves one host and eighteen locale
# PATHS under it (`/es/predictions`, `/zh/event/…`), not eighteen hosts, so
# this genuinely does not vary — it is here because the family's first column
# is `source` and a consumer reading six of these repos reads it in every one.
SOURCE_DEFAULT = "polymarket.com"


@dataclass
class Market:
    # --- the family prefix, byte-identical and in order across the family ---
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The market's own address. `/event/{event-slug}/{market-slug}` where the
    # market is one of several under an event, `/event/{event-slug}` where the
    # event holds exactly one market and the site does not give it a second
    # address.
    url: str = ""
    # The market slug. See the module docstring for why this and not the id.
    sku: Optional[str] = None
    # The question, as the site words it: "Will the Fed decrease interest
    # rates by 25 bps after the September 2026 meeting?"
    #
    # TRANSLATED under a locale path — the same market is a Spanish sentence
    # on `/es/` and a Chinese one on `/zh/`, with the same `sku` and the same
    # price. Join on the id, never on this (§20).
    title: Optional[str] = None

    # --- the price -------------------------------------------------------
    # What one share of the FIRST outcome costs, 0.0 to 1.0, which is the
    # market's own estimate of that outcome's probability.
    #
    # `outcomes[0]` and not "Yes", and the difference is not pedantry: most
    # markets here are binary and read `["Yes","No"]`, but a sports market
    # reads `["Alex de Minaur","Stefanos Tsitsipas"]`, `["Over 2.5","Under
    # 2.5"]` or `["FC Barcelona","Draw (…)","Real Racing Club"]` as three
    # separate binary markets under one event. Five of the eleven markets on
    # one captured listing had no "Yes" in them at all. So `price` is the
    # price of `outcomes[0]`, `outcome_prices` carries the whole vector, and
    # a consumer that wants a specific side reads the label.
    #
    # Read by INDEX for a second reason too: an event page under a locale
    # path ships `["Sí","No"]` where the listing under the same path ships
    # `["Yes","No"]`, so a parser keyed on the word is correct on one page
    # kind and silently empty on the other.
    price: Optional[float] = None
    # "USD". A fact, from the site's own JSON-LD (`offers.priceCurrency`),
    # not a default: shares settle in USDC and the site quotes them in
    # dollars. Null if a row was built from a path that never saw it.
    currency: Optional[str] = None
    # Every outcome and its price, in the site's own order and index-aligned.
    # Two entries on a binary market, which is all of them today; the columns
    # above are `outcomes[0]` and `outcome_prices[0]`.
    outcomes: Optional[List[str]] = None
    outcome_prices: Optional[List[float]] = None
    # The label this market carries INSIDE its event — "25 bps increase".
    # Null on a single-market event, where the event title is the question.
    group_item_title: Optional[str] = None

    # --- the order book --------------------------------------------------
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    # `bestAsk - bestBid` as the site publishes it, not computed here: the
    # payload ships `spread` on an event page and the two agreed on 5 of 5
    # markets of one capture, so this is the site's number where it gives one.
    spread: Optional[float] = None
    last_trade_price: Optional[float] = None
    # The site's own 24h/1w price deltas, in probability points. Only an
    # event page publishes them.
    price_change_24h: Optional[float] = None
    price_change_1w: Optional[float] = None

    # --- what the venue counts -------------------------------------------
    # USD, all-time and windowed. A listing row gets the EVENT's volume (the
    # only one that page ships); an event row gets the MARKET's own.
    # `volume_scope` says which, because 199,171,278 for an event and
    # 19,154,686 for one of its five markets are both correct and not
    # comparable.
    volume: Optional[float] = None
    volume_24h: Optional[float] = None
    volume_1w: Optional[float] = None
    liquidity: Optional[float] = None
    volume_scope: Optional[str] = None

    # --- the event this market belongs to --------------------------------
    event_id: Optional[str] = None
    event_slug: Optional[str] = None
    event_title: Optional[str] = None
    event_url: Optional[str] = None
    # Polymarket's own breadcrumb, where the listing ships one: "Finance" /
    # "Fed". Null on an event page, which ships tags instead — not a failed
    # read.
    category: Optional[str] = None
    subcategory: Optional[str] = None
    tags: Optional[List[str]] = None

    # --- when ------------------------------------------------------------
    # ISO 8601 UTC, as the site publishes them.
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    # --- what state the market is in --------------------------------------
    active: Optional[bool] = None
    closed: Optional[bool] = None
    # Whether the book is taking orders right now. Distinct from `active`: a
    # market can be active and paused. Event mode only.
    accepting_orders: Optional[bool] = None
    # Polymarket's numeric market id, and the on-chain condition id the CLOB
    # API keys on. `clob_token_ids` are the two ERC-1155 token ids, YES then
    # NO, index-aligned with `outcomes` — the pair anyone querying the order
    # book needs. Event mode only; a listing does not ship them.
    market_id: Optional[str] = None
    condition_id: Optional[str] = None
    clob_token_ids: Optional[List[str]] = None
    # Whether this market is part of a negative-risk (mutually exclusive)
    # group, where the outcomes of the sibling markets sum to one.
    neg_risk: Optional[bool] = None

    # --- provenance -------------------------------------------------------
    # WHICH of the page's three views built this row (never a guess presented
    # as a fact). `diff_runs.py` reports a difference that comes with a
    # `data_source` difference as `source_changed` rather than as a change.
    #
    #   flight         the inlined Next.js payload — the site's own data
    #   flight+jsonld  and the page's JSON-LD agreed on the price
    #   jsonld         structured data only
    #   dom            the rendered tile only, and therefore an EVENT's
    #                  headline market keyed by the event slug, with no
    #                  market id available to key it better
    data_source: Optional[str] = None
    # The fetch this row came from, and its position within it. Unique as a
    # pair across a run; `smoke_test.py` asserts it, because `position`
    # restarts at 1 on every page and the column is worthless without `page`
    # beside it (§18).
    page: Optional[int] = None
    position: Optional[int] = None


# Every mode yields the same class: a Polymarket row is a market whichever
# view named it, and the columns a given view cannot fill are null with
# `data_source` saying why.
ROW_CLASS_BY_MODE = {"markets": Market, "events": Market, "event": Market}

# Kept under the family's name so that code shared with the siblings — and
# anything a user wrote against one of them — keeps importing successfully.
# This repo has exactly one row class, so the alias is the same object rather
# than a second definition that could drift.
Product = Market

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. All three qualify: a market appears once per
# listing and once per event page.
UNIQUE_BY_SKU_MODES = ("markets", "events", "event")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.
    This repo needs it for a reason its siblings do not: in `--mode events`
    page 1 is the LISTING and pages 2..N are the event pages of the very
    markets page 1 already named. Every deep row therefore arrives as a
    duplicate of a shallow one, and without a rule the output would hold both.
    The rule is the one the family already has — first row wins, page order,
    merged after every page has landed — plus `--prefer-deep` in the engines,
    which drops the shallow row when its own event page answered.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


def is_deeper(new_row: Any, old_row: Any) -> bool:
    """Whether `new_row` is the same market read from a richer page.

    "Richer" is not a judgement call here, it is a field the shallower page
    cannot have: `condition_id` is the market's on-chain id and ONLY an event
    page publishes it. A listing ships 11-12 fields per market and an event
    page 111-115, so the one carrying a condition id carries the other
    hundred too.
    """
    return bool(getattr(new_row, "condition_id", None)) and not \
        bool(getattr(old_row, "condition_id", None))


def merge_pages(groups: Sequence[Any], key: str = "sku") -> tuple:
    """Merge per-page row groups into one list, in PAGE order.

    `groups` is [(page_num, rows), …] and is sorted here rather than trusted,
    because in `--mode events` the pages can be fetched by workers and arrive
    in any order — and dedupe that mutates a running set inside the loop
    makes the OUTPUT depend on which page finished first (§8).

    The rule that is specific to this repo: when the same `sku` arrives twice
    and the second reading is DEEPER, the deeper one replaces the first IN
    PLACE. That happens by construction in `--mode events`, where page 1 is
    the listing that names every market shallowly and pages 2..N are those
    markets' own event pages. Keeping the first would throw away exactly the
    columns the extra twenty fetches were for; appending both would emit two
    rows for one market. Replacing in place keeps the listing's ORDER —
    which is the site's own ranking and the only ordering information there
    is — while taking the better reading of each row.

    Returns (rows, new_per_page, upgraded_per_page).
    """
    rows: List[Any] = []
    position_of: dict = {}
    new_per_page: dict = {}
    upgraded_per_page: dict = {}
    for page_num, group in sorted(groups, key=lambda g: g[0]):
        new_count = upgraded = 0
        for row in group:
            value = getattr(row, key, None)
            if value is None:
                # Nothing to check a duplicate against, and dropping it would
                # be a silent data loss rather than a duplicate removal.
                rows.append(row)
                new_count += 1
                continue
            if value not in position_of:
                position_of[value] = len(rows)
                rows.append(row)
                new_count += 1
            elif is_deeper(row, rows[position_of[value]]):
                rows[position_of[value]] = row
                upgraded += 1
        new_per_page[page_num] = new_count
        upgraded_per_page[page_num] = upgraded
    return rows, new_per_page, upgraded_per_page


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the two ways to get a
# page with no markets on it: an event slug that does not exist, which
# answers HTTP 404 with the site's own "Page not found" and no payload at
# all; and a category route that exists and lists nothing. Both are
# EXIT_NO_PRODUCTS — the request was served exactly as asked and simply has
# no markets on it. Reporting either as blocked would send a user hunting
# for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "markets", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` is recorded because it decides which columns a row can have at
    all: a listing ships 11 to 12 fields per market and an event page ships
    111 to 115, so `condition_id`, `clob_token_ids`, `spread` and the volume
    windows are null on every `markets` row and populated on every `event`
    one. Diffing a shallow run against a deep one without that would report
    a dozen columns as having appeared from nowhere, so diff_runs.py refuses
    a pair whose modes differ. `source` is recorded for the family's shape;
    on this site it is `polymarket.com` on every row, because the eighteen
    locales are PATHS under one host rather than hosts of their own.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the site's own `total_events` there — the listing's
    `totalCount`, 21,511 for `/predictions` on 2026-09-16 — beside the 20
    that page actually ships, because the gap between them is the single
    most important thing a reader of this output needs to understand, and
    `locale` where the URL carried one.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the correct output.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected outcome.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" beside it, and on
# this site the ordering between them is not a preference — it is the only
# thing that works.
#
# Polymarket publishes no `link[rel=next]`, no pagination control and no
# numbered anchors, and its listing ignores every page parameter tried
# against it — `?page=2`, `?_p=2` and `?offset=20` each answered with page 1,
# byte for byte the same twenty events (measured 2026-09-16). A listing URL
# is ONE page and there is no address for event 21.
#
# "listing_has_one_page" is therefore a COMPLETE stop reason and not a
# partial one, and that is the most consequential line in this file. The run
# asked the catalogue question the URL poses and got the whole answer the
# site will give to it; nothing was missed, refused or truncated. Calling it
# partial would put an amber light on every correct run, and §7's lesson —
# a complete-looking success holding a third of the data — is the opposite
# failure, guarded by the README stating the twenty and the canary asserting
# it.
#
# "no_new_products" stays as the data-side termination condition for
# `--mode events`, whose page 2..N are event pages named by page 1: a page
# whose markets are all already in `seen` ends the walk. "pagination_
# exhausted" is kept for the family's shape.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted",
                         "no_new_products", "listing_has_one_page")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "markets", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are genuinely different things and a pipeline branches on
        # them (§8: blocked is not empty is not partial):
        #
        #   blocked          something stood between the run and the content
        #   did not complete we never reached the site — a dead proxy, a
        #                    load timeout, a refused batch
        #   completed        we asked, and the site's reply was nothing
        #
        # The middle one used to fall through to EXIT_NO_PRODUCTS, and that
        # was measured rather than reasoned about in a sibling repo: an
        # unreachable proxy produced exit 4 — "ran fine, found nothing" — on
        # a feed with hundreds of rows, while the sidecar beside it said
        # `status: failed`, `pages_completed: 0`. A consumer branching on the
        # exit code, which is what this family says exit codes are for, would
        # have recorded an empty catalogue.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Failed run: 0 of {pages_requested} page(s) were "
                  f"fetched ({stop_reason}). This is NOT an empty result — "
                  f"nothing was read from the site at all.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
