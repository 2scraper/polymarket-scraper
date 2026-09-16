# Troubleshooting

Every entry here is something that actually happened while this repo was
built or run, with the measurement that settled it. If you hit something not
listed, `--dump-html` writes the exact bytes the parser was given — on
success as well as on failure — and that file is the single most useful thing
to attach to an issue.

---

## "It only returned twenty events"

**That is the site, not the run.** A Polymarket listing URL renders twenty
events and publishes no address for the twenty-first. Measured 2026-09-16,
three ways:

| what was tried | what came back |
|---|---|
| `?page=2`, `?_p=2`, `?offset=20` | the same twenty events, HTTP 200 |
| 12 scroll rounds to the bottom | 0 new events, document height unchanged |
| the "Show more markets" button | +118 characters of text, 0 new events |
| headless vs headful | 20 events both ways |

The page's own state says `"totalCount":21511,"hasNextPage":true` behind an
opaque cursor, and nothing in the UI ever spends it. The run records that
number in the sidecar as `total_events` so the gap is visible rather than
guessed at.

What to do instead:

* **for depth** — `--mode events` fetches the listing and then every event
  page under it, which is where `condition_id`, `clob_token_ids`, `spread`
  and the per-market volumes come from;
* **for breadth** — run more listings. `--category crypto`, `politics`,
  `sports`, `geopolitics`, `tech`, or `--category q:bitcoin` for a search.
  Those are separate catalogues rather than pages of one.

---

## "Every row has null `condition_id` / `spread` / `clob_token_ids`"

Expected on a `--mode markets` run. A listing ships **11 to 12 fields per
market**; an event page ships **111 to 115**. The columns only an event page
publishes are null on a listing row by definition, and `data_source` plus
`volume_scope` say which view built each row.

Use `--mode event` for one event, or `--mode events` for a listing and all
twenty of its events.

---

## "`price` is not the Yes price"

`price` is the price of `outcomes[0]`. On a binary market that is "Yes"; on a
sports market it is a side — `["Alex de Minaur","Stefanos Tsitsipas"]`,
`["Over 2.5","Under 2.5"]`, `["FC Barcelona","Draw (…)","Real Racing Club"]`.
Five of the eleven markets on one captured listing had no "Yes" in them at
all. `outcomes` and `outcome_prices` carry the whole vector, index-aligned,
so a consumer that wants a named side reads the label.

---

## "The volumes disagree between two runs of the same market"

Check `volume_scope` before assuming a bug:

* `event` — the EVENT's volume, which is what a listing page ships
  (199,856,030 for the Fed event);
* `market` — that market's own, which is what an event page ships
  (19,376,636 for one of its five markets).

Both are correct and they are not comparable. `diff_runs.py` refuses to
compare runs whose `mode` differs for exactly this reason.

---

## "Everything came back exit 3 (blocked)"

Unusual here. Nothing was refused while this repo was built: twelve browser
captures and three raw fetches from one datacentre address, all HTTP 200 —
including `curl` announcing itself as `curl/8.0`, as `HeadlessChrome/140` and
as `python-requests/2.32`, all served the identical 791,003 bytes.

So open the dump the run saved (`<out>_page1_debug.html`) and look:

| what is in it | what it is |
|---|---|
| `challenges.cloudflare.com`, `Just a moment...` | a Cloudflare challenge. `--retries` opens a FRESH context for it, which is what clears one; `--solve-captcha when-blocked` hands a rendered Turnstile to 2Captcha |
| `ERR_PROXY_CONNECTION_FAILED` and `<title>polymarket.com</title>` | **Chromium's own error page**, not the site. The proxy is dead. The title carries the site's hostname, which is why this scraper checks whether the document was built out of the site's own assets instead of trusting a title |
| the site's own chrome and "Page not found" | a slug that does not exist. That is exit 4, not exit 3 |

---

## "0 rows, and the log says `parser_found_nothing`"

That message means the page **was served**, links to events, and produced no
rows — which is this parser's bug rather than an empty listing. Please open
an issue with the dump attached. The two likeliest causes:

1. the payload's entry point moved (`self.__next_f.push` renamed or
   restructured), and the tile markup changed at the same time;
