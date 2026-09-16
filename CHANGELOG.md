# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as closely as a
CLI toolkit can: a **patch** means fixes, not that every flag is frozen. Where
a patch changes a default in a way you would otherwise discover from a bill or
from a row count, it leads the section in a blockquote.

## [Unreleased]

## [0.1.0] — 2026-09-16

The first release of this repo as a member of the 2scraper family. It
replaces three standalone scripts that read Polymarket's Gamma API with a
browser-based scraper of the site's own pages, sharing the family's row
schema, exit codes, run metadata, proxy pool, captcha path and test suite.

### Added

- **Three modes, one row per MARKET.** `--mode markets` reads a listing page
  (`/predictions`, `/predictions/{tag}`, `/{category}`, `?q={query}`);
  `--mode event` reads one `/event/{slug}`; `--mode events` reads the listing
  and then every event page under it, which is the only multi-page path here
  and the only one where `--concurrency` means anything.
- **Four engines behind one contract** — Playwright (primary, with the worker
  pool), Selenium, pyppeteer and 2Captcha's Scraper API. All three browser
  engines were run live against the same URL and returned identical rows,
  identical sidecars and identical exit codes.
- **Forty columns**, including the order book (`best_bid`, `best_ask`,
  `spread`, `last_trade_price`), the volume windows, the event breadcrumb,
  and — from an event page — `condition_id` and both `clob_token_ids`, which
  are what Polymarket's own CLOB API keys on.
- **`volume_scope`**, because a listing publishes the EVENT's volume and an
  event page the MARKET's own, and the two are not comparable.
- **A sidecar per run** carrying the site's own `total_events` beside the
  twenty events a listing actually renders.
- **Cloudflare Turnstile support** in all three engines: an interception
  script installed on the context before any page script runs, because a
  Challenge page publishes no sitekey in its markup and no static read can
  produce a solvable task. reCAPTCHA v2/v2-invisible/v3 and enterprise are
  implemented too.
- **Twelve fixtures cut from real captures** by `make_fixtures.py`, which
  proves each one parses identically to its untrimmed original, column for
  column, and that the trim did not change how the page classifies.

### Measured, 2026-09-16

From one datacentre address in Finland, headless, no proxy and no key:

- `/predictions` → 92–94 markets from 20 events in about 8 seconds; the same
  page served to `curl/8.0` byte for byte (791,003 bytes), so the site does
  not read the client;
- **a listing is ONE page.** `?page=2`, `?_p=2` and `?offset=20` each answer
  with the same twenty events; twelve scroll rounds add none; the "Show more
  markets" button expands one card. The site's own state says
  `"totalCount":21511,"hasNextPage":true` behind a cursor the interface never
  spends;
- **headless and headful are identical** here (20 events both ways), which is
  why headless is the default;
- **no captcha was met at all** — zero markers of any vendor and zero captcha
  configuration across twelve captures. That is what happened, not what is
  possible: the site is behind Cloudflare and this repo implements the solve;
- **a second address agrees.** The canary's first dispatch, on a bare GitHub
  runner with no proxy, returned 70 markets from 20 events on the `--mode
  events` walk and 92 from 20 on `/predictions` — the same twenty events per
  listing from another continent.

### Fixed during the rebuild

Each of these was found by running the thing rather than by reading it (§15):

- **An event page parsed to zero rows** when its payload shipped no event
  object. The scanner tracked a brace stack through the RSC stream, and the
  site's own inlined bootstrap script contains a brace inside a single-quoted
  string — which desynced the stack and lost every object after it. The
  enclosing object is now found by decoding candidates backwards from the key,
  which cannot desync. The capture that broke it is a committed fixture.
- **An event page returned its rails' markets as its own** — 21 rows for a
  single-market event, 25 for one with five. Rows are now scoped to the event
  the URL asked for.
- **`--mode events` threw away what it fetched.** Every deep row arrived as a
  duplicate of a shallow one and the dedupe kept the shallow. The merge now
  replaces a shallow row with the deeper reading in place, keeping the
  listing's own order.
- **The Selenium engine crashed on its first fetch** with a `NameError` in
  the Turnstile branch — invisible to import, `--help` and `compileall`.

### Inherited from the family core and fixed here

- `requirements*.txt` described a different site entirely (a sibling's, three
  files' worth), including an instruction to install real Chrome that is
  unnecessary here.
- An issue template described two other sites at once.
- `scraper_api_client.py` imported a parser function that no longer exists.

[Unreleased]: https://github.com/2scraper/polymarket-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/polymarket-scraper/releases/tag/v0.1.0
