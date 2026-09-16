# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as closely as a
CLI toolkit can: a **patch** means fixes, not that every flag is frozen. Where
a patch changes a default in a way you would otherwise discover from a bill or
from a row count, it leads the section in a blockquote.

## [Unreleased]

## [0.1.1] — 2026-09-16

An audit of v0.1.0 against the family's own checklist, done by MUTATION —
breaking one thing at a time and watching whether the suite noticed — rather
than by re-reading the code. Eight defects this family has shipped before
were injected; six were caught immediately and two were not.

> **Correction to v0.1.0's notes.** That section was edited after the tag was
> cut, adding a paragraph about the credential-gated paths. A released
> section is history — `git show v0.1.0:CHANGELOG.md` is what a reader can
> check it against — so it has been restored verbatim and the paragraph moved
> here, where it belongs (§19).

### Fixed

- **`diff_runs.py` compared columns that do not exist here.** It arrived
  from a sibling tracking seven fields, not one of which is a column in this
  repo's schema, so two runs diffed against each other would have reported
  "no changes" for ever, on any input, while exiting 0. `TRACKED_FIELDS` now
  names what a change IS on a prediction market — the price, the order book
  around it, the volume windows, the state transitions that end a market's
  life, and the question being re-worded — and the suite asserts every name
  in it is a real column of `Market`.
- **A `markets` run diffed against an `events` run reported a collapse that
  never happened.** `volume_scope` moves between the two while `data_source`
  stays `flight` on both, so `volume` went from the event's 199,000,000 to
  the market's 19,000,000 and read as a change. `volume_scope` now decides
  "which view" alongside `data_source`, and those rows land in
  `source_changed` where they belong.
- **A test that could not fail.** Deleting the event-scoping rule outright
  left the suite green: `make_fixtures.py` had trimmed each event fixture
  down to its own event's markets, so there were no rails left to leak.
  The generator now keeps two of the rails' markets on purpose, and the same
  mutation is caught by ten checks.
- **`scraper_api_client.py` logged "Parsed 70 stor(ies)"** through a full
  live run — a sibling's vocabulary surviving in a shipped file.

### Added

- **The vocabulary guard**, which is what caught the last of those: the
  file-describes-another-site check now also bans the words a copied
  paragraph keeps after the site's name has been swapped out — `stor(ies)`,
  `claps`, `day archive`, `parse_posts`. Each was counted across the repo
  before being banned, so a word that occurs legitimately here is not in the
  list (§18).
- **A check that every field `diff_runs.py` tracks is a real column**, which
  is the check that would have caught the first defect above.
- **A page+position check that spans TWO pages.** The existing one only ever
  saw page 1, so it passed happily on a parser that hardcoded `page = 1` —
  which is §18's arithmetic bug, where 60 of 119 rows silently claimed a
  position another row already had. Proven by making that change and
  watching the new check fire.

### Measured, 2026-09-16

- **every credential-gated path was run end to end** (§16), and all of them
  return the same 70 markets a local browser does: the Scraper API at
  $0.0005 for 766,857 bytes in 11s, the Scraping Browser over CDP, and a
  fingerprint fetched and applied (user agent, locale `en-US`, timezone
  `America/New_York`). The Fingerprint API's documented multi-tag example is
  rejected with HTTP 400 and this repo's default is the single `Windows` tag
  that works — the §17 defect that made `--fingerprint` inert in four
  sibling repos is not present here.

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

[Unreleased]: https://github.com/2scraper/polymarket-scraper/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/2scraper/polymarket-scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/2scraper/polymarket-scraper/releases/tag/v0.1.0
