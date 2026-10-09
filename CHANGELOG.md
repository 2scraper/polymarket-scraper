# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as closely as a
CLI toolkit can: a **patch** means fixes, not that every flag is frozen. Where
a patch changes a default in a way you would otherwise discover from a bill or
from a row count, it leads the section in a blockquote.

## [Unreleased]

### Fixed

- **`events_on_page_1` under-reported the listing in `--mode events`.** The
  audit's last recommendation asks for a freshness and completeness report
  so a consumer can see the date, the walk type, the number of events and
  the gaps. The sidecar already carries all four — `finished_at`, `mode` and
  `pagination`, `total_events` and `events_on_page_1`, and `pages_failed`
  beside `pages_without_rows` — so checking that claim was a matter of
  reading a real sidecar rather than building anything. Which is how this
  turned up.

  The field was counted over the MERGED rows with `r.page == 1`, and that
  asks a different question once the walk has merged: a listing row is
  REPLACED IN PLACE by the deeper read from its event page, and the
  replacement carries that event page's number. So it counted the events
  whose rows happened not to be upgraded. Measured on a real ten-page walk:
  **3 reported where page 1 had named 11** — printed beside
  `total_events: 23475`, which is exactly the comparison a reader makes to
  judge how much of the catalogue a run saw.

  Right in `--mode markets`, where nothing is upgraded, which is why it
  looked fine. Counted from page 1's own rows now, in all three engines,
  with a check controlled against each engine separately — this is the shape
  of mistake that gets fixed in one of three.

- **The new virtualenv check failed a fresh clone on its first command.**
  Found by doing the thing that keeps finding this class of bug: cloning the
  published repo the way a stranger does, running `python3 -m venv myenv` in
  it, and typing `python3 smoke_test.py`. The only failure was a check added
  hours earlier — which asserted that EVERY directory holding a
  `pyvenv.cfg` is gitignored, "whatever its name".

  That is a promise `.gitignore` cannot keep: a glob cannot recognise a
  virtualenv, so the check demanded something no pattern set can satisfy and
  greeted a reasonable first command with a red suite. A guard somebody has
  to argue with on their first command is one they learn to suppress, and
  the next real finding goes with it — which is the failure this suite
  exists to avoid rather than to cause.

  The hard assertion now covers what the project can actually promise: the
  virtualenv names its own instructions lead people to. An unexpected name
  gets one line of output explaining what would happen and how to fix it,
  and the suite stays green.

  Two things inside that check were also wrong and are worth naming, because
  both passed for the wrong reason:

  * `git check-ignore` matches a trailing-slash pattern only against
    something it can see IS a directory, so asking about a name that does
    not exist in this checkout answered "not ignored" for five of the seven
    names the check had just been written to cover. It probes a path INSIDE
    the directory now.
  * `git check-ignore` skips TRACKED paths by default, and `.env.example` is
    tracked — so the assertion that it stays visible could never fail
    whatever the patterns said. Caught by planting the fault, removing the
    `!.env.example` negation, and watching the suite stay green. It passes
    `--no-index` now.

### Added

- **The canary now runs all three engines against the live site, daily.**
  The audit's third finding, and a fair one: the offline suite asserts the
  engines take the same flags, bind the same shared-module signatures and
  share one `finish_run` — so their exit codes and sidecars cannot drift —
  but none of that tests whether they still agree about the SITE, and only
  the primary engine ran against it on a schedule.

  The audit offered two remedies and this takes the second. The first was to
  declare Playwright the only supported engine and demote the others, which
  would contradict the README and throw away three working engines to close
  a gap that one job closes.

  Compared on **ids and schema, not on values**: the three runs are minutes
  apart and a prediction market reprices continuously, so requiring equal
  prices would produce a red badge about nothing. What must not differ is
  which markets were found, what the columns are called, and the fields a
  market does not change by trading — an explicit allowlist, inverted from
  the obvious blocklist so a column added later is not compared by default
  and does not start failing the badge the first time the market moves. The
  allowlist is itself checked against the real columns, or it could shrink
  to nothing and the step would pass by comparing almost nothing.

  `data_source` is in that allowlist on purpose: if one engine fell back to
  the DOM while its twins read the payload, every other field would still
  match and only that column would say so.

  Verified by running it here before shipping it: all three engines on one
  event page returned the same five markets, the same 40 columns and the
  same 22 stable fields, with `scraped_at` the only field that differed.

