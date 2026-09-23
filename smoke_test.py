#!/usr/bin/env python3
"""polymarket-scraper — offline smoke tests.

One file of plain functions with fixtures loaded from `fixtures_generated.json`,
no pytest required. `tests/test_smoke.py` wraps it as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is REPORTED, because "skipped, engine absent" reads
exactly like a passing run. CI's engine-smoke job installs each engine in its
own venv and fails if that skip list is non-empty.

The fixtures are cut from real captures by `make_fixtures.py`, which proves
each one parses IDENTICALLY to its untrimmed original, column for column,
and replaces every real author handle with a pseudonym. Do not hand-edit
them.

WHAT THIS SUITE IS FOR, beyond the obvious
------------------------------------------
Most of these checks exist because of a specific failure, in this repo or in
a sibling. The ones worth knowing about before you change anything:

  * `test_values_on_real_fixtures` asserts VALUES, not coverage. A column can
    be 100% populated and entirely wrong. The one that matters here is
    `claps`: Medium's legacy payload carries `virtuals.totalClapCount` AND
    `virtuals.recommends` side by side, both populated on 128 of 128 stories,
    and the second is the retired pre-2017 recommend count — 25 against 248
    on the same story. Reading it would have filled the column completely and
    wrongly, and no coverage check would have said a word.

  * `test_markers_do_not_match_a_good_page` is the §18 rule as a test. Medium
    ships reCAPTCHA markup for its own sign-in widget on every page it
    serves, and `challenge-platform` appears twice on good and refused pages
    alike. A marker that matches every page is worse than no marker, so every
    marker in every set is asserted ABSENT from four pages known to be good.

  * `test_engine_parity` binds every shared-module call in every engine
    against the callee's REAL signature. Two engines in a sibling repo called
    `classify(html, url=...)` where the parameter is positional, both crashed
    on their first fetch, and nothing short of a live run saw it. This repo's
    own first live run hit the same class twice — `scroll_until_settled` was
    called with a `target=` this module no longer takes, and Selenium passed
    `session.driver` to a helper that wanted `session`.

  * `test_publication_pages_are_refused` pins the decision that keeps this
    repo honest: a publication home page carries post IDS and no post data,
    and rows built from it would hold a sku and 26 nulls while the run
    reported success.
"""

import ast
import contextlib
import csv as csv_module
import inspect
import io
import json
import os
import pathlib
import re
import sys
import tempfile
from dataclasses import asdict, fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import captcha_solver
import env_config
from fingerprint_client import (fingerprint_user_agent,
                                playwright_context_kwargs)
import page_flow
import product_parser
from diff_runs import diff_products
from output_writer import (EXIT_FETCH_FAILED, Market, Product, save, finish_run, write_csv,
                           run_meta, dedupe_by_key, dedupe_by_sku,
                           merge_pages, is_deeper,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           COMPLETE_STOP_REASONS, EXIT_BLOCKED,
                           EXIT_NO_PRODUCTS, EXIT_PARTIAL, EXIT_API_ERROR,
                           LIST_CSV_SEPARATOR, SOURCE_DEFAULT)
from product_parser import (parse_markets, SELECTORS, HOSTS, LOCALES,
                            PAGE_CAP, NEXT_PAGE_SELECTOR, PAGINATES_BY_URL,
                            PAGE_URL_REASON, BOT_CHALLENGE_MARKERS,
                            category_from_url, detect_block_marker,
                            detect_bot_challenge, detect_page_state,
                            event_urls_from, is_supported_host, jsonld_blocks,
                            jsonld_prices, looks_not_found, markets_from_dom,
                            markets_from_flight, normalize_url, page_kind,
                            page_url, paginates_by_url, redirected_away,
                            served_by_polymarket, site_host, source_of,
                            strip_tracking, unsupported_reason)
from proxy_pool import ProxyPool, mask, to_playwright, split_credentials

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
SHARED_MODULES = {"page_flow": page_flow, "product_parser": product_parser}

# Ten fixtures cut from pages Polymarket actually served — five listings
# across three routes and two locales, and five event pages including a
# sports fixture and the one whose payload ships no event object at all.
# Every marker check below asserts its markers are ABSENT from all ten (§18).
GOOD_PAGES = ("listing", "listing_tag", "listing_search", "listing_dash",
              "listing_es", "event_multi", "event_single", "event_sports",
              "event_es", "event_no_object")

_failures = []
_total_checks = 0



def check(label, condition):
    """Print and record one check. Returns the condition so callers can
    accumulate with `ok &= check(...)`."""
    global _total_checks
    _total_checks += 1
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
if not os.path.exists(_FIXTURE_PATH):
    # Said in words rather than as a bare FileNotFoundError, because the
    # first time this happened it was not missing from the disk — it was
    # missing from the COMMIT. `.gitignore` carries a blanket `*.json` (a
    # scraper's own output is large and stale by the time anyone reads it),
    # which swallowed it silently: the whole suite was green locally and
    # every CI job died at import. `test_required_files_are_committed` now
    # catches that case directly.
    raise SystemExit(
        f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
        f"If you are in a clean checkout, it should have been committed — "
        f"check that .gitignore's `*.json` rule still carries the "
        f"`!fixtures_generated.json` exception.\n"
        f"If you are regenerating fixtures, run: python3 make_fixtures.py")
with open(_FIXTURE_PATH, encoding="utf-8") as _f:
    FIXTURES = json.load(_f)
URLS = FIXTURES["_URLS"]


def fixture(name):
    return FIXTURES[name]


def rows_of(name, page=1, mode=""):
    return parse_markets(FIXTURES[name], URLS[name], page=page, mode=mode)


def by_sku(name, page=1, mode=""):
    return {r.sku: r for r in rows_of(name, page, mode)}


# ---------------------------------------------------------------------------
def _engine_source(module_name):
    """The engine's source, or None when its driver is not installed.

    Read off disk rather than through `inspect.getsource`, so a check about
    an engine's TEXT does not itself need the engine's library.
    """
    path = os.path.join(REPO_ROOT, module_name + ".py")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_payload_decoding():
    group("The inlined payload — how it is read out of the document")
    ok = True

    # The chunk strings are JSON string literals full of escaped quotes, and
    # a non-greedy `"(.*?)"` stops at the first of them. Decoding with
    # `raw_decode` at the opening quote is what makes a 292 KB chunk come
    # back whole instead of truncated at byte 40.
    payload = product_parser.flight_payload(fixture("listing"))
    ok &= check("the payload decodes to something substantial",
                len(payload) > 5_000)
    ok &= check("and it carries the site's own market key",
                '"outcomePrices"' in payload)
    ok &= check("a document with no payload yields the empty string",
                product_parser.flight_payload("<html></html>") == "")
    ok &= check("and so does None, rather than raising",
                product_parser.flight_payload(None) == "")

    # THE REGRESSION THIS FIXTURE EXISTS FOR. Next.js inlines the site's own
    # bootstrap script into the RSC stream, and that script contains a brace
    # inside a SINGLE-quoted string. A scanner that tracked double-quoted
    # strings only — which is what a JSON scanner tracks — popped its brace
    # stack to empty there, lost every object start after it, and returned no
    # rows at all for a page whose market sat in plain text in the payload.
    # Twelve captures did not show it; the thirteenth did, on the first live
    # run of --mode events (§15).
    rows = rows_of("event_no_object")
    ok &= check("an event page whose payload has no event object still parses",
                len(rows) == 1)
    ok &= check("...and the row is the market the URL asked for",
                rows and rows[0].sku.startswith(
                    "crypto-market-structure-legislation-becomes-law"))
    ok &= check("...with its on-chain id, which only an event page publishes",
                rows and (rows[0].condition_id or "").startswith("0x"))

    # The enclosing-object search is the piece that survives that: it decodes
    # candidates rather than trusting a stack, so a desynced document costs
    # nothing.
    sample = '{"a":{"b":1},"outcomePrices":["0.5","0.5"],"slug":"x"}'
    hit = product_parser._enclosing_object(sample, sample.index('"outcomePrices"'))
    ok &= check("the enclosing object of a key is the object holding it",
                hit is not None and hit[2].get("slug") == "x")
    ok &= check("a key with no object around it finds nothing",
                product_parser._enclosing_object('"outcomePrices"', 0) is None)
    return ok


def test_values_on_real_fixtures():
    group("VALUES on real captures, not coverage (§10)")
    ok = True

    # A column can be 100% populated and entirely wrong. These are the exact
    # figures the site served on 2026-09-16, pinned so a parser that starts
    # reading the neighbouring field has to fail rather than look healthy.
    deep = by_sku("event_multi")
    key = ("will-the-fed-decrease-interest-rates-by-50-bps-"
           "after-the-september-2026-meeting-863")
    row = deep.get(key)
    ok &= check("the Fed event's 50bps market is read by its slug", row is not None)
    if row:
        ok &= check("price is outcomePrices[0], to four decimals",
                    row.price == 0.0005)
        ok &= check("...and the pair is index-aligned with the outcomes",
                    row.outcomes == ["Yes", "No"]
                    and row.outcome_prices == [0.0005, 0.9995])
        ok &= check("the group label is the market's name inside its event",
                    row.group_item_title == "50+ bps decrease")
        ok &= check("spread is the site's own number, not one computed here",
                    row.spread == 0.001)
        ok &= check("volume is the MARKET's, exact, not the tile's rounding",
                    row.volume == 19376636.147784993)
        ok &= check("...and volume_scope says which it is",
                    row.volume_scope == "market")
        ok &= check("the on-chain condition id is carried whole",
                    row.condition_id ==
                    "0x5e464d85eb49f22d876f3ed6168a7db5e2288e9ae1eb91effd"
                    "2758e994676f86")
        ok &= check("both CLOB token ids come with it, YES then NO",
                    len(row.clob_token_ids or []) == 2)
        ok &= check("the numeric market id is kept beside the slug",
                    row.market_id == "2252242")
        ok &= check("negative-risk grouping is recorded", row.neg_risk is True)
        ok &= check("currency is the page's own statement, not a default",
                    row.currency == "USD")
        ok &= check("the market's URL is its own address under its event",
                    row.url.endswith("/event/fed-decision-in-september-762/"
                                     + key))

    # The same five markets in Spanish. THE IDS AND THE PRICES ARE
    # IDENTICAL and only the words move — which is why every join in this
    # repo is on an id (§20).
    spanish = by_sku("event_es")
    other = spanish.get(key)
    ok &= check("the same market is the same sku under /es/", other is not None)
    if other and row:
        ok &= check("...and the same price", other.price == row.price)
        ok &= check("...and the same condition id",
                    other.condition_id == row.condition_id)
        ok &= check("...but the question is translated",
                    other.title != row.title and "Fed" in (other.title or ""))
        # THE TRAP. An event page under a locale path translates the outcome
        # labels; a LISTING page under the same path does not. A parser that
        # found its price by matching the word "Yes" would be right on one
        # page kind and silently empty on the other.
        ok &= check("an event page localises the outcome labels",
                    other.outcomes == ["Sí", "No"])
    # ...while a LISTING under the same locale does not. Measured across the
    # 11 markets the English and Spanish listing fixtures share: 0 differing
    # outcome labels, 0 differing prices, and 6 of 11 titles translated.
    english = by_sku("listing")
    spanish_listing = by_sku("listing_es")
    shared = set(english) & set(spanish_listing)
    ok &= check("the two locales share markets to compare", len(shared) >= 5)
    ok &= check("...a listing does NOT localise its outcome labels",
                all(english[k].outcomes == spanish_listing[k].outcomes
                    for k in shared))
    ok &= check("...and the prices are identical, to the last decimal",
                all(english[k].price == spanish_listing[k].price
                    for k in shared))
    ok &= check("...while the questions are translated",
                any(english[k].title != spanish_listing[k].title
                    for k in shared))

    # NOT every market is Yes/No, and a reader who assumes otherwise gets a
    # column of nonsense. A sports event's markets carry the sides' own names
    # — which is why `price` is documented as the price of `outcomes[0]`
    # rather than as "the Yes price".
    named = [r for r in rows_of("listing_es")
             if r.outcomes and "Yes" not in r.outcomes]
    ok &= check("some markets have named outcomes rather than Yes/No",
                len(named) >= 3)
    ok &= check("...and their price is still outcome_prices[0]",
                all(r.price == r.outcome_prices[0] for r in named))

    # A sports event: three outcomes, one of them a draw, and prices that do
    # not sum to 1 because each is its own binary market.
    sports = rows_of("event_sports")
    ok &= check("a sports event reads all three of its markets", len(sports) == 3)
    ok &= check("...with the team names as the group labels",
                {s.group_item_title for s in sports} ==
                {"FC Barcelona", "Draw (FC Barcelona vs. Real Racing Club)",
                 "Real Racing Club"})
    ok &= check("...and the favourite priced at 0.915",
                any(s.price == 0.915 for s in sports))

    # A listing row: shallower by design, and the columns it cannot fill are
    # null rather than guessed.
    shallow = rows_of("listing")[0]
    ok &= check("a listing row carries the event's breadcrumb",
                (shallow.category, shallow.subcategory) == ("Finance", "Fed"))
    ok &= check("...the EVENT's volume, exactly as published",
                shallow.volume == 199856030.11 and shallow.volume_scope == "event")
    ok &= check("...and no condition id, because a listing has none",
                shallow.condition_id is None)
    ok &= check("...which is a null and not a zero",
                shallow.clob_token_ids is None and shallow.spread is None)
    return ok


