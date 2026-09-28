#!/usr/bin/env python3
"""polymarket-scraper — Playwright edition (primary engine)

Scrapes Polymarket prediction markets out of one of three views:

    --mode markets   (default)  a listing page — /predictions,
                                /predictions/{tag}, /{category} or ?q={query}
                                — one row per market on it
    --mode events               the same listing, then EVERY event's own
                                page, for the columns only a detail page has
    --mode event                /event/{slug} — one event, deep

All three yield the same row, because all three are views of the same thing;
see `output_writer.Market`. The mode is inferred from the URL where the URL
settles it, so passing it is a way to be explicit, to ask for the deep walk,
or to be told you are wrong.

There is deliberately no `--language` flag. Polymarket runs one catalogue
behind eighteen locale PATHS (`/es/predictions`, `/zh/event/{slug}`), so a
flag could only disagree with the URL it was given. Put the locale in the URL
and the run follows it; the ids and the prices are the same either way, and
only the wording changes.

WHAT IS DIFFERENT ABOUT THIS SITE
---------------------------------
* **A listing URL is ONE page, and that is measured rather than assumed.**
  `/predictions` renders twenty events and publishes no address for the
  twenty-first: `?page=2`, `?_p=2` and `?offset=20` each answered with the
  same twenty, twelve scroll rounds added none, and the "Show more markets"
  button expands one card rather than the list. The page's own state says
  `"totalCount":21511,"hasNextPage":true` behind an opaque cursor the UI
  never spends. So `--pages` above 1 is reported as COMPLETE with the
  reason, and `--mode events` is how this repo gets depth.

* **Everything is server-rendered, in a payload rather than in the DOM.**
  The twenty events and their ninety-four markets arrive inside
  `self.__next_f.push(…)` — exact volumes, order-book prices, ids and all —
  so a `markets` run parses the first response and stops. The readiness wait
  and the scroll loop are bounded safety nets for the day that payload
  changes shape, not the main event.

* **Nothing refused us.** From one datacentre address on 2026-09-16: 12
  browser captures across 4 page kinds and 3 locales, all HTTP 200, plus
  `curl` claiming `curl/8.0`, `HeadlessChrome/140` and `python-requests` —
  byte-identical 791,003-byte responses to all three. This site does not
  discriminate on the User-Agent and did not rate-limit a normal run, so the
  paid products here buy volume, a country and browser infrastructure rather
  than access. The README says so in those words.

* **Headless and headful were measured IDENTICAL** — 20 events both ways —
  so headless is the default here, unlike a sibling repo where it was the
  difference between 4 of 4 and 0 of 4. A number that came from another
  site's measurement is not a number about this one (§13).

* **It is behind Cloudflare even though it never challenged us.**
  `server: cloudflare` and a `cf-ray` on every response. A managed challenge
  renders a Turnstile, whose parameters exist only inside the one
  `turnstile.render` call the page makes — so this engine installs the
  interception script on the CONTEXT before any page script runs, and
  2Captcha solves it with `TurnstileTaskProxyless`. None was met while this
  was built; the capability is implemented and the README never claims a
  captcha here cannot be solved (§19).

* **A page that is not Polymarket's HTML is recognised STRUCTURALLY.**
  Chromium's own proxy-error page carries `<title>polymarket.com</title>` —
  the site's own hostname — no vendor marker and `ERR_PROXY_CONNECTION_
  FAILED` in a div (186,712 bytes of it, captured for the fixtures). A title
  check calls that a real page. Asking whether the document was built out of
  the site's own assets does not.

Examples
--------
    python3 playwright_scraper.py \\
        --url "https://polymarket.com/predictions"

    # one category, as a spreadsheet
    python3 playwright_scraper.py \\
        --category crypto --format csv

    # the depth path: the listing, then all twenty event pages, 3 workers
    python3 playwright_scraper.py \\
        --url "https://polymarket.com/predictions/politics" \\
        --mode events --pages 21 --concurrency 3

    python3 playwright_scraper.py \\
        --url "https://polymarket.com/event/fed-decision-in-september-762"
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS, detect_turnstile,
                            wait_for_turnstile, TURNSTILE_INTERCEPT_JS,
                            TURNSTILE_INJECT_JS)
from product_parser import (parse_markets, SELECTORS, PAGE_CAP,
                            detect_bot_challenge, page_kind, normalize_url,
                            page_url, paginates_by_url, redirected_away,
                            served_by_polymarket, site_host, is_supported_host,
                            source_of, unsupported_reason, event_urls_from,
                            listing_url_for, category_from_url, locale_of)
from output_writer import (dedupe_by_key, merge_pages, finish_run,
                           EXIT_API_ERROR)
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# No browser channel is forced here, and that is a measurement rather than an
# omission. A sibling site reads the CLIENT before the address and refuses a
# bundled Chromium outright; Polymarket does not read it at all. Playwright's
# own Chromium was served HTTP 200 and the full payload on every one of the
# twelve captures taken for this repo, from one datacentre exit on
# 2026-09-16 — and so was `curl` sending `curl/8.0`, which is as far from a
# real browser as a request gets. Named here rather than at the call site so
# the smoke suite can assert the three engines agree on it;
# `--browser-channel chrome` is still available for a reader who wants it.
DEFAULT_BROWSER_CHANNEL = None

# How long to wait for a remote browser to accept the CDP connection.
#
# 150s, not the 30s this family shipped. What is MEASURED is narrow and
# arithmetic: against a live Scraping Browser endpoint the WebSocket upgrade
# hung for **121 seconds** before the SERVER hung up, so a 30s client timeout
# gives up while the server is still working. Sitting above the server's own
# give-up point means the client is never the one that walks away first.
#
# What is NOT established, and was claimed here for one commit before the
# evidence contradicted it: that giving up early is what leaves a profile
# stuck at `profile_locked`. Two profiles were observed locked and not
# clearing (one for over forty minutes, one after four minutes of complete
# silence), and the second locked INSTANTLY on its first WebSocket attempt —
# with no timed-out connect anywhere in its history. So the lock has some
# other cause, and this timeout is not a fix for it. Raising it is still
# right; expecting it to unwedge anything is not.
CDP_CONNECT_TIMEOUT_MS = 150_000


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chrome
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes: dedupe that mutates a running set inside the loop
    makes the OUTPUT depend on the order pages happened to arrive in. Pages
    are strictly sequential on this site, which is exactly why keeping the
    merge order-independent costs nothing and keeps the family's contract.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # How many events the LISTING holds in total, where the site states one
    # — 21,511 for /predictions against the twenty it ships. NOT a per-page
    # counter and never used as one: reading it as a gap would claim
    # twenty-one thousand missing rows on a page that rendered everything it
    # was ever going to. It goes in the sidecar beside what the run actually
    # read, which is what makes the difference between the two visible
    # rather than assumed.
    total_events: Optional[int] = None
    # None, always, on this site: Polymarket numbers nothing, and an unknown
    # gap must not read the same as a gap of zero (§8).
    gap: Optional[int] = None
    # What the scroll did: how many cards it reached and whether it SETTLED.
    # A batch whose feed was still growing when the round budget ran out is
    # a floor rather than the whole feed, and a run that reported it as
    # complete would read as a shrinking catalogue.
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# The lowest share of rows that must carry a price before the read is
# suspect. Measured across five captures on 2026-09-16: 94/94, 70/70, 84/84,
# 5/5 and 1/1 — every market the payload named had an `outcomePrices` pair,
# on every page kind and in every locale. So the floor sits high; the 5% of
# room below it is for a market the venue has just created and not yet
# priced.
#
# There is deliberately NO condition-id floor beside it, and the reason is
# the mode split rather than the site: a listing ships 11 to 12 fields per
# market and an event page 111 to 115, so `condition_id` is 0% on a
# `markets` run and 100% on an `event` one. A single threshold across both
# could only be wrong for one of them; `mode` in the sidecar is what says
# which columns to expect.
FIELD_FLOOR = 95



# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a page has turned over — lives in page_flow.py so all three
# engines make it identically. What lives here is only HOW to ask this
# particular driver.
#
# The primitives are NAMED OPERATIONS rather than JavaScript (§1). Selenium's
# execute_script takes a function BODY with an explicit `return` while
# Playwright and pyppeteer take `() => expr`, so a shared module handing JS
# across this boundary would quietly acquire one driver's dialect.
def _count(page, selector: str) -> int:
    """How many elements match, or 0 if the page moved under us.

    GUARDED, like its twins in the other two engines, and the crash that
    taught us why came from the canary's first dispatch: a scroll batch was
    polling the card count when the page navigated — Cloudflare's challenge
    can arrive at any moment on this site — and Playwright raised
    `Execution context was destroyed, most likely because of a navigation`.
    That left the run with exit 1, a CRASH, where the correct answer was
    "blocked".

    0 is the safe reading rather than a lie: every caller treats it as "no
    cards seen this poll", which makes a readiness wait keep waiting and a
    scroll batch report no growth — both of which are what actually happened.
    The alternative, letting it propagate, turns a routine mid-poll
    navigation into a traceback.
    """
    try:
        return len(page.query_selector_all(selector))
    except (PWError, PWTimeout) as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _scroll_to_bottom(page) -> None:
    """Scroll the WINDOW to the end of the document.

    The window really is the scroller here — measured, because a sibling
    repo's site kept its results in an inner container and a window scroll
    did nothing there: `document.scrollingElement` is <html>, there is no
    element on the page with its own overflow, and `window.scrollY` reached
    2,654 of a 3,654px document. It just does not help: twelve rounds of it
    added zero events. See page_flow's scroll section for why the loop is
    kept anyway.

    To `document.body.scrollHeight` rather than by a fixed wheel distance: a
    fixed wheel stopped three rounds short of the bottom on a sibling site's
    7,600px grid, so the lazy-load trigger was never reached and a run took
    30 of 50 cards while looking settled (§8).

    Guarded for the same reason `_count` is: the page can navigate mid-loop
    on this site, and a scroll that raises turns a routine challenge into a
    traceback. A failed scroll needs no report of its own — the next poll
    sees the card count unchanged and the loop draws the right conclusion.
    """
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except (PWError, PWTimeout) as e:
        logger.debug("scroll failed: %s", e)


def _page_height(page) -> Optional[int]:
    """The document's scroll height, or None if the page is mid-navigation."""
    try:
        return page.evaluate("() => document.body.scrollHeight")
    except (PWError, PWTimeout):
        return None