2. the tile scoping broke, so the DOM fallback found links but no tiles.

If the payload alone moved, you will see rows with `data_source="dom"`
instead: event-level rows, keyed by the event slug, with a rounded volume.
That is a degraded but honest run.

---

## "A `--mode event` run returned other events' markets"

It should not, and that is worth an issue. An event page ships the markets of
everything in its related-events rail as well as its own — twenty-one market
objects on a single-market event, twenty-five on the Fed event's five — and
the parser scopes rows to the event the URL names. `test_event_pages_are_
scoped_to_their_event` pins all four cases.

---

## "The Spanish page gave me different data"

It gives the same data in different words. Measured across the eleven markets
the English and Spanish listing captures share: **0** differing outcome
labels, **0** differing prices, and 6 of 11 titles translated. Ids are
identical. Join on `sku`, `market_id` or `condition_id` — never on a title.

One asymmetry to know about: an EVENT page under a locale path localises the
outcome labels (`["Sí","No"]`) while a LISTING page under the same path does
not. That is why this parser reads the price by index.

---

## "`--concurrency 4` printed a refusal"

Concurrency is only meaningful in `--mode events`, where page 1 is the
listing and pages 2..N are the twenty event pages it named — real independent
addresses. In `--mode markets` and `--mode event` the run is one fetch and
there is nothing to hand a worker, so the flag is refused with that reason
rather than quietly running a single worker.

It is also refused with `--cdp-endpoint`: a Scraping Browser profile allows
one live connection, and workers would collide on it (`profile_locked`). Use
several `pid`s, one run each.

---

## "It fetched the wrong listing"

Two of this site's redirects change what you asked for:

| asked for | landed on | is it a problem |
|---|---|---|
| `/markets` | `/predictions` | no — a rename |
| `/search?q=x` | `/predictions?q=x` | no — the same search |
| `/predictions?_q=x` | `/predictions/x` | **yes** — a SEARCH turned into a TAG listing, which is a different set of events |

The run reports the third one and refuses to plan event pages from it,
because page 1's events would belong to a listing nobody asked for.

---

## "`python3 env_config.py` says my key is a placeholder"

Anything still carrying `{...}` braces is treated as unset. That is
deliberate: the Scraping Browser endpoint and the proxy URL are documented in
the vendor's own `{login}-zone-…` shape, and a literal-only placeholder check
let a copied `.env.example` connect with the string `{login}-zone-…` as its
username and collect a 401 a long way from its cause.

Run `python3 env_config.py` — it prints what was picked up, without printing
secrets.

---

## "Selenium cannot use my proxy / my CDP endpoint"

Both are real engine limits and both are reported loudly rather than
half-working:

* `--proxy-server` takes an address with nowhere to put a password, so
  credentials are stripped and a warning is printed. 2Captcha's IP-whitelist
  mode is the way around it.
* chromedriver's `debuggerAddress` is a bare `host:port`, so an authenticated
  Scraping Browser endpoint cannot be reached from Selenium at all.

Use `playwright_scraper.py` or `puppeteer_scraper.py` for either.

---

## "pyppeteer downloaded a two-year-old Chromium"

It does — build 117.0.5938.0 — and this engine truthfully announces
`Chrome/117`, because the User-Agent is built from the browser's own version
rather than from a literal. On this site that costs nothing: the same page
was served to `curl/8.0`. `--chromium-path /usr/bin/google-chrome` points it
at an installed browser if you would rather.

---

## "The fixtures file is missing"

`fixtures_generated.json` is committed, and `.gitignore`'s blanket `*.json`
rule carries an explicit exception for it. If it is genuinely absent, take
your own captures and run `python3 make_fixtures.py` — it cuts them down and
proves each fixture parses identically to its untrimmed original, column for
column, before writing anything.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked — something stood between the run and the content |
| 4 | zero rows — the request was served and has no markets on it |
| 5 | remote API error (the Scraping Browser or the Scraper API) |
| 6 | partial — rows were gathered, then the run stopped early |

A **failed** run writes no output and no sidecar, so last night's good data
is never replaced by an empty file. `--allow-empty` is the opt-out.
