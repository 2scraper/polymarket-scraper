#!/usr/bin/env python3
"""polymarket-scraper — Selenium edition

A parity engine. It must agree with the other two on exit codes, run status,
and whether a run crashes or spends money — the shared modules
(`product_parser`, `page_flow`, `output_writer`, `proxy_pool`,
`captcha_solver`) are what keep it honest, and this file holds only "how to
ask Selenium".

Read playwright_scraper.py's docstring for what is different about this SITE.
The short version: a listing URL is ONE page of twenty events with no address
for the twenty-first, everything is server-rendered into the page's own
payload, and nothing refused us from a datacentre address — a plain `curl`
was served the full 791,003 bytes.

Three things are different about this ENGINE:

* **It drives the installed Chrome, and there is nothing to choose.**
  chromedriver has no bundled browser, so there is no browser-channel flag
  here. On this site that costs nothing either way: the page was served to
  every client tried, including `curl`.

* **It cannot authenticate a proxy, and it cannot use an authenticated CDP
  endpoint.** `--proxy-server` takes an address with nowhere to put a
  password, and chromedriver's `debuggerAddress` takes a bare `host:port`.
  Both are reported loudly rather than silently half-working. Use the
  Playwright or pyppeteer engine for either.

* **`--concurrency` above 1 is not implemented here.** The worker pool lives
  in playwright_scraper.py. This engine walks the same event pages one at a
  time and produces identical rows, exit codes and metadata — it just takes
  longer. Said out loud rather than run as one worker quietly, which would
  look like the flag did something.

Examples
--------
    python3 selenium_scraper.py --url "https://polymarket.com/predictions"

    python3 selenium_scraper.py --category politics --mode events --pages 21

    python3 selenium_scraper.py \\
        --url "https://polymarket.com/event/fed-decision-in-september-762"
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

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
logger = logging.getLogger("selenium_scraper")

# Explicit, because a driver that stops answering otherwise hangs the run:
# "every remote call is bounded" applies to this engine too.
PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Kept identical to the Playwright engine's, and the smoke suite asserts it:
# a floor that differed between engines would mean one of them warning about
# a page its twin called healthy.
FIELD_FLOOR = 95


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Global, not first-match: an error can repeat an endpoint several times,
    and a masker that handles one occurrence prints the password for the
    rest while looking like it works.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` out of a CDP endpoint, refusing one with credentials.

    Selenium cannot use an authenticated remote CDP endpoint at all, and this
    is the one place to say so. Playwright's `connect_over_cdp` and
    Puppeteer's `browserWSEndpoint` take a full `ws://user:pass@host:port`
    and authenticate on the WebSocket upgrade; chromedriver's
    `debuggerAddress` takes a bare `host:port` with nowhere to put a
    password. Silently stripping the credentials would produce a connection
    refusal a long way from its cause.
    """
    parts = urlsplit(endpoint)
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials, and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "The 2Captcha Scraping Browser API endpoint is authenticated, so "
            "it cannot be used from this engine — run playwright_scraper.py "
            "or puppeteer_scraper.py for it. Endpoint: %s",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


