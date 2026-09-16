"""page_flow.py — what to do with the page Polymarket just gave us.

Polymarket answers a request five ways, and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

    content    the page's own payload names markets, or its tiles are
               rendered
    empty      the site's own "Page not found", under a real HTTP 404
    shell      served, built out of the site's own assets, nothing shipped
               or painted yet. Wants a WAIT, not a refetch
    challenge  an interstitial from the edge. Worth RETRYING in a fresh
               context and worth handing to a solver — see the block
               constants below for what is and is not measured about it here
    blocked    a refusal, or a page that is not Polymarket's HTML at all

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

`content` is the NORMAL first state, and it is the first state
---------------------------------------------------------------
Polymarket server-renders everything this scraper wants. The first response
of `/predictions` already carries its twenty events and their ninety-four
markets inside `self.__next_f.push(…)` — before a pixel has painted, before
any scrolling, and identically whether the browser is headless or headful.
Measured 2026-09-16: 94 rows parsed from the raw bytes of a `curl` with no
browser involved at all.

So the readiness wait and the scroll loop below are BOUNDED SAFETY NETS for
one specific case — the payload shape changing under us, which would drop the
run onto the DOM fallback — and not the main event. An engine that parsed the
first response and stopped would already have the data, and the engines here
do exactly that when the payload answers.

That is worth stating plainly because the opposite would be expensive: 12
rounds of scrolling at 2s each on every page, on a site that measurably never
grows a listing by scrolling, is four minutes of nothing per run.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int              how many elements match
    scroll_to_bottom() -> None          scroll the window to the document end
    page_height() -> Optional[int]      document.body.scrollHeight
    sleep(ms) -> None                   wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so this module names the
OPERATION and each engine spells it in its own driver's dialect.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from product_parser import (SELECTORS, PAGE_CAP, CONCURRENCY_REASON,
                            PAGE_STATES, PAGE_URL_REASON, count_cards,
                            detect_block_marker, detect_page_state,
                            flight_payload, is_challenge_page, page_kind,
                            served_by_polymarket, total_events)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
READY_SELECTOR = SELECTORS["item_card"]

# Above 1, per §5: waiting for a single match resolves on the site's own
# navigation — which links five listings above the fold — long before a grid
# paints. Measured first paints: 20 distinct events on `/predictions`, 20 on
# a category listing, and on an event page 14 to 40 links into the rail of
# related events beside the market itself.
MIN_CARD_MATCHES = 2

# Generous against a measured first paint of 2-4s on a 750 KB listing.
CONTENT_TIMEOUT_MS = 25_000


def ready_selector(mode: str = "") -> str:
    """One selector for every mode: every page kind here is made of links to
    events, and there is no stable tile class worth waiting on instead."""
    return READY_SELECTOR


def min_matches(mode: str = "", expected: Optional[int] = None) -> int:
    """How many matches mean "painted".

    `expected` clamps it for a caller that knows better. There is no mode
    that legitimately renders one link: even a single-market event page
    carries its own breadcrumb and its related-events rail.
    """
    floor = MIN_CARD_MATCHES
    if expected is None or expected <= 0:
        return floor
    return max(1, min(floor, expected))


def content_timeout_ms(mode: str = "") -> int:
    return CONTENT_TIMEOUT_MS


def payload_answered(html: Optional[str]) -> bool:
    """Whether the page's own payload already names markets.

    The fast path, and the reason a run of this scraper takes seconds rather
    than minutes: when this is True there is nothing to wait for and nothing
    to scroll, because everything a `markets` run will write is already in
    the bytes the server sent. The engines check it before spending the
    readiness timeout.
    """
    payload = flight_payload(html)
    return bool(payload) and ('"outcomePrices"' in payload
                              or '"markets":[' in payload)


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 500) -> int:
    """Poll until `minimum` elements match, or the timeout runs out.

    Polls through the driver's own element-count primitive rather than
    waiting on an evaluated STRING. That is not a style preference: a sibling
    repo's site ships a Content-Security-Policy without `unsafe-eval`, and
    Playwright's `wait_for_function` — which hands the browser a string to
    evaluate — died there with an `EvalError` and took the whole run down
    with exit 1. Counting elements over the protocol works under any CSP and
    spells the same in all three drivers.

    Polymarket's own CSP was read for this repo and does allow `unsafe-eval`
    today, so nothing here is currently at risk — which is exactly why it is
    written the safe way now rather than after a header changes.

    Returns the count it ended on, whether or not it reached the floor:
    reporting a timeout is not the same as exiting on one (§8).
    """
    waited = 0
    found = count(selector)
    while found < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        found = count(selector)
    if found < minimum:
        logger.info("readiness wait ended at %d matches (wanted %d) after "
                    "%dms", found, minimum, waited)
    return found


# ---------------------------------------------------------------------------
# Lazy loading
# ---------------------------------------------------------------------------
# §8's second case, and on this site it is measured to be a non-event: a
# listing scrolled to the bottom twelve times held exactly the twenty events
# its first response shipped, with the document height unchanged at 3,654px
# throughout, headless and headful alike.
#
# The loop is kept for one reason: the DOM fallback. If the payload shape
# changes and the run drops to reading tiles, a tile that has not painted is
# a row that is not written, and then scrolling is the difference between 20
# rows and fewer. So it runs only when the payload produced nothing, and
# `SCROLL_MAX_ROUNDS` is 4 rather than the family's 12 because there is no
# measured case of this site adding anything after the first round.
#
# Scroll to `document.body.scrollHeight` rather than wheeling a fixed
# distance — a fixed wheel stops short on a long grid and the trigger is
# never reached — and require the count AND the height to hold still for
# THREE rounds, because the next batch takes longer to arrive than a single
# pause.
SCROLL_STABLE_ROUNDS = 3
SCROLL_MAX_ROUNDS = 4
SCROLL_PAUSE_MS = 2_000


def scroll_until_settled(count: Callable[[str], int],
                         scroll_to_bottom: Callable[[], None],
                         page_height: Callable[[], Optional[int]],
                         sleep: Callable[[int], None],
                         selector: str = "",
                         max_rounds: int = SCROLL_MAX_ROUNDS) -> Dict[str, int]:
    """Scroll to the bottom until neither the card count nor the height moves.

    Returns a small trace — rounds spent, cards before and after — which goes
    into the run's sidecar. On this site that trace reads "3 rounds, 20
    cards, 20 cards", and a reader seeing it should conclude the listing is
    twenty events long rather than that the scroll failed: see the module
    docstring.
    """
    selector = selector or READY_SELECTOR
    started = count(selector)
    stable = 0
    last_height = None
    rounds = 0
    while rounds < max_rounds and stable < SCROLL_STABLE_ROUNDS:
        before = count(selector)
        scroll_to_bottom()
        sleep(SCROLL_PAUSE_MS)
        rounds += 1
        height = page_height()
        after = count(selector)
        if after == before and height == last_height:
            stable += 1
        else:
            stable = 0
        last_height = height
    ended = count(selector)
    return {"rounds": rounds, "cards_before": started, "cards_after": ended}


# ---------------------------------------------------------------------------
# Classification and the policy that follows from it
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it (§17). This repo's smoke suite binds every shared-module
    call in every engine for that reason.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, in a fresh context, plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # A 404 for an event slug that does not exist. The site was asked and
    # answered; a second fetch returns the same 404. Not blocked: exit 4 is
    # "ran fine, found nothing", which is exactly what happened (§8).
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served and still painting. Parsed rather than discarded, because by the
    # time an engine asks, the readiness wait has already run — and on this
    # site a page that shipped a payload has already classified as `content`,
    # so reaching `shell` at all means the payload was absent and the DOM is
    # the only thing that can still arrive.
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # Retried in a fresh context, HANDED TO THE SOLVER, and counted as
    # blocked if neither works.
    #
    # `solve: True` here is the one line in this file that differs from the
    # sibling repo this policy was adapted from, and §19 is why. Polymarket
    # sits behind Cloudflare — `server: cloudflare` and a `cf-ray` on every
    # response — and Cloudflare's managed challenge renders a Turnstile,
    # which 2Captcha solves with `TurnstileTaskProxyless`. No challenge was
    # met in the 12 captures + 3 raw fetches taken for this repo, so what is
    # claimed here is a CAPABILITY and not a measurement; the one thing that
    # must never be written is that it cannot be solved, because that would
    # be a sentence about this repo's code dressed up as a fact about a paid
    # product.
    #
    # Nothing is charged for a page that carries no widget: `turnstile_task_
    # for` raises rather than building a task from a sitekey-less detection,
    # which is §8's "detected != paying" with a price tag on it.
    "challenge": {"parse": False, "retry": True,  "solve": True,  "blocked": True},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


# Every state the parser can return has a row here, and every row here is a
# state the parser can return. Asserted at import rather than in a test,
# because a state with no policy falls back to `blocked` — which is the safe
# direction and also the silent one (§17: a policy that looks enforced and is
# not).
assert set(STATE_POLICY) == set(PAGE_STATES), (
    "STATE_POLICY and product_parser.PAGE_STATES disagree: "
    f"{sorted(set(STATE_POLICY) ^ set(PAGE_STATES))}")


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not painted its grid yet."""
    if state != "shell":
        return False
    return served_by_polymarket(html or "")