- **The Docker base image is pinned by digest**, with the tag kept beside it
  so a reader can see which release the digest is. `python:3.12-slim` is a
  moving target, so the same Dockerfile built a month apart is a different
  image: a release and its own rebuild are not the same artefact, and a
  regression arriving through the base reads as a regression in this code.
  Built and run after pinning — 53 markets live from inside the container.

### Deliberately not done

- **Lock files for the Python dependencies**, which the audit recommends in
  the same breath as the Docker pin. The two are not the same trade. A lock
  file would stop the daily canary from testing new releases of `requests`,
  `beautifulsoup4` and the engine libraries — and that daily test is the
  thing that catches a dependency change the morning it happens rather than
  whenever someone next bumps a pin. The image is a build artefact and
  should be reproducible; the canary is a detector and should be exposed.
  Lower bounds plus a daily live run is the combination that reports
  breakage, and pinning both halves would buy reproducibility by turning the
  detector off.

- **A run identifier across the JSON, CSV and sidecar set.** The failure it
  guards against is already impossible by construction: a failed run writes
  no sidecar at all, `save` refuses to overwrite good output with an empty
  result, and as of this release each file is replaced by an atomic rename.
  A transaction id would add a second thing to keep consistent, which is a
  second thing that can disagree.

### Fixed

- **An interrupted write destroyed the previous good run's output.** The
  audit's second finding, and real. `open(path, "w")` truncates before a
  single byte is written, so a crash, a kill or a full disk partway through
  `json.dump` left a SHORTER file where a complete one had been. Measured
  here before the fix: a 204,292-byte `out.json` came back **28 bytes and
  invalid JSON**.

  That breaks the same promise `save` keeps when it refuses to overwrite
  good output with an empty result — by a different route, with the previous
  run destroyed by the ATTEMPT to replace it rather than by its outcome. The
  sidecar is the part that matters most and the part the audit did not
  mention: `<out>.meta.json` is the file a consumer branches on, so a
  truncated one beside good rows reads as a broken run over data that is
  fine.

  All three writers now go through one `_atomic` helper — a temporary file
  in the TARGET's own directory (`os.replace` is atomic only within one
  filesystem), `flush` + `fsync` before the rename, and the temp file
  unlinked on any exception including `KeyboardInterrupt`.

  Lifted from a sibling rather than written here, because the one part that
  is easy to get wrong was got wrong across the family: `NamedTemporaryFile`
  creates its file `0600` and a rename keeps that, so an output nobody else
  can read is the default. Measured 2026-10-09 by CALLING each sibling's
  writer and stat-ing the file it produced — **8 of 11 leave their output
  0600**, one hardcodes `0644`, and two derive the mode from the umask. This
  takes the third kind: a new file gets what `open()` would have given it,
  and a target somebody tightened deliberately keeps its own mode.

  `newline=""` is threaded through for the CSV, or the csv module doubles
  the carriage return on every row.

  Checked with its own control: the suite asserts that a truncating writer
  really does destroy the file before asserting that this one does not, so
  "the file survived" cannot be a property of the test.

- **`--mode events` stopped at the first event page with no markets and
  reported the run as `complete`.** Found by a third-party audit of 8
  October and real. Reproduced against `main` end to end: one empty page at
  position 4 of 20 left **17 event pages never fetched**, with `status:
  complete`, exit 0, and `pages_completed: 4 of 21` in the same sidecar —
  a file contradicting itself, the way a run with FAILED pages used to
  before that case was fixed.

  The rule arrived from a sibling whose pages are consecutive days, where
  stopping early genuinely saves fetches. Here pages 2..N are the event URLs
  page 1 named, capped at `--pages - 1`, so there is no catalogue left to
  exhaust and nothing to save: the queue is exactly as long as the listing's
  own event list. The walk now visits every planned URL, records the empty
  ones in the sidecar as `pages_without_rows`, and keeps `complete` for a
  run that actually finished its plan. `no_new_products` is set nowhere now
  and has been removed from `COMPLETE_STOP_REASONS` rather than left as
  policy with no reader.

