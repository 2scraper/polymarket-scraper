# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Polymarket changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH of
the three read paths broke, because this repo has three and they fail
differently.

**Path 1 — the inlined Next.js payload.** Every page ships its own data in
React Server Components chunks:

```
self.__next_f.push([1, "…\"outcomePrices\":[\"0.12\",\"0.88\"]…"])
```

Decoded and concatenated, those chunks contain the site's own market objects
verbatim — slug, numeric id, `outcomePrices`, volumes, and on an event page
the on-chain `conditionId` and both CLOB token ids. Every healthy run reads
this and nothing else.

If the push pattern moves, a run does NOT fail. It drops to the tiles, and
then every row is EVENT-level: keyed by the event slug, priced from the
tile's rounded percentage, with a rounded volume and no market id at all.
The `data_source` column is what shows it — every row reads `dom` where it
used to read `flight` or `flight+jsonld`.

Two things about that payload are worth knowing before you touch the
scanner. It is **not JSON**: Next.js inlines the site's own bootstrap script
into the same stream, and that script contains a brace inside a
single-quoted string. A scanner that tracks double-quoted strings only —
which is what a JSON scanner tracks — desyncs there and loses every object
after it. That is a real regression this repo shipped for an afternoon, and
`test_payload_decoding` pins the capture that caught it. And an event page
ships the markets of everything in its **rails** as well as its own, so the
parser scopes rows to the event the URL asked for; without that, a run for a
single-market event returned twenty-one rows.

**Path 2 — JSON-LD.** A listing publishes `CollectionPage → ItemList` with
one `Event` node per tile, each carrying ONE price and the currency. It is
used to confirm the payload's price (recorded as `data_source="flight+jsonld"`)
and to read the currency, which the payload never states. An EVENT page
publishes a different `@type` whose `offers.price` is `"0"` for markets
trading at 0.07 and 0.91 — a placeholder, and deliberately not read.

**Path 3 — the rendered tiles.** Anchored on the `/event/{slug}` href and
never on a class name: the classes here are Tailwind utilities. The tile
scope widens to the outermost ancestor still covering exactly ONE event,
counting distinct event slugs rather than links — a tile links its event two
or three times.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its WARNING branch, which is what runs when a bare GitHub
   runner's datacentre address is refused and no `POLYMARKET_PROXY` secret is set.
   This canary needs no secret to do real work: eight of fourteen fetches
   were served in full with no key and no proxy, from a DATACENTRE address
   at that. What it has NOT been measured doing is getting past
   Cloudflare from a shared datacentre address, and since the challenge here
   tracks the address's recent request rate, a runner is the worst case for
   it. That is exactly why a block there is a warning rather than a failure —
   until you set `POLYMARKET_PROXY`, after which it is a failure, because then it
   means something.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Ten properties in this repo exist because they were once absent, or because
they cost a sibling repo real time. Tests pin all ten, so a PR that breaks one
will fail rather than silently regress:

- **`sku` is the market SLUG, not its numeric id.** Both are unique and both
  are in the payload; the slug wins because every path can produce it — the
  payload carries it, the DOM carries it in the href, and it survives in a
  `--dump-html` capture. `market_id` and `condition_id` carry the numeric and
  on-chain ids beside it. A consumer tracking one market across months should
  join on `condition_id`, because a re-worded market gets a new slug.
- **`price` is the price of `outcomes[0]`, not "the Yes price".** Most
  markets here are binary and read `["Yes","No"]`, but a sports market reads
  `["Alex de Minaur","Stefanos Tsitsipas"]` or `["Over 2.5","Under 2.5"]`.
  Five of eleven markets on one captured listing had no "Yes" in them.
- **The outcome labels are read by INDEX and never by matching a word.** An
  event page under a locale path ships `["Sí","No"]` where the listing under
  the same path ships `["Yes","No"]`. A parser keyed on "Yes" is correct on
  one page kind and silently empty on the other.
- **`volume_scope` says whose volume a row carries.** A listing ships the
  EVENT's volume (199,856,030 for one) and an event page the MARKET's own
  (19,376,636 for one of its five). Both are correct and they are not
  comparable.
- **A listing is ONE page, and `listing_has_one_page` is a COMPLETE stop
  reason.** `?page=2`, `?_p=2` and `?offset=20` each answer with the same
  twenty events; scrolling adds none. A run that asked for more is complete
  rather than partial, and says so in the log and in the sidecar.
- **`--mode events` upgrades rows rather than duplicating them.** Page 1 is
  the listing and names every market shallowly; pages 2..N carry those same
  markets from their own event pages. `output_writer.merge_pages` replaces
  the shallow row in place, keeping the listing's order and taking the deeper
  reading.
- **An event page's JSON-LD price is a placeholder and is not read.** It is
  `"0"` on every capture, for markets trading at 0.07 and 0.91.
- **A marker that matches every good page is not a marker.** Every marker in
  `BOT_CHALLENGE_MARKERS` is asserted ABSENT from all ten good-page fixtures,
  and `cf-turnstile` is deliberately not among them: 2Captcha's own Scraping
  Browser extension injects a `cf-turnstile-response` hunter into every page
  it loads.
- **A page that was SERVED, links to events and parses to zero rows is OUR
  bug.** It gets its own stop reason (`parser_found_nothing`) so a reader
  goes to `product_parser.py` rather than checking their URL for a typo.
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote API error, `6` partial. A
  pipeline branches on these. And a scroll that produced nothing new is
  `complete` only when nothing behind it was refused — otherwise it is
  `partial`, because a throttled run reporting "complete" is the failure
  §7 exists to prevent.

Two more that are about the fixtures rather than the code:

- **A fixture is CUT from a real capture and proven to parse identically**,
  column for column, by `make_fixtures.py`. Never hand-written.
- **Nothing in a capture is rewritten, and the scrub check still runs.** This
  site's rows are questions about public events, with no person's prose and
  no session material in them — so `make_fixtures.py` reduces VOLUME (a
  listing's `results` array is trimmed, the chart geometry and the Tailwind
  class soup go) and changes no value. The leak scan runs anyway, against
  PATTERNS rather than the literals of one capture, because the next capture
  may not be so tidy.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha
classifier and the CLI contract against committed fixtures. If yours
genuinely needs polymarket.com, say in the PR what you ran, which URL and
page kind, from which exit, and what you got — including the row count, the
`data_source` breakdown the run prints, and the sidecar's `total_events`.
A datacentre address is fine here: every measurement in this repo was taken
from one, and the site served `curl/8.0` the full page. Market counts differ
by listing and change through the day, so a bare "worked for me" is not
reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that signs in, connects a wallet or places an order.
This project reads public pages as an anonymous visitor and nothing else; a
token proved valid by trading with real money is not a result worth having.

## Scope

This repo scrapes **public pages** on Polymarket: listing pages, search
listings and event pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
