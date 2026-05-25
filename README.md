# Polymarket Scraper

Open-source Python scraper for [Polymarket](https://polymarket.com) — the world's leading prediction market platform. Extract markets, events, real-time prices, order books, trading volumes, and liquidity across all categories.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://python.org)
[![2captcha](https://img.shields.io/badge/CAPTCHA-2captcha.com-orange.svg)](https://2captcha.com)

## Features

- **Up to ~35 000 markets** — bypasses the Gamma API 10 100-record offset cap via multi-sort deduplication across 10 sort combinations
- **All data in one pass** — prices (Yes/No, best ask/bid, last trade, spread), volumes, liquidity, and outcomes are all included in the Gamma API response. No secondary requests needed.
- **Three scraping engines** — Playwright (primary), Selenium, Pyppeteer
- **Keyword filtering** — `--question-filter` searches market questions client-side
- **Order book snapshots** — full bids/asks per price level via CLOB API
- **JSON + CSV output** — structured, ready for analysis or trading bots
- **CAPTCHA solving** — integrated with [2captcha.com](https://2captcha.com)
- **Proxy support** — [2prx.com](https://2prx.com) ready

## Quick Start

```bash
git clone https://github.com/2scraper/polymarket-scraper
cd polymarket-scraper
pip install -r requirements.txt
playwright install chromium   # for the Playwright version
```

## Usage

### Playwright (primary)

```bash
# All active markets (~35 000 unique records, ~5 min)
python polymarket_scraper_playwright.py --mode markets

# Filter by keyword in market question
python polymarket_scraper_playwright.py --mode markets --question-filter bitcoin
python polymarket_scraper_playwright.py --mode markets --question-filter "interest rate"

# All prediction events
python polymarket_scraper_playwright.py --mode events

# Limit to top 500 by 24h volume
python polymarket_scraper_playwright.py --mode markets --limit 500

# Order book for a specific outcome token
python polymarket_scraper_playwright.py --mode orderbook --token-id <TOKEN_ID>

# Include closed/resolved markets
python polymarket_scraper_playwright.py --mode markets --include-closed

# With proxy and CAPTCHA solving
python polymarket_scraper_playwright.py --mode markets \
  --proxy http://user:pass@proxy.2prx.com:8080 \
  --captcha-key YOUR_2CAPTCHA_KEY

# Debug: save raw API responses to debug/
python polymarket_scraper_playwright.py --mode markets --debug
```

### Selenium

```bash
python polymarket_scraper_selenium.py --mode markets --question-filter trump
python polymarket_scraper_selenium.py --mode events --limit 200
```

### Pyppeteer

```bash
python polymarket_scraper_pyppeteer.py --mode markets --question-filter eth
python polymarket_scraper_pyppeteer.py --mode orderbook --token-id <TOKEN_ID>
```

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `markets` | `markets` / `events` / `orderbook` |
| `--limit` | `0` | Max records (0 = all; uses multi-sort to reach ~35 000) |
| `--format` | `json csv` | Output format(s) |
| `--output` | `output/` | Output directory |
| `--question-filter KEYWORD` | — | Keep only markets whose question contains KEYWORD |
| `--include-closed` | off | Include closed/resolved markets |
| `--token-id` | — | Outcome token ID for orderbook mode |
| `--proxy` | — | Proxy URL (or `TWO_PRX_URL` env var) |
| `--captcha-key` | — | 2captcha API key (or `TWO_CAPTCHA_API_KEY` env var) |
| `--browser` | off | Use browser for Cloudflare session warmup |
| `--headed` | off | Show browser window |
| `--debug` | off | Save raw API responses to `debug/` |

> **Note on category filtering:** Polymarket's API does not support server-side category filtering — tag parameters are silently ignored. Use `--question-filter` to search by keyword instead.
>
> **Note on record count:** Gamma API caps offset-based pagination at 10 100 records per sort order. The scraper bypasses this by running 10 sort combinations and deduplicating by ID, yielding ~35 000 unique markets. Use `--limit` to stop early.

## Data Schema

### Market record

```json
{
  "id": "...",
  "condition_id": "0xabc...",
  "question": "Will Bitcoin exceed $100k before July 2025?",
  "description": "...",
  "url": "https://polymarket.com/event/...",
  "image": "https://...",
  "price_yes": 0.72,
  "price_no": 0.28,
  "best_ask": 0.73,
  "best_bid": 0.71,
  "last_trade_price": 0.72,
  "spread": 0.02,
  "volume_24h": 150000.0,
  "volume_total": 3200000.0,
  "liquidity": 85000.0,
  "active_traders": 412,
  "outcomes": ["Yes", "No"],
  "outcome_prices": ["0.72", "0.28"],
  "clob_token_ids": ["TOKEN_ID_YES", "TOKEN_ID_NO"],
  "start_date": "2024-01-01T00:00:00Z",
  "end_date": "2025-07-01T00:00:00Z",
  "created_at": "2024-01-01T00:00:00Z",
  "active": true,
  "closed": false,
  "resolved": false,
  "resolution_value": null
}
```

### Event record

```json
{
  "id": "...",
  "slug": "bitcoin-2025",
  "title": "Bitcoin 2025",
  "description": "...",
  "url": "https://polymarket.com/event/bitcoin-2025",
  "volume_24h": 5000000.0,
  "volume_total": 120000000.0,
  "liquidity": 2500000.0,
  "start_date": "2024-01-01T00:00:00Z",
  "end_date": "2025-12-31T23:59:59Z",
  "active": true,
  "closed": false,
  "markets_count": 8,
  "markets": ["id1", "id2", "..."]
}
```

## Getting an Order Book

Use a `clob_token_ids` value from any market record:

```bash
python polymarket_scraper_playwright.py --mode orderbook \
  --token-id 98022490269692409998126496127597032490334070080325855126491859374983463996227
```

## Environment Variables

```bash
export TWO_PRX_URL="http://user:pass@proxy.2prx.com:8080"
export TWO_CAPTCHA_API_KEY="your_key_here"
```

## Related Tools

| Tool | Purpose |
|------|---------|
| [2captcha.com](https://2captcha.com) | Solve CAPTCHAs automatically |
| [2prx.com](https://2prx.com) | Residential & datacenter proxies |
| [Anti-detect browser](https://2captcha.com/anti-detect-browser) | Full browser fingerprint isolation |

## License

MIT — see [LICENSE](LICENSE).
