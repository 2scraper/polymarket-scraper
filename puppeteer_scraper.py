#!/usr/bin/env python3
"""polymarket-scraper — pyppeteer edition

A parity engine. It must agree with the other two on exit codes, run status,
and whether a run crashes or spends money — the shared modules
(`product_parser`, `page_flow`, `output_writer`, `proxy_pool`,
`captcha_solver`) are what keep it honest, and this file holds only "how to
ask pyppeteer".

Read playwright_scraper.py's docstring for what is different about this SITE.
The short version: a listing URL is ONE page of twenty events with no address
for the twenty-first, everything is server-rendered into the page's own
payload, and nothing refused us from a datacentre address — a plain `curl`
was served the full 791,003 bytes.

Three things are different about this ENGINE:

* **The bundled Chromium is old, and here it does not matter.** pyppeteer
  ships build 117.0.5938.0 — two years old — and because the User-Agent is
  built from the browser's OWN version (§8: never a hardcoded literal), this
  engine truthfully announces `Chrome/117`. On a sibling site that was the
  difference between data and exit 3. Polymarket was measured serving
  `curl/8.0` the full page, so it is not reading the client at all, and
  `--chromium-path` is a convenience here rather than a requirement.

* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It is here for parity and for anyone already committed to it.

* **`--concurrency` above 1 is not implemented here.** The worker pool lives
  in playwright_scraper.py; this engine walks `--mode events` one page at a
  time and produces identical output.

Examples
--------
    python3 puppeteer_scraper.py --url "https://polymarket.com/predictions"

    python3 puppeteer_scraper.py --category crypto --format csv

    python3 puppeteer_scraper.py \\
        --url "https://polymarket.com/event/fed-decision-in-september-762"
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job fails on any reported skip —
# that whole mechanism only works if importing this module actually requires
# the driver. With the import hidden inside _Session.open(), the module
# imports cleanly with no pyppeteer installed at all, the group never skips,
# and CI cannot notice a broken import (§10).
from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, detect_turnstile,
                            wait_for_turnstile, TURNSTILE_INTERCEPT_JS,
                            TURNSTILE_INJECT_JS)
from product_parser import (parse_markets, SELECTORS, PAGE_CAP,
                            detect_bot_challenge, page_kind, normalize_url,
                            page_url, paginates_by_url, redirected_away,
                            served_by_polymarket, source_of,
                            unsupported_reason, event_urls_from,
                            listing_url_for, category_from_url, locale_of)
from output_writer import dedupe_by_key, merge_pages, finish_run
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

FIELD_FLOOR = 95

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
# 150, not 30, and kept identical to the Playwright engine's
# CDP_CONNECT_TIMEOUT_MS — see its comment. Short version: the upgrade was
# measured hanging 121s before the SERVER hung up, so 30s gives up while the
# server is still working. It is NOT a cure for `profile_locked`, which was
# observed with no timed-out connect in its history at all.
CONNECT_TIMEOUT = 150


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share, and it is written against plain
    synchronous callables — the right shape for two of the three drivers.
    Bridging here keeps the policy in one place rather than growing an async
    copy of it that would drift.

    The second benefit is what the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which pyppeteer's own API
    does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one at ERROR level, AFTER a successful run has
        # printed its results. Five of those under a "Saved 95 products" line
        # read as a failed run. Only that shape is swallowed; anything else
        # still gets the default handler, because silencing the loop
        # wholesale would hide real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the
        # library's in `exception`, and an `or` between them looks at the
        # exception and never sees the message — which is why these kept
        # printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's background tasks
        pending — its websocket reader and keepalive — and asyncio then
        prints "Task was destroyed but it is pending!" plus a traceback for
        each. That happens AFTER the output is written, so the run is fine
        and the log looks like a crash.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # See the Playwright engine for why this is NOT a per-page counter.
    total_events: Optional[int] = None
    gap: Optional[int] = None
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA from the browser's own reported version.

    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


def _install_turnstile_intercept(session) -> None:
    """Hook `turnstile.render` before any page script can run.

    THE ONLY MOMENT a Cloudflare Turnstile's parameters can be captured: a
    Challenge page calls `turnstile.render(container, params)` once and keeps
    nothing, while `TurnstileTaskProxyless` needs the sitekey, action, cData
    and chlPageData that live only inside that call. pyppeteer spells it
    `evaluateOnNewDocument`; the other two engines spell the same thing
    `context.add_init_script` and `Page.addScriptToEvaluateOnNewDocument`,
    which is why this cannot live in the shared module (§1).

    Harmless where there is no Turnstile — every page measured on this site
    so far — because it wraps a function that never appears and stops
    watching after 30 seconds.
    """
    try:
        session.bridge.run(
            session.page.evaluateOnNewDocument(TURNSTILE_INTERCEPT_JS))
    except Exception as e:  # noqa: BLE001 — never break a run over this
        logger.debug("Could not install the Turnstile interception: %s", e)


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit."""

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True),
                timeout=getattr(self.args, "cdp_connect_timeout",
                                CONNECT_TIMEOUT))
            self.page = self.bridge.run(self.browser.newPage())
            _install_turnstile_intercept(self)
            _watch_refusals(self)
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       "--disable-blink-features=AutomationControlled"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the browser at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises "signal
        # only works in main thread of the main interpreter" because the
        # event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        _install_turnstile_intercept(self)
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        # 1440x900, matching the captures. The viewport decides how far the
        # grid is laid out, and the captures this repo's numbers come from
        # were taken at this size.
        self.bridge.run(self.page.setViewport({"width": 1440, "height": 900}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        _watch_refusals(self)
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. pyppeteer takes `() => expr` like
# Playwright and unlike Selenium, which is exactly why page_flow names
# operations rather than passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.bridge.run(session.page.querySelectorAll(selector)))
    except Exception as e:  # noqa: BLE001
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


# A page's own data arrives in the HTML on this site, so there is no XHR
# whose refusal could turn a complete run into a partial one. What IS worth
# counting is the opposite: responses the browser could not get at all.
#
# Any status at or above 400 on a request to this site's own host, and the
# SAME threshold in all three engines — a threshold that differed between
# them would mean one engine reporting `complete` where its twins report
# `partial` on the identical run (§6).
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

    session._response_hook = _on_response
    try:
        session.page.on("response", _on_response)
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not attach the response watcher: %s", e)


def _refused_count(session) -> int:
    return getattr(session, "_refused", 0)


def _content(session) -> Optional[str]:
    try:
        return session.bridge.run(session.page.content())
    except Exception as e:  # noqa: BLE001
        logger.debug("content() unavailable (page navigating?): %s", e)
        return None


def _current_url(session) -> str:
    try:
        return session.bridge.run(session.page.evaluate("() => location.href"))
    except Exception:  # noqa: BLE001
        return ""


def _scroll_to_bottom(session) -> None:
    """Scroll the WINDOW to the end of the document. See page_flow."""
    try:
        session.bridge.run(session.page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"))
    except Exception as e:  # noqa: BLE001
        logger.debug("scroll failed: %s", e)


def _page_height(session) -> Optional[int]:
    try:
        return session.bridge.run(session.page.evaluate(
            "() => document.body.scrollHeight"))
    except Exception:  # noqa: BLE001
        return None


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _classify(session, html: str, status=None) -> str:
    # `status` POSITIONAL and second, matching the other two engines and the
    # callee's real signature (§17).
    return page_flow.classify(html, status, _current_url(session))


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    return parse_markets(html, url, page=page_num, mode=args.mode)


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved."""
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    url = _current_url(session)
    html_challenge = detect_recaptcha_v3(html, url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: session.bridge.run(session.page.evaluate(js)), page_url=url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        # No reCAPTCHA. Turnstile is the other thing 2Captcha can solve, and
        # the RUNTIME reading is the one that matters: a Cloudflare Challenge
        # page publishes no sitekey in its markup, so only the interception
        # installed before any page script ran can produce a solvable
        # challenge. The static read is kept as the fallback for a standalone
        # widget, whose sitekey IS in the markup.
        challenge = (wait_for_turnstile(lambda js: session.bridge.run(session.page.evaluate(js)),
                                        lambda sec: _sleep(int(sec * 1000)),
                                        page_url=url)
                     or detect_turnstile(html, url))
        if challenge and not challenge.sitekey:
            logger.warning(
                "A Cloudflare Turnstile is on this page but no sitekey was "
                "captured, so it cannot be solved and nothing will be "
                "charged for it.")
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
        # the page's own callback, not through `g-recaptcha-response`.
        session.bridge.run(session.page.evaluate(TURNSTILE_INJECT_JS, token))
    else:
        session.bridge.run(session.page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        session.bridge.run(session.page.reload(
            {"waitUntil": "domcontentloaded", "timeout": 60000}))
    except Exception as e:  # noqa: BLE001
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _scroll_the_feed(session, args, html: str, page_num: int) -> dict:
    """Scroll to the bottom until the feed stops growing.

    RUN ONLY WHEN THE PAYLOAD DID NOT ANSWER — see the Playwright engine's
    copy for the measurements. Short version: this site ships its markets in
    the page's own payload and twelve scroll rounds added zero events, so
    this is the fallback path's safety net and not the main event. No
    `target`: the only total the site publishes is the size of the whole
    catalogue, which is not a target for one page.
    """
    before = _count(session, page_flow.READY_SELECTOR)
    trace = page_flow.scroll_until_settled(
        lambda sel: _count(session, sel),
        lambda: _scroll_to_bottom(session),
        lambda: _page_height(session),
        _sleep,
        selector=page_flow.READY_SELECTOR)
    logger.info("Scrolled batch %d: %d card(s) at first paint, %d after "
                "%d round(s) of scrolling.", page_num, trace["cards_before"],
                trace["cards_after"], trace["rounds"])
    if trace["cards_after"] <= trace["cards_before"]:
        logger.info(
            "%d round(s) of scrolling added no events (%d -> %d). That is "
            "the SITE and not the scroll: a Polymarket listing is twenty "
            "events long and has no address for the twenty-first, measured "
            "three ways on 2026-09-16.",
            trace["rounds"], trace["cards_before"], trace["cards_after"])
    return {"first_paint": before, "reached": trace["cards_after"],
            "rounds": trace["rounds"], "settled": True}


def _fetch_one_page(session, args, pool, page_num: int,
                    url: Optional[str]) -> PageOutcome:
    outcome = PageOutcome(page_num=page_num, url=url or _current_url(session))

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        load_failed = False

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
                    session.bridge.run(session.page.goto(
                        url, {"waitUntil": "domcontentloaded",
                              "timeout": 60000}))
                    load_failed = False
                    break
                except Exception as e:  # noqa: BLE001
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d: "
                                       "%s) — retrying in %.1fs.", url,
                                       attempt, args.retries, str(e)[:120],
                                       pause)
                        time.sleep(pause)
        if load_failed:
            break

        if handle_captcha_if_present(session, args):
            _sleep(1000)

        html = _content(session) or ""
        state = _classify(session, html)

        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, no cards) — waiting up to "
                        "%.0fs for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            page_flow.wait_for_count(
                lambda sel: _count(session, sel), _sleep,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            html = _content(session) or html
            state = _classify(session, html)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                _sleep(1000)
                html = _content(session) or html
                state = _classify(session, html)
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
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
                # No pool, so nowhere else to go. What DOES clear it here is
                # a FRESH browser context: 9 of 27 first attempts were
                # challenged and all 9 were served on the next attempt in a
                # new context, while waiting inside the same context cleared
                # none. `page_flow.RETRY_NEEDS_FRESH_CONTEXT` says so, and a
                # constant no engine consulted would be dead policy dressed
                # up as enforcement (§17).
                #
                # NOT relaunched over --cdp-endpoint: a Scraping Browser
                # profile allows one live connection, so reconnecting risks
                # `profile_locked`.
                fresh = (page_flow.RETRY_NEEDS_FRESH_CONTEXT
                         and not args.cdp_endpoint)
                logger.warning("Page %d came back as %s — waiting %.1fs and "
                               "re-fetching %s (%d/%d).",
                               page_num, state, pause,
                               "in a FRESH browser context, which is what "
                               "clears this site's challenge" if fresh
                               else "through the same access path",
                               block_attempt + 1, block_retries)
                time.sleep(pause)
                if fresh:
                    session.relaunch()
            if url is None:
                logger.warning("Page %d was reached by a button press, so "
                               "there is no address to re-fetch — stopping "
                               "here rather than silently restarting the "
                               "listing at page 1.", page_num)
                break

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
            session.bridge.run(session.page.screenshot(
                {"path": f"{args.out}_page{page_num}_debug.png"}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_polymarket(html or "")
        vendor = detect_bot_challenge(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).", len(html or ""),
            "which references" if served else "with no reference to",
            debug_html)
        if not args.chromium_path and not args.cdp_endpoint:
            logger.error(
                "This engine launched pyppeteer's OWN Chromium (build "
                "117.0.5938.0), the one thing that differs from its twins. "
                "polymarket.com was measured serving it, but pass "
                "--chromium-path pointing at an installed Chrome before "
                "concluding anything about the address.")
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "not-served")
        outcome.final_url = _current_url(session)
        return outcome

    if page_flow.should_parse(state) and page_flow.payload_answered(html):
        # THE FAST PATH, and the normal one: the page's own payload already
        # names every market this run will write, so there is nothing to wait
        # for and nothing to scroll. Identical to the other two engines by
        # construction — the test for it lives in page_flow (§6).
        logger.info("Page %d shipped its markets in the page payload — "
                    "parsing it directly (no readiness wait, no scroll).",
                    page_num)

    elif page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        found = page_flow.wait_for_count(
            lambda sel: _count(session, sel), _sleep, selector, threshold,
            content_timeout)
        _sleep(500)
        if found < threshold:
            logger.info("No event tiles appeared within %.0fs. If this "
                        "listing genuinely holds nothing, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).",
                        content_timeout / 1000)
        outcome.scroll = _scroll_the_feed(session, args, html, page_num)
        html = _content(session) or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    products = _parse_for_mode(html or "", _current_url(session), args, page_num)
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
                    "site's own cap and not a missing read.",
                    outcome.total_events,
                    len({p.event_slug for p in products if p.event_slug}))

    if products:
        with_title = sum(1 for p in products if p.title)
        with_price = sum(1 for p in products if p.price is not None)
        worst = min(with_title, with_price)
        share = 100.0 * worst / len(products)
        logger.info("Title/price coverage on page %d: %d and %d of %d "
                    "(%.0f%% at worst); the measured floor is %d%%.",
                    page_num, with_title, with_price, len(products), share,
                    FIELD_FLOOR)
        if share < FIELD_FLOOR:
            logger.warning(
                "Only %.0f%% of page %d carries both a question and a price, "
                "against a measured floor of %d%%. Every market the payload "
                "named had both on all five captures this repo was built "
                "from, so this is the read breaking rather than the listing "
                "being unusual. Re-run with --dump-html; the data_source "
                "column says whether the rows came from the DOM alone.",
                share, page_num, FIELD_FLOOR)

        from collections import Counter
        views = Counter(p.data_source for p in products)
        logger.info("Rows by view on page %d: %s.", page_num,
                    ", ".join(f"{k}={v}" for k, v in sorted(views.items())))
    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.warning("0 rows parsed — saved what the browser actually saw "
                       "to %s.", debug_html)

    outcome.products = products
    outcome.final_url = _current_url(session)
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint "
                       "the remote browser has its own exit.")
        pool = None

    # Two shapes of pagination, and the MODE picks which (§7). In
    # --mode events page 1 is the LISTING and pages 2..N are the event pages
    # page 1 named — real independent addresses, but knowable only from page
    # 1's own data, which is why they are planned from `event_urls_from`
    # rather than from a URL convention. The other two modes are ONE page and
    # there is no second one: ?page=2, ?_p=2 and ?offset=20 all answer with
    # page 1 and scrolling adds nothing (measured 2026-09-16).
    plans_pages = args.mode == "events"

    if args.concurrency > 1:
        if not plans_pages:
            logger.warning("--concurrency %d is refused: %s.",
                           args.concurrency,
                           page_flow.concurrency_refusal(args.url, args.mode))
        else:
            # A DOCUMENTED ENGINE LIMIT, in the same class as this family's
            # "Selenium cannot authenticate a remote CDP endpoint" (§6): the
            # worker pool is implemented in the Playwright engine only. Said
            # out loud rather than run as one worker quietly, which would
            # look like the flag did something.
            logger.warning("--concurrency %d is not implemented in this "
                           "engine — the worker pool lives in "
                           "playwright_scraper.py. Walking the days one at a "
                           "time instead. Exit codes, status and output are "
                           "identical either way.", args.concurrency)

    bridge = _AsyncBridge()
    session = _Session(bridge, args, pool).open()
    try:
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None

        elif plans_pages:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            # VERIFY before planning (§7). A search `?q=x` can land on the
            # TAG listing `/predictions/x`, which is a different set of
            # events — and page 1's event URLs would then belong to that
            # other listing rather than to the one this run asked for.
            drift = redirected_away(args.url, first.final_url or args.url)
            planned = []
            if drift:
                logger.warning(
                    "Not walking the event pages: %s. Point --url at the "
                    "address the site settled on.", drift)
                stop_reason = "pagination_redirected"
            elif args.pages > 1:
                planned = event_urls_from(first.products, limit=args.pages - 1)
                if not planned:
                    logger.warning("Page 1 named no event pages to walk.")
                    stop_reason = "no_event_pages"

            for index, url in enumerate(planned):
                page_num = index + 2
                if page_flow.page_cap_reached(page_num):
                    logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                    stop_reason = "page_cap"
                    break
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()
                time.sleep(args.delay)
                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not outcome.products:
                    # NOT "no new sku": every event page re-states markets
                    # page 1 already named, so a fresh-sku test would end the
                    # walk on page 2 of every healthy run and throw away the
                    # deep columns the walk exists to fetch (§7).
                    logger.info("Event page %s produced no rows at all — "
                                "treating that as the end of the walk.", url)
                    stop_reason = "no_new_products"
                    break

        else:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            # ONE page, and the run is COMPLETE holding it. §7's warning read
            # from the other side: the single page is DELIBERATE, stated in
            # the log with the measurement behind it and recorded in the
            # sidecar as its own stop reason, so it can never be confused
            # with a dead next-page selector quietly returning a third of the
            # data.
            note = page_flow.one_page_note(args.url, args.pages)
            if note:
                logger.info("%s.", note)
                stop_reason = "listing_has_one_page"
    finally:
        session.close()
        bridge.close()

    # Merged in PAGE order, with a DEEPER reading of a market replacing a
    # shallower one in place — see output_writer.merge_pages. Identical to
    # the other two engines by construction, because the rule lives there
    # rather than being written out three times (§6).
    all_rows, fresh_by_batch, upgraded_by_page = merge_pages(
        [(o.page_num, o.products) for o in outcomes], key="sku")
    for page_num in sorted(fresh_by_batch):
        if upgraded_by_page.get(page_num):
            logger.info("Page %d: %d row(s) new, %d existing row(s) upgraded "
                        "to the event page's deeper reading.",
                        page_num, fresh_by_batch[page_num],
                        upgraded_by_page[page_num])

    total_events = next((o.total_events for o in outcomes
                              if o.total_events is not None), None)
    if all_rows and total_events:
        logger.info("Polymarket states this listing could show %d event(s); "
                    "this run took %d market(s). The two are not comparable "
                    "and the difference is not a gap — see "
                    "page_flow.page_gap.", total_events, len(all_rows))
    if all_rows:
        enriched = sum(1 for r in all_rows
                       if r.data_source and r.data_source != "dom")
        logger.info("Payload coverage over the merged run: %d/%d (%.0f%%) — "
                    "rows built from the site's own inlined payload "
                    "rather than from a rendered tile alone.",
                    enriched, len(all_rows), 100.0 * enriched / len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    extra = {
        "scroll": {o.page_num: o.scroll for o in outcomes if o.scroll},
        "rows_new_per_page": fresh_by_batch,
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
        # Byte-identical to the other two engines. A sidecar key that
        # differed between them would make two runs of the same URL look
        # like two different pagination models (§6).
        "pagination": ("listing+event-pages" if args.mode == "events"
                       else "single-page"),
    }

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url, extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Polymarket prediction-market scraper (pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="A Polymarket URL: a listing (/predictions, "
                        "/predictions/{tag}, /{category}, /predictions?q={q}) "
                        "or an event (/event/{slug}). A locale path such as "
                        "/es/predictions works and changes the wording, not "
                        "the ids or the prices. Required, unless --category "
                        "is given or POLYMARKET_URL is set in the "
                        "environment or .env.")
    p.add_argument("--mode", choices=["markets", "events", "event"],
                   default=None,
                   help="Which view to take. Inferred from the URL by "
                        "default; `events` is the one worth asking for — it "
                        "fetches the listing AND every event page under it, "
                        "which is where condition ids, CLOB token ids and "
                        "per-market volumes come from.")
    p.add_argument("--category", default=None,
                   help="A listing to fetch instead of --url: `crypto`, "
                        "`politics`, any tag the site publishes, or "
                        "`q:bitcoin` for a search. Also fills the row label "
                        "from the URL when --url is used.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Pages to fetch (default 1, cap {PAGE_CAP}). In "
                        f"--mode markets and --mode event there is exactly "
                        f"ONE page and asking for more is reported as "
                        f"complete with the reason: a listing renders twenty "
                        f"events and publishes no address for the "
                        f"twenty-first. In --mode events page 1 is the "
                        f"listing and pages 2..N are its event pages.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds (default 2.0). Nothing "
                        "was refused while this repo was built, so this is "
                        "politeness rather than a measured requirement.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and NOT "
                        "implemented in this engine — the worker pool lives "
                        "in playwright_scraper.py. Refused above 1 in "
                        "--mode markets and --mode event for a second "
                        "reason: those are one fetch each and there is no "
                        "address to hand a worker.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="polymarket_markets",
                   help="Output file prefix")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Use the browser at PATH instead of pyppeteer's own "
                        "Chromium. A convenience on this site rather than a "
                        "requirement: polymarket.com was measured serving a "
                        "plain `curl/8.0` the full page, so a two-year-old "
                        "bundled Chromium is served too. e.g. "
                        "'/Applications/Google Chrome.app/Contents/MacOS/"
                        "Google Chrome' or /usr/bin/google-chrome.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. Credentials are sent over CDP "
                        "(page.authenticate), never on the command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default). per-page cannot be honoured "
                        "mid-listing here.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Retries from other exits when a page is refused.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "challenge if the content is not already readable. "
                        "This repo implements reCAPTCHA v2, v2-invisible, v3 "
                        "and enterprise, and Cloudflare Turnstile including "
                        "the Challenge-page form. No challenge was met on "
                        "this site while it was written, so the path is "
                        "unexercised HERE — which is not a claim about what "
                        "a solver can do (§19). A page carrying no widget is "
                        "reported unsolved rather than charged for.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9).")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so the Scraping Browser API "
                        "endpoint works from this engine.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CONNECT_TIMEOUT, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {CONNECT_TIMEOUT}). Deliberately "
                        f"high, and identical to the Playwright engine's: a "
                        f"Scraping Browser provisions a browser when the "
                        f"WebSocket upgrade arrives, and one was measured "
                        f"taking 121s before the SERVER gave up. Giving up "
                        f"earlier than the server does leaves the profile held "
                        f"by a half-open session.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    # HEADLESS by default, and that is this site's own measurement rather
    # than the family's habit. §19 says to measure headless against headful
    # once per site because a sibling repo found 0 of 4 against 4 of 4; here
    # the answer came back IDENTICAL — 20 events, the same twenty, on both.
    # The site also served a `curl/8.0` User-Agent the full 791,003 bytes,
    # so it is not reading the client at all.
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=True,
                   help="Run headless. THE DEFAULT, and measured identical "
                        "to headful on this site (20 events both ways, "
                        "2026-09-16).")
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run with a real browser window. Measured identical "
                        "to headless here, so this is for watching a run. "
                        "Needs a display.")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url and args.category:
        args.url = listing_url_for(args.category)
        logger.info("Reading the %s listing: %s", args.category, args.url)
    if not args.url:
        p.error("no --url given, no --category given, and POLYMARKET_URL is "
                "not set in the environment or in .env.")
    why = unsupported_reason(args.url)
    if why:
        p.error(why)

    normalized = normalize_url(args.url)
    if normalized != args.url:
        logger.info("Fetching %s instead of %s — the site's own click "
                    "parameters are stripped so one market has one address.",
                    normalized, args.url)
        args.url = normalized

    kind = page_kind(args.url)
    if args.mode is None:
        args.mode = "markets" if kind == "listing" else "event"
        logger.info("Reading %s as a %s run.", args.url, args.mode)
    elif args.mode == "events" and kind != "listing":
        p.error(f"--mode events needs a LISTING URL to start from; "
                f"{args.url!r} is an {kind} page.")
    elif args.mode == "markets" and kind != "listing":
        p.error(f"--mode markets needs a listing URL; {args.url!r} is an "
                f"{kind} page. Leave --mode off and it is inferred.")
    elif args.mode == "event" and kind != "event":
        p.error(f"--mode event needs an /event/ URL; {args.url!r} is a "
                f"{kind} page. Leave --mode off and it is inferred.")
    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-batch cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.mode != "events" and args.pages > 1:
        logger.info("%s.", page_flow.one_page_note(args.url, args.pages))
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