- **Every page the site serves was classified as its own 404 — and 12 of 20
  event pages had every row discarded because of it.** Found while measuring
  whether an empty event page is an edge case; it is not, because the
  classifier was manufacturing them. The two defects compound into a
  silently truncated run reported as complete, which is worse than either.

  Next.js inlines its 404 TEMPLATE — a `"notFound":[…]` branch the router
  would render if the route had 404'd — into the payload of every page.
  `looks_not_found` scanned the first 200,000 bytes for that template's
  wording. Counted 2026-10-09 over the twenty event pages `/predictions`
  named, as the site serves them: the wording is on **20 of 20**, inside the
  scanned window on **17 of 20** (always at offset ~134,000), rendered into
  the body markup on **0 of 5** checked — and **12 of 20 held markets and
  were read as "this address does not exist"**, 5 to 53 rows each, thrown
  away by `empty`'s `parse: False`.

  So the markers are gone rather than narrowed: a candidate that is on every
  good page is not a marker, it is a fact about the site. The content check
  moved to the top, where a page whose payload names markets is content
  whatever templates it also ships. Two measurements decided the shape of
  the fix: a slug that does not exist answers **HTTP 200** (4 of 4), so the
  status was never the backstop the markers were assumed to have — the
  docstring promising "a real HTTP 404" was simply wrong — and `"digest"`,
  which looked like a clean replacement, is on 2 of 10 good pages and was
  not adopted.

  The browser engines escaped this by byte position rather than by design: a
  browser serialises rendered markup first and puts the template at 511,547
  where the served bytes put it at 134,033, on the same URL. The margin on
  the smallest page measured was 67 KB. `scraper_api_client.py` reads the
  served bytes and had no margin at all.

