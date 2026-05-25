#!/usr/bin/env python3
"""
Polymarket Scraper — Selenium version
Scrapes markets and events from Polymarket via the public Gamma REST API.

All price data (Yes/No prices, best ask/bid, last trade, spread) is already
included in the Gamma API response — no separate CLOB enrichment needed.

Data source: https://gamma-api.polymarket.com
Order books:  https://clob.polymarket.com/book?token_id=TOKEN_ID

Notes on filtering:
  Polymarket's API does not support server-side category filtering.
  Use --question-filter KEYWORD to filter by text in the market question.

GitHub: https://github.com/2scraper/polymarket-scraper
CAPTCHA solving: https://2captcha.com
Proxies: https://2prx.com
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
SITE_URL  = "https://polymarket.com"
SITE_KEY  = "6LfMwpEpAAAAANXDy5RBi4e3BrWBjIEFdnQIRQYX"

DEFAULT_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://polymarket.com/",
    "Origin":          "https://polymarket.com",
}

DEBUG_DIR = Path("debug")


def build_session(proxy_url: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s


def fetch_json(session: requests.Session, url: str, params: dict = None,
               debug: bool = False, debug_name: str = "response") -> dict | list:
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code == 422:
        return []
    resp.raise_for_status()
    data = resp.json()
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{debug_name}.json").write_text(json.dumps(data, indent=2))
    return data


def fetch_markets(session: requests.Session, limit: int,
                  active_only: bool, debug: bool = False) -> list[dict]:
    """
    Fetch markets from Gamma API using multiple sort orders to bypass the
    10 100-record offset cap. Deduplicates by market ID. Yields ~35 000
    unique markets across all sort combinations.
    """
    SORT_COMBOS = [
        ("volume24hr", "false"),
        ("volume24hr", "true"),
        ("volume",     "false"),
        ("volume",     "true"),
        ("createdAt",  "false"),
        ("createdAt",  "true"),
        ("liquidity",  "false"),
        ("liquidity",  "true"),
        ("endDate",    "false"),
        ("endDate",    "true"),
    ]

    seen: set[str] = set()
    all_markets: list[dict] = []

    for sort_idx, (order, ascending) in enumerate(SORT_COMBOS):
        offset = 0
        combo_new = 0

        while True:
            params = {
                "limit":     100,
                "offset":    offset,
                "order":     order,
                "ascending": ascending,
            }
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"

            data = fetch_json(session, f"{GAMMA_API}/markets", params=params,
                              debug=(debug and sort_idx == 0 and offset == 0),
                              debug_name="gamma_markets_sample")

            if not data:
                break

            for m in data:
                mid = m.get("id")
                if mid and mid not in seen:
                    seen.add(mid)
                    all_markets.append(m)
                    combo_new += 1

            total = len(all_markets)
            print(f"  [{sort_idx+1}/{len(SORT_COMBOS)}] order={order} asc={ascending} "
                  f"offset={offset} | total unique: {total}    ", end="\r")

            if limit > 0 and total >= limit:
                all_markets = all_markets[:limit]
                print()
                return all_markets

            if len(data) < 100:
                break

            offset += 100
            time.sleep(0.1)

        if combo_new == 0 and sort_idx > 2:
            break

    print()
    return all_markets


def fetch_events(session: requests.Session, limit: int,
                 debug: bool = False) -> list[dict]:
    all_events = []
    offset = 0
    while True:
        batch = min(limit - len(all_events), 100) if limit > 0 else 100
        params = {"limit": batch, "offset": offset,
                  "order": "volume24hr", "ascending": "false", "active": "true"}
        data = fetch_json(session, f"{GAMMA_API}/events", params=params,
                          debug=debug, debug_name=f"gamma_events_{offset}")
        if not data:
            break
        all_events.extend(data)
        print(f"  Fetched {len(all_events)} events...", end="\r")
        if not data or len(data) < 100 or (limit > 0 and len(all_events) >= limit):
            break
        offset += 100
        time.sleep(0.25)
    print()
    return all_events[:limit] if limit > 0 else all_events


def fetch_order_book(session: requests.Session, token_id: str,
                      debug: bool = False) -> dict:
    return fetch_json(session, f"{CLOB_API}/book",
                      params={"token_id": token_id},
                      debug=debug, debug_name=f"orderbook_{token_id[:8]}")


def build_driver(proxy_url: Optional[str] = None, headless: bool = True):
    if not SELENIUM_AVAILABLE:
        raise RuntimeError("selenium not installed. Run: pip install selenium")
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument(f"--user-agent={DEFAULT_HEADERS['User-Agent']}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if proxy_url:
        opts.add_argument(f"--proxy-server={proxy_url}")
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"})
    return driver


def browser_warmup(driver, debug: bool = False) -> None:
    try:
        print("  Browser warmup: loading polymarket.com...")
        driver.get(SITE_URL)
        deadline = time.time() + 15
        while time.time() < deadline:
            if driver.execute_script("return document.readyState") == "complete":
                time.sleep(2)
                break
            time.sleep(0.3)
        if debug:
            DEBUG_DIR.mkdir(exist_ok=True)
            driver.save_screenshot(str(DEBUG_DIR / "warmup.png"))
            (DEBUG_DIR / "warmup.html").write_text(driver.page_source)
    except Exception as e:
        print(f"  Warmup warning: {e}")


def solve_captcha_if_needed(driver, api_key: str) -> bool:
    try:
        if not driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']"):
            return True
        print("  CAPTCHA detected — solving via 2captcha.com...")
        r = requests.post("https://2captcha.com/in.php", data={
            "key": api_key, "method": "userrecaptcha",
            "googlekey": SITE_KEY, "pageurl": driver.current_url, "json": 1,
        }, timeout=30)
        result = r.json()
        if result.get("status") != 1:
            return False
        task_id = result["request"]
        for _ in range(24):
            time.sleep(5)
            poll = requests.get("https://2captcha.com/res.php", params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=15)
            pr = poll.json()
            if pr.get("status") == 1:
                driver.execute_script(
                    f'document.getElementById("g-recaptcha-response").innerHTML = "{pr["request"]}";'
                )
                print("  CAPTCHA solved!")
                return True
            if pr.get("request") != "CAPCHA_NOT_READY":
                return False
        return False
    except Exception as e:
        print(f"  CAPTCHA solving failed: {e}")
        return False


def parse_outcome_prices(raw: dict) -> tuple:
    prices = raw.get("outcomePrices")
    if not prices:
        return None, None
    try:
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(prices, list) and len(prices) >= 2:
            return float(prices[0]), float(prices[1])
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None, None


def normalize_market(raw: dict) -> dict:
    price_yes, price_no = parse_outcome_prices(raw)
    clob_token_ids = raw.get("clobTokenIds", [])
    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except (ValueError, json.JSONDecodeError):
            clob_token_ids = []
    return {
        "id":               raw.get("id"),
        "condition_id":     raw.get("conditionId"),
        "question":         raw.get("question", "").strip(),
        "description":      raw.get("description", "").strip(),
        "url":              f"https://polymarket.com/event/{raw['slug']}" if raw.get("slug") else "",
        "image":            raw.get("image", ""),
        "price_yes":        price_yes,
        "price_no":         price_no,
        "best_ask":         raw.get("bestAsk"),
        "best_bid":         raw.get("bestBid"),
        "last_trade_price": raw.get("lastTradePrice"),
        "spread":           raw.get("spread"),
        "volume_24h":       raw.get("volume24hr"),
        "volume_total":     raw.get("volume"),
        "liquidity":        raw.get("liquidity"),
        "active_traders":   raw.get("activeTraderCount"),
        "outcomes":         raw.get("outcomes", []),
        "outcome_prices":   raw.get("outcomePrices", []),
        "clob_token_ids":   clob_token_ids,
        "start_date":       raw.get("startDate"),
        "end_date":         raw.get("endDate"),
        "created_at":       raw.get("createdAt"),
        "active":           raw.get("active", True),
        "closed":           raw.get("closed", False),
        "resolved":         raw.get("resolved", False),
        "resolution_value": raw.get("resolutionValue"),
    }


def normalize_event(raw: dict) -> dict:
    return {
        "id":            raw.get("id"),
        "slug":          raw.get("slug"),
        "title":         raw.get("title", "").strip(),
        "description":   raw.get("description", "").strip(),
        "url":           f"https://polymarket.com/event/{raw['slug']}" if raw.get("slug") else "",
        "image":         raw.get("image", ""),
        "volume_24h":    raw.get("volume24hr"),
        "volume_total":  raw.get("volume"),
        "liquidity":     raw.get("liquidity"),
        "start_date":    raw.get("startDate"),
        "end_date":      raw.get("endDate"),
        "active":        raw.get("active", True),
        "closed":        raw.get("closed", False),
        "markets_count": len(raw.get("markets", [])),
        "markets":       [m.get("id") for m in raw.get("markets", [])],
    }


def save_json(data: list, path: Path) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"  Saved JSON → {path} ({len(data)} records)")


def save_csv(data: list, path: Path) -> None:
    if not data:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
        writer.writeheader()
        for row in data:
            flat = {k: json.dumps(v) if isinstance(v, (list, dict)) else v
                    for k, v in row.items()}
            writer.writerow(flat)
    print(f"  Saved CSV  → {path} ({len(data)} records)")


def run(args) -> None:
    proxy_url   = args.proxy or os.getenv("TWO_PRX_URL") or os.getenv("PROXY_URL")
    captcha_key = args.captcha_key or os.getenv("TWO_CAPTCHA_API_KEY")

    print(f"\n{'─'*55}")
    print(f"  Polymarket Scraper (Selenium)  |  mode: {args.mode}")
    if args.question_filter:
        print(f"  Filter: '{args.question_filter}'")
    print(f"  Proxy: {'yes' if proxy_url else 'no'}")
    print(f"  2captcha: {'yes' if captcha_key else 'no'}")
    print(f"{'─'*55}\n")

    driver = None
    if args.browser or args.headed:
        driver = build_driver(proxy_url=proxy_url, headless=not args.headed)
        browser_warmup(driver, debug=args.debug)
        if captcha_key:
            solve_captcha_if_needed(driver, captcha_key)

    session = build_session(proxy_url)

    if args.mode == "markets":
        print("Fetching markets from Gamma API...")
        raw = fetch_markets(session, args.limit, not args.include_closed, debug=args.debug)
        print(f"Fetched {len(raw)} markets.")
        if args.question_filter:
            kw = args.question_filter.lower()
            before = len(raw)
            raw = [m for m in raw if kw in m.get("question", "").lower()]
            print(f"  Filter '{args.question_filter}': {before} → {len(raw)} markets.")
        data = [normalize_market(m) for m in raw]
        suffix = args.question_filter.replace(" ", "_") if args.question_filter else "all"
        stem = f"polymarket_markets_{suffix}"

    elif args.mode == "events":
        print("Fetching events from Gamma API...")
        raw = fetch_events(session, args.limit, debug=args.debug)
        print(f"Fetched {len(raw)} events.")
        if args.question_filter:
            kw = args.question_filter.lower()
            before = len(raw)
            raw = [e for e in raw if kw in e.get("title", "").lower()]
            print(f"  Filter '{args.question_filter}': {before} → {len(raw)} events.")
        data = [normalize_event(e) for e in raw]
        stem = "polymarket_events"

    elif args.mode == "orderbook":
        if not args.token_id:
            print("Error: --token-id required for orderbook mode.")
            sys.exit(1)
        ob = fetch_order_book(session, args.token_id, debug=args.debug)
        data = [ob]
        stem = f"polymarket_orderbook_{args.token_id[:8]}"

    else:
        print(f"Unknown mode: {args.mode}")
        sys.exit(1)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    if "json" in args.format:
        save_json(data, out_dir / f"{stem}.json")
    if "csv" in args.format:
        save_csv(data, out_dir / f"{stem}.csv")

    if driver:
        driver.quit()
    print(f"\n✓ Done. {len(data)} records saved.\n")


def parse_args():
    p = argparse.ArgumentParser(
        description="Polymarket Scraper — Selenium edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python polymarket_scraper_selenium.py --mode markets
  python polymarket_scraper_selenium.py --mode markets --question-filter bitcoin
  python polymarket_scraper_selenium.py --mode events --limit 500
  python polymarket_scraper_selenium.py --mode orderbook --token-id <TOKEN_ID>
        """,
    )
    p.add_argument("--mode", choices=["markets", "events", "orderbook"], default="markets")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--format", nargs="+", choices=["json", "csv"], default=["json", "csv"])
    p.add_argument("--output", "-o", default="output")
    p.add_argument("--include-closed", action="store_true")
    p.add_argument("--question-filter", metavar="KEYWORD")
    p.add_argument("--token-id")
    p.add_argument("--proxy")
    p.add_argument("--captcha-key")
    p.add_argument("--browser", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args)
