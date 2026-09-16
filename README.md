# polymarket-scraper

[![release](https://img.shields.io/github/v/release/2scraper/polymarket-scraper?sort=semver)](https://github.com/2scraper/polymarket-scraper/releases)
[![tests](https://github.com/2scraper/polymarket-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/polymarket-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/polymarket-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/polymarket-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20Scraper%20API-lightgrey)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without%20an%20account-yes-brightgreen)](#do-i-need-to-pay-for-anything)

A Polymarket prediction-market scraper. Reads a listing page, a category, a
search or a single event, and writes JSON and CSV with **one row per market**
— the question, its price, its order book, its volumes and, from an event
page, its on-chain ids.

Four engines — Playwright (primary), Selenium, pyppeteer, and 2Captcha's
Scraping Browser over CDP. All four produce the same rows, the same exit
codes and the same run metadata.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --url "https://polymarket.com/predictions"
```

```
[+] Saved 92 products -> polymarket_markets.json
[+] Saved 92 products -> polymarket_markets.csv
[+] Wrote run metadata -> polymarket_markets.meta.json (status=complete)
```

---

## What a row is

Polymarket nests two things and this repo is explicit about which one it
writes:

* an **event** is a folder — *"Fed Decision in September?"* — with a title, a
  volume, a category and an end date;
* a **market** is one binary question inside it — *"Will the Fed increase
  interest rates by 25 bps after the September 2026 meeting?"* — with its own
  id, its own price and its own order book.

A row is a **market**. The event travels with it in the `event_*` columns, so
one file answers both "what is this event worth" and "what does each outcome
cost".

---

## The one thing to know before you run it

**A listing URL is ONE page of twenty events.** Polymarket publishes no
address for the twenty-first. Measured 2026-09-16, three ways:

| what was tried | what came back |
|---|---|
| `?page=2`, `?_p=2`, `?offset=20` | **ignored** — HTTP 200 and the same twenty events |
| 12 scroll rounds to the bottom | 0 new events, document height unchanged at 3,654px |
| the page's own "Show more markets" button | +118 characters of text, 0 new events |
| headless vs headful | 20 events both ways |

The page's own state says `"totalCount":21,511` with `hasNextPage: true`
behind an opaque cursor that the interface never spends. Every run records
that number in its sidecar as `total_events`, beside the twenty it actually
read, so the gap is visible rather than guessed at — and a run that was asked
for more pages reports **complete** with `stop_reason:
"listing_has_one_page"` rather than pretending it was cut short.

Two ways to get more than twenty events' worth of data:

```bash
# DEPTH: the listing, then every event page under it (this is where
# condition ids, CLOB token ids, spreads and per-market volumes come from)
python3 playwright_scraper.py \
  --url "https://polymarket.com/predictions/crypto" \
  --mode events --pages 21 --concurrency 3

# BREADTH: separate catalogues, one run each
for c in crypto politics sports geopolitics tech economy; do
  python3 playwright_scraper.py --category "$c" --out "pm_$c"
done
```

---

## Modes

| mode | what it fetches | rows | fields per market |
|---|---|---|---|
| `--mode markets` (default) | one listing page | every market on it | 11–12 |
| `--mode events` | the listing, then each event's own page | the same markets, upgraded | 111–115 on the upgraded ones |
| `--mode event` | one `/event/{slug}` page | that event's markets | 111–115 |

The mode is inferred from the URL for `markets` and `event`; `events` is a
listing URL plus an intention, so it has to be asked for.

In `--mode events`, page 1 is the listing and pages 2..N are the event pages
page 1 named. Those are real independent addresses, so `--concurrency` is
meaningful there and refused — with the reason — everywhere else. When the
same market arrives twice, the deeper reading replaces the shallower one in
place, keeping the listing's own ranking as the row order.

```bash
python3 playwright_scraper.py --url "https://polymarket.com/predictions"
python3 playwright_scraper.py --category crypto --format csv
python3 playwright_scraper.py --category q:bitcoin          # a search
python3 playwright_scraper.py --url "https://polymarket.com/es/predictions"
python3 playwright_scraper.py \
  --url "https://polymarket.com/event/fed-decision-in-september-762"
```

---

## What a run actually returned

Measured 2026-09-16 from one datacentre address in Finland, headless,
no proxy, no key:

| URL | mode | rows | events | priced | JSON-LD confirmed | time |
|---|---|---|---|---|---|---|
| `/predictions` | markets | 92–94 | 20 | 100% | 20 of 20 events | ~8s |
| `/predictions/crypto` | markets | 70 | 20 | 100% | 19 of 20 | ~8s |
| `/predictions?q=bitcoin` | markets | 84 | 20 | 100% | 18 of 20 | ~8s |
| `/politics` | markets | 79 | 21 | 100% | 15 | ~9s |
| `/predictions/crypto` | events, 6 pages | 74 | 20 | 100% | — | ~50s |
| `/event/fed-decision-in-september-762` | event | 5 | 1 | 100% | — | ~8s |

All three engines returned **identical rows** on the same URL — same 70 skus,
same sidecar, same exit code.

And from a second address entirely: the canary's first dispatch, on a bare
GitHub runner with no proxy and no key, returned **70 markets from 20 events**
on the `--mode events` walk (4 pages, 4 deep rows) and **92 markets from 20
events** on `/predictions`, with the site's own totals at 4,138 and 21,585.
Two addresses on two continents, the same twenty events per listing.

One number worth reading twice: in the `--mode events` run, five of the event
pages carried markets the listing had not shown at all (74 rows against page
1's 70). A listing shows an event's leading markets; its own page shows all
of them.

---

### Columns

Forty columns, one row per market. JSON and CSV carry the same fields in the
same order; list columns are joined with ` | ` in CSV.

**The family prefix**, byte-identical across the sibling scrapers:
`source`, `scraped_at`, `url`, `sku`, `title`.

* `sku` — the market's **slug**. Every read path can produce it (the payload
  carries it, the DOM carries it in the href), which the numeric id cannot.
  A market that gets re-worded gets a new slug, so join on `condition_id` or
  `market_id` to track one over months.
* `title` — the question, in the page's own language. **Translated** under a
  locale path; the ids and the prices are not.

**The price.** `price`, `currency`, `outcomes`, `outcome_prices`,
`group_item_title`.

* `price` is the price of `outcomes[0]` — 0 to 1, which is the market's own
  estimate of that outcome's probability. It is **not** always the "Yes"
  price: a sports market's outcomes are named sides
  (`["Alex de Minaur","Stefanos Tsitsipas"]`, `["Over 2.5","Under 2.5"]`).
* `currency` is `USD`, read from the page's own structured data rather than
  defaulted. Shares settle in USDC and the site quotes them in dollars.
* `group_item_title` is the market's label inside its event — `"25 bps
  increase"`, `"FC Barcelona"`. Null on a single-market event.

**The order book.** `best_bid`, `best_ask`, `spread`, `last_trade_price`,
`price_change_24h`, `price_change_1w`. `spread` and the two deltas are
published on an event page only.

**What the venue counts.** `volume`, `volume_24h`, `volume_1w`, `liquidity`,
`volume_scope`.

* `volume_scope` says **whose** volume the row carries: `event` on a listing
  row (the event's total, 199,856,030 for one) and `market` on an event-page
  row (that market's own, 19,376,636 for one of its five). Both are correct
  and they are not comparable.

**The event.** `event_id`, `event_slug`, `event_title`, `event_url`,
`category`, `subcategory`, `tags`.

**When and what state.** `start_date`, `end_date`, `active`, `closed`,
`accepting_orders`.

**The ids a trader needs.** `market_id`, `condition_id`, `clob_token_ids`,
`neg_risk`.

* `condition_id` is the on-chain condition, and `clob_token_ids` the two
  ERC-1155 token ids (YES then NO, index-aligned with `outcomes`) that
  Polymarket's CLOB API keys on. **Event pages only** — a listing publishes
  neither.
* `neg_risk` says the market is part of a mutually exclusive group.

**Provenance.** `data_source`, `page`, `position`.

* `data_source` is which of the page's three views built the row:
  `flight` (the site's own inlined payload), `flight+jsonld` (and the page's
  structured data agreed on the price), or `dom` (the payload could not be
  read at all, so the row is EVENT-level with a rounded volume and no market
  id). `diff_runs.py` reports a change that comes with a `data_source` change
  as a SOURCE change rather than as a price move.
* `page` + `position` are unique as a pair across a run. `position` restarts
  at 1 on every page, so the column is worthless without `page` beside it.

---

## How it reads the page

Three views, in this order:

1. **The inlined Next.js payload** — `self.__next_f.push([1,"…"])`. Decoded
   and concatenated, it holds the site's own market objects verbatim, with
   exact figures: `volumeNum` 19,376,636.15 where the tile shows "$19M".
   This is what every healthy run reads.
2. **JSON-LD** — a listing's `CollectionPage → ItemList` names one price per
   event. Used to confirm the payload's price and to read the currency, which
   the payload never states. An **event page's** JSON-LD publishes
   `"price": "0"` for markets trading at 0.07 and 0.91, so it is deliberately
   not read for prices.
3. **The rendered tiles** — a fallback, anchored on the `/event/{slug}` href
   and never on a class name. Rows from here are event-level and say so.

---

## Traps that look like bugs

* **Twenty events per listing.** See above. It is the site.
* **`condition_id` null on every row** — you are in `--mode markets`. A
  listing does not publish it.
* **`price` is not the Yes price** — it is the price of `outcomes[0]`, and
  not every market is Yes/No.
* **The Spanish page "changed the data"** — it did not. Across the eleven
  markets two locale captures shared: 0 differing outcome labels, 0 differing
  prices, 6 of 11 titles translated.
* **`/predictions?_q=bitcoin` lands on `/predictions/bitcoin`** — a search
  redirected into a TAG listing, which is a different set of events. The run
  reports it and refuses to walk event pages from it.
* **A dead proxy looks like the site.** Chromium's own error page carries
  `<title>polymarket.com</title>` and no vendor marker at all. This scraper
  asks whether the document was built out of the site's own assets instead of
  trusting the title.

More in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Do I need to pay for anything?

**No.** Every measurement in this README was taken with **no account, no API
key and no proxy**, from an ordinary datacentre address. On 2026-09-16 the
site served the full page to:

| client | response |
|---|---|
| Playwright's bundled Chromium, headless | HTTP 200, twelve captures, four page kinds, three locales |
| `curl` announcing `curl/8.0` | HTTP 200, 791,003 bytes |
| `curl` claiming `HeadlessChrome/140` | HTTP 200, byte-identical |
| `curl` claiming `python-requests/2.32` | HTTP 200, byte-identical |

This site does not read the client, and it did not rate-limit an ordinary
run.

What a [2captcha](https://2captcha.com) key buys here is therefore **volume
and geography, not access**:

| product | what it is for here | measured 2026-09-16 |
|---|---|---|
| **proxy** pool | many addresses instead of one, when you walk many listings | not needed to get in; `--delay` is the cheaper lever |
| **Scraping Browser API** | a remote browser over CDP: no local browser, a chosen exit country (`--cdp-endpoint`) | **70 markets, identical to a local browser** |
| **Scraper API** | one HTTP request per page, no browser anywhere (`scraper_api_client.py`) | **$0.0005, 766,857 bytes, 70 markets, 11s — identical rows** |
| **fingerprint** API | a consistent device identity across runs (`--fingerprint`) | fetched, applied (UA, locale `en-US`, timezone `America/New_York`), 70 markets |

Every one of those was run end to end against this site rather than assumed
(CLAUDE.md §16: the credential-gated paths are the ones nobody runs). They
all return **the same 70 markets** a local browser does, which is the useful
finding: on this site the paid products buy convenience and geography, and
none of them buys data you could not already get.

### Captchas

**No challenge was met** while this repo was built: twelve browser captures
and three raw fetches contained zero markers of any vendor, and zero captcha
configuration — no `recaptcha`, `hcaptcha`, `turnstile`, `datadome`,
`perimeterx` or `sitekey` anywhere.

That is a statement about what happened, not about what is possible. The site
sits behind Cloudflare (`server: cloudflare`, a `cf-ray` on every response),
and a Cloudflare managed challenge renders a Turnstile. This repo implements:

* reCAPTCHA v2, v2-invisible and v3 — `RecaptchaV2TaskProxyless`,
  `RecaptchaV3TaskProxyless`;
* reCAPTCHA **enterprise** — `RecaptchaV2EnterpriseTaskProxyless`, a separate
  task type, because an enterprise widget solved as ordinary v2 returns a
  token the site rejects;
* Cloudflare **Turnstile** — `TurnstileTaskProxyless`, including the
  Challenge-page form, whose `sitekey`, `action`, `cData` and `chlPageData`
  exist only inside the one `turnstile.render` call the page makes. All three
  engines install an interception script on the context before any page
  script runs, because no static read of the HTML can produce those.

A page carrying no widget is reported unsolved rather than charged for, and
`--solve-captcha when-blocked` (the default) counts event links before paying.

---

## Engines

| engine | notes |
|---|---|
| `playwright_scraper.py` | **primary.** The only one with the worker pool for `--mode events`. `playwright install chromium` is enough here — the bundled Chromium was served every page tried. |
| `selenium_scraper.py` | parity. Cannot authenticate a proxy (`--proxy-server` has nowhere to put a password) and cannot attach to an authenticated CDP endpoint. |
| `puppeteer_scraper.py` | parity, via pyppeteer. Downloads its own Chromium (build 117.0.5938.0, two years old) and announces `Chrome/117` truthfully; this site does not care. `--chromium-path` points it elsewhere. |
| `scraper_api_client.py` | no browser at all — one HTTP request per page through 2Captcha's Scraper API. |

Install **one** engine: playwright and pyppeteer declare mutually
unsatisfiable pins, and pyppeteer and selenium collide on `urllib3`.

---

## Flags

```
--url --mode --category --pages --format --out --delay --retries --retry-delay
--concurrency --proxy --proxy-file --proxy-rotate --proxy-shuffle
--proxy-block-retries --twocaptcha-key --captcha-api --solve-captcha
--min-score --cdp-endpoint --allow-empty --dump-html --headless/--headful
--fingerprint --fp-tags --fp-country --browser-channel --locale
```

`--dump-html` writes the snapshot on **success** as well as on failure: a run
can return the right number of rows with a column silently empty, and then the
exact bytes are the only way to tell a parsing bug from a too-early snapshot.

Credentials belong in `.env` and never on a command line — see
[.env.example](.env.example) and run `python3 env_config.py` to see what was
picked up, without printing secrets.

---

## Output contract

* **A run that finds nothing writes nothing.** Last night's good output is
  never replaced by an empty file; `--allow-empty` is the opt-out.
* **Exit codes**: `0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero
  rows · `5` remote API error · `6` partial.
* **`<out>.meta.json`** per run: status, stop reason, which pages failed by
  number, the site's own `total_events`, the row counts per page, and the
  category and locale the URL carried.
* **An empty CSV still carries its header.**
* `diff_runs.py` compares two runs by `sku` and refuses a pair whose `mode`
  differs — a shallow run against a deep one would report a dozen columns as
  having appeared from nowhere.

---

## Tests

```bash
python3 smoke_test.py          # the offline suite, no engine library needed
pytest                          # the same checks, wrapped as one test
```

Fixtures are cut from real captures by `make_fixtures.py`, which proves each
one parses identically to its untrimmed original, column for column, before
writing anything.

`tests.yml` runs offline only and never touches the site. `canary.yml` runs
one real `--mode events` walk and one listing run daily, and asserts the
pagination, the deep columns, the price coverage and the site's own
`total_events`.

---

## Licence

MIT. See [LICENCE](LICENSE). Scrapes public pages as an anonymous visitor;
nothing here signs in, connects a wallet or places an order.