# ---------------------------------------------------------------------------
# Blocks, and what is actually known about them here
# ---------------------------------------------------------------------------
# NOTHING WAS REFUSED WHILE THIS REPO WAS BUILT, and that is the honest
# headline. From one Hetzner datacentre address in Finland on 2026-09-16:
#
#   12 browser captures across 4 page kinds and 3 locales   all HTTP 200
#   curl with `curl/8.0` as its User-Agent                  200, 791,003 bytes
#   curl claiming `HeadlessChrome/140`                      200, 791,003 bytes
#   curl claiming `python-requests/2.32`                    200, 791,003 bytes
#
# Byte-identical responses to all three: this site does not discriminate on
# the User-Agent and did not rate-limit a normal run. A sibling repo found
# the opposite (11 of 11 refused for a `HeadlessChrome` token), which is
# precisely why it was measured here rather than inherited (§13).
#
# So the constants below are the family's defaults and are NOT measurements
# of this site. They are here because a run that does meet a refusal should
# behave like its siblings rather than inventing something, and they will
# stay unmeasured until someone meets one — at which point the number, the
# date and the address belong in this comment.
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 3
BLOCK_RETRIES_WITH_POOL = 4
SOLVES_PER_PAGE = 1

# Whether a retry has to discard the browser context rather than reload the
# page. True, on the family's rule that a challenge issued against one
# session is not cleared by asking that same session again — and on §8's
# "a rotation is a fresh browser", which is the same rule with a proxy in it.
# Consulted by all three engines; a constant no engine read would be the §17
# defect of a policy that looks enforced and is not.
RETRY_NEEDS_FRESH_CONTEXT = True