- **A nonexistent event cost 67 seconds instead of 8**, as a direct
  consequence of the reclassification above: `shell` spends the readiness
  wait and four scroll rounds, every round logging "added no events
  (0 -> 0)". That wait exists for one case — the payload never arrived — so
  it is now gated on the payload ARRIVING rather than on it naming markets
  (`page_flow.payload_arrived`, consulted by `is_unpainted` and by all three
  engines' fast path). Back to 7.8s, same exit 4.

- **The tree-wide scanners walked into a virtualenv.** Not in the audit —
  the suite failed on its own working tree. The scanner widened to the whole
  tree in the previous release reported that pip's vendored
  `charset_normalizer` "describes this site", because it skipped virtualenvs
  by NAME and this one is called `.venv-pw`. `.github/ci_checks.py` had
  already been taught to recognise a venv by `pyvenv.cfg`; the four scanners
  in `smoke_test.py` had not. It is the first thing a new user sees from
  `python3 smoke_test.py`, since the README tells them to make a virtualenv
  per engine and never says where.

- **A virtualenv in the working tree was not ignored.** `.gitignore` listed
  `.venv`, `venv` and `env`; the one made while working on this change is
  called `.venv-pw`, so it was untracked and NOT ignored — one `add -A` from
  being committed, which is the same name-versus-kind mistake as the
  scanners above. The patterns are broader now, and because a glob can
  always be out-named, the suite also asserts that every directory holding a
  `pyvenv.cfg` really is ignored.

### Added

- **`captures/event_served_bytes.html` — the first capture of what the HTTP
  client path actually sees.** All eleven existing captures are a browser's
  `page.content()`, which is a different document shape from the same URL,
  and that is precisely why the 404-template defect was invisible to a green
  suite. `make_fixtures.py` now carries the `"notFound"` branch through the
  trim on purpose: without it the new checks could not fail, which the first
  version of this fixture demonstrated by passing against the unfixed
  parser.

- **A check that every tree-wide scanner skips virtualenvs structurally**,
  pinned as wiring rather than by content. The scanners fail three different
  ways — a false FAILURE on the describes-this-site scan, a false PASS on
  the dead-name corpus (vendored code keeps a dead name alive), and no
  effect at all on the banned-wording scan — and only the first is visible
  from a planted fault.

- **The repo is public**, and the published surfaces are now checkable. Its
  GitHub description had shipped carrying the wording §12 bans for the
  Scraping Browser API, straight out of the family template — caught by hand
  minutes before publishing, because the suite scans FILES and a description
  lives behind an API. `.github/repo-metadata.yml` now holds the intended
  description, homepage and topics where the wording checks scan them, with
  the command that applies them beside it, and `test_wording` scans the
  whole tree instead of only the top level (which is why `.github/` had
  never been checked at all).

- **A fixture fetched over the Scraping Browser, and the census behind it.**
  Asked which captcha the site had, the honest answer needed a count rather
  than a re-reading of the code — and the count found that every captcha
  string this project has seen on polymarket.com is injected by 2Captcha's
  own auto-solve extension. The same URL, the same minute: 0 of everything
  from the site, 21 `captcha` / 16 extension scripts / 1 `cf-turnstile` over
  the Scraping Browser. All eleven previous fixtures came from a local
  browser or curl, so the marker checks would have passed with
  `cf-turnstile` in the set; now they do not (§21).

- **A README section on the container**, which the repo shipped and
  documented nowhere. Measured rather than described: the image is 1.34 GB,
  its entrypoint answers `--help`, a run inside it returns the same 70
  markets a local browser does, and it carries no `.env`, no test suite and
  no fixtures. CI has built it on every PR from the start; this is the first
  time it was built and RUN by hand (§16).

### Fixed

- **The Scraper API path sent `waitFor` in a form the live API rejects,
  and read the wrong field as the target's status.** Measured 2026-09-23
  against `scraper.2captcha.com/tasks/sync`: `waitFor` sent as a
  JSON-encoded string (what this client built for every `--wait-*` flag)
  is answered HTTP 422 "params.waitFor must be an object" and is still
  billed ($0.0005); the same request with an object gets HTTP 200. It is
  now an object. And the response's `status` is the API's own verdict
  ("success"), not the target site's HTTP code, which is `http_code` —
  so a target 403 or 503 never reached the page classifier. The target
  status is now read from `http_code` (falling back to `status` only if
  that is an integer). Pinned by an offline check that drives the real
  client with `requests.post` stubbed; verified by control (red with the
  old client).

- **Donor-repo leftovers that described another site as this one.** The
  family core arrived from sibling repos about a blogging site, a Q&A site
  and a rental site, and some of their prose and runtime strings survived:
  - user-visible: Selenium logged "No property cards appeared … this search"
    and pyppeteer "No answer cards appeared … this feed"; both now say "No
    event tiles … this listing", as Playwright already did. pyppeteer's block
    advice claimed its bundled Chromium was "the build this site was
    measured refusing" — false here, where it was measured served; it now
    names the build and says so. The Scraper API client's block message
    cited a sibling's 2026-09-10 datacentre refusal, its `--help`
    description a sibling's archive payload and author pages, and its
    `--url` help ended in a broken sentence about a topic's answers;
  - the sidecar's `stop_reason` for an unrecognised refusal was
    `blocked_bot-or-not`, a sibling's challenge-page name; it is now
    `blocked_not-served`;
  - `output_writer.run_meta` / `finish_run` defaulted `mode` to a sibling's
    `"topic"`, which `diff_runs.py` would have refused as not one row per
    sku. The engines always pass `--mode`, so no run was affected; the
    default is now `"markets"`;
  - `diff_runs.py`: its `--price-tolerance-pct` help and three comments
    explained a sibling's live counters, per-story languages and missing
    lifecycle, and its host-mismatch refusal said the site "serves one story
    from several addresses". All now describe markets;
  - `captcha_solver.py` said this site refuses a headless browser with a
    394-byte "Access Denied" page. The README measures headless and headful
    identical and no challenge met; the paragraph now says that;
  - `smoke_test.py`: the docstring described a sibling's column, sign-in
    widget and a test this repo does not have, and claimed the fixtures
    pseudonymise author handles (`make_fixtures.py` rewrites nothing). The
    worker-pool test fed a sibling's archive URLs and counted "days"; it now
    uses event-page URLs, asserting the same behaviour;
  - `.dockerignore` excluded a sibling's output prefix instead of this
    repo's `polymarket_markets.*`; the bug-report template's "expected"
    placeholder quoted a sibling's 96 products; `SECURITY.md` said the
    project has no releases.

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