# A page's own data arrives in the HTML on this site, so there is no XHR
# whose refusal could turn a complete run into a partial one — which is why
# the sibling repo's response watcher is not here. What IS worth counting is
# the opposite: responses the browser could not get at all.
#
# Any status at or above 400 on a request to this site's own host, and the
# SAME threshold in all three engines. A threshold that differed between
# them would mean one engine reporting `complete` where its twins report
# `partial` on the identical run, which is precisely the drift the shared
# modules exist to prevent (§6).
_SITE_HOST_FRAGMENT = "polymarket.com"


def _watch_refusals(session) -> None:
    """Start counting refused responses from the site's own host."""
    session._refused = 0

    def _on_response(response):
        try:
            if _SITE_HOST_FRAGMENT in response.url and response.status >= 400:
                session._refused += 1
        except Exception:  # noqa: BLE001 — a listener must never break a run
            pass

    session.page.on("response", _on_response)


def _refused_count(session) -> int:
    return getattr(session, "_refused", 0)


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    """The readiness threshold, lowered to what THIS page actually holds.

    Passing the counter's own range is what keeps a short last page from
    spending the whole timeout and then reporting itself unpainted.
    """
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status, page.url)


def _same_url(a: str, b: str) -> bool:
    from product_parser import strip_tracking
    return strip_tracking(a or "") == strip_tracking(b or "")


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different one — retrying it unchanged just
    spends the budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch a browser on `pool`'s current exit; return (browser, context, page).

    Uses Playwright's own Chromium by default, and on this site that is a
    measurement rather than a shrug: it was served HTTP 200 and the full
    payload on all twelve captures taken, and so was a `curl` announcing
    itself as `curl/8.0`. `--browser-channel chrome` is offered for a reader
    who wants it and is not needed.

    Factored out so a proxy rotation can tear the whole browser down and call
    it again. Swapping the proxy under a live session would be cheaper and
    wrong: cookies a bot manager issued against one exit, replayed from
    another, are a stronger signal than either address alone.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    channel = args.browser_channel
    if channel:
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
        except (PWError, PWTimeout) as e:
            # A fallback, not a downgrade. Unlike a sibling site, this one
            # was measured serving the bundled Chromium the full feed, so
            # losing the requested channel costs nothing that is known.
            logger.info(
                "Could not launch the %r channel (%s) — using Playwright's "
                "own Chromium instead, which this site was measured serving "
                "normally. Install the channel with `playwright install %s` "
                "if you want it.", channel, str(e)[:160], channel)
            browser = pw.chromium.launch(**launch_kwargs)
    else:
        browser = pw.chromium.launch(**launch_kwargs)

    ctx_kwargs = {"user_agent": _chrome_ua(browser.version),
                  "locale": args.locale,
                  "viewport": {"width": 1440, "height": 900}}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)",
                    fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    # THE ONLY MOMENT a Cloudflare Turnstile's parameters can be captured.
    # A Challenge page calls `turnstile.render(container, params)` once and
    # keeps nothing: `sitekey`, `action`, `cData` and `chlPageData` live only
    # inside that call, and `TurnstileTaskProxyless` needs all four. No static
    # read of the HTML, however careful, can produce a solvable task — so the
    # hook goes on the CONTEXT, before any page script runs, and covers every
    # document including the one a redirect lands on.
    #
    # Harmless where there is no Turnstile, which on this site is every page
    # measured so far: it wraps a function that never appears and stops
    # watching after 30 seconds. Installed anyway because the alternative is
    # discovering on the day it matters that the one chance to capture the
    # parameters has already passed.
    context.add_init_script(TURNSTILE_INTERCEPT_JS)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        _watch_refusals(self)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    try:
        browser = pw.chromium.connect_over_cdp(
            args.cdp_endpoint, timeout=args.cdp_connect_timeout * 1000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # that endpoint is a URL with a password in it — repeated five times,
        # in the message plus a four-line call log. Unmasked it lands in the
        # terminal, in CI output and in any log the run is piped to, which is
        # the one thing this project promises does not happen. The host and
        # port are KEPT: which endpoint failed is the useful half and is not
        # the secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so `profile_locked` means something holds this `pid`.\n"
            f"Worth knowing before you go looking for it on your side: two "
            f"profiles were observed here entering that state and NOT leaving "
            f"it — one for over forty minutes, one still locked after four "
            f"minutes of no requests at all, having locked on its very first "
            f"connection attempt. Waiting did not clear either. If that is "
            f"what you are seeing, it is not another run of this tool holding "
            f"it, and nothing on this side will free it: use a different pid, "
            f"or reset the profile from the 2Captcha dashboard."
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser. Tried first when --cdp-endpoint is set;
    # this script's own detect+solve logic still runs as a fallback.
    #
    # Worth knowing what it can and cannot do here: no challenge was met on
    # this site while this repo was built, so what follows is a capability
    # rather than a measurement. If Cloudflare does issue its managed
    # challenge, the extension solves the rendered Turnstile inside the
    # remote browser; this script's own interception covers the same case on
    # a local browser. Neither is charged for a page that carries no widget.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve",
                         {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning(
            "[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this "
                    "--cdp-endpoint (%s) — relying on this script's own "
                    "detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY rather than once is the point: a Playwright connection
# error repeats the endpoint five times, so a masker that handled only the
# first occurrence would print the password four times and look like it was
# working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright RAISES rather than returning empty while a navigation is in
    flight ("Unable to retrieve content because the page is navigating"), and
    this site's challenge handler resolves by navigating — so the one moment
    this is called is the one moment it can fail. Returns None if the page
    will not hold still, so a caller can skip a check instead of failing the
    run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating — retrying content() in %dms "
                        "(%d/%d).", pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page. The static-HTML and runtime
    reCAPTCHA detectors are run and RECONCILED against each other rather than
    short-circuited, because they can disagree about the variant and the
    parameters for one are rejected for the other.

    WHAT IS AND IS NOT KNOWN HERE. No challenge was met in the twelve
    captures and three raw fetches taken for this repo, and the pages carry
    no captcha configuration at all: 0 occurrences of `recaptcha`,
    `hcaptcha`, `turnstile`, `datadome`, `perimeterx`, `sitekey` or
    `*_SITE_KEY` across all of them. So this path is UNEXERCISED on
    polymarket.com, and saying that plainly is the point — a sibling repo
    shipped a README sentence claiming a captcha here "cannot be solved",
    which was a statement about its own missing code dressed up as a fact
    about a paid product, and it cost a release (§19).

    What IS true: the site sits behind Cloudflare, a managed challenge
    renders a Turnstile, and 2Captcha solves that with
    `TurnstileTaskProxyless` — 11 seconds and $0.00145 when a sibling repo
    measured it end to end. The parameters for it can only be captured by the
    interception installed at context creation, because a Challenge page
    publishes no sitekey in its markup.

    The detector runs after EVERY navigation and stays broad, which is
    deliberate (§8): which challenge a visitor meets depends on the exit
    country and on what the address has been doing, and a narrow detector is
    how a rendered challenge gets reported as an empty listing months later.
    """
    html = _content_when_settled(page)
    if html is None:
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # answers are already rendered guards nothing, and counting the anchors is
    # instant — which is why this check sits here rather than after the
    # readiness wait. The other way round would cost 25 wasted seconds on a
    # page the challenge genuinely gates, where solving FIRST is what makes
    # the content appear.
    already_rendered = _count(page, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        # No reCAPTCHA. Turnstile is the other thing 2Captcha can solve, and
        # the RUNTIME reading is the one that matters: a Cloudflare Challenge
        # page publishes no sitekey in its markup, so only the interception
        # installed at context creation can produce a solvable challenge.
        # The static read is kept as the fallback for a standalone widget,
        # whose sitekey IS in the markup.
        challenge = (wait_for_turnstile(lambda js: page.evaluate(js),
                                        lambda sec: page.wait_for_timeout(sec * 1000),
                                        page_url=page.url)
                     or detect_turnstile(html, page.url))
        if challenge and not challenge.sitekey:
            logger.warning(
                "A Cloudflare Turnstile is on this page but no sitekey was "
                "captured, so it cannot be solved and nothing will be "
                "charged for it. That means the page rendered its widget "
                "before this run's interception script was installed — which "
                "should not happen on a page this engine navigated to, and "
                "does happen if the browser was attached to mid-flight.")
            return False
    if not challenge:
        return False

    if when_blocked and already_rendered > page_flow.MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d cards are already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting "
                   "to solve.", challenge.kind, challenge.source,
                   challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    if challenge.is_turnstile:
        # Turnstile hands its token back through `cf-turnstile-response` and
        # the page's own callback, not through `g-recaptcha-response` and
        # grecaptcha's client registry.
        called_back = page.evaluate(TURNSTILE_INJECT_JS, token)
        logger.info("Turnstile token injected%s.",
                    " and handed to the page's callback" if called_back
                    else " (no callback was captured — relying on the form "
                         "field)")
        if challenge.solved_user_agent:
            logger.info("2captcha solved it against user agent %r. Cloudflare "
                        "checks that on a Challenge page, so a mismatch here "
                        "is the likeliest reason a paid token is refused.",
                        challenge.solved_user_agent[:60] + "…")
    else:
        page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this mode, always as a list even when the mode yields one.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it a row from
    page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output.
    """
    return parse_markets(html, url, page=page_num, mode=args.mode)


def _scroll_the_feed(session, args, html: str, page_num: int) -> dict:
    """Scroll to the bottom until the grid stops growing.

    RUN ONLY WHEN THE PAYLOAD DID NOT ANSWER, and that condition is the whole
    design. Polymarket server-renders its markets into the page's own
    payload, so a `markets` run has everything it will ever write before a
    pixel paints — and scrolling was measured adding exactly nothing: twelve
    rounds to the bottom, headless and headful, left the listing at twenty
    events and the document at 3,654px. Twelve rounds at two seconds each on
    every page would be four minutes of a run spent proving that.

    Where it DOES matter is the fallback: if the payload shape changes and
    the run drops to reading tiles, a tile that has not painted is a row that
    is not written. So the caller checks `page_flow.payload_answered` first
    and only spends the rounds when it is False.

    Deliberately no `target`. The only total this site publishes is
    `totalCount` — 21,511 for `/predictions` — which is the size of the whole
    catalogue rather than of this page. Using it to decide when to stop
    scrolling would scroll forever.
    """
    before = _count(session.page, page_flow.READY_SELECTOR)
    trace = page_flow.scroll_until_settled(
        lambda sel: _count(session.page, sel),
        lambda: _scroll_to_bottom(session.page),
        lambda: _page_height(session.page),
        session.page.wait_for_timeout,
        selector=page_flow.READY_SELECTOR)
    logger.info("Scrolled batch %d: %d card(s) at first paint, %d after "
                "%d round(s) of scrolling.", page_num, trace["cards_before"],
                trace["cards_after"], trace["rounds"])
    if trace["cards_after"] <= trace["cards_before"]:
        logger.info(
            "%d round(s) of scrolling added no events (%d -> %d). That is "
            "the SITE and not the scroll: a Polymarket listing is twenty "
            "events long and has no address for the twenty-first, measured "
            "three ways on 2026-09-16. Use --mode events for depth, or point "
            "--url at another category for breadth.",
            trace["rounds"], trace["cards_before"], trace["cards_after"])
    return {"first_paint": before, "reached": trace["cards_after"],
            "rounds": trace["rounds"], "settled": True}


# ===========================================================================
# Workers — `--mode events` only
# ===========================================================================
def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock:
        the concurrency is safe by construction rather than by discipline
        (§7).
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.

    Only ever called for a `--mode events` walk, and that mode is §7's "page
    1 is fetched alone because its content decides whether the rest can be
    addressed" in its most literal form: page 5's address does not merely
    depend on page 4, it does not EXIST until the listing is parsed. Once it
    is, the twenty event URLs are independent by construction and a worker
    can own one exit for its lifetime.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a day comes back with no rows at all. Without it, asking for
    # 40 days of a tag that only published on three of them would fetch 37
    # empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra fetches are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args,
                                          _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the "
                                        "walk and stopping dispatch. On this "
                                        "site that means an event page held "
                                        "no markets, which is either a "
                                        "resolved event or a parse worth "
                                        "looking at.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as "
                             "unattempted rather than failed.", name)

    threads = [threading.Thread(target=worker, args=(i,),
                                name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). NOT reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage (§8).
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def _fetch_one_page(session, args, pool, page_num: int, url: Optional[str]) -> PageOutcome:
    """Fetch (or turn to) one page and parse it.

    `url` is always an address here, for every page. Page 1 is the listing
    or the event the run was pointed at; pages 2..N exist only in
    `--mode events` and are the event pages page 1 named. A sibling repo
    passes None for a page reached by scrolling — this site has no such page,
    because scrolling reaches nothing (see the module docstring), and a None
    arriving here is a bug rather than a mode.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a refusal, a challenge page, a dead exit are all recorded on
    the outcome instead.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url or session.page.url)

    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not merely documented — a policy
    # constant nothing reads is the same defect as dead code (§17). It is
    # True on the family's rule rather than on a measurement of this site,
    # which refused nothing while this was written: see page_flow's block
    # section, which says so in as many words rather than inheriting a
    # sibling's numbers.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        load_failed, exit_failed = False, None

        if url is None:
            # Never expected: every page of every mode on this site has an
            # address. Reported rather than swallowed, because the sibling
            # engine this was adapted from used None to mean "scroll for it"
            # and a silent fallthrough here would look like a fetch.
            logger.error("Page %d has no URL to fetch. That is a bug in this "
                         "engine's page planning, not a site behaviour: "
                         "every page here is an address.", page_num)
            outcome.load_failed = True
            return outcome

        if True:
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            for attempt in range(1, args.retries + 1):
                try:
                    session.page.goto(url, wait_until="domcontentloaded",
                                      timeout=60000)
                    load_failed = False
                    break
                except (PWTimeout, PWError) as e:
                    # A dead or misconfigured proxy raises PWError
                    # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                    # catching only the latter lets it escape as a traceback,
                    # which is the likeliest failure the first time anyone
                    # points --proxy-file at a real list.
                    reason = _proxy_failure(e)
                    if reason:
                        exit_failed, load_failed = reason, True
                        break  # a different exit is the only thing that helps
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d) — "
                                       "retrying in %.1fs.", url, attempt,
                                       args.retries, pause)
                        time.sleep(pause)
        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # the distinction §8 is about. A search page's first response is a
        # shell — the grid arrives over client-side GraphQL — so classified
        # naively it reads as something to retry, and retrying a shell buys
        # another shell. Wait for the anchor and re-classify BEFORE the retry
        # decision.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, no cards) — waiting up to "
                        "%.0fs for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: _count(session.page, sel),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            if found < _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty search
            # is a CORRECT one, so retrying it would spend the budget
            # re-confirming the same right answer and rotating the exit would
            # blame an address for the URL it was given.
            break

        if block_attempt < block_retries:
            pause = args.retry_delay * (block_attempt + 1)
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit in %.1fs (%d/%d).",
                               page_num, state, mask(pool.current), pause,
                               block_attempt + 1, block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                time.sleep(pause)
            else:
                # No pool, so nowhere else to go. What the family has
                # measured elsewhere is that a fresh browser CONTEXT clears
                # an edge challenge where a reload does not, so the retry
                # relaunches rather than reloading — this is what
                # `page_flow.RETRY_NEEDS_FRESH_CONTEXT` says, and a constant
                # no engine consulted would be dead policy dressed up as
                # enforcement (§17).
                #
                # NOT relaunched over --cdp-endpoint: a Scraping Browser
                # profile allows one live connection, so reconnecting risks
                # `profile_locked` and would lose the cookies the retry is
                # meant to build on.
                fresh = (page_flow.RETRY_NEEDS_FRESH_CONTEXT
                         and not args.cdp_endpoint)
                logger.warning("Page %d came back as %s — waiting %.1fs and "
                               "re-fetching %s (%d/%d).",
                               page_num, state, pause,
                               "in a FRESH browser context, which is what "
                               "clears a challenge elsewhere in this family"
                               if fresh else "through the same access path",
                               block_attempt + 1, block_retries)
                time.sleep(pause)
                if fresh:
                    session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png")
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_polymarket(html or "")
        vendor = detect_bot_challenge(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).%s",
            len(html or ""),
            "which references" if served else "with no reference to",
            debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "not-served")
        outcome.final_url = session.page.url
        return outcome

    if page_flow.should_parse(state) and page_flow.payload_answered(html):
        # THE FAST PATH, and the normal one: the page's own payload already
        # names every market this run will write, so there is nothing to wait
        # for and nothing to scroll. Skipping both is worth about four
        # minutes a page against the sibling-repo flow this was adapted from,
        # and costs nothing that was measured — twelve scroll rounds added
        # zero events on every listing tried.
        logger.info("Page %d shipped its markets in the page payload — "
                    "parsing it directly (no readiness wait, no scroll).",
                    page_num)

    elif page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function. wait_for_function hands the browser a
        # STRING to evaluate, and a site whose CSP lacks `unsafe-eval`
        # refuses that outright — it took a sibling repo's run down with
        # EvalError and exit 1. A count poll is a CDP call under any CSP.
        found = page_flow.wait_for_count(
            lambda sel: _count(session.page, sel),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found < threshold:
            logger.info("No event tiles appeared within %.0fs. If this "
                        "listing genuinely holds nothing, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).", content_timeout / 1000)

        outcome.scroll = _scroll_the_feed(session, args, html, page_num)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is the exact bytes.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    products = _parse_for_mode(html or "", session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    # How many events the LISTING holds in total, where the site states one
    # — 21,511 for /predictions. Recorded beside what this run read, and
    # never used as a gap: see PageOutcome.total_events.
    outcome.total_events = page_flow.total_events_on_page(html or "")
    outcome.gap = page_flow.page_gap(html or "", len(products))
    if outcome.total_events:
        logger.info("Polymarket states this listing could show %d event(s) "
                    "in total; this page carried %d event(s) worth of "
                    "markets, which is all it renders. The difference is the "
                    "site's own cap and not a missing read — see the README "
                    "on why there is no page 2.",
                    outcome.total_events,
                    len({p.event_slug for p in products if p.event_slug}))

    if products:
        with_title = sum(1 for p in products if p.title)
        with_price = sum(1 for p in products if p.price is not None)
        worst = min(with_title, with_price)
        share = 100.0 * worst / len(products)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed.
        logger.info("Title/price coverage on page %d: %d and %d of %d "
                    "(%.0f%% at worst); the measured floor is %d%%.",
                    page_num, with_title, with_price, len(products), share,
                    FIELD_FLOOR)
        if share < FIELD_FLOOR:
            logger.warning(
                "Only %.0f%% of page %d carries both a question and a price, "
                "against a measured floor of %d%%. Every market the payload "
                "named had both on all five captures this repo was built "
                "from — 94/94, 70/70, 84/84, 5/5 and 1/1 — so this is the "
                "read breaking rather than the listing being unusual. Re-run "
                "with --dump-html. The likeliest cause is that the payload "
                "did not parse and the rows came from the DOM alone, which "
                "the data_source column will say.",
                share, page_num, FIELD_FLOOR)

        # WHICH view built these rows. `flight` is the site's own payload and
        # the only one carrying market ids and exact volumes;
        # `flight+jsonld` means the page's structured data agreed on the
        # price; `dom` means the payload could not be read at all and these
        # are EVENT-level rows. Reported rather than warned about, because
        # which one answers is a property of the page (§9).
        from collections import Counter
        views = Counter(p.data_source for p in products)
        logger.info("Rows by view on page %d: %s.", page_num,
                    ", ".join(f"{k}={v}" for k, v in sorted(views.items())))

        deep = sum(1 for p in products if p.condition_id)
        if args.mode in ("event", "events") and deep < len(products):
            logger.info("%d of %d row(s) on page %d carry a condition_id. A "
                        "listing does not publish one, so a shortfall here "
                        "is page 1 of an --mode events run rather than a "
                        "problem.", deep, len(products), page_num)
    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=debug_png)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        if page_flow.parsed_nothing_from_a_served_page(html, len(products)):
            # §20: a page that was SERVED, links to N events and parses to
            # zero rows is THIS PARSER'S bug, not an empty listing. Reported
            # as what it is, so a reader opens product_parser.py instead of
            # checking their URL for a typo.
            outcome.state = "parser_found_nothing"
            logger.error(
                "0 rows parsed from a page the site SERVED, which links to "
                "%d event(s). That is a parser regression rather than an "
                "empty listing: the payload shape or the tile markup has "
                "changed. Saved what the browser saw to %s and %s — please "
                "open an issue with the .html attached.",
                page_flow.count_cards(html or ""), debug_html, debug_png)
        else:
            logger.warning("0 rows parsed — saved what the browser actually "
                           "saw to %s and %s. Open the .png to see it.",
                           debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    dedupe_key = "sku"
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    # Two shapes of pagination live here, and the MODE picks which (§7).
    #
    #   events    page 1 is the LISTING and pages 2..N are the event pages
    #             page 1 named. Those are real, independent addresses — but
    #             they are not knowable from a URL convention, only from
    #             page 1's own data, which is why `page_url()` returns None
    #             and the plan is built from `event_urls_from(first.products)`
    #             instead. Once built, the walk can be handed to workers.
    #
    #   markets / event   one page, and there is no second one. Not "one page
    #             until the next link goes missing": `?page=2`, `?_p=2` and
    #             `?offset=20` all answer with page 1, scrolling adds
    #             nothing, and the site's own cursor is opaque. So
    #             `--concurrency` above 1 is refused with that reason rather
    #             than quietly running one worker, which would look like the
    #             flag did something (§18).
    plans_pages = args.mode == "events"

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if not plans_pages:
            logger.warning("--concurrency %d is refused: %s.", concurrency,
                           page_flow.concurrency_refusal(args.url, args.mode))
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection "
                           "per profile, and several workers would collide on "
                           "it (profile_locked). Use several pids instead, "
                           "one run each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster "
                           "way to get that address scored than to gather "
                           "data. This site refused nothing while this "
                           "repo was built, which is a reason to keep it "
                           "that way rather than a licence to hammer it. "
                           "Pass --proxy-file to spread the load.",
                           concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own "
                        "exit for its lifetime, which is the same spread "
                        "without a browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). A listing names twenty event "
                           "pages, so more than a handful of workers mostly "
                           "buys memory pressure.", concurrency, concurrency)

    session = None
    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 is always fetched on its own, because its answer is what
            # decides whether the rest can be addressed independently.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None

            elif plans_pages:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)

                # VERIFY before planning (§7). Two redirects on this site
                # change what was asked for: a search `?q=x` can land on the
                # TAG listing `/predictions/x`, which is a different set of
                # events, and an `/event/` URL can resolve to its league path.
                # If the site answered a different address than the one asked
                # for, what page 1 named belongs to that other listing.
                drift = redirected_away(args.url, first.final_url or args.url)
                planned = None
                if drift:
                    logger.warning(
                        "Not walking the event pages: %s. Page 1's events "
                        "belong to the listing the site actually served, not "
                        "to the one this run asked for, so fetching them "
                        "would attribute one listing's markets to another. "
                        "Point --url at the address the site settled on.",
                        drift)
                    stop_reason = "pagination_redirected"
                elif args.pages > 1:
                    planned = event_urls_from(first.products,
                                              limit=args.pages - 1)
                    if not planned:
                        logger.warning(
                            "Page 1 named no event pages to walk. In "
                            "--mode events that is the end of the run rather "
                            "than a failure: an event page with no event "
                            "links on it has nothing under it.")
                        stop_reason = "no_event_pages"
                    elif len(planned) < args.pages - 1:
                        logger.info(
                            "--pages %d was asked for and page 1 named %d "
                            "event(s), so the walk is %d page(s) long. A "
                            "Polymarket listing is twenty events, so "
                            "--pages 21 is the most a single listing can "
                            "use.", args.pages, len(planned), len(planned) + 1)

                if planned and concurrency > 1:
                    # Close the page-1 browser before starting workers: it has
                    # done its job, and holding it open would cost one more
                    # window than was asked for.
                    session.close()
                    specs = [(n + 2, u) for n, u in enumerate(planned)]
                    logger.info("Fetching %d event page(s) across %d "
                                "workers%s.", len(specs), concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)
                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        stop_reason = "pages_unattempted"
                    session = None       # already closed
                elif planned:
                    for index, url in enumerate(planned):
                        page_num = index + 2
                        if page_flow.page_cap_reached(page_num):
                            logger.warning("Stopping at the %d-page cap.",
                                           PAGE_CAP)
                            stop_reason = "page_cap"
                            break
                        if pool and pool.rotates_per_page():
                            pool.advance(f"per-page rotation, page {page_num}")
                            session.relaunch()
                        time.sleep(args.delay)
                        outcome = _fetch_one_page(session, args, pool,
                                                  page_num, url)
                        outcomes.append(outcome)
                        if not outcome.ok:
                            stop_reason = ("page_load_timeout"
                                           if outcome.load_failed
                                           else f"blocked_{outcome.blocked_by}")
                            blocked = outcome.blocked_by is not None
                            break
                        seen_keys.update(p.sku for p in outcome.products
                                         if p.sku is not None)
                        if not outcome.products:
                            # NOT "no new sku": in this mode every event page
                            # re-states markets page 1 already named, so a
                            # fresh-sku test would end the walk on page 2 of
                            # every healthy run and throw away the deep
                            # columns the walk exists to fetch. The data-side
                            # ending here (§7) is a page that produced NO
                            # rows at all.
                            logger.info("Event page %s produced no rows at "
                                        "all — treating that as the end of "
                                        "the walk.", url)
                            stop_reason = "no_new_products"
                            break

            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                # ONE page, and the run is COMPLETE holding it. This branch is
                # the one §7 warns about from the other side: a sibling repo
                # shipped three dead next-page selectors, fetched one page of
                # three and exited 0, and nobody noticed for months. The
                # defence here is that the single page is DELIBERATE, stated
                # in the log with the measurement behind it, recorded in the
                # sidecar as its own stop reason, and asserted by the canary.
                note = page_flow.one_page_note(args.url, args.pages)
                if note:
                    logger.info("%s.", note)
                    stop_reason = "listing_has_one_page"
        finally:
            if session is not None:
                session.close()


    # Merge once, in PAGE order — not in the order pages happened to finish —
    # and let a DEEPER reading of a market replace a shallower one in place.
    # See output_writer.merge_pages: in --mode events page 1 names every
    # market from the listing (11-12 fields) and pages 2..N carry those same
    # markets from their own event pages (111-115 fields), so without the
    # upgrade the twenty extra fetches would be thrown away by the dedupe.
    all_rows, fresh_by_batch, upgraded_by_page = merge_pages(
        [(o.page_num, o.products) for o in outcomes], key=dedupe_key)
    for page_num in sorted(fresh_by_batch):
        if upgraded_by_page.get(page_num):
            logger.info("Page %d: %d row(s) new, %d existing row(s) upgraded "
                        "to the event page's deeper reading.",
                        page_num, fresh_by_batch[page_num],
                        upgraded_by_page[page_num])

    # There is deliberately NO thin-page check here, and its absence is a
    # measurement rather than an oversight. A sibling repo warns when a page
    # comes back much smaller than the fullest one, because its pages are a
    # steady 50. These are not comparable: page 1 is a listing carrying 44 to
    # 94 markets and every page after it is ONE event carrying 1 to 5, so
    # every healthy --mode events run would trip such a check on every page
    # after the first. What IS reported is the new-row count per page, above.

    total_events = next((o.total_events for o in outcomes
                         if o.total_events is not None), None)

    if all_rows and total_events:
        logger.info("Polymarket states this listing could show %d event(s); "
                    "this run took %d market(s) from the %d event(s) the page "
                    "renders. The two are not comparable and the difference "
                    "is not a gap — see page_flow.page_gap and the README's "
                    "note on why a listing has one page.",
                    total_events, len(all_rows),
                    len({r.event_slug for r in all_rows if r.event_slug}))

    if all_rows:
        enriched = sum(1 for r in all_rows
                       if r.data_source and r.data_source != "dom")
        logger.info("Payload coverage over the merged run: %d/%d (%.0f%%) — "
                    "rows built from the site's own inlined payload rather "
                    "than from a rendered tile alone.",
                    enriched, len(all_rows), 100.0 * enriched / len(all_rows))
        confirmed = sum(1 for r in all_rows if r.data_source == "flight+jsonld")
        logger.info("JSON-LD confirmed the price of %d/%d row(s) (%.0f%%). A "
                    "listing's structured data names ONE market per event, "
                    "so this tops out near one in five on a listing and at "
                    "zero on an event page, which has no ItemList.",
                    confirmed, len(all_rows),
                    100.0 * confirmed / len(all_rows))
        deep = sum(1 for r in all_rows if r.condition_id)
        logger.info("Deep rows (a condition_id and CLOB token ids): %d/%d. "
                    "Only an event page publishes those.", deep, len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    extra = {
        "scroll": {o.page_num: o.scroll for o in outcomes if o.scroll},
        "rows_new_per_page": fresh_by_batch,
        # What the LISTING holds, where the site states it. Beside `rows` in
        # the sidecar rather than subtracted from it: the difference is the
        # site's own twenty-event cap, not a gap this run failed to close
        # (§8 — an unknown gap is not a gap).
        "total_events": total_events,
        "events_on_page_1": len({r.event_slug for r in all_rows
                                 if r.event_slug and r.page == 1}),
        "inline_payload_rows": sum(1 for r in all_rows
                                   if r.data_source != "dom"),
        "jsonld_confirmed_rows": sum(1 for r in all_rows
                                     if r.data_source == "flight+jsonld"),
        "deep_rows": sum(1 for r in all_rows if r.condition_id),
        "category": category_from_url(final_url),
        "locale": locale_of(final_url),
        # Recorded because a reader comparing two runs needs to know the
        # pagination model before comparing anything: a listing here is ONE
        # page by the site's own design, and `--mode events` turns its
        # events into pages 2..N.
        "pagination": ("listing+event-pages" if args.mode == "events"
                       else "single-page"),
    }

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Polymarket prediction-market scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A Polymarket URL: a listing (/predictions, "
                        "/predictions/{tag}, /{category} such as /crypto or "
                        "/politics, or /predictions?q={query}) or an event "
                        "(/event/{slug}, /sports/{league}/{slug}). A locale "
                        "path works too — /es/predictions — and changes the "
                        "wording, not the ids or the prices. One of the "
                        "site's own pages such as /leaderboard is refused "
                        "with the reason. Required, unless --category is "
                        "given or POLYMARKET_URL is set in the environment "
                        "or in .env.")
    p.add_argument("--mode", choices=["markets", "events", "event"],
                   default=None,
                   help="Which view to take. Inferred from the URL by "
                        "default — a listing gives `markets`, an event page "
                        "gives `event` — and `events` is the one worth asking "
                        "for explicitly: it fetches the listing AND every "
                        "event page under it, which is where condition ids, "
                        "CLOB token ids, spreads and per-market volumes come "
                        "from. A listing ships 11-12 fields per market; an "
                        "event page ships 111-115. All three yield the same "
                        "row and the data_source column says which view "
                        "built it.")
    p.add_argument("--category", default=None,
                   help="A listing to fetch instead of --url: `crypto`, "
                        "`politics`, `sports`, any tag the site publishes, or "
                        "`q:bitcoin` for a search. Also fills the row label "
                        "from the URL when --url is used, so it is rarely "
                        "empty.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Pages to fetch (default 1, cap {PAGE_CAP}). What a "
                        f"page IS depends on the mode, and this is the one "
                        f"flag worth reading twice. In --mode markets and "
                        f"--mode event there is exactly ONE page and asking "
                        f"for more is reported as complete with the reason: "
                        f"a listing renders twenty events and publishes no "
                        f"address for the twenty-first (?page=2, ?_p=2 and "
                        f"?offset=20 all answer with page 1; scrolling adds "
                        f"nothing). In --mode events page 1 is the listing "
                        f"and pages 2..N are its event pages, so --pages 21 "
                        f"is a full listing plus all twenty events.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds (default 2.0). Nothing "
                        "was refused while this repo was built — 12 captures "
                        "and 3 raw fetches, all 200 from one datacentre "
                        "address — so this is politeness rather than a "
                        "measured requirement. Raise it before reaching for "
                        "a proxy if you ever do meet a refusal.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Workers for --mode events (default 1). REFUSED "
                        "above 1 in the other two modes, with the reason: a "
                        "listing and an event page are one fetch each and "
                        "there is no second address to hand a worker. In "
                        "--mode events the twenty event URLs page 1 names ARE "
                        "independent addresses, each worker owns its own "
                        "browser and its own exit, and no lock is needed.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty search is "
                        "a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="polymarket_markets",
                   help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "the site language: the URL PATH does "
                        "(/es/predictions). This only affects what the "
                        "browser claims about itself.")
    p.add_argument("--browser-channel", default=DEFAULT_BROWSER_CHANNEL,
                   metavar="CHANNEL",
                   help="Which installed browser to drive. Unset by default, "
                        "which means Playwright's own Chromium — and on this "
                        "site that is enough: it was served HTTP 200 and the "
                        "full payload on all twelve captures taken, as was a "
                        "plain `curl`. Pass `chrome` to drive an installed "
                        "Chrome anyway (`playwright install chrome`).")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and "
                        "blank lines skipped) to rotate across. Wins over "
                        "--proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page is accepted but cannot be honoured mid-feed "
                        "here — the next batch lives inside the current "
                        "browser session, so rotating would restart the feed "
                        "at its first batch. The run says so when it "
                        "happens.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do "
                        "not all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up (default 2). Needs "
                        "a pool of more than one; ignored otherwise. Worth "
                        "knowing on this site: the refusal is Cloudflare's "
                        "nothing was refused while this repo was built, "
                        "so if you do meet one, --delay is the first lever "
                        "to try before assuming the address is the problem.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off "
                        "by default so a failed run can't overwrite a good "
                        "result with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser supplies "
                        "its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" across
    # this family, which the API rejects with HTTP 400, so --fingerprint
    # failed on every invocation in four repos at once (§17).
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. Use --fp-country to narrow further. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. What "
                        "this repo implements: reCAPTCHA v2, v2-invisible, "
                        "v3 and enterprise, and Cloudflare Turnstile "
                        "including the Challenge-page form, whose parameters "
                        "are captured by an init script because the page "
                        "publishes no sitekey. No challenge was met on "
                        "polymarket.com while this was written, so the path "
                        "is unexercised HERE — which is a fact about this "
                        "repo's testing and not a claim about what a solver "
                        "can do (§19). A page carrying no widget is reported "
                        "unsolved rather than charged for.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or "
                        "0.9 — the API only accepts these three). Ignored for "
                        "v2 widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching one locally, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy, --browser-channel and --headless/--headful "
                        "are ignored when this is set.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {CDP_CONNECT_TIMEOUT_MS // 1000}). "
                        f"Deliberately high: a Scraping Browser provisions a "
                        f"browser when the WebSocket upgrade arrives, and one "
                        f"was measured taking 121s before the SERVER gave up. "
                        f"Giving up earlier than the server does leaves the "
                        f"profile held by a half-open session — measured "
                        f"`profile_locked` on every later attempt, for over "
                        f"twenty minutes.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure. Useful when the row count is "
                        "right but a column comes back empty — see "
                        "TROUBLESHOOTING.md.")
    # HEADLESS by default, and that is this site's own measurement rather
    # than the family's habit. §19 says to measure headless against headful
    # once per site because a sibling repo found 0 of 4 against 4 of 4; here
    # the answer came back IDENTICAL — 20 events, the same twenty, on both,
    # with the same 3,654px document. The site also served a `curl/8.0` User-
    # Agent the full 791,003 bytes, so it is not reading the client at all.
    #
    # Headless is therefore the default: it needs no display, which is what
    # a CI runner and a container have, and it costs nothing that was
    # measured. `--headful` stays available and is worth trying if a refusal
    # ever does appear — an untested lever is still a lever.
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT, and measured identical "
                        "to headful on this site (20 events both ways, "
                        "2026-09-16).")
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Measured identical "
                        "to headless here, so this is for watching a run or "
                        "for trying a lever if the site ever starts "
                        "refusing. Needs a display.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url and args.category:
        # --category is a first-class way in on this site, because the
        # listings ARE the catalogue's structure: /predictions/crypto and
        # /politics are separate catalogues rather than pages of one.
        args.url = listing_url_for(args.category)
        logger.info("Reading the %s listing: %s", args.category, args.url)
    if not args.url:
        p.error("no --url given, no --category given, and POLYMARKET_URL is "
                "not set in the environment or in .env.")
    why = unsupported_reason(args.url)
    if why:
        # Refused rather than attempted. The parser's payload reader, its
        # path patterns and its tile scoping are all this site's, so pointing
        # it at another prediction market would not fail loudly — it would
        # return zero rows and look like an empty listing.
        p.error(why)

    # Tracking parameters are stripped rather than kept. A sub-market link
    # inside a tile carries `?tid=1758…`, a millisecond timestamp, so two
    # runs an hour apart would otherwise write two different `url` values for
    # a market that never moved. Said out loud, because silently fetching a
    # different URL than the one given is how a run's rows stop matching its
    # command line.
    normalized = normalize_url(args.url)
    if normalized != args.url:
        logger.info("Fetching %s instead of %s — the site's own click "
                    "parameters are stripped so one market has one address.",
                    normalized, args.url)
        args.url = normalized

    kind = page_kind(args.url)
    if args.mode is None:
        # Inferred from the URL, which is the only thing that can be right
        # for two of the three: `markets` and `event` are properties of the
        # path. `events` is the exception — it is a listing URL plus an
        # intention — so it must be asked for.
        args.mode = "markets" if kind == "listing" else "event"
        logger.info("Reading %s as a %s run.", args.url, args.mode)
    elif args.mode == "events" and kind != "listing":
        p.error(f"--mode events needs a LISTING URL to start from; "
                f"{args.url!r} is an {kind} page. It fetches the listing "
                f"first and then every event page under it, so there has to "
                f"be a listing.")
    elif args.mode == "markets" and kind != "listing":
        p.error(f"--mode markets needs a listing URL; {args.url!r} is an "
                f"{kind} page. Leave --mode off and it is inferred.")
    elif args.mode == "event" and kind != "event":
        p.error(f"--mode event needs an /event/ URL; {args.url!r} is a "
                f"{kind} page. Leave --mode off and it is inferred.")
    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-page cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.mode != "events" and args.pages > 1:
        # Said out loud at PARSE time as well as at run time, because it
        # changes what a run CAN return rather than how it is spelled, and
        # because a reader who only sees it in the sidecar has already waited
        # for the run.
        logger.info("%s.", page_flow.one_page_note(args.url, args.pages))
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it's a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch "
                       "rather than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). `profile_locked` means another run still holds this
        # `pid`, and a harness that sees exit 1 goes looking for a bug in the
        # scraper instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