@dataclass
class PageOutcome:
    """What one page produced. Same shape as the other two engines'."""
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


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            self._install_turnstile_intercept()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # 1440x900 matches what the captures were taken at. The viewport
        # decides how the grid is laid out, and the captures this repo's
        # numbers come from were taken at this size.
        options.add_argument("--window-size=1440,900")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with a
        # bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        # Chrome's performance log, which is how this engine counts refused
        # refused responses — see `_refused_count`. It has to be
        # asked for at driver creation; there is no way to turn it on later.
        # Without it this engine would report `complete` where its twins
        # report `partial` on the identical run (§6).
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        self._install_turnstile_intercept()
        _watch_refusals(self)

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        # THROUGH THE SHARED HELPER, never by reaching into the response
        # shape here. This line dug the UA out of the response itself until a
        # live call to the API showed what it actually returns: the UA is at
        # `userAgent.userAgent` in the chromium format and at `data.ua` in
        # the raw one, and the key this engine asked for exists in NEITHER.
        # So `--fingerprint`
        # silently set no user agent at all and the browser kept its own —
        # which defeats the flag rather than breaking it, because the run
        # then presents a Windows fingerprint's screen, locale and timezone
        # over a local Chromium's UA. That is the identity MISMATCH the flag
        # exists to avoid (§16, where the same defect was live in four
        # sibling repos at once).
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def _install_turnstile_intercept(self):
        """Hook `turnstile.render` before any page script can run.

        THE ONLY MOMENT a Cloudflare Turnstile's parameters can be captured:
        a Challenge page calls `turnstile.render(container, params)` once and
        keeps nothing, while `TurnstileTaskProxyless` needs the sitekey,
        action, cData and chlPageData that live only inside that call.
        Selenium spells it `Page.addScriptToEvaluateOnNewDocument` over CDP;
        the other two engines spell the same thing `add_init_script` and
        `evaluateOnNewDocument`, which is why this cannot live in the shared
        module (§1).

        Harmless where there is no Turnstile — every page measured on this
        site so far — because it wraps a function that never appears and
        stops watching after 30 seconds.
        """
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": TURNSTILE_INTERCEPT_JS})
        except WebDriverException as e:
            logger.debug("Could not install the Turnstile interception: %s", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is exactly why page_flow
# names operations instead of passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.driver.find_elements(By.CSS_SELECTOR, selector))
    except WebDriverException as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


def _content(session) -> Optional[str]:
    try:
        return session.driver.page_source
    except WebDriverException as e:
        logger.debug("page_source unavailable (page navigating?): %s", e)
        return None


def _scroll_to_bottom(session) -> None:
    """Scroll the WINDOW to the end of the document. See page_flow.

    Note the dialect: Selenium's `execute_script` takes a function BODY with
    an explicit `return`, where Playwright and pyppeteer take `() => expr`.
    That difference is exactly why `page_flow` names the OPERATION and each
    engine spells it itself (§1).
    """
    try:
        session.driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight); return null;")
    except WebDriverException as e:
        logger.debug("scroll failed: %s", e)


def _page_height(session) -> Optional[int]:
    try:
        return session.driver.execute_script(
            "return document.body.scrollHeight;")
    except WebDriverException:
        return None


# Counting the responses the site refused. The other two engines get this
# from a response listener, which Selenium has no equivalent of — so it is
# read out of Chrome's performance log instead.
#
# This is NOT a cosmetic difference. If this engine always answered 0, it
# would report `complete` where its twins report `partial` on the identical
# run, and that is precisely the drift the shared modules exist to prevent
# (§6). The log has to be ENABLED at driver creation; see `_Session.open`.
_SITE_HOST_FRAGMENT = "polymarket.com"


def _watch_refusals(session) -> None:
    """Reset the running count. The log itself is enabled on the driver."""
    session._refused = 0
    _refused_count(session)   # drain anything already buffered


def _refused_count(session) -> int:
    """How many of the site's responses have come back >= 400 this session.

    Chrome's performance log is drained on read — each `get_log` call returns
    only entries since the last one — so this accumulates rather than
    recounting, and callers take a difference across the window they care
    about.
    """
    import json as _json
    total = getattr(session, "_refused", 0)
    try:
        entries = session.driver.get_log("performance")
    except Exception:  # noqa: BLE001 — an absent log must never break a run
        return total
    for entry in entries:
        try:
            message = _json.loads(entry.get("message", "{}"))["message"]
            if message.get("method") != "Network.responseReceived":
                continue
            response = message["params"]["response"]
            if _SITE_HOST_FRAGMENT in response.get("url", "") \
                    and int(response.get("status", 0)) >= 400:
                total += 1
        except Exception:  # noqa: BLE001
            continue
    session._refused = total
    return total


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _current_url(session) -> str:
    try:
        return session.driver.current_url
    except WebDriverException:
        return ""