def block_advice(html: Optional[str], headless: bool, has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Says what is measured and says what is not. The temptation here is to
    write the sibling repo's ordering — "use a real window first" — but that
    ordering came from ITS measurements, and on this site headless and
    headful were measured identical (20 events both ways). Repeating it would
    be §13's inherited number wearing a fact's clothes.
    """
    marker = detect_block_marker(html or "") or "no vendor marker"
    lead = f"blocked ({marker})"
    hints: List[str] = []

    if is_challenge_page(html or ""):
        lead = f"blocked by a challenge page ({marker})"
        hints.append("a challenge is transient and clears in a FRESH BROWSER "
                     "CONTEXT rather than on a reload — --retries already "
                     "opens a new one between attempts")
        hints.append("--solve-captcha when-blocked (the default) will hand a "
                     "Cloudflare Turnstile or a reCAPTCHA to 2Captcha if one "
                     "is actually rendered; a page carrying no widget is "
                     "reported unsolved rather than charged for")
    else:
        hints.append("no challenge widget was found on the page, so there is "
                     "nothing to solve here — this is a refusal rather than "
                     "a puzzle")

    if headless:
        hints.append("headless and headful were measured IDENTICAL on this "
                     "site (20 events both ways, 2026-09-16), so --headful "
                     "is worth trying but is not the known fix it is on some "
                     "sibling sites")
    if not has_pool:
        hints.append("this site served a plain datacentre address and even a "
                     "`curl/8.0` User-Agent on every attempt measured, so a "
                     "refusal is more likely to be the request RATE than the "
                     "address: raise --delay before reaching for "
                     "--proxy-file")
    else:
        hints.append("with a pool in play, raise --delay before raising the "
                     "request rate: N exits still means N times the traffic")
    return lead + ". " + "; ".join(hints) + "."


# ---------------------------------------------------------------------------
# Pagination — one mode has addresses, two do not
# ---------------------------------------------------------------------------
# `--mode events` walks the `/event/{slug}` pages a listing named. Those are
# real, independent addresses, knowable the moment page 1 is parsed — which
# is what makes `--concurrency` meaningful there and nowhere else (§7).
#
# `--mode markets` and `--mode event` are ONE page each. Not "one page until
# we find the next link": there is no next page, and the three ways that was
# established are in `product_parser`'s docstring. A `?page=2` here is not an
# error, it is IGNORED, and a run built on it would add no new sku, call the
# listing exhausted and report COMPLETE holding one page (§18).
def page_cap_reached(page_num: int) -> bool:
    return page_num >= PAGE_CAP


def one_page_note(url: str, pages_requested: int) -> Optional[str]:
    """The sentence a run prints when it was asked for more than it can get.

    Printed rather than swallowed, because "I asked for 3 pages and got 1"
    with no explanation is indistinguishable from the family's most expensive
    bug — a dead next-page selector quietly returning a third of the data
    with exit 0 (§7). Here the run really did get everything the URL has, and
    the difference has to be visible in the log and in the sidecar.
    """
    if pages_requested <= 1:
        return None
    return (f"--pages {pages_requested} was asked for and this listing has "
            f"exactly one page: Polymarket renders twenty events per listing "
            f"URL and publishes no address for the twenty-first. ?page=2, "
            f"?_p=2 and ?offset=20 each answer with the same twenty events, "
            f"and scrolling adds none (measured 2026-09-16). The run is "
            f"COMPLETE rather than partial — it holds everything this URL "
            f"has. For depth use --mode events, which fetches each of those "
            f"twenty events' own pages; for breadth run more listings "
            f"(--category crypto, politics, sports, …), which are separate "
            f"catalogues rather than pages of one")


def total_events_on_page(html: Optional[str]) -> Optional[int]:
    """How many events the listing holds IN TOTAL, where the site says so.

    21,511 for `/predictions` against the twenty it ships. NOT an expectation
    for this page and never used as a gap — see `page_gap`. It goes in the
    sidecar's `extra` under its own name, so a consumer sees the size of the
    catalogue beside what the run actually read without mistaking one for the
    other.
    """
    return total_events(html)


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def page_gap(html: Optional[str], parsed: int) -> Optional[int]:
    """None, always, on this site — and that is the honest answer.

    §8's rank arithmetic works where a site NUMBERS its items: 30 rows
    spanning ranks 1-50 proves 20 cards never loaded. Polymarket numbers
    nothing, and the only total it publishes is the size of the whole
    catalogue (21,511 against a page of 20). Reading that as a gap would
    claim twenty-one thousand missing rows on a page that rendered
    everything it was ever going to.

    None rather than 0, because an unknown gap is not a gap of zero and the
    two must not read the same in a sidecar (§8).
    """
    return None


def parsed_nothing_from_a_served_page(html: Optional[str], rows: int) -> bool:
    """A page that was SERVED, links to events, and produced no rows.

    That is this parser's bug and not an empty listing, and the two must not
    report the same (§20). The engines give it its own stop reason so a
    reader goes to `product_parser.py` instead of checking their URL.
    """
    if rows or not html:
        return False
    return served_by_polymarket(html) and count_cards(html) >= MIN_CARD_MATCHES


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str = "", mode: str = "") -> Optional[int]:
    """No limit for an `events` walk; 1 for everything else."""
    return None if mode == "events" else 1


def concurrency_refusal(url: str = "", mode: str = "") -> Optional[str]:
    """Why concurrency above 1 is refused for this run.

    Refused WITH the reason rather than silently running one worker, which
    would look like the flag did something.
    """
    if mode == "events":
        return None
    kind = page_kind(url) if url else "listing"
    return (f"{PAGE_URL_REASON}, so {CONCURRENCY_REASON}. This is a "
            f"{kind} page and it is one fetch. Use --mode events to fetch "
            f"the event pages a listing names across workers, or run several "
            f"listings in parallel, one process each")