def test_the_three_read_paths():
    group("Three views of the same markets, in order (§4)")
    ok = True

    rows = rows_of("listing")
    ok &= check("the payload is the primary path",
                all(r.data_source in ("flight", "flight+jsonld") for r in rows))
    confirmed = [r for r in rows if r.data_source == "flight+jsonld"]
    ok &= check("JSON-LD confirms the price of the events it names",
                len(confirmed) >= 3)
    ok &= check("...but never more rows than events, since it names one each",
                len(confirmed) <= len({r.event_slug for r in rows}))

    # The ItemList publishes ONE price per event and does not say which
    # market it belongs to. On a sports event it was the LEADING side while
    # the first market was 0.0, so the check is "is it among this event's
    # prices" rather than "does it equal the first one" (§4).
    prices = product_parser.jsonld_prices(fixture("listing"))
    ok &= check("jsonld_prices is keyed by event slug", bool(prices))
    ok &= check("...and carries the currency beside the price",
                any(cur == "USD" for _price, cur in prices.values()))

    # AN EVENT PAGE'S JSON-LD IS A TRAP, and this is the check that keeps it
    # out: its `Event` node publishes "offers": {"price": "0"} for markets
    # trading at 0.0705 and 0.915. Reading it would write a placeholder into
    # every row of every event run while the rest of the row looked right.
    ok &= check("an event page's JSON-LD is not read for prices",
                product_parser.jsonld_prices(fixture("event_single")) == {})
    ok &= check("...and the row keeps the payload's price instead",
                rows_of("event_single")[0].price == 0.0705)
    ok &= check("...while its currency IS read from that same node",
                rows_of("event_single")[0].currency == "USD")

    # The DOM fallback. Exercised by renaming the payload's own entry point,
    # which is what a Next.js restructure would look like from here.
    broken = fixture("listing").replace("self.__next_f.push", "self.__next_g.push")
    dom_rows = product_parser.parse_markets(broken, URLS["listing"])
    ok &= check("a page whose payload cannot be read still yields rows",
                len(dom_rows) >= 3)
    ok &= check("...all of them marked as DOM-sourced",
                all(r.data_source == "dom" for r in dom_rows))
    ok &= check("...keyed by the EVENT slug, because the DOM has no market id",
                all(r.sku == r.event_slug for r in dom_rows))
    ok &= check("...with a price read off the tile's percentage",
                any(r.price is not None for r in dom_rows))
    ok &= check("...and a ROUNDED volume, which the column's provenance says",
                any(r.volume is not None and r.volume % 1000 == 0
                    for r in dom_rows))
    ok &= check("...and still the currency, which is a fact on the page",
                all(r.currency == "USD" for r in dom_rows))

    # There is deliberately NO overlay: the tile's percentage and the
    # payload's first price are two DIFFERENT MARKETS of one event, not two
    # views of one price (§4).
    ok &= check("no DOM overlay is applied to a payload row",
                all(r.data_source != "flight+dom" for r in rows))
    return ok


def test_the_page_number_is_threaded_through():
    group("page + position, across MORE THAN ONE page (§18)")
    ok = True
    # §18's arithmetic bug, as a check. `position` restarts at 1 on every
    # page, so without `page` beside it a row from page 2 claims a position
    # another row already has — and on a sibling repo 60 of 119 rows did.
    #
    # It has to be asserted across TWO pages to mean anything: a check that
    # only ever sees page 1 passes happily on a parser that hardcodes
    # `page = 1`, which is exactly the defect. Proven by making that change
    # and watching this fire.
    page_one = rows_of("listing", page=1)
    page_two = rows_of("listing", page=2)
    ok &= check("both pages parsed", bool(page_one) and bool(page_two))
    ok &= check("page 1 says it is page 1",
                all(r.page == 1 for r in page_one))
    ok &= check("page 2 says it is page 2",
                all(r.page == 2 for r in page_two))
    ok &= check("position restarts at 1 on each page",
                page_one[0].position == 1 and page_two[0].position == 1)
    pairs = {(r.page, r.position) for r in page_one + page_two}
    ok &= check("...so page+position is unique across the two",
                len(pairs) == len(page_one) + len(page_two))
    # And the merge keeps them: a row that survived dedupe must still carry
    # the page it came from, or the sidecar's per-page counts describe
    # nothing.
    merged, _new, _up = merge_pages([(1, page_one), (2, page_two)])
    ok &= check("the merge does not flatten the page number",
                {r.page for r in merged} == {1})
    return ok


def test_event_pages_are_scoped_to_their_event():
    group("An event page is scoped to ITS event, not to its rails")
    ok = True
    # Found by running it: an event page ships the markets of everything in
    # its related-events rail too. The Fed page carried twenty-five market
    # objects for its five markets and a single-market event carried
    # twenty-one. Emitting those would report four other events' prices as
    # this event's own — §4's junk-link data theft with no junk link in it.
    for name, expected in (("event_multi", 5), ("event_single", 1),
                           ("event_sports", 3), ("event_no_object", 1)):
        rows = rows_of(name)
        ok &= check(f"{name} yields exactly its own {expected} market(s)",
                    len(rows) == expected)
        wanted = product_parser.event_slug_from_url(URLS[name])
        ok &= check(f"{name}: every row belongs to {wanted}",
                    all(r.event_slug == wanted for r in rows))
    # A listing is NOT scoped that way: it is a page of many events.
    ok &= check("a listing keeps all of its events",
                len({r.event_slug for r in rows_of("listing")}) > 1)
    return ok


def test_unsupported_urls_are_refused():
    group("Refusing a URL WITH the reason (§5)")
    ok = True
    reason = unsupported_reason("https://polymarket.us/event/anything")
    ok &= check("polymarket.us is refused", bool(reason))
    ok &= check("...and the reason names it as a separate venue, not a typo",
                reason and "separate venue" in reason.lower())
    reason = unsupported_reason("https://polymarket.com/leaderboard")
    ok &= check("one of the site's own pages is refused", bool(reason))
    ok &= check("...and the reason points at what to use instead",
                reason and "/predictions" in reason)
    ok &= check("another site entirely is refused by host",
                unsupported_reason("https://kalshi.com/markets") is not None)
    ok &= check("a listing is accepted",
                unsupported_reason("https://polymarket.com/predictions") is None)
    ok &= check("a category listing is accepted",
                unsupported_reason("https://polymarket.com/politics") is None)
    ok &= check("an event page is accepted",
                unsupported_reason("https://polymarket.com/event/x-1") is None)
    ok &= check("a sports event page is accepted",
                unsupported_reason(
                    "https://polymarket.com/sports/laliga/lal-bar-1") is None)
    ok &= check("a locale path is accepted",
                unsupported_reason("https://polymarket.com/es/predictions") is None)
    ok &= check("no URL at all is refused with a reason",
                bool(unsupported_reason("")))
    return ok


def test_urls():
    group("URLs — slugs, locales, and the click parameter")
    ok = True
    ok &= check("an event slug is read out of an /event/ path",
                product_parser.event_slug_from_url(
                    "https://polymarket.com/event/fed-decision-762")
                == "fed-decision-762")
    ok &= check("...and out of a /sports/{league}/ path, which is the same page",
                product_parser.event_slug_from_url(
                    "https://polymarket.com/sports/laliga/lal-bar-rrc-2026-09-16")
                == "lal-bar-rrc-2026-09-16")
    ok &= check("a market slug is read out of the two-segment form",
                product_parser.market_slug_from_url(
                    "https://polymarket.com/event/fed-762/will-the-fed-cut")
                == "will-the-fed-cut")
    ok &= check("...and there is none on an event URL",
                product_parser.market_slug_from_url(
                    "https://polymarket.com/event/fed-762") is None)

    ok &= check("a locale prefix is recognised",
                product_parser.locale_of("https://polymarket.com/es/predictions")
                == "es")
    ok &= check("...and English has none",
                product_parser.locale_of("https://polymarket.com/predictions")
                is None)
    ok &= check("...and it does not change what the page IS",
                page_kind("https://polymarket.com/zh/event/fed-762") == "event")
    ok &= check("a market URL is rebuilt under the same locale",
                product_parser.market_url_for("fed-762", "will-cut", "es")
                == "https://polymarket.com/es/event/fed-762/will-cut")
    ok &= check("...and collapses to the event URL when the slugs are the same",
                product_parser.market_url_for("one-market", "one-market")
                == "https://polymarket.com/event/one-market")

    # `?tid=` is a millisecond timestamp the site hangs off a sub-market
    # link. Two runs an hour apart would otherwise write two different `url`
    # values for a market that never moved.
    ok &= check("the click parameter is stripped",
                strip_tracking("https://polymarket.com/event/a/b?tid=1758")
                == "https://polymarket.com/event/a/b")
    ok &= check("...and a real parameter is kept",
                "q=bitcoin" in strip_tracking(
                    "https://polymarket.com/predictions?q=bitcoin&tid=9"))
    ok &= check("a trailing slash is not a second address",
                normalize_url("https://polymarket.com/predictions/")
                == normalize_url("https://polymarket.com/predictions"))
    ok &= check("a relative href resolves against the site",
                normalize_url("/event/x-1")
                == "https://polymarket.com/event/x-1")

    ok &= check("the category is read off a tag listing",
                category_from_url("https://polymarket.com/predictions/crypto")
                == "crypto")
    ok &= check("...and off a top-level one",
                category_from_url("https://polymarket.com/politics") == "politics")
    ok &= check("...and a SEARCH is labelled as one, not as a tag",
                category_from_url("https://polymarket.com/predictions?q=bitcoin")
                == "q:bitcoin")
    ok &= check("...because the two are different listings",
                product_parser.listing_url_for("q:bitcoin")
                != product_parser.listing_url_for("bitcoin"))
    ok &= check("the plain listing has no category",
                category_from_url("https://polymarket.com/predictions") is None)

    ok &= check("`source` is the bare host",
                source_of("https://www.polymarket.com/predictions")
                == "polymarket.com")
    ok &= check("a search redirected to a tag listing is reported",
                redirected_away("https://polymarket.com/predictions?q=bitcoin",
                                "https://polymarket.com/predictions/bitcoin")
                is not None)
    ok &= check("...and an unchanged address is not",
                redirected_away("https://polymarket.com/predictions",
                                "https://polymarket.com/predictions") is None)
    return ok


def test_pagination():
    group("A listing has ONE page, and that is the measurement (§7)")
    ok = True
    ok &= check("PAGINATES_BY_URL is False", PAGINATES_BY_URL is False)
    ok &= check("...and the reason names what was tried",
                "?page=2" in PAGE_URL_REASON and "cursor" in PAGE_URL_REASON)
    ok &= check("page_url returns the URL for page 1",
                page_url("https://polymarket.com/predictions", 1)
                == "https://polymarket.com/predictions")
    # Returning a constructed `?page=2` would be WORSE than nothing: the site
    # answers it with HTTP 200 and page 1, so a run that trusted it would
    # find no new sku, call the listing exhausted, and report a COMPLETE run
    # holding one page (§18).
    ok &= check("...and None for every page after it",
                all(page_url("https://polymarket.com/predictions", n) is None
                    for n in (2, 3, 40)))
    ok &= check("NEXT_PAGE_SELECTOR is empty, because the site publishes none",
                NEXT_PAGE_SELECTOR == "")

    ok &= check("the site's own total is read where it states one",
                product_parser.total_events(fixture("listing")) > 20_000)
    ok &= check("...and an event page's rail total is NOT read as one",
                product_parser.total_events(fixture("event_multi")) is None)
    ok &= check("the site says it has a next page, and that is recorded",
                product_parser.has_next_page_flag(fixture("listing")) is True)
    ok &= check("page_gap is None, always, and not 0",
                page_flow.page_gap(fixture("listing"), 21) is None)

    note = page_flow.one_page_note("https://polymarket.com/predictions", 3)
    ok &= check("asking for more pages produces a note, not a silence",
                bool(note))
    ok &= check("...that says the run is complete rather than partial",
                note and "COMPLETE" in note)
    ok &= check("...and names --mode events as the way to go deeper",
                note and "--mode events" in note)
    ok &= check("one page asked for needs no note",
                page_flow.one_page_note("https://polymarket.com/predictions", 1)
                is None)
    ok &= check("`listing_has_one_page` is a COMPLETE stop reason",
                "listing_has_one_page" in COMPLETE_STOP_REASONS)

    group("--mode events plans its pages from page 1's own data")
    rows = rows_of("listing")
    planned = product_parser.event_urls_from(rows)
    ok &= check("every event on the page becomes a page to fetch",
                len(planned) == len({r.event_slug for r in rows}))
    ok &= check("...deduplicated, because five markets share one event",
                len(planned) == len(set(planned)))
    ok &= check("...in the listing's own order, which is the site's ranking",
                planned[0].endswith(rows[0].event_slug))
    ok &= check("...and the plan can be cut to --pages",
                len(product_parser.event_urls_from(rows, limit=2)) == 2)
    ok &= check("rows with no event yield no pages",
                product_parser.event_urls_from([]) == [])

    group("Concurrency is refused where there is nothing to hand a worker")
    ok &= check("markets mode is one fetch",
                page_flow.concurrency_limit(mode="markets") == 1)
    ok &= check("event mode is one fetch",
                page_flow.concurrency_limit(mode="event") == 1)
    ok &= check("events mode has no limit",
                page_flow.concurrency_limit(mode="events") is None)
    refusal = page_flow.concurrency_refusal(
        "https://polymarket.com/predictions", "markets")
    ok &= check("...and the refusal explains itself", bool(refusal))
    ok &= check("...naming --mode events as the answer",
                refusal and "--mode events" in refusal)
    ok &= check("events mode is not refused",
                page_flow.concurrency_refusal(
                    "https://polymarket.com/predictions", "events") is None)
    return ok