def _classify(session, html: str, status=None) -> str:
    # `status` is POSITIONAL and second. Two engines in a sibling repo passed
    # it as a keyword and both crashed on their first fetch (§17); this
    # repo's smoke suite binds every shared-module call in every engine
    # against the callee's real signature for that reason.
    return page_flow.classify(html, status, _current_url(session))


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    return parse_markets(html, url, page=page_num, mode=args.mode)


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same contract and same reconciliation as the Playwright engine. No
    challenge was met on polymarket.com while this repo was built, so the
    path is UNEXERCISED here — which is a fact about this repo's testing and
    never a claim that a captcha on this site could not be solved (§19).
    """
    driver = session.driver
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    url = _current_url(session)
    html_challenge = detect_recaptcha_v3(html, url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        # No reCAPTCHA. Turnstile is the other thing 2Captcha can solve, and
        # the RUNTIME reading is the one that matters: a Cloudflare Challenge
        # page publishes no sitekey in its markup, so only the interception
        # installed through `Page.addScriptToEvaluateOnNewDocument` before
        # any page script ran can produce a solvable challenge. The static
        # read is kept as the fallback for a standalone widget, whose sitekey
        # IS in the markup.
        challenge = (wait_for_turnstile(
                         lambda js: driver.execute_script(f"return ({js})();"),
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
        driver.execute_script(f"return ({TURNSTILE_INJECT_JS})(arguments[0]);",
                              token)
    else:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);",
                              token)
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        driver.refresh()
    except WebDriverException as e:
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _scroll_the_feed(session, args, html: str, page_num: int) -> dict:
    """Scroll to the bottom until the feed stops growing.

    RUN ONLY WHEN THE PAYLOAD DID NOT ANSWER — see the Playwright engine's
    copy for the measurements. Short version: this site ships its markets in
    the page's own payload and twelve scroll rounds added zero events, so
    this is the fallback path's safety net and not the main event.

    Deliberately no `target`. The only total this site publishes is
    `totalCount` — 21,511 for `/predictions` — which is the size of the whole
    catalogue rather than of this page. Using it to decide when to stop
    scrolling would scroll forever.
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
    """Fetch (or turn to) one page and parse it.

    `url` is the address for page 1 and None for every page after it: pages
    2..N on this site are not addresses, they are the result of pressing the
    site's own button.
    """
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
                    session.driver.get(url)
                    load_failed = False
                    break
                except (TimeoutException, WebDriverException) as e:
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d: "
                                       "%s) — retrying in %.1fs.", url,
                                       attempt, args.retries,
                                       str(e)[:120], pause)
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
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_polymarket(html or "")
        vendor = detect_bot_challenge(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).", len(html or ""),
            "which references" if served else "with no reference to",
            debug_html)
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "bot-or-not")
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
            logger.info("No property cards appeared within %.0fs. If this "
                        "search genuinely matches nothing, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).", content_timeout / 1000)
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
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
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

    session = _Session(args, pool).open()
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
        description="Polymarket prediction-market scraper (Selenium edition)")
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
    p.add_argument("--locale", default="en-US",
                   help="Browser locale. It does NOT decide the site "
                        "language: the HOSTNAME does.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy — credentials are stripped with a warning. Use "
                        "playwright_scraper.py or puppeteer_scraper.py for "
                        "an authenticated one.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default). per-page cannot be honoured "
                        "mid-feed here — the next batch lives inside the "
                        "current browser session.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Retries from other exits when a page is refused.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag: Windows, Microsoft Windows or "
                        "Android. NOT a list — Chrome, Desktop and Mobile are "
                        "each rejected by the API with 400.")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2.")
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
                   help="Attach to a running browser at host:port. NOTE: "
                        "Selenium cannot use an AUTHENTICATED endpoint — "
                        "chromedriver's debuggerAddress has nowhere to put a "
                        "password — so the Scraping Browser API is not "
                        "reachable from this engine.")
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
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