def test_page_state():
    group("Five states, and the ORDER the checks run in (§17)")
    ok = True
    for name in ("listing", "listing_tag", "listing_search", "listing_dash",
                 "listing_es", "event_multi", "event_single", "event_sports",
                 "event_es", "event_no_object"):
        ok &= check(f"{name} is content",
                    detect_page_state(fixture(name), 200, URLS[name]) == "content")

    # The site's own 404, under a real HTTP 404. A correct answer to a wrong
    # URL, and NOT a block: exit 4 is "ran fine, found nothing", which is
    # exactly what happened (§8).
    ok &= check("a missing event is `empty`, not `blocked`",
                detect_page_state(fixture("not_found"), 404,
                                  URLS["not_found"]) == "empty")
    ok &= check("...and it is not retried, because a second 404 is the same 404",
                page_flow.should_retry("empty") is False)
    ok &= check("...and does not count towards exit 3",
                page_flow.counts_as_blocked("empty") is False)

    # CHROMIUM'S OWN ERROR PAGE, captured through a dead proxy. It carries
    # `<title>polymarket.com</title>` — the SITE'S OWN HOSTNAME — no vendor
    # marker of any kind, and ERR_PROXY_CONNECTION_FAILED in a div that no
    # marker list would know. A title check calls it a real page (§18).
    ok &= check("Chromium's own error page is `blocked`",
                detect_page_state(fixture("proxy_error"), None,
                                  URLS["proxy_error"]) == "blocked")
    ok &= check("...and it is recognised STRUCTURALLY, not by a marker",
                product_parser.detect_bot_challenge(fixture("proxy_error"))
                is None)
    ok &= check("...because it was not built out of the site's own assets",
                served_by_polymarket(fixture("proxy_error")) is False)
    ok &= check("...while every real page was",
                all(served_by_polymarket(fixture(n)) for n in GOOD_PAGES))
    ok &= check("...and it really does carry the site's hostname in its title",
                "<title>polymarket.com</title>" in fixture("proxy_error"))

    ok &= check("an empty document is blocked rather than crashing",
                detect_page_state("", None, "") == "blocked")
    ok &= check("...and so is a page that is not this site's",
                detect_page_state("<html><body>hello</body></html>", 200, "")
                == "blocked")
    return ok


def test_markers_do_not_match_a_good_page():
    group("A marker that fires on a good page is worse than no marker (§18)")
    ok = True
    good = [fixture(n) for n in GOOD_PAGES]
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        for marker in markers:
            fires = [n for n, html in zip(GOOD_PAGES, good)
                     if marker.lower() in html.lower()]
            ok &= check(f"{vendor}: {marker!r} is absent from every good page",
                        not fires)
    ok &= check("...and no vendor is detected on any of them",
                all(product_parser.detect_bot_challenge(h) is None
                    for h in good))

    # `cf-turnstile` is the obvious marker for a Turnstile and is MEASURED
    # useless in any repo that can reach the Scraping Browser: 2Captcha's own
    # auto-solve extension injects a `cf-turnstile-response` hunter into
    # every page it loads. The host the challenge loads from is what works.
    flat = [m for markers in BOT_CHALLENGE_MARKERS.values() for m in markers]
    ok &= check("`cf-turnstile` is deliberately not a marker",
                not any(m == "cf-turnstile" for m in flat))
    ok &= check("...while challenges.cloudflare.com is one",
                "challenges.cloudflare.com" in flat)

    # A marker must survive BOTH encodings of the same page: an edge can
    # entity-escape the punctuation for an HTTP client and serve it plain to
    # a browser, and a literal marker then matches the browser and silently
    # misses the client (§20).
    escaped = ("<html><body>Just a moment&#8230; "
               "https&#58;&#47;&#47;challenges&#46;cloudflare&#46;com/turnstile"
               "</body></html>")
    ok &= check("an entity-escaped marker is still found",
                product_parser.detect_bot_challenge(escaped) == "cloudflare")

    # And the extension trap from the other side: a script tag injected by a
    # browser extension is not the site's markup.
    injected = ('<html><body><script src="chrome-extension://abc/content/'
                'captcha/turnstile/hunter.js" data-ts-input="cf-turnstile-'
                'response"></script></body></html>')
    ok &= check("an extension-injected hunter does not count as a challenge",
                product_parser.detect_bot_challenge(injected) is None)
    return ok


def test_the_scraping_browser_injects_a_captcha_and_it_is_not_the_sites():
    group("Whose captcha markup is it? (§18, §21)")
    ok = True
    # MEASURED ON THIS SITE, not inherited. The same URL, fetched twice
    # within a minute on 2026-09-16:
    #
    #   straight from the site (curl, 779,085 bytes)   0 of everything below
    #   over the Scraping Browser (740,876 bytes)      21 "captcha", 16
    #                                                  extension scripts,
    #                                                  1 cf-turnstile,
    #                                                  1 <captcha-widgets>
    #
    # So every captcha string this project has ever seen on polymarket.com
    # came from 2Captcha's own auto-solve extension, and none from the site.
    page = fixture("cdp_extension")
    ok &= check("the Scraping Browser fixture still carries its injections",
                page.count("chrome-extension://") >= 10)
    ok &= check("...including the Turnstile hunter's own attribute",
                "cf-turnstile" in page)
    ok &= check("...and the empty mount point it adds",
                "<captcha-widgets>" in page)

    # THE COUNTERFACTUAL, and the reason this fixture exists. `cf-turnstile`
    # is the obvious marker for a Turnstile. With it in the set, THIS page —
    # which the site served in full — would be reported as a challenge, and
    # a run over --cdp-endpoint would exit 3 holding a complete listing.
    flat = {m.lower() for markers in BOT_CHALLENGE_MARKERS.values()
            for m in markers}
    ok &= check("`cf-turnstile` is NOT in the marker set", "cf-turnstile" not in flat)
    ok &= check("...and here is why: it IS on this good page",
                "cf-turnstile" in page.lower())
    ok &= check("...while the marker that does work is absent from it",
                "challenges.cloudflare.com" not in page.lower())

    # And the page reads as what it is.
    ok &= check("the page classifies as content, not as a challenge",
                detect_page_state(page, 200, URLS["cdp_extension"]) == "content")
    ok &= check("...no vendor is detected on it",
                detect_bot_challenge(page) is None)
    ok &= check("...and its markets parse",
                len(rows_of("cdp_extension")) >= 5)

    # The site's own configuration, asked the way §18 asks it: not "did we
    # meet a captcha" but "is one configured, and would we see it?" Counted
    # with the extension's own scripts removed, so the answer is about
    # polymarket.com rather than about our tooling.
    import re as _re
    site_only = _re.sub(r"<script[^>]+src=\"(?:chrome|moz)-extension://[^\"]*\"[^>]*>.*?</script>",
                        " ", page, flags=_re.S | _re.I)
    for label, pattern in (("a *_SITE_KEY", r"[A-Z_]*SITE_KEY"),
                           ("a 6L… reCAPTCHA key", r"\b6L[A-Za-z0-9_-]{20,}"),
                           ("a data-sitekey", r"data-sitekey"),
                           ("a Turnstile loader", r"challenges\.cloudflare\.com")):
        ok &= check(f"the site's own markup carries no {label}",
                    not _re.search(pattern, site_only, _re.I))
    return ok


def test_the_solver_is_not_declared_useless():
    group("What may be said about a captcha here (§19)")
    ok = True
    # THE MOST EXPENSIVE SHAPE OF ERROR THIS FAMILY CAN PRODUCE is a sentence
    # that tells a reader a 2Captcha key would not help. No test fails, no
    # run crashes, the output is correct — and it is wrong about a product.
    # The only sentence this repo is entitled to is "this repo does not
    # implement X".
    # Phrased narrowly ON PURPOSE. "cannot be solved" on its own is a
    # legitimate sentence — a Turnstile whose sitekey was never captured
    # genuinely cannot be, and all three engines say so at that exact moment.
    # What is banned is a claim about the SITE or about the PRODUCT: that a
    # captcha here is out of reach, or that a 2Captcha key would not help.
    # That is the sentence a sibling repo shipped, and it is invisible to
    # every other check because nothing fails and the output is correct.
    banned = (
        "captcha solver is inapplicable",
        "no solve is ever attempted",
        "a captcha on this site cannot be solved",
        "captchas here cannot be solved",
        "2captcha cannot solve",
        "a 2captcha key would not help",
        "no key is ever charged",
        "this site's captcha is unsolvable",
    )
    offenders = []
    for path in pathlib.Path(REPO_ROOT).rglob("*"):
        if path.is_dir() or path.suffix not in (".py", ".md", ".yml", ".txt"):
            continue
        if ".git" in path.parts or "fixtures_generated" in path.name:
            continue
        if path.name == "smoke_test.py":
            # This file holds the banned list itself. Skipping it is not a
            # loophole: every other shipped file is scanned, and the list
            # living in one place is what makes the rule enforceable at all.
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for phrase in banned:
            if phrase in text:
                offenders.append(f"{path.name}: {phrase!r}")
    ok &= check("no shipped file claims a captcha here cannot be solved",
                not offenders)
    if offenders:
        for offender in offenders:
            print("        " + offender)

    # And the capability the policy claims must actually be wired: a
    # challenge state that says `solve` with no solver behind it would be the
    # same defect in the other direction.
    ok &= check("the policy sends a challenge to the solver",
                page_flow.should_solve("challenge") is True)
    ok &= check("...and nothing else is ever solved",
                not any(page_flow.should_solve(s)
                        for s in ("content", "empty", "shell", "blocked")))
    ok &= check("the solver knows Cloudflare Turnstile",
                hasattr(captcha_solver, "turnstile_task_for")
                and hasattr(captcha_solver, "TURNSTILE_INTERCEPT_JS"))
    ok &= check("...and enterprise reCAPTCHA, which is a separate task type",
                "RecaptchaV2EnterpriseTaskProxyless"
                in open(os.path.join(REPO_ROOT, "captcha_solver.py"),
                        encoding="utf-8").read())
    for engine in ENGINES:
        src = _engine_source(engine)
        if src is None:
            continue
        # A Challenge page publishes NO sitekey, so the interception has to
        # be installed before any page script runs. An engine that imported
        # the script and never installed it would look capable and not be.
        ok &= check(f"{engine} installs the Turnstile interception",
                    "TURNSTILE_INTERCEPT_JS" in src
                    and src.count("TURNSTILE_INTERCEPT_JS") >= 2)
    ok &= check("a sitekey-less detection raises rather than being paid for",
                _raises(lambda: captcha_solver.turnstile_task_for(
                    captcha_solver.CaptchaChallenge(kind="turnstile",
                                                    sitekey=""))))
    return ok


def test_page_flow_policy():
    group("STATE_POLICY — the triage as DATA, not three if-chains")
    ok = True
    for state in ("content", "empty", "shell", "challenge", "blocked"):
        ok &= check(f"{state} has a full policy row",
                    set(page_flow.STATE_POLICY[state]) ==
                    {"parse", "retry", "solve", "blocked"})
    ok &= check("content is parsed and not retried",
                page_flow.should_parse("content") and not page_flow.should_retry("content"))
    ok &= check("empty is a final answer, not a fault",
                not page_flow.should_parse("empty") and not page_flow.should_retry("empty"))
    ok &= check("shell is parsed after the wait, never refetched",
                page_flow.should_parse("shell") and not page_flow.should_retry("shell"))
    # THE DIFFERENCE FROM EVERY SIBLING REPO, and it was found by running
    # the thing (§15). A challenge is retried first; if the retries are spent
    # and it is still a challenge, the run is BLOCKED (exit 3), not empty
    # (exit 4). With this False the first live run parsed the 6 KB
    # interstitial as a feed and reported "ran fine, found nothing" on a
    # topic holding hundreds of answers.
    ok &= check("a challenge that survives its retries counts as blocked",
                page_flow.counts_as_blocked("challenge") is True)
    ok &= check("but it is retried before that verdict is reached",
                page_flow.should_retry("challenge") is True)
    ok &= check("and it is never parsed",
                page_flow.should_parse("challenge") is False)
    ok &= check("blocked counts towards exit 3",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("an unknown state falls back to the blocked row",
                page_flow.should_parse("nonsense") is False)

    group("Retrying a block DOES help here — and the engines CONSULT that")
    # A policy constant nothing reads is the same defect as dead code (§17).
    ok &= check("RETRY_ON_BLOCKED is True on this site",
                page_flow.RETRY_ON_BLOCKED is True)
    ok &= check("the budget is non-zero", page_flow.BLOCK_RETRIES_WITHOUT_POOL > 0)
    ok &= check("a pool buys more attempts",
                page_flow.BLOCK_RETRIES_WITH_POOL >= page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    consulted = []
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        consulted.append("RETRY_ON_BLOCKED" in src)
    ok &= check("every engine reads RETRY_ON_BLOCKED", all(consulted))

    group("Readiness — a count poll, never an evaluated string")
    ok &= check("MIN_CARD_MATCHES is above 1 (§5)", page_flow.MIN_CARD_MATCHES > 1)
    ok &= check("the anchor is the event link itself, in every mode",
                all(page_flow.ready_selector(m) == SELECTORS["item_card"]
                    for m in ("markets", "events", "event")))
    ok &= check("min_matches is clamped by what the caller knows is there",
                page_flow.min_matches("event", 1) == 1)
    ok &= check("and is not raised above the floor",
                page_flow.min_matches("event", 50) == page_flow.MIN_CARD_MATCHES)
    # The site publishes the size of the whole CATALOGUE — 21,511 events
    # against a page of twenty — and never a per-page counter. It is recorded
    # beside the run and never used as an expectation: reading it as a gap
    # would report twenty-one thousand missing rows on a page that rendered
    # everything it was ever going to.
    ok &= check("the catalogue total is read where the site gives it",
                page_flow.total_events_on_page(fixture("listing")) > 20000)
    ok &= check("an event page publishes no such count",
                page_flow.total_events_on_page(fixture("event_multi")) is None)
    ok &= check("page_gap is None, always, and not 0",
                page_flow.page_gap(fixture("listing"), 21) is None)

    group("The payload answers before anything is waited for")
    # The fast path, and the reason a run takes seconds rather than minutes:
    # a page that shipped its markets needs no readiness wait and no scroll.
    ok &= check("a served listing is recognised as already answered",
                page_flow.payload_answered(fixture("listing")) is True)
    ok &= check("...and an event page too",
                page_flow.payload_answered(fixture("event_multi")) is True)
    ok &= check("a 404 page has not answered",
                page_flow.payload_answered(fixture("not_found")) is False)
    for engine_name in ENGINES:
        engine_text = _engine_source(engine_name)
        if engine_text is None:
            continue
        ok &= check(f"{engine_name} takes the fast path",
                    "payload_answered" in engine_text)

    calls = []

    def count(_sel):
        calls.append(1)
        return 0 if len(calls) < 4 else 9

    found = page_flow.wait_for_count(count, lambda ms: None, "x", 5, 5_000)
    ok &= check("wait_for_count returns the count it reached", found == 9)
    ok &= check("a wait that never satisfies still returns, bounded",
                page_flow.wait_for_count(lambda s: 0, lambda ms: None, "x", 5, 400) == 0)
    return ok


def test_scroll_loop():
    group("The scroll — the WINDOW, and THREE stable rounds (§8)")
    ok = True
    # The opposite of a sibling repo, whose body never scrolled. Here the
    # window is what moves, and every engine must scroll to the document's
    # own end rather than by a fixed wheel distance — a fixed wheel stopped
    # three rounds short of the bottom on that sibling's grid, so the
    # lazy-load trigger was never reached and a run took 30 of 50 cards while
    # looking settled.
    for engine in ENGINES:
        src = _engine_source(engine)
        if src is None:
            continue
        ok &= check(f"{engine} scrolls to document.body.scrollHeight",
                    "document.body.scrollHeight" in src)
        ok &= check(f"{engine} has no inner scroll container to chase",
                    "scroll_container" not in src)

    # A pause is not an ending: the next batch takes longer to arrive than a
    # single pause, so the loop needs three quiet rounds rather than one.
    counts = [3, 7, 13, 13, 13, 13, 13, 13]
    state = {"i": 0}
    heights = {"h": 1000}

    def count(_sel):
        return counts[min(state["i"], len(counts) - 1)]

    def scroll():
        state["i"] += 1
        heights["h"] += 500

    trace = page_flow.scroll_until_settled(
        count, scroll, lambda: heights["h"], lambda ms: None)
    ok &= check("the loop settles on the count the feed stopped at",
                trace["cards_after"] == 13)
    ok &= check("and records where it started", trace["cards_before"] == 3)
    ok &= check("and how many rounds it spent", trace["rounds"] >= 3)
    ok &= check("three stable rounds, not one",
                page_flow.SCROLL_STABLE_ROUNDS >= 3)

    # The measured case on THIS site: the grid never grows. Twelve rounds of
    # scrolling added zero events, headless and headful alike, with the
    # document height unchanged at 3,654px. The loop must settle and stop
    # rather than spending the whole budget chasing it.
    rounds = {"n": 0}

    def scroll2():
        rounds["n"] += 1

    trace2 = page_flow.scroll_until_settled(
        lambda s: 7, scroll2, lambda: 500, lambda ms: None)
    ok &= check("a feed that never grows settles and stops",
                trace2["cards_after"] == 7)
    ok &= check("and does not spend the whole budget",
                rounds["n"] <= page_flow.SCROLL_STABLE_ROUNDS + 1)

    # The scroll is a FALLBACK here and not the main event: the engines only
    # spend it when the payload did not answer. A loop that ran on every page
    # would cost four minutes a run proving what is already measured.
    ok &= check("the round budget is small, because nothing grows",
                page_flow.SCROLL_MAX_ROUNDS <= 6)

    # A feed that grows forever must not hang the run — and on an infinite
    # scroll that is not a hypothetical.
    forever = {"n": 0}

    def count3(_sel):
        forever["n"] += 1
        return forever["n"]

    page_flow.scroll_until_settled(count3, lambda: None,
                                   lambda: forever["n"] * 10,
                                   lambda ms: None)
    ok &= check("a forever-growing feed is bounded by the round budget",
                forever["n"] <= page_flow.SCROLL_MAX_ROUNDS * 2 + 2)
    return ok


def test_one_page_is_complete_not_partial():
    group("One page is a COMPLETE answer to this URL (§7, from both sides)")
    ok = True
    # The family's most expensive bug was a dead next-page selector: one page
    # of three, exit 0, nobody noticed for months. The defence here cannot be
    # "report partial", because one page really is everything this URL has —
    # so it is "say so, loudly, in three places".
    ok &= check("the stop reason is its own, not `completed`",
                "listing_has_one_page" in COMPLETE_STOP_REASONS)
    for engine in ENGINES:
        src = _engine_source(engine)
        if src is None:
            continue
        ok &= check(f"{engine} sets it rather than staying silent",
                    "listing_has_one_page" in src)
        ok &= check(f"{engine} prints the note at parse time too",
                    "one_page_note" in src)

    group("A deeper reading of a market replaces a shallower one")
    # In --mode events page 1 names every market from the listing and pages
    # 2..N carry those same markets from their own event pages. Keeping the
    # first would throw away exactly the columns the extra fetches were for.
    shallow = Market(sku="m1", price=0.5, data_source="flight")
    deep = Market(sku="m1", price=0.5, condition_id="0xabc",
                  data_source="flight")
    merged, new_per_page, upgraded = merge_pages([(1, [shallow]), (2, [deep])])
    ok &= check("the deep row wins", len(merged) == 1
                and merged[0].condition_id == "0xabc")
    ok &= check("...and is counted as an upgrade, not as a new row",
                upgraded.get(2) == 1 and new_per_page.get(2) == 0)
    ok &= check("a shallow row never replaces a deep one",
                merge_pages([(1, [deep]), (2, [shallow])])[0][0].condition_id
                == "0xabc")
    ok &= check("rows arriving out of order still merge in page order",
                merge_pages([(2, [Market(sku="b")]), (1, [Market(sku="a")])])[0][0].sku
                == "a")
    ok &= check("a row with no sku is always kept",
                len(merge_pages([(1, [Market(sku=None), Market(sku=None)])])[0]) == 2)
    return ok


def test_output_contract():
    group("The output contract (§9)")
    ok = True
    ok &= check("every mode yields the same row class",
                set(ROW_CLASS_BY_MODE.values()) == {Market})
    ok &= check("the three modes are the three views",
                set(ROW_CLASS_BY_MODE) == {"markets", "events", "event"})
    ok &= check("every mode is one row per sku",
                set(UNIQUE_BY_SKU_MODES) == set(ROW_CLASS_BY_MODE))
    ok &= check("Product is kept as an alias so family code keeps importing",
                Product is Market)
    ok &= check("JSON and CSV agree on column order",
                [f.name for f in fields(Market)]
                == list(asdict(Market()).keys()))
    ok &= check("source defaults to the main host, never to an empty string",
                SOURCE_DEFAULT == "polymarket.com"
                and Market().source == SOURCE_DEFAULT)
    # The family prefix, byte-identical and in order across six repos (§9).
    ok &= check("the family prefix leads the schema, in order",
                [f.name for f in fields(Market)][:5]
                == ["source", "scraped_at", "url", "sku", "title"])
    # A column that is null on every row of every run should not exist (§9).
    # These are the ones this repo dropped, with the reason in
    # output_writer's docstring.
    absent = {"brand", "original_price", "discount_pct", "rating", "in_stock",
              "review_count"}
    ok &= check("the columns this site has no data for are absent",
                not (absent & {f.name for f in fields(Market)}))

    group("Complete stop reasons")
    ok &= check("`completed` is complete", "completed" in COMPLETE_STOP_REASONS)
    ok &= check("`no_new_products` is complete — the data-side condition",
                "no_new_products" in COMPLETE_STOP_REASONS)
    ok &= check("one page of a one-page listing is complete",
                "listing_has_one_page" in COMPLETE_STOP_REASONS)
    ok &= check("a refused batch is NOT a complete stop reason",
                "next_batch_refused" not in COMPLETE_STOP_REASONS)
    ok &= check("nor is a block",
                not any(r.startswith("blocked") for r in COMPLETE_STOP_REASONS))
    return ok


def test_writers_and_finish_run():
    group("Writers")
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        # A run that finds nothing writes NOTHING — never replacing last
        # night's good output with [].
        with open(prefix + ".json", "w") as f:
            f.write('[{"sku": "keep-me"}]')
        rc = save([], prefix, "both")
        ok &= check("0 rows returns exit 4", rc == EXIT_NO_PRODUCTS)
        ok &= check("0 rows leaves the previous good output alone",
                    "keep-me" in open(prefix + ".json").read())
        rc = save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out", rc == EXIT_NO_PRODUCTS
                    and json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(tmp, "empty.csv")
        write_csv([], csv_path, row_cls=Market)
        header = next(csv_module.reader(open(csv_path)))
        ok &= check("an empty CSV keeps the header",
                    header == [f.name for f in fields(Market)])

        # `tags` is a LIST, and CSV cannot hold one. The family's writer
        # joins it with LIST_CSV_SEPARATOR so the cell stays readable in a
        # spreadsheet and round-trippable by splitting on the same string —
        # `repr()` of a Python list, which is the default if this is not
        # handled, is neither readable nor parseable by anything but Python.
        row = next(r for r in rows_of("listing") if r.tags)
        csv2 = os.path.join(tmp, "rows.csv")
        write_csv([row], csv2, row_cls=Market)
        body = list(csv_module.DictReader(open(csv2)))[0]
        ok &= check("tags is the one list column", isinstance(row.tags, list))
        ok &= check("...and the CSV joins it rather than repr-ing it",
                    body["tags"] == LIST_CSV_SEPARATOR.join(row.tags))
        ok &= check("...and it round-trips by splitting on the same string",
                    body["tags"].split(LIST_CSV_SEPARATOR) == row.tags)
        # Three list columns on this site, not one: the outcomes, their
        # prices and the CLOB token ids are index-aligned lists by design,
        # and the writer has to join every one of them.
        list_columns = {"tags", "outcomes", "outcome_prices", "clob_token_ids"}
        ok &= check("every OTHER column is scalar",
                    not any(isinstance(getattr(row, f.name), (list, dict))
                            for f in fields(Market)
                            if f.name not in list_columns))
        deep_row = rows_of("event_multi")[0]
        csv3 = os.path.join(tmp, "deep.csv")
        write_csv([deep_row], csv3, row_cls=Market)
        deep_body = list(csv_module.DictReader(open(csv3)))[0]
        ok &= check("the outcomes list is joined, not repr-ed",
                    deep_body["outcomes"]
                    == LIST_CSV_SEPARATOR.join(deep_row.outcomes))
        ok &= check("...and so are the CLOB token ids",
                    deep_body["clob_token_ids"]
                    == LIST_CSV_SEPARATOR.join(deep_row.clob_token_ids))
        ok &= check("the CSV round-trips the sku",
                    body["sku"] == row.sku)
        ok &= check("the list separator is still available for the family",
                    bool(LIST_CSV_SEPARATOR))

        group("finish_run — the status/exit mapping all three engines share")
        def run(rows, stop_reason, blocked=False, allow_empty=False):
            out = os.path.join(tmp, f"r{abs(hash(stop_reason))}{len(rows)}{blocked}")
            code = finish_run(rows, out, "json", allow_empty, blocked=blocked,
                              stop_reason=stop_reason, pages_requested=2,
                              pages_completed=1, start_url="u", final_url="u")
            meta_path = out + ".meta.json"
            meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
            return code, meta

        rows = rows_of("listing")
        code, meta = run(rows, "completed")
        ok &= check("a finished run is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "next_batch_refused")
        ok &= check("a REFUSED batch is partial, exit 6",
                    code == EXIT_PARTIAL and meta["status"] == "partial")
        code, meta = run(rows, "no_new_products")
        ok &= check("a feed that added nothing new is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "pagination_exhausted")
        ok &= check("a feed that ran out is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run([], "blocked_cloudflare", blocked=True)
        ok &= check("blocked with no rows is exit 3", code == EXIT_BLOCKED)
        ok &= check("a FAILED run writes no sidecar beside good data",
                    meta is None)
        code, meta = run([], "completed")
        ok &= check("empty and not blocked is exit 4", code == EXIT_NO_PRODUCTS)

        # THE DISTINCTION A LIVE DEAD PROXY FOUND. Zero rows has three
        # causes and they are not the same thing (§8: blocked is not empty).
        # This one used to return exit 4 — "ran fine, found nothing" — on a
        # run that never reached the site at all, while the sidecar beside
        # it correctly said `status: failed`, `pages_completed: 0`. A
        # pipeline branching on the exit code, which is what this family
        # says exit codes are for, would have recorded an empty catalogue.
        code, meta = run([], "page_load_timeout", allow_empty=True)
        ok &= check("0 rows because nothing was FETCHED is exit 5, not 4",
                    code == EXIT_FETCH_FAILED)
        ok &= check("and the sidecar says failed, not complete",
                    meta is not None and meta["status"] == "failed")
        code, meta = run([], "next_batch_refused", allow_empty=True)
        ok &= check("a refused batch with no rows is exit 5 too",
                    code == EXIT_FETCH_FAILED)
        code, meta = run([], "blocked_cloudflare", blocked=True, allow_empty=True)
        ok &= check("but a BLOCK still outranks both, at exit 3",
                    code == EXIT_BLOCKED)

        group("The sidecar records WHICH pages failed, by number")
        out = os.path.join(tmp, "meta")
        finish_run(rows, out, "json", False, blocked=False,
                   stop_reason="blocked_x", pages_requested=5, pages_completed=3,
                   pages_failed=[2, 4], start_url="u", final_url="u",
                   mode="markets", source="polymarket.com",
                   extra={"rows_new_per_page": {2: 10},
                          "pagination": "single-page"})
        meta = json.load(open(out + ".meta.json"))
        ok &= check("pages_failed is a list of numbers", meta["pages_failed"] == [2, 4])
        ok &= check("mode and source are recorded",
                    meta["mode"] == "markets"
                    and meta["source"] == "polymarket.com")
        ok &= check("the per-page row counts ride in the sidecar",
                    meta["rows_new_per_page"] == {"2": 10})
        ok &= check("and so does the pagination model, which a consumer "
                    "needs before comparing two runs",
                    meta["pagination"] == "single-page")
        ok &= check("extra cannot overwrite a run field",
                    meta["status"] == "partial")

    group("Merging and dedupe")
    seen = set()
    p1 = rows_of("listing", 1)
    ok &= check("a fresh batch keeps every row",
                len(dedupe_by_key(p1, seen)) == len(p1))
    # EXPECTED in --mode events rather than exceptional: page 1 is the
    # listing and names every market shallowly, so each event page re-states
    # markets page 1 already wrote. `merge_pages` is what keeps the DEEPER
    # reading of those; this plain dedupe is what the single-page modes use.
    ok &= check("the same listing again is fully dropped",
                dedupe_by_key(rows_of("listing", 2), seen) == [])
    ok &= check("dedupe_by_sku is the same function",
                dedupe_by_sku([], set()) == [])
    # A row with no key is always kept: there is nothing to check a duplicate
    # against, and dropping it would be a silent data loss.
    keyless = [Market(sku=None, title="a"), Market(sku=None, title="b")]
    ok &= check("keyless rows are kept, not collapsed",
                len(dedupe_by_key(keyless, set())) == 2)
    return ok


def test_diff():
    group("diff_runs — what counts as a change here")
    ok = True
    # THE CHECK THAT WOULD HAVE CAUGHT IT. This file arrived from a sibling
    # repo tracking `claps`, `reading_time_min` and `publication` — none of
    # which is a column here — so `diff_runs.py` compared eight fields absent
    # from every row of both runs and reported "no changes" for ever. Nothing
    # failed, because the checks below exercise the diff's MECHANICS with
    # hand-built rows and never ask whether its subject is real (§16).
    columns = {f.name for f in fields(Market)}
    import diff_runs as _diff
    unreal = sorted(set(_diff.TRACKED_FIELDS) - columns)
    ok &= check(f"every tracked field is a real column {unreal or ''}", not unreal)
    stray = sorted(set(_diff.COUNT_FIELDS) - set(_diff.TRACKED_FIELDS))
    ok &= check(f"every count field is also tracked {stray or ''}", not stray)
    # And the columns a diff MUST watch on this site, named rather than
    # counted: a price monitor that failed to notice a market closing, or a
    # price moving, would be worth nothing.
    for essential in ("price", "closed", "volume", "title"):
        ok &= check(f"...and {essential} is among them",
                    essential in _diff.TRACKED_FIELDS)

    def row(**kw):
        base = dict(sku="will-the-fed-cut-in-september",
                    title="Will the Fed cut rates in September?",
                    price=0.62, outcome_prices=[0.62, 0.38],
                    best_bid=0.61, best_ask=0.63, spread=0.02,
                    last_trade_price=0.62, volume=1_000_000.0,
                    volume_24h=50_000.0, liquidity=120_000.0,
                    volume_scope="market", active=True, closed=False,
                    accepting_orders=True, end_date="2026-09-30T00:00:00Z",
                    data_source="flight")
        base.update(kw)
        return base

    out = diff_products([row()], [row(price=0.71, outcome_prices=[0.71, 0.29])])
    ok &= check("a real price move, same view, is `changed`",
                len(out["changed"]) == 1 and not out["source_changed"])
    ok &= check("...and the bucket names the column that moved",
                "price" in out["changed"][0]["changes"])

    # THE BUCKET THIS SITE NEEDS MOST. A row read off a listing has no spread
    # and carries the EVENT's volume; the same market read off its own event
    # page has a spread and the MARKET's volume. That is our two snapshots
    # differing, not the site.
    out = diff_products([row(spread=None, volume_scope="event",
                             volume=199_000_000.0, data_source="flight+jsonld")],
                        [row()])
    ok &= check("a column appearing with the view is `source_changed`, "
                "not `changed`",
                len(out["source_changed"]) == 1 and not out["changed"])
    ok &= check("the bucket names both views",
                out["source_changed"][0]["data_source"]
                == {"old": "flight+jsonld", "new": "flight"})

    # A DOM-sourced run against a payload run is the same artefact, and the
    # more likely one: a DOM row is event-level with a rounded volume.
    out = diff_products([row(data_source="dom", volume=200_000_000.0,
                             spread=None, best_bid=None, best_ask=None)],
                        [row()])
    ok &= check("a DOM run diffed against a payload run is `source_changed`",
                len(out["source_changed"]) == 1 and not out["changed"])

    # But a MARKET CLOSING alongside is a real event and must not be
    # swallowed by the same bucket. It is the most consequential thing that
    # can happen to a row and no price column shows it.
    out = diff_products([row(data_source="flight+jsonld", spread=None)],
                        [row(closed=True, active=False,
                             accepting_orders=False)])
    ok &= check("a market closing survives a view change",
                len(out["changed"]) == 1
                and "closed" in out["changed"][0]["changes"])

    # And so is the question being re-worded, which changes what the row
    # MEANS without touching its price.
    out = diff_products([row(data_source="flight+jsonld", spread=None)],
                        [row(title="Will the Fed cut rates by 50bps in "
                                   "September?")])
    ok &= check("a re-worded question survives a view change",
                len(out["changed"]) == 1
                and "title" in out["changed"][0]["changes"])

    group("The tolerance, which unlike the family's has a real use here")
    # A prediction market's price moves continuously: two runs minutes apart
    # differ by a tick on most rows, and a monitor alerted on every tick is a
    # monitor nobody reads.
    out = diff_products([row(price=0.620)], [row(price=0.624)],
                        price_tolerance_pct=1.0)
    ok &= check("a price ticking is `within_tolerance`",
                len(out["within_tolerance"]) == 1 and not out["changed"])
    out = diff_products([row(price=0.620)], [row(price=0.624)])
    ok &= check("and the DEFAULT reports it, deciding nothing for the reader",
                len(out["changed"]) == 1 and not out["within_tolerance"])
    out = diff_products([row(price=0.620)], [row(price=0.900)],
                        price_tolerance_pct=1.0)
    ok &= check("a real repricing is never absorbed by the tolerance",
                len(out["changed"]) == 1 and not out["within_tolerance"])

    group("Added, removed and unmatchable")
    out = diff_products([row()], [row(), row(sku="a-second-market")])
    ok &= check("a new market is `added`", len(out["added"]) == 1)
    out = diff_products([row(), row(sku="a-second-market")], [row()])
    ok &= check("a market that vanished is `removed`", len(out["removed"]) == 1)
    out = diff_products([row(sku=None)], [row(sku=None)])
    ok &= check("a row with no sku is counted, not silently dropped",
                out["unmatchable_old"] == 1 and out["unmatchable_new"] == 1)
    out = diff_products([row(), row()], [row()])
    ok &= check("a duplicate sku in one run is counted as unmatchable",
                out["unmatchable_old"] == 1)
    return ok


def _fail_on_change_source():
    """The `--fail-on-change` condition, as written."""
    src = open(os.path.join(REPO_ROOT, "diff_runs.py"), encoding="utf-8").read()
    match = re.search(r"if args\.fail_on_change and \(([^)]*)\)", src)
    return match.group(1) if match else src


def test_env_config():
    group("env_config — precedence and placeholders")
    ok = True
    ok &= check("every ENV_KEYS value is a real CLI destination",
                set(env_config.ENV_KEYS.values()) ==
                {"twocaptcha_key", "cdp_endpoint", "proxy", "url"})
    # A variable mapped onto a flag with a non-empty default would be
    # silently inert — a setting that looks configurable and is not.
    ok &= check("--out is deliberately NOT mapped",
                "out" not in env_config.ENV_KEYS.values())

    # .env.example must document exactly what the code reads, both ways.
    example = open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    ok &= check("every ENV_KEYS name is in .env.example",
                set(env_config.ENV_KEYS) <= documented)
    ok &= check("every .env.example name is read by the code",
                documented <= set(env_config.ENV_KEYS))

    group("A COPIED .env.example must read as unset (§17)")
    # `cp .env.example .env` followed by a run used to connect with the
    # literal string `{login}-zone-...` as a username and get a 401 — the
    # confusing auth error a long way from its cause that this rule exists to
    # prevent. Round-tripped through the real loader.
    saved = {k: os.environ.get(k) for k in env_config.ENV_KEYS}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write(example)
            with contextlib.redirect_stderr(io.StringIO()):
                env_config.load_env(path, override=True)
                # Every CREDENTIAL in the example must read as unset. The
                # two credentialled URLs are written the way the vendor
                # documents them, so a literal-only placeholder list misses
                # both — `cp .env.example .env` then connected with the
                # string `{login}-zone-...` as a username and got a 401 a
                # long way from its cause (§17).
                for name in ("TWOCAPTCHA_KEY", "POLYMARKET_CDP_ENDPOINT",
                             "POLYMARKET_PROXY"):
                    ok &= check(f"{name} from a copied example reads as unset",
                                env_config.env_value(name) is None)
                # And the non-credential default must still be USABLE, or
                # the check above would pass by making everything unset.
                url = env_config.env_value("POLYMARKET_URL")
            ok &= check("POLYMARKET_URL from the example survives and is usable",
                        url is not None and is_supported_host(url))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return ok


def test_fingerprint_is_read_through_the_shared_helper():
    group("--fingerprint reads the UA through ONE helper (§16)")
    ok = True
    # The defect this pins was live in FOUR sibling repos at once and was
    # live here too, in the Selenium engine, until a live call to the API
    # showed what it returns. The UA is at `userAgent.userAgent` in the
    # chromium format and at `data.ua` in the raw one; `userAgent.value` —
    # which that engine read — exists in NEITHER. So `--fingerprint` set no
    # user agent at all, silently, and the run presented a Windows
    # fingerprint's screen, locale and timezone over a local Chromium's UA.
    # That is the identity MISMATCH the flag exists to avoid.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        if "fingerprint" not in source:
            continue
        ok &= check(f"{engine} does not reach into the response shape itself",
                    'get("userAgent")' not in source
                    and '["userAgent"]' not in source)

    # And the helper itself, against the three shapes the API is known to
    # return. Fixtures rather than a live call: the suite must pass offline.
    ok &= check("chromium format: userAgent.userAgent",
                fingerprint_user_agent({"userAgent": {"userAgent": "UA-1"}}) == "UA-1")
    ok &= check("raw format: data.ua",
                fingerprint_user_agent({"data": {"ua": "UA-2"}}) == "UA-2")
    ok &= check("a bare string is accepted too",
                fingerprint_user_agent({"userAgent": "UA-3"}) == "UA-3")
    ok &= check("and `value`, which the API does NOT return, is still read "
                "rather than being made an error — a response shape this "
                "repo has not seen is not a reason to set no UA",
                fingerprint_user_agent({"userAgent": {"value": "UA-4"}}) == "UA-4")
    ok &= check("nothing recognisable -> None, never a fabricated UA",
                fingerprint_user_agent({"userAgent": {}}) is None)

    group("Its kwargs are ones the driver actually accepts (§10)")
    # An unknown key in new_context(**kwargs) is a TypeError at launch, on
    # the paid path, at runtime.
    fp = {"userAgent": {"userAgent": "UA"},
          "screen": {"width": 1920, "height": 1080},
          "intl": {"contentLocale": "en-US", "timeZone": "America/New_York"}}
    kwargs = playwright_context_kwargs(fp)
    try:
        from playwright.sync_api import Browser
        allowed = set(inspect.signature(Browser.new_context).parameters)
        unknown = sorted(set(kwargs) - allowed)
        ok &= check(f"every context kwarg is a real one "
                    f"{'' if not unknown else unknown}", not unknown)
    except ImportError:
        skips_note = "playwright absent, context-kwarg binding not checked"
        ok &= check(skips_note, True)

    # The locale must come from the fingerprint, not be built out of its
    # country: a sibling family shipped `en-{country}` and gave every German
    # fingerprint the locale `en-DE`, which is not a locale anyone has.
    ok &= check("locale comes from the fingerprint's own intl block",
                kwargs.get("locale") == "en-US")
    ok &= check("and so does the timezone, which was never applied at all "
                "in four sibling repos",
                kwargs.get("timezone_id") == "America/New_York")
    return ok


def test_fingerprint_cache_is_key_aware():
    group("a cached fingerprint must not make a bad key look good")
    ok = True
    import fingerprint_client as fc

    params = {"format": "chromium", "tags": "Windows", "country": "us"}
    a = fc._cache_path("/tmp", params, False, "key-one")
    b = fc._cache_path("/tmp", params, False, "key-two")
    same = fc._cache_path("/tmp", params, False, "key-one")
    # Measured 2026-09-16 before this was fixed: get_fingerprint() with the
    # key "deadbeef"*4 returned fingerprint 5393493 off disk and raised
    # nothing, because a REAL key had cached the same parameters earlier. A
    # user whose fingerprint subscription lapsed would see --fingerprint keep
    # working on their own machine and 401 on a fresh one — §16's "a path
    # that looks like it works", in the one place this family has already
    # been bitten five times.
    ok &= check("two keys do not share a cache entry", a != b)
    ok &= check("the same key is stable across calls", a == same)
    ok &= check("different parameters still differ",
                a != fc._cache_path("/tmp", {**params, "country": "de"},
                                    False, "key-one"))
    # The key must not be recoverable from the path it produces.
    ok &= check("the key never reaches the filename",
                "key-one" not in a and len(pathlib.Path(a).stem) == 16)
    # And the caller must actually pass it — a key-aware helper nobody hands
    # a key to is the §17 defect this family names dead policy.
    src = open(os.path.join(REPO_ROOT, "fingerprint_client.py"),
               encoding="utf-8").read()
    ok &= check("get_fingerprint passes the key to the cache path",
                src.count("_cache_path(cache_dir, params, generate, api_key)") == 2)
    return ok


def test_proxy_pool():
    group("Credentials never reach argv or a log")
    ok = True
    url = "http://user:" + "s3cr3t" + "@exit.example.com:2334"
    masked = mask(url)
    ok &= check("the password is masked", "s3cr3t" not in masked)
    ok &= check("the host and port are KEPT — that is the point of the log",
                "exit.example.com" in masked and "2334" in masked)
    scrubbed, credentials = split_credentials(url)
    ok &= check("split_credentials strips them from the address",
                "s3cr3t" not in scrubbed and credentials == ("user", "s3cr3t"))
    pw = to_playwright(url)
    ok &= check("Playwright gets them in its own fields, not in the server URL",
                pw["password"] == "s3cr3t" and "s3cr3t" not in pw["server"])

    group("A worker owns one exit; rotation is a fresh browser")
    pool = ProxyPool(["http://a@h1:1", "http://b@h2:2", "http://c@h3:3"])
    first = pool.current
    pool.advance("test")
    ok &= check("advance moves to a different exit", pool.current != first)
    ok &= check("the pool knows its size", len(pool) == 3)
    return ok


def test_credentials_never_reach_a_log():
    group("An EXCEPTION MESSAGE is a log (§8)")
    ok = True
    secret = "hunter2"
    # Concatenated rather than interpolated, so no line in this file holds a
    # complete `scheme://user:pass@host` literal. That keeps ci_checks.py's
    # credential scan meaningful on the one file where a real credential is
    # most likely to be pasted while debugging — an allowlist entry here
    # would switch the check off exactly where it matters.
    endpoint = "ws://user:" + secret + "@cb.2captcha.com:9222"
    for engine in ENGINES:
        try:
            module = __import__(engine)
        except ImportError:
            continue
        masker = getattr(module, "_mask_credentials", None)
        if masker is None:
            ok &= check(f"{engine} has a credential masker", False)
            continue
        # Globally, not once: a Playwright connection error repeats the
        # endpoint five times, and a masker that handles the first prints the
        # password the other four while looking like it works.
        repeated = " ".join([endpoint] * 5)
        ok &= check(f"{engine} masks EVERY occurrence",
                    secret not in masker(repeated))
        ok &= check(f"{engine} keeps the host and port",
                    "cb.2captcha.com:9222" in masker(endpoint))
    # And the solver redacts a key out of an error message, because the
    # fingerprint API takes its key as a query parameter and `requests` puts
    # the full URL into the text of every error it raises.
    key = "a" * 32
    redacted = captcha_solver._redact(f"GET https://x/y?key={key} failed")
    ok &= check("the solver redacts a key from an error message",
                key not in redacted)
    return ok


def test_driver_primitives_tolerate_a_navigation():
    group("Every driver primitive survives the page moving under it")
    ok = True
    # The canary's FIRST dispatch caught this, which is exactly why §15 says
    # to dispatch it once rather than trusting the badge. A scroll batch was
    # polling the card count when the page navigated — Cloudflare's challenge
    # can arrive at any moment here — and one engine raised
    # `Execution context was destroyed, most likely because of a navigation`.
    # Exit 1, a CRASH, where the honest answer was "blocked".
    #
    # Its two twins had guarded the same call from the start. That is the
    # same shape as the refused-GraphQL threshold: two engines agree, one
    # does not, and only a live run in a different environment shows it.
    #
    # Checked as TEXT so this needs no engine library, and by structure
    # rather than by phrasing: the function must contain a try and a return
    # of 0.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "_count":
                continue
            guarded = any(isinstance(child, ast.Try) for child in node.body)
            zero = any(isinstance(child, ast.Return)
                       and isinstance(child.value, ast.Constant)
                       and child.value.value == 0
                       for child in ast.walk(node))
            ok &= check(f"{engine}._count catches the driver's error", guarded)
            ok &= check(f"{engine}._count answers 0 rather than raising", zero)
            break
        else:
            ok &= check(f"{engine} has a _count primitive", False)

    # The other primitives the scroll loop drives, for the same reason.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        tree = ast.parse(source)
        for name in ("_page_height", "_scroll_to_bottom"):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    guarded = any(isinstance(c, ast.Try) for c in node.body)
                    ok &= check(f"{engine}.{name} is guarded too", guarded)
                    break
            else:
                ok &= check(f"{engine} has {name}", False)
    return ok


def test_concurrency_machinery(skips):
    """§10: drive the worker pool with the browser stubbed out.

    A live run cannot reach this. Page 1 is fetched alone and its answer
    decides whether the rest may be addressed, so a blocked page 1 means the
    workers never start — and on this site page 1 is blocked often enough
    that a live test would pass by not running.

    The pool only exists in the Playwright engine (Selenium and pyppeteer
    walk the days one at a time and say so), which is why this group targets
    that engine alone.
    """
    group("the archive worker pool, with no browser in it")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError:
        skips.append("playwright_scraper (playwright not installed)")
        return ok

    import threading
    import types

    class _Args:
        delay = 0
        out = "unused"

    def _run(pages, behaviour, concurrency=3):
        """Drive the real dispatcher against a stubbed session + fetcher.

        `behaviour(page_num)` returns the PageOutcome for that page, or
        raises to simulate a worker dying.
        """
        seen, lock = [], threading.Lock()
        fake_session = types.SimpleNamespace(
            pool=None, close=lambda: None, open=lambda: fake_session)

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                seen.append(page_num)
            return behaviour(page_num, url)

        real_session, real_fetch, real_pw = (
            eng._BrowserSession, eng._fetch_one_page, eng.sync_playwright)
        eng._BrowserSession = lambda *a, **k: fake_session
        eng._fetch_one_page = fake_fetch
        # The dispatcher opens a Playwright context per worker; hand it one
        # that does nothing rather than launching three real browsers.
        #
        # A real CLASS, not a SimpleNamespace with `__enter__` attached:
        # Python looks dunder methods up on the TYPE, so an instance
        # attribute named `__enter__` is never called and `with` raises —
        # which the worker's own except-clause swallows, leaving a test that
        # "passes" against zero workers. It cost this check a debugging pass.
        class _NoPlaywright:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        eng.sync_playwright = _NoPlaywright
        try:
            specs = [(n, "https://medium.com/tag/python/archive/2026/09/%02d" % n)
                     for n in pages]
            return eng._fetch_pages_concurrently(_Args(), None, specs,
                                                 concurrency), seen
        finally:
            eng._BrowserSession, eng._fetch_one_page, eng.sync_playwright = (
                real_session, real_fetch, real_pw)

    def good(page_num, url, rows=3):
        o = eng.PageOutcome(page_num=page_num, url=url)
        o.products = [Market(sku="%012x" % (page_num * 1000 + i), url=url,
                           title="t%d" % i) for i in range(rows)]
        o.state = "content"
        return o

    # 1. Every queued page is fetched EXACTLY once. A page fetched twice is
    #    paid for twice and deduped silently; a page fetched zero times is a
    #    hole the run would report as complete.
    (results, unattempted, exhausted), seen = _run(
        list(range(2, 10)), lambda n, u: good(n, u))
    ok &= check("every queued day is fetched", sorted(seen) == list(range(2, 10)))
    ok &= check("...exactly once", len(seen) == len(set(seen)))
    ok &= check("every fetch produced an outcome", len(results) == 8)
    ok &= check("nothing is left unattempted when all succeed", unattempted == [])
    ok &= check("the end-of-listing event did not fire", not exhausted)

    # 2. Outcomes come back in ARRIVAL order and must be restorable to PAGE
    #    order — §8's "merge in page order, not arrival order". With workers
    #    the two genuinely differ.
    ordered = sorted(results, key=lambda o: o.page_num)
    ok &= check("outcomes carry their page number",
                [o.page_num for o in ordered] == list(range(2, 10)))
    ok &= check("...and each kept its own URL",
                all(str(o.page_num).zfill(2) in o.url for o in ordered))

    # 3. A day with no rows ends dispatch. Without this, asking for 40 days
    #    of a tag that published on three fetches 37 empty ones.
    def empty_after_4(page_num, url):
        return good(page_num, url, rows=0 if page_num >= 4 else 3)

    (results, unattempted, exhausted), seen = _run(
        list(range(2, 40)), empty_after_4, concurrency=2)
    ok &= check("an empty day stops dispatch", exhausted)
    ok &= check("...and most of the queue is never fetched", len(seen) < 12)
    ok &= check("...with the unfetched days reported, not counted as failed",
                len(unattempted) == 38 - len(seen))
    ok &= check("unattempted days are page NUMBERS, in order",
                unattempted == sorted(unattempted))

    # 4. A worker that raises must not hang the run and must not take its
    #    siblings' pages with it. This is the one that would otherwise be
    #    discovered as a hung CI job.
    def explode_on_5(page_num, url):
        if page_num == 5:
            raise RuntimeError("simulated worker death")
        return good(page_num, url)

    (results, unattempted, exhausted), seen = _run(
        list(range(2, 8)), explode_on_5, concurrency=2)
    ok &= check("a dying worker does not hang the run", True)  # reaching here IS the check
    ok &= check("...and its siblings' pages still arrive",
                {o.page_num for o in results} >= {2, 3, 4})
    ok &= check("...and nothing claims the dead worker's pages succeeded",
                5 not in {o.page_num for o in results})

    # 5. The pool is refused where it cannot help, and that refusal is DATA
    #    rather than three copies of an if-chain.
    ok &= check("--mode events may use workers",
                page_flow.concurrency_limit(
                    "https://polymarket.com/predictions", "events") is None)
    for url, mode in (("https://polymarket.com/predictions", "markets"),
                      ("https://polymarket.com/politics", "markets"),
                      ("https://polymarket.com/event/fed-762", "event")):
        ok &= check("%s in --mode %s is capped at one worker" % (url, mode),
                    page_flow.concurrency_limit(url, mode) == 1)
        ok &= check("...with a reason naming --mode events",
                    "--mode events" in
                    (page_flow.concurrency_refusal(url, mode) or ""))
    return ok


def test_engine_parity(skips):
    group("The three engines agree — flags, in BOTH directions (§17)")
    ok = True
    flagsets = {}
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        flagsets[engine] = set(re.findall(r'p\.add_argument\("(--[a-z0-9-]+)"', src))

    # The family contract (§9). Every engine must carry all of these.
    contract = {"--url", "--pages", "--category", "--format", "--out", "--delay",
                "--retries", "--retry-delay", "--concurrency", "--proxy",
                "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
                "--proxy-block-retries", "--twocaptcha-key", "--captcha-api",
                "--solve-captcha", "--min-score", "--cdp-endpoint",
                "--allow-empty", "--dump-html", "--headless", "--headful",
                "--mode"}
    for engine, flags in flagsets.items():
        missing = contract - flags
        ok &= check(f"{engine} carries the whole contract "
                    f"{'' if not missing else sorted(missing)}", not missing)

    # And the DOCUMENTED differences, asserted in both directions so closing
    # one needs a README edit rather than a quiet patch.
    documented_extra = {
        # --cdp-connect-timeout is on the two engines that can actually USE
        # an authenticated CDP endpoint. Selenium cannot (chromedriver's
        # debuggerAddress has nowhere to put a password), so a connect
        # timeout there would be a flag for a path that does not exist.
        "playwright_scraper": {"--locale", "--fingerprint", "--fp-tags",
                               "--fp-country", "--browser-channel",
                               "--cdp-connect-timeout"},
        "selenium_scraper": {"--locale", "--fingerprint", "--fp-tags",
                             "--fp-country"},
        "puppeteer_scraper": {"--chromium-path", "--cdp-connect-timeout"},
    }
    for engine, extra in documented_extra.items():
        actual = flagsets[engine] - contract
        ok &= check(f"{engine}'s extra flags are exactly the documented set",
                    actual == extra)

    group("Every shared-module call binds against the real signature (§17)")
    problems = []
    for engine in ENGINES:
        path = os.path.join(REPO_ROOT, f"{engine}.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in SHARED_MODULES:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = None
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                module, name = imported[node.func.id]
                fn = getattr(SHARED_MODULES[module], name, None)
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in SHARED_MODULES):
                fn = getattr(SHARED_MODULES[node.func.value.id], node.func.attr, None)
            if fn is None or not callable(fn):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(k.arg is None for k in node.keywords):
                continue
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            try:
                signature.bind(*[object()] * len(node.args),
                               **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                problems.append(f"{engine}:{node.lineno} {getattr(fn,'__name__','?')}: {exc}")
    ok &= check(f"no call site disagrees with its callee "
                f"{'' if not problems else problems[:3]}", not problems)

    group("Every engine imports its driver at MODULE level (§10)")
    # Without this the module imports cleanly with no driver installed, the
    # skip below never fires, and CI's engine-smoke job cannot notice a
    # broken import.
    drivers = {"playwright_scraper": "playwright",
               "selenium_scraper": "selenium",
               "puppeteer_scraper": "pyppeteer"}
    for engine, driver in drivers.items():
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{engine}.py"),
                              encoding="utf-8").read())
        top_level = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(driver):
                top_level.append(node)
            if isinstance(node, ast.Import):
                top_level += [a for a in node.names if a.name.startswith(driver)]
        ok &= check(f"{engine} imports {driver} at module level", bool(top_level))

    group("The browser channel — unforced here, and that is a measurement")
    # The opposite of a sibling repo, which must drive real Chrome or be
    # refused. Playwright's bundled Chromium was measured serving the full page,
    # so no channel is forced — and pinning that here is what stops someone
    # copying the sibling's default back in and quietly changing what the
    # README's numbers describe.
    try:
        import playwright_scraper as pws
        ok &= check("Playwright forces no browser channel",
                    pws.DEFAULT_BROWSER_CHANNEL is None)
    except ImportError:
        skips.append("playwright_scraper (playwright not installed)")
    sel = _engine_source("selenium_scraper") or ""
    ok &= check("Selenium says there is nothing to choose",
                "there is nothing to choose" in sel)
    pup = _engine_source("puppeteer_scraper") or ""
    # The opposite of what a sibling repo asserts, and it is measured: this
    # engine's OWN bundled Chromium is build 117.0.5938.0, the UA follows the
    # browser's real version, and Medium refused it 3 times out of 3. The
    # engine must say so in its docstring and in its block advice, because a
    # reader who concludes "my address is burned" from that is going to buy a
    # proxy they do not need.
    ok &= check("pyppeteer names the Chromium build it is refused on",
                "117.0.5938.0" in pup)
    ok &= check("...and tells the reader to pass --chromium-path",
                "--chromium-path" in pup)
    ok &= check("...and the README agrees",
                "117.0.5938.0" in open(
                    os.path.join(REPO_ROOT, "README.md"),
                    encoding="utf-8").read())

    group("The coverage floor is one number, not three")
    floors = []
    for engine in ENGINES:
        src = _engine_source(engine)
        match = re.search(r"^FIELD_FLOOR = (\d+)", src or "", re.M)
        floors.append(match.group(1) if match else None)
    ok &= check(f"all three engines share a FIELD_FLOOR ({floors[0]})",
                len(set(floors)) == 1 and floors[0] is not None)

    group("Engines import cleanly (skipped if the driver is absent)")
    for engine in ENGINES:
        try:
            __import__(engine)
            ok &= check(f"{engine} imports", True)
        except ImportError as exc:
            skips.append(f"{engine} ({exc})")
            print(f"  SKIP  {engine} — {exc}")
    return ok


def test_module_attributes_exist(skips):
    group("Every `module.name` an engine reaches for actually exists (§17)")
    ok = True
    # The gap the signature-binding check leaves, found by a live run rather
    # than by reading. `_min_matches` in one engine called
    # `page_flow.expected_cards(...)` — a name renamed in the other two and
    # not in that one — and the run died with AttributeError on its FIRST
    # fetch, exit 1. Invisible to import, to --help, to compileall, to the
    # undefined-NAME walk (it is an attribute, not a name) and to 490 green
    # assertions, because nothing but a live fetch reaches that line.
    #
    # This walks every `page_flow.X` and `product_parser.X` in every engine
    # and asserts X is really there. It needs no engine library: the modules
    # being reached INTO are the shared ones, and the reaching files are read
    # as text.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            skips.append(f"{engine} (source missing)")
            continue
        tree = ast.parse(source)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            module = SHARED_MODULES.get(node.value.id)
            if module is None:
                continue
            if not hasattr(module, node.attr):
                missing.append(f"{node.value.id}.{node.attr}")
        ok &= check(f"{engine} reaches for nothing that is not there "
                    f"{'' if not missing else sorted(set(missing))}",
                    not missing)
    return ok


def test_no_dead_public_names():
    group("Every public name in the policy modules has a reader (§17)")
    ok = True
    # §17's check 5, automated. A public name nothing reads is dead code, and
    # a policy CONSTANT nothing reads is worse: the prose beside it reads
    # like enforcement. A sibling repo shipped `RETRY_ON_BLOCKED` with a
    # paragraph of measured justification and no engine consulting it.
    #
    # Scoped to the two modules that hold this repo's decisions, because the
    # family core is shared and its unused corners are another repo's
    # problem. References are counted across the whole repository INCLUDING
    # the defining module, so a helper used only by its own neighbours
    # counts — what this catches is a name with no reader anywhere at all.
    scanned = []
    for path in sorted(pathlib.Path(REPO_ROOT).rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        # Skip local tools and any nested checkout. Matching on RELATIVE
        # parts, not on the absolute path: the absolute one can itself sit
        # under a directory this would otherwise exclude, and then the
        # corpus comes back empty and every name reads as dead — which is
        # how this check first "found" 91 dead names in a healthy module.
        if path.name.startswith("_"):
            continue
        if any(part in {"worktrees", ".venv", "venv", "build", "dist"}
               for part in parts):
            continue
        scanned.append(path.read_text(encoding="utf-8"))
    corpus = "\n".join(scanned)
    if len(corpus) < 10_000:
        return check("the dead-name corpus is not empty (it would make "
                     "every name look dead)", False)

    for module in ("product_parser", "page_flow"):
        source = open(os.path.join(REPO_ROOT, module + ".py"),
                      encoding="utf-8").read()
        tree = ast.parse(source)
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                if not node.name.startswith("_"):
                    names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        names.append(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id.isupper():
                    names.append(node.target.id)
        dead = []
        for name in names:
            # Two references minimum: the definition, and at least one read.
            hits = len(re.findall(r"\b" + re.escape(name) + r"\b", corpus))
            if hits < 2:
                dead.append(name)
        ok &= check(f"{module} has no unread public name "
                    f"{'' if not dead else sorted(dead)}", not dead)
    return ok


def test_no_undefined_names():
    group("Names that resolve, not just parse (§10)")
    # `compileall` proves a file PARSES, not that its names RESOLVE. A live
    # run of a sibling repo's engine died with NameError on a line reached
    # only while fetching, after an import had been removed — invisible to
    # import, --help, compileall and 400+ green assertions. Kept COARSE so it
    # under-reports rather than inventing problems.
    ok = True
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith(".py") or name == "smoke_test.py":
            continue
        undefined = _undefined_names(os.path.join(REPO_ROOT, name))
        ok &= check(f"{name}: no undefined names "
                    f"{'' if not undefined else sorted(undefined)[:5]}", not undefined)
    return ok


def _undefined_names(path):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    bound = set(dir(__builtins__) if not isinstance(__builtins__, dict)
                else __builtins__.keys())
    bound |= set(dir(__import__("builtins")))
    # Module-level dunders are always bound and are not imports.
    bound |= {"__file__", "__name__", "__doc__", "__package__", "__spec__",
              "__loader__", "__builtins__", "__debug__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for arg in (args.posonlyargs + args.args + args.kwonlyargs):
                    bound.add(arg.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


def test_dockerfile_matches_its_entrypoint():
    group("The Dockerfile COPY list against the import graph (§10)")
    # All three repos in this family once shipped an image that died with
    # ModuleNotFoundError on every invocation, --help included, because one
    # module was missing from an explicit COPY list. This check needs no
    # Docker.
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("a Dockerfile exists", False)
    dockerfile = open(path, encoding="utf-8").read()
    # Join backslash continuations first: the COPY list spans five lines, and
    # a line-by-line reader sees an empty list and passes vacuously.
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    copied = set()
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            # [1:-1]: the first token is COPY and the LAST is the
            # destination. Including the destination made `./` look like
            # "copy everything" and the check passed vacuously.
            for token in line.split()[1:-1]:
                if token.endswith(".py"):
                    copied.add(os.path.basename(token))
                elif token in ("./", "."):
                    copied.update(n for n in os.listdir(REPO_ROOT)
                                  if n.endswith(".py"))

    # Walk the entrypoint's own import graph.
    entrypoints = [n for n in ENGINES if f"{n}.py" in dockerfile]
    if not entrypoints:
        entrypoints = ["playwright_scraper"]
    needed, queue = set(), list(entrypoints)
    local = {n[:-3] for n in os.listdir(REPO_ROOT) if n.endswith(".py")}
    while queue:
        module = queue.pop()
        if module in needed:
            continue
        needed.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{module}.py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in local:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                queue += [a.name for a in node.names if a.name in local]
    missing = {f"{m}.py" for m in needed} - copied
    ok &= check(f"every module the entrypoint imports is COPYed "
                f"{'' if not missing else sorted(missing)}", not missing)

    group("The image carries no secrets and no test material")
    for unwanted in (".env", "smoke_test.py", "fixtures_generated.json",
                     "captures"):
        ok &= check(f"{unwanted} is not COPYed into the image",
                    unwanted not in copied and f"COPY {unwanted}" not in dockerfile)
    return ok


def test_no_file_describes_another_site():
    group("No shipped file still describes a different site (§17)")
    ok = True
    # A sibling repo's audit found "a shipped file still described another
    # site", and this repo inherited the same thing FOUR times over: a
    # CONTRIBUTING section about `data-testid="divSRPContentProducts"` and
    # sold counts, a list of invariants about auction lots and reserve
    # prices, a captcha module explaining an Akamai "Access Denied" page, and
    # an issue template about seller feedback scores. All four were copied in
    # with the family core and all four read as authoritative.
    #
    # Nothing here can tell a paragraph about Medium from a paragraph about a
    # rental site in general. What it CAN do is notice the vocabulary of the
    # specific siblings this repo was copied from, which is where the real
    # leakage comes from.
    foreign = {
        "akamai": "a sibling's bot manager",
        "datadome": "a sibling's bot manager",
        "bot or not": "a sibling's challenge page",
        "reserve_price_set": "a sibling's auction column",
        "seller_score": "a sibling's seller column",
        "stay_dates": "a sibling's booking column",
        "fewo-direkt": "a sibling's storefront",
        "stayz.com.au": "a sibling's storefront",
        "vrbo": "a sibling repo",
        "medium.com": "the sibling repo this one was cloned from",
        "foodpanda": "the sibling repo the solver came from",
        "tokopedia": "a sibling repo",
        "catawiki": "a sibling repo",
        "craigslist": "a sibling repo",
        "mediamarkt": "a sibling repo",
        "farfetch": "a sibling repo",
        "divsrpcontentproducts": "a sibling's grid selector",
        "lodging-card-responsive": "a sibling's card selector",
        # VOCABULARY, not just names. A paragraph copied from a sibling keeps
        # its subject's words long after the site's name has been swapped
        # out, and those words are what makes it authoritative and wrong.
        # `scraper_api_client.py` logged "Parsed 70 stor(ies)" through a full
        # live run, and `diff_runs.py` tracked `claps` and `reading_time_min`
        # — columns that do not exist here — while every check stayed green.
        #
        # Every word below was COUNTED across this repo before being banned
        # (§18: a marker that matches a page you know is good is worse than
        # no marker). Words that DO occur legitimately here — "answer",
        # "publication", "subtitle" — are deliberately absent from this list.
        "stor(ies)": "a sibling's row",
        "day archive": "a sibling's pagination",
        "archive day": "a sibling's pagination",
        "reading_time_min": "a sibling's column",
        "word_count": "a sibling's column",
        "claps": "a sibling's column",
        "upvotes": "a sibling's column",
        "reserve_price": "a sibling's column",
        "seller_score": "a sibling's column",
        "bid_kind": "a sibling's column",
        "parse_posts": "a sibling's parser entry point",
        "listing_kind": "a sibling's URL classifier",
    }
    # A CONTEXT allowlist, the same shape ci_checks.py uses for credentials,
    # because one of these words is legitimate in exactly one place. §8 says
    # captcha DETECTION stays broad — which challenge a visitor meets depends
    # on the exit and on what the address has been doing — so
    # `BOT_CHALLENGE_MARKERS` names vendors this site has never served, on
    # purpose. That is a marker list, not a description of the site, and the
    # difference is the whole point of this check.
    allowed = {("product_parser.py", "datadome"),
               # The same reason, in the two files that NAME the marker set:
               # captcha_solver's docstring lists what was scanned for and
               # found absent, and the engines' captcha docstrings repeat it.
               ("captcha_solver.py", "datadome"),
               ("playwright_scraper.py", "datadome"),
               ("README.md", "datadome"),
               # diff_runs.py's own post-mortem NAMES the three columns it
               # used to track, and naming them is what makes the comment
               # worth reading. Allowed here and nowhere else, because the
               # regression it describes is caught by a sharper check a few
               # lines up — every name in TRACKED_FIELDS must be a real
               # column — rather than by this vocabulary sweep.
               ("diff_runs.py", "claps"),
               ("diff_runs.py", "reading_time_min"),
               ("diff_runs.py", "upvotes"),
               # The CHANGELOG entry that RECORDS the removal names what was
               # removed, which is the whole value of the entry. Scoped to
               # these four words: a site NAME in the CHANGELOG is still
               # banned, and so is any other sibling vocabulary.
               ("CHANGELOG.md", "stor(ies)"),
               ("CHANGELOG.md", "claps"),
               ("CHANGELOG.md", "day archive"),
               ("CHANGELOG.md", "parse_posts")}

    checked = 0
    for path in sorted(pathlib.Path(REPO_ROOT).rglob("*")):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file() or path.suffix not in (".py", ".md", ".yml",
                                                     ".yaml", ".toml",
                                                     ".example"):
            continue
        if any(part in {"worktrees", ".venv", "venv", "build", "dist", ".git"}
               for part in rel.parts) or path.name.startswith("_"):
            continue
        # The suite names these words in order to ban them, so it cannot be
        # scanned for them without failing on its own check.
        if path.name == "smoke_test.py":
            continue
        checked += 1
        lowered = path.read_text(encoding="utf-8", errors="replace").lower()
        hits = sorted({word for word in foreign
                       if word in lowered
                       and (path.name, word) not in allowed})
        ok &= check(f"{rel} describes this site "
                    f"{'' if not hits else hits}", not hits)
    ok &= check(f"…and {checked} files were actually scanned", checked > 20)
    return ok


def test_wording():
    group("Wording enforced by a test (§12)")
    ok = True
    banned = {
        "cloud browser": "Scraping Browser API",
        "antidetect browser": "Scraping Browser API",
        "gate.2prx.com": "2captcha.com/proxy",
        "2prx.com": "2captcha.com/proxy",
        "--antidetect": "removed",
        "ANTIDETECT_LOCAL_API": "removed",
    }
    # THE WHOLE TREE, not just the top level. This scanned `os.listdir`
    # until the day the repo went public, so `.github/` — the workflows, the
    # issue templates and the repo-metadata file below — was never checked at
    # all. Widened after the banned phrase turned up on a surface no check
    # could see (§21).
    shipped = [path for path in sorted(pathlib.Path(REPO_ROOT).rglob("*"))
               if path.is_file()
               and path.suffix in (".py", ".md", ".txt", ".toml", ".yml",
                                   ".yaml", ".example")
               and ".git" not in path.parts
               and path.name != "fixtures_generated.json"]
    for path in shipped:
        name = str(path.relative_to(REPO_ROOT))
        if path.name == "smoke_test.py":
            continue  # this file names them in order to ban them
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for phrase, instead in banned.items():
            ok &= check(f"{name}: no {phrase!r} (write {instead!r})",
                        phrase.lower() not in text)

    group("What the repo publishes about ITSELF (§2, §21)")
    # A description, topics and a homepage are the first thing anyone reads
    # and the one surface a file-scanning suite cannot reach. Keeping the
    # intended values in a FILE is what puts them back in reach — the checks
    # above have just scanned this one like any other.
    meta_path = os.path.join(REPO_ROOT, ".github", "repo-metadata.yml")
    ok &= check("the published metadata is kept in the repo",
                os.path.exists(meta_path))
    if os.path.exists(meta_path):
        meta = open(meta_path, encoding="utf-8").read()
        description = ""
        for line in meta.splitlines():
            if line.startswith("description: "):
                description = line[len("description: "):].strip()
        words = len(description.split())
        ok &= check(f"...with a description of 15-25 words (it has {words})",
                    15 <= words <= 25)
        ok &= check("...naming the engines a reader chooses between",
                    all(e in description for e in ("Playwright", "Selenium")))
        ok &= check("...and the product by its right name",
                    "Scraping Browser API" in description)
        topics = [l.strip()[2:] for l in meta.splitlines()
                  if l.strip().startswith("- ")]
        ok &= check(f"...and 10-15 topics (it has {len(topics)})",
                    10 <= len(topics) <= 15)
        ok &= check("...the first of which names the site",
                    topics and topics[0] == "polymarket")

    group("Removed flags stay removed — scoped to the ENGINES")
    # --country is banned on a scraper (it could disagree with the URL, and
    # here the storefront IS the hostname) and legitimate on
    # fingerprint_client.py, where it picks a fingerprint locale.
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        ok &= check(f"{engine} has no --country flag",
                    'add_argument("--country"' not in src)
    return ok


def test_no_capture_leaks():
    group("Committed fixtures carry no credential-shaped material (§10)")
    ok = True
    text = open(_FIXTURE_PATH, encoding="utf-8").read()

    # PATTERNS, not the literals one capture happened to contain, so the next
    # capture is caught too.
    #
    # The list is SHORTER than a sibling repo's and that is the site rather
    # than a shortcut: a Polymarket page carries questions about public
    # events, not a person's prose, a display name or a review. What it does
    # carry is a great deal of on-chain material — a market's `conditionId`
    # is a 66-character hex string and its two CLOB token ids are 77-digit
    # integers — and those are PUBLIC, they identify a market rather than a
    # person, and they are the whole reason anyone reads an event page. A
    # blanket "no long hex run" rule would flag every one of them, so the
    # patterns below name the shapes that would actually be a leak.
    patterns = {
        "a session id": r'"sessionId"\s*:\s*"[^"]{8,}',
        "a CSRF token": r"anti-csrftoken",
        "an authorization header": r'"authorization"\s*:\s*"[^"]{8,}',
        "a JWT": r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.",
        "a reCAPTCHA site key": r"\b6L[A-Za-z0-9_-]{30,}",
        "an embedded credential": r"[a-z]+://[^\s\"/@]+:[^\s\"/@]+@",
        "a 2captcha key": r"\b[0-9a-f]{32}\b(?=[^\n]{0,40}(?:key|token|secret))",
    }
    for label, pattern in patterns.items():
        found = re.findall(pattern, text)
        ok &= check(f"no {label} in fixtures_generated.json "
                    f"{'' if not found else found[:2]}", not found)

    # What must SURVIVE the trim, because the checks are about it. A fixture
    # that lost the site's own root element would classify as `blocked`, and
    # every good-page assertion would then be testing the wrong thing.
    for name in GOOD_PAGES:
        ok &= check(f"{name} still looks like a page this site served",
                    served_by_polymarket(fixture(name)))
    ok &= check("the on-chain ids an event page exists for survived",
                all(len(r.clob_token_ids or []) == 2
                    for r in rows_of("event_multi")))
    ok &= check("...and the payload the rows come from is still in there",
                '"outcomePrices"' in product_parser.flight_payload(
                    fixture("listing")))
    ok &= check("the marker matcher still works on a page that has one",
                detect_bot_challenge(
                    "<html>challenges.cloudflare.com/turnstile</html>")
                == "cloudflare")
    return ok


def test_ci_checks_is_wired_up():
    group("One credential check, invoked from CI and from here (§17)")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        # A check nothing runs is not a check; two sources of truth that
        # disagree is worse.
        ok &= check("tests.yml CALLS ci_checks.py rather than reimplementing it",
                    "ci_checks.py" in text)
    if os.path.exists(script):
        # And it must pass on THIS repo. A check that fails on its own
        # repository is a check nobody can read.
        import subprocess
        result = subprocess.run([sys.executable, script, "--all"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check(f"ci_checks.py passes on this repo "
                    f"{'' if result.returncode == 0 else result.stdout[-300:]}",
                    result.returncode == 0)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    for name, loader in (("sample_output.json", json.load),):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            ok &= check(f"{name} exists", False)
            continue
        rows = loader(open(path, encoding="utf-8"))
        ok &= check(f"{name} is a non-empty list", isinstance(rows, list) and rows)
        columns = [f.name for f in fields(Product)]
        ok &= check(f"{name} columns match Product exactly",
                    all(list(r) == columns for r in rows))
        # Fabrication markers — a sample nobody ran reads exactly like one
        # somebody did.
        text = json.dumps(rows)
        for marker in ("example.com", "lorem", "PLACEHOLDER", "TODO", "foo bar"):
            ok &= check(f"{name}: no {marker!r}", marker.lower() not in text.lower())
        ok &= check(f"{name}: every row names the host that served it",
                    all(is_supported_host("https://%s/" % r["source"])
                        for r in rows))
        ok &= check(f"{name}: every sku is a market slug",
                    all(re.fullmatch(r"[a-z0-9][a-z0-9-]{4,}", r["sku"] or "")
                        for r in rows))
        # The sample is cut from a --mode events run ON PURPOSE, so a reader
        # can see the difference the modes make without running anything: the
        # deep rows carry a condition id and both CLOB token ids, the shallow
        # ones carry the event's volume instead of the market's.
        ok &= check(f"{name}: it shows both depths",
                    any(r.get("condition_id") for r in rows)
                    and any(not r.get("condition_id") for r in rows))
        ok &= check(f"{name}: every row carries a price and a currency",
                    all(r.get("price") is not None and r.get("currency")
                        for r in rows))
        ok &= check(f"{name}: page+position unique",
                    len({(r["page"], r["position"]) for r in rows}) == len(rows))
    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = next(csv_module.reader(open(csv_path, encoding="utf-8")))
        ok &= check("sample_output.csv header matches Product",
                    header == [f.name for f in fields(Product)])
    else:
        ok &= check("sample_output.csv exists", False)
    return ok


def test_required_files_are_committed():
    group("Everything the suite needs is tracked by git")
    # A blanket `*.json` / `*.csv` in .gitignore — which this repo wants,
    # because a scraper's own output is large and stale — silently swallowed
    # `fixtures_generated.json`. The suite was green on the machine that
    # wrote it and every CI job died with FileNotFoundError at import. A
    # check that a file EXISTS cannot see that; only asking git can.
    ok = True
    import subprocess
    required = ("fixtures_generated.json", "sample_output.json",
                "sample_output.csv", ".env.example", "README.md",
                "CHANGELOG.md", "Dockerfile", "requirements.txt",
                ".github/ci_checks.py", ".github/workflows/tests.yml",
                ".github/workflows/canary.yml", "tests/test_smoke.py")
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("  SKIP  not a git checkout — cannot verify what is committed")
        return ok
    tracked = set(result.stdout.split())
    for name in required:
        ok &= check(f"{name} is committed, not just present on disk",
                    name in tracked)
    # And the other direction: nothing a scraper produced should be.
    leaked = [f for f in tracked
              if re.search(r"_debug\.(html|png)$|\.meta\.json$|^captures/|^\.env$",
                           f)]
    ok &= check(f"no run output or capture is committed "
                f"{'' if not leaked else leaked[:3]}", not leaked)
    return ok


def test_readme_claims():
    group("README numbers exist, are dated, and match the artefacts")
    ok = True
    path = os.path.join(REPO_ROOT, "README.md")
    if not os.path.exists(path):
        return check("README.md exists", False)
    readme = open(path, encoding="utf-8").read()

    ok &= check("the README names all three modes",
                all(("--mode " + m) in readme
                    for m in ("markets", "events", "event")))
    ok &= check("it says a listing is ONE page",
                "twenty events" in readme.lower()
                or "20 events" in readme)
    ok &= check("...and that the page parameter is IGNORED rather than failing",
                "?page=2" in readme and "ignored" in readme.lower())
    ok &= check("...and names the site's own total beside it",
                "21,511" in readme or "21,550" in readme)
    ok &= check("it says concurrency is refused outside --mode events",
                "--concurrency" in readme)
    ok &= check("it states plainly that no key is needed for this site",
                "no account" in readme.lower() or "without an account" in readme.lower())
    ok &= check("measurements carry a date", "2026-09-16" in readme)

    # THE SENTENCE THIS FAMILY GOT WRONG ONCE (§19): what may be said about a
    # captcha is what THIS REPO implements, never what a solver can do.
    ok &= check("it names the task types this repo implements",
                "TurnstileTaskProxyless" in readme)
    ok &= check("...and says no challenge was met, rather than that none can be",
                "no challenge" in readme.lower())

    group("The README's numbers match the artefacts on disk (§17)")
    # Re-derived from the committed fixtures rather than retyped, so a claim
    # that goes stale fails here rather than in a reader's terminal.
    listing = rows_of("listing")
    deep = rows_of("event_multi")
    ok &= check("no listing row carries a condition id, as stated",
                all(r.condition_id is None for r in listing))
    ok &= check("every event-page row carries one, as stated",
                all(r.condition_id for r in deep))
    ok &= check("...and both CLOB token ids with it",
                all(len(r.clob_token_ids or []) == 2 for r in deep))
    ok &= check("the README says which columns that difference affects",
                all(c in readme for c in ("condition_id", "clob_token_ids",
                                          "spread", "volume_scope")))
    ok &= check("every row of every fixture carries a price, as stated",
                all(r.price is not None
                    for name in GOOD_PAGES for r in rows_of(name)))

    # Every column the README lists must exist, and every column that exists
    # must be listed — in both directions, because a column missing from the
    # list is a column nobody knows they have.
    ok &= check("the README has a Columns section", "### Columns" in readme)
    if "### Columns" in readme:
        listed = set(re.findall(r"`([a-z_0-9]+)`",
                                readme.split("### Columns")[1].split("\n## ")[0]))
        actual = {f.name for f in fields(Market)}
        missing = actual - listed
        extra = {name for name in listed - actual if "_" in name}
        ok &= check(f"every column that exists is listed (missing: "
                    f"{sorted(missing)[:4]})", not missing)
        ok &= check(f"...and nothing is listed that does not exist "
                    f"(extra: {sorted(extra)[:4]})", not extra)

    group("What the paid products are said to buy")
    # None of them is needed to READ this site, and saying so is the
    # positioning §13 asks for: state plainly when the paid path is
    # unnecessary, then list what it actually buys.
    ok &= check("the README names the four 2Captcha products",
                all(term in readme for term in
                    ("Scraping Browser API", "fingerprint", "proxy", "2captcha")))
    ok &= check("...and says what they buy, rather than implying access",
                "volume" in readme.lower())
    return ok


# A FLOOR on how many checks must run, and deliberately not a number this
# repo states anywhere else. The exact count goes stale the next time anyone
# adds a check, and a stale count in a README is worse than no count (§13) —
# so the README says nothing about it and this constant exists only to catch
# checks DISAPPEARING. It came in from a sibling repo at 650 against 648 here,
# which is the same rot in miniature: an inherited number is not a measured
# one. Raise it when it is comfortably passed.
CLAIMED_CHECK_FLOOR = 600


def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    ok = True
    ok &= check("x-debug: the credential and the key are gone",
                      pw not in out and key not in out)
    ok &= check("x-debug: the cost, host and status survive",
                      "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                      and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    ok &= check("x-debug: both credentials are masked, not just the first",
                      s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    ok &= check("x-debug: the log line calls the redactor",
                      'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)
    return ok

def test_scraper_api_payload_and_status():
    """Measured 2026-09-23 against the live Scraper API: `waitFor` sent as
    a JSON-encoded string is answered HTTP 422 and still billed, and the
    response's `status` is the API's own "success" while the target's code
    is `http_code`. Drive the real fetch_html with requests.post stubbed:
    no network, no key spent."""
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403,
                    "headers": {}, "body": "<html></html>"}

    def fake_post(url, **kw):
        captured.update(kw.get("json") or {})
        return _Resp()

    real_post, real_argv = sac.requests.post, sys.argv
    sac.requests.post = fake_post
    sys.argv = ["scraper_api_client.py", "--key", "k" * 8,
                "--url", 'https://polymarket.com/predictions/crypto',
                "--wait-text", 'Crypto']
    try:
        _html, status = sac.fetch_html(sac.parse_args())
    finally:
        sac.requests.post, sys.argv = real_post, real_argv

    ok = True
    ok &= check("scraper API: waitFor is sent as an object, not a JSON string",
                captured.get("waitFor") == {"text": 'Crypto'})
    ok &= check("scraper API: the target status comes from http_code (403), "
                "not the API's own 'success'", status == 403)
    return ok



def main() -> int:
    ok = True
    skips = []

    ok &= test_payload_decoding()
    ok &= test_values_on_real_fixtures()
    ok &= test_the_three_read_paths()
    ok &= test_the_page_number_is_threaded_through()
    ok &= test_event_pages_are_scoped_to_their_event()
    ok &= test_unsupported_urls_are_refused()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_markers_do_not_match_a_good_page()
    ok &= test_the_scraping_browser_injects_a_captcha_and_it_is_not_the_sites()
    ok &= test_the_solver_is_not_declared_useless()
    ok &= test_page_flow_policy()
    ok &= test_scroll_loop()
    ok &= test_one_page_is_complete_not_partial()
    ok &= test_output_contract()
    ok &= test_writers_and_finish_run()
    ok &= test_diff()
    ok &= test_env_config()
    ok &= test_fingerprint_is_read_through_the_shared_helper()
    ok &= test_fingerprint_cache_is_key_aware()
    ok &= test_proxy_pool()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_driver_primitives_tolerate_a_navigation()
    ok &= test_concurrency_machinery(skips)
    ok &= test_engine_parity(skips)
    ok &= test_module_attributes_exist(skips)
    ok &= test_no_dead_public_names()
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_matches_its_entrypoint()
    ok &= test_no_file_describes_another_site()
    ok &= test_wording()
    ok &= test_no_capture_leaks()
    ok &= test_ci_checks_is_wired_up()
    ok &= test_sample_output()
    ok &= test_required_files_are_committed()
    ok &= test_readme_claims()
    ok &= test_x_debug_header_is_redacted()
    ok &= test_scraper_api_payload_and_status()

    passed = _total_checks - len(_failures)
    if passed < CLAIMED_CHECK_FLOOR:
        ok = False
        _failures.append(
            f"this suite is supposed to run over {CLAIMED_CHECK_FLOOR} "
            f"checks and only {passed} ran — either checks were removed or "
            f"the claim needs lowering")

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs each engine in its own "
              "venv and fails if this list is non-empty, because a skip reads "
              "exactly like a passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
