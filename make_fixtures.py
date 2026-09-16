"""Cut the offline suite's fixtures out of real captures, and PROVE they
parse the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads. This
script is shipped because two files point at it — `smoke_test.py`'s own
docstring and TROUBLESHOOTING.md — and an instruction pointing at a file that
does not exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/` relative to the repo, named as `SOURCES`
below expects. They are deliberately NOT in the repository: one listing
capture is 745 KB and there are a dozen of them.

Take them with a real browser — `--dump-html` on any engine writes exactly
the bytes the parser was given. Take at least one of each PAGE KIND, because
this site ships three different payload shapes:

    a listing   `/predictions`, `/predictions/{tag}`   events inside a
                                                       `results` array
    a dashboard `/politics`                            events loose in the
                                                       payload, no `results`
    an event    `/event/{slug}`                        one event's markets,
                                                       and on some of them no
                                                       event object at all

and at least one LOCALE path (`/es/…`), which is where the outcome labels
turn out to be translated on an event page and not on a listing.

WHAT IT ENFORCES, and why each rule is here
-------------------------------------------
  * every fixture is CUT from a real capture, never hand-written. The one
    thing in a sibling repo that WAS hand-written — a guess at the site's
    "nothing matched" copy — matched none of the real strings, and an empty
    result came back as `shell` and spent a 25-second readiness wait on an
    answer the site had already given;
  * each one is verified to parse IDENTICALLY to the untrimmed original for
    the markets it keeps — every column, not just a count;
  * the trimmed fixture must still CLASSIFY the same way, which is what
    catches a trim that dropped the site's own asset references and turned a
    good page into a `blocked` one.

WHAT IS NOT VERBATIM, and why
-----------------------------
Nothing is rewritten. This site's rows are questions about public events —
"Will the Fed decrease interest rates by 25 bps after the September 2026
meeting?" — with no person's prose and no session material in them. The
checks below scan for both anyway, because the next capture may not be so
tidy and the guard has to be a PATTERN rather than a memory of what was
clean last time (§10).

What IS reduced is volume: a listing's `results` array is trimmed to the
first few events and the JSON-LD's `itemListElement` to the same ones, so a
745 KB capture becomes a fixture small enough to commit. The values that
survive are the site's own, byte for byte.
"""

from __future__ import annotations

import html as html_module
import json
import os
import re
import sys
from dataclasses import asdict

from bs4 import BeautifulSoup

import product_parser as P

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.abspath(os.path.join(REPO_ROOT, "..", "captures"))
OUT_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")

# name -> (capture file, the URL it was taken from, how many EVENTS to keep)
# `None` for the count means "keep the payload whole": an event page is one
# event already, and the two refusal/empty pages carry no events at all.
SOURCES = {
    "listing":        ("listing_predictions.html",
                       "https://polymarket.com/predictions", 5),
    "listing_tag":    ("listing_crypto.html",
                       "https://polymarket.com/predictions/crypto", 5),
    "listing_search": ("listing_search_bitcoin.html",
                       "https://polymarket.com/predictions?q=bitcoin", 4),
    "listing_dash":   ("listing_politics.html",
                       "https://polymarket.com/politics", 4),
    "listing_es":     ("listing_es.html",
                       "https://polymarket.com/es/predictions", 3),
    "event_multi":    ("event_fed_multi.html",
                       "https://polymarket.com/event/fed-decision-in-september-762",
                       None),
    "event_single":   ("event_clarity_single.html",
                       "https://polymarket.com/event/clarity-act-signed-into-law-in-2026",
                       None),
    "event_sports":   ("event_sports.html",
                       "https://polymarket.com/sports/laliga/lal-bar-rrc-2026-09-16",
                       None),
    "event_es":       ("event_es_fed.html",
                       "https://polymarket.com/es/event/fed-decision-in-september-762",
                       None),
    "event_no_object": ("event_crypto_bill.html",
                        "https://polymarket.com/event/"
                        "crypto-market-structure-legislation-becomes-law-in-2026"
                        "-20260727223933088", None),
    # THE FIXTURE THE MARKER CHECKS ACTUALLY NEED. A page the site served in
    # full, fetched over the 2Captcha Scraping Browser — so it carries that
    # service's auto-solve extension injections as well as the site's own
    # markup. Every other capture here comes from a local browser or curl,
    # and a marker set is only tested against the way a real run fetches
    # (§21: the check that should have caught this in a sibling repo passed
    # for the wrong reason, because its only fixture was curl-fetched).
    "cdp_extension":  ("cdp_scraping_browser.html",
                       "https://polymarket.com/predictions/crypto", 5),
    "not_found":      ("notfound.html",
                       "https://polymarket.com/event/this-event-does-not-exist-zzz-9999",
                       None),
    "proxy_error":    ("chromium_proxy_error.html",
                       "https://polymarket.com/predictions", None),
}

# Pages the marker checks assert are CLEAN — no vendor marker may fire on any
# of them (§18: a marker that matches a good page is worse than no marker).
GOOD_PAGES = ("listing", "listing_tag", "listing_search", "listing_dash",
              "listing_es", "event_multi", "event_single", "event_sports",
              "event_es", "event_no_object", "cdp_extension")


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[\s*(\d+)\s*,\s*')
_DECODER = json.JSONDecoder()


def _payload_chunks(html):
    """[(start, end, text)] for every RSC chunk string in the document."""
    out = []
    for match in _PUSH_RE.finditer(html):
        index = match.end()
        if index >= len(html) or html[index] != '"':
            continue
        try:
            chunk, end = _DECODER.raw_decode(html, index)
        except ValueError:
            continue
        out.append((index, end, chunk))
    return out


def _trim_results(payload, keep):
    """Cut every `results` array in the payload down to `keep` events.

    Works on the DECODED payload text and re-serialises only the array, so
    everything around it stays exactly as the site sent it. Returns the new
    text, or None when there is no array to trim.
    """
    changed = False
    for match in list(re.finditer(r'"results"\s*:\s*\[', payload)):
        start = payload.index("[", match.end() - 1)
        try:
            array, end = _DECODER.raw_decode(payload, start)
        except ValueError:
            continue
        if not isinstance(array, list) or len(array) <= keep:
            continue
        trimmed = json.dumps(array[:keep], ensure_ascii=False,
                             separators=(",", ":"))
        payload = payload[:start] + trimmed + payload[end:]
        changed = True
    return payload if changed else None


def _trim_jsonld(html, slugs):
    """Cut every ItemList down to the events `slugs` names."""
    def replace(match):
        raw = match.group(1)
        try:
            node = json.loads(raw)
        except ValueError:
            return match.group(0)
        main = node.get("mainEntity") if isinstance(node, dict) else None
        if not isinstance(main, dict) or main.get("@type") != "ItemList":
            return match.group(0)
        items = []
        for item in main.get("itemListElement") or []:
            entity = item.get("item") if isinstance(item, dict) else None
            url = entity.get("url") if isinstance(entity, dict) else ""
            if P.event_slug_from_url(url or "") in slugs:
                items.append(item)
        main["itemListElement"] = items
        body = json.dumps(node, ensure_ascii=False)
        return match.group(0).replace(raw, body)

    return re.sub(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                  replace, html, flags=re.S)


# The document's furniture: everything that is neither the site's data nor
# the markup the DOM fallback reads. Dropping it takes a 745 KB listing to
# about 70 KB with every row, every price and every classification unchanged
# — which the verification below proves rather than assumes.
#
# `<script>` is the careful one: the RSC payload and the JSON-LD are scripts
# too, and dropping either would turn a good page into an empty one. So the
# rule is "drop a script UNLESS it carries one of those two", not "drop
# scripts".
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I)
# Chart geometry. An event page's price chart is tens of thousands of bytes
# of SVG path data, and `<svg>…</svg>` alone does not catch it: the charts
# nest, so a non-greedy match ends at the first closing tag and leaves the
# rest behind. Nothing here reads a path.
_PATH_RE = re.compile(r"<(?:path|polyline|polygon|circle|rect|line|g)\b[^>]*/?>",
                      re.I)
_NOSCRIPT_RE = re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_SRCSET_RE = re.compile(r'\s(?:srcSet|srcset|sizes|imagesrcset)="[^"]*"')
# Tailwind class soup is 40-60% of a captured document and NOTHING here reads
# it: every selector in `product_parser.SELECTORS` is anchored on the
# `/event/{slug}` href, on purpose, because this site's class names are
# build-time utilities (§4). Dropping them halves a fixture and cannot hide a
# regression in a path that never looked at them — and the column-by-column
# verification below would catch it if it could.
_CLASS_RE = re.compile(r'\s(?:class|style|data-nimg|decoding|loading)="[^"]*"')


def _strip_furniture(html):
    """Everything that is not data and not a tile."""
    def keep_script(match):
        body = match.group(0)
        if "application/ld+json" in body or "self.__next_f.push" in body:
            return body
        # A browser EXTENSION's injected tag is kept too, and it is the whole
        # reason the `cdp_extension` fixture exists: the marker checks are
        # about markup that is NOT the site's, and a trim that dropped it
        # would leave them asserting nothing (§18, §21). These tags carry a
        # src and no body, so keeping them costs a few hundred bytes.
        if "-extension://" in body:
            return body
        return ""

    html = _COMMENT_RE.sub("", html)
    html = _STYLE_RE.sub("", html)
    html = _SVG_RE.sub("", html)
    html = _PATH_RE.sub("", html)
    html = _NOSCRIPT_RE.sub("", html)
    html = _SCRIPT_RE.sub(keep_script, html)
    html = _SRCSET_RE.sub("", html)
    html = _CLASS_RE.sub("", html)
    # `<link>` goes too. Nothing is added back to compensate: the site's own
    # root element id survives in the <html> tag, which is what
    # `served_by_polymarket` reads first — and a fixture that DID lose every
    # marker must be allowed to classify as `blocked`, because that is the
    # trim mistake the state check below exists to catch. Re-adding a
    # reference here would have made Chromium's own error page look served.
    html = _LINK_RE.sub("", html)
    return html


def _trim_tiles(html, keep_slugs):
    """Drop the markup of every event tile we are not keeping.

    A tile is found the same way the DOM fallback finds it — the outermost
    ancestor still covering exactly ONE event — so this cannot take a
    neighbour's markup with it, which is the same scoping rule and the same
    reason as §4's junk-link case.

    The document is re-serialised by BeautifulSoup here, so a fixture's
    whitespace is not byte-identical to the capture. Its VALUES are, and the
    verification below proves it column by column; what would not survive a
    hand-edit is exactly what this script exists to avoid.
    """
    soup = BeautifulSoup(html, "html.parser")
    for anchor in list(soup.select('a[href*="/event/"]')):
        # An anchor whose tile was already removed with an earlier one is
        # detached by now, and a detached node has no attributes to read.
        if anchor.attrs is None or anchor.parent is None:
            continue
        href = anchor.get("href") or ""
        slug = P.event_slug_from_url(
            href if href.startswith("http") else "https://polymarket.com" + href)
        if not slug or slug in keep_slugs:
            continue
        tile = P._tile_of(anchor)
        target = tile if tile is not None else anchor
        try:
            target.decompose()
        except Exception:  # noqa: BLE001 — a tile already removed with its parent
            pass
    return str(soup)


def _payload_window(payload, keep_skus, keep_foreign=0):
    """The slice of the payload holding the objects we are keeping.

    An event page has no `results` array to trim, and most of its 450 KB is
    the rail of related events beside the market. This keeps the objects the
    fixture is about and drops the rest; a cut that lands inside some other
    object simply leaves text that will not decode, which the scanner skips.

    `keep_foreign` keeps that many objects belonging to OTHER events on
    purpose, and it is not an optimisation — it is what makes the scoping
    check able to fail. An event page ships its rails' markets as well as its
    own, and `markets_from_flight` scopes rows to the event the URL asked
    for. Trim the rails away and that rule cannot be broken by any change,
    so the test asserting it passes on a fixture with nothing to scope out —
    a check that looks load-bearing and is not (§17). Found by deleting the
    scoping and watching the suite stay green.
    """
    spans = []
    # A listing's grid is kept as the `"results":[…]` array it arrives in,
    # not as loose event objects. The array is what the parser prefers (it is
    # the grid, as opposed to the trending rail beside it), and a fixture
    # that lost the wrapper would exercise the fallback scan instead of the
    # path a real listing takes.
    for match in re.finditer(r'"results"\s*:\s*\[', payload):
        start = payload.index("[", match.end() - 1)
        try:
            _array, end = _DECODER.raw_decode(payload, start)
        except ValueError:
            continue
        spans.append((match.start(), end))
    for marker in ('"outcomePrices"', '"markets":['):
        position = payload.find(marker)
        while position != -1:
            hit = P._enclosing_object(payload, position)
            if hit:
                start, end, obj = hit
                slugs = {obj.get("slug")}
                for market in (obj.get("markets") or []):
                    if isinstance(market, dict):
                        slugs.add(market.get("slug"))
                if slugs & keep_skus:
                    spans.append((start, end))
            position = payload.find(marker, position + 1)
    if keep_foreign:
        foreign = []
        for marker in ('"outcomePrices"',):
            position = payload.find(marker)
            while position != -1 and len(foreign) < keep_foreign:
                hit = P._enclosing_object(payload, position)
                if hit:
                    start, end, obj = hit
                    slug = obj.get("slug")
                    if slug and slug not in keep_skus and \
                            not any(start >= s0 and end <= e0 for s0, e0 in spans):
                        foreign.append((start, end))
                position = payload.find(marker, position + 1)
        spans.extend(foreign)

    if not spans:
        return None
    # Keep the OUTERMOST spans only, and concatenate them rather than
    # keeping everything between the first and the last. On an event page the
    # markets we want are scattered through the rail of related events, so a
    # first-to-last window keeps the whole stream and saves nothing; the
    # objects themselves are what every check reads, and the scanner finds
    # them wherever they sit.
    # The listing's own totals sit outside every object — `"totalCount":
    # 21511,"hasNextPage":true` is a sibling key of `results`, not a field on
    # an event — so they have to be carried over explicitly. Without this the
    # fixture loses the one number the README's "twenty of twenty-one
    # thousand" claim is checked against, and the check would quietly assert
    # None == None.
    spans.sort()
    kept = []
    for start, end in spans:
        if any(start >= s0 and end <= e0 for s0, e0 in kept):
            continue          # nested inside one we already keep
        kept.append((start, end))
    body = ",".join(payload[start:end] for start, end in kept)
    totals = re.search(r'"totalCount"\s*:\s*\d+\s*,\s*"hasNextPage"\s*:\s*\w+', payload)
    if totals:
        body = body + "," + totals.group(0)
    return body


def _rebuild(original, chunks, payload_text):
    """The document with its RSC chunks replaced by one carrying `payload_text`."""
    start, end = chunks[0][0], chunks[-1][1]
    head = original[:original.rindex("self.__next_f.push(", 0, start)]
    tail = original[end:]
    tail = tail[tail.index("</script>"):] if "</script>" in tail else tail
    return (head + 'self.__next_f.push([1,'
            + json.dumps(payload_text, ensure_ascii=False) + ']);' + tail)


def build(name, filename, url, keep):
    path = os.path.join(CAPTURES, filename)
    if not os.path.exists(path):
        return None, f"missing capture: {path}"
    with open(path, encoding="utf-8", errors="replace") as handle:
        original = handle.read()

    before = P.parse_markets(original, url)
    state_before = P.detect_page_state(original, 404 if "notfound" in filename else 200, url)

    chunks = _payload_chunks(original)
    payload = "".join(text for _s, _e, text in chunks)
    if not before and chunks:
        # A page this parser reads no markets from — the 404 and Chromium's
        # own error page. Its payload is 200 KB of stream that no check here
        # touches, and the classification these two fixtures exist for is
        # made from the site's own words and its own root element rather
        # than from the payload. Dropped entirely, and the state check below
        # is what proves that was safe.
        trimmed = _rebuild(original, chunks, "")
    elif not chunks:
        # No payload at all. The furniture strip below is the whole job.
        trimmed = original
    else:
        shorter = payload
        if keep is not None:
            cut = _trim_results(payload, keep)
            if cut is not None:
                shorter = cut
        # Then keep only the SPAN holding the objects that survived. Most of
        # a listing's 332 KB payload is neither its grid nor its JSON-LD —
        # it is the RSC stream's own furniture, i18n strings and component
        # trees — and none of that is what any check here is about.
        rebuilt = P.parse_markets(
            _rebuild(original, chunks, shorter), url)
        # An event page keeps two of its rails' markets, so the scoping rule
        # has something to scope out and the check asserting it can fail.
        window = _payload_window(shorter, {row.sku for row in rebuilt},
                                 keep_foreign=0 if keep is not None else 2)
        if window:
            shorter = window
        trimmed = _rebuild(original, chunks, shorter)
        # Replace the whole run of chunks with one push carrying the trimmed
        # payload. The FIRST chunk's offsets bound the replacement so the
        # surrounding document is untouched.
        start = chunks[0][0]
        end = chunks[-1][1]
        # Every push between the first and the last is swallowed by the
        # replacement, so the document is rebuilt around a single one.
        head = original[:original.rindex("self.__next_f.push(", 0, start)]
        tail = original[end:]
        tail = tail[tail.index("</script>"):] if "</script>" in tail else tail
        trimmed = (head + 'self.__next_f.push([1,'
                   + json.dumps(shorter, ensure_ascii=False) + ']);' + tail)
    kept_slugs = {row.event_slug for row
                  in P.parse_markets(trimmed, url) if row.event_slug}
    if kept_slugs:
        trimmed = _trim_jsonld(trimmed, kept_slugs)
        trimmed = _trim_tiles(trimmed, kept_slugs)
    trimmed = _strip_furniture(trimmed)

    after = P.parse_markets(trimmed, url)
    state_after = P.detect_page_state(trimmed, 404 if "notfound" in filename else 200, url)

    if state_before != state_after:
        return None, (f"the trim changed the page state: {state_before} -> "
                      f"{state_after}")

    # Every market the fixture keeps must parse to the SAME ROW it did in the
    # untrimmed capture — every column, not just a count. A trim that dropped
    # a field would otherwise make the suite assert a fiction.
    before_by_sku = {row.sku: asdict(row) for row in before}
    for row in after:
        original_row = before_by_sku.get(row.sku)
        if original_row is None:
            return None, f"the trim invented a row: {row.sku}"
        mine = asdict(row)
        for column, value in original_row.items():
            if column in ("scraped_at", "position"):
                continue
            if mine.get(column) != value:
                return None, (f"{row.sku}: {column} changed, "
                              f"{value!r} -> {mine.get(column)!r}")
    if keep is not None and not after:
        return None, "the trim left no rows at all"
    return trimmed, None


# ---------------------------------------------------------------------------
# Guards on what goes into the repository
# ---------------------------------------------------------------------------
# PATTERNS rather than the literals of one capture, because the point is to
# catch the NEXT capture's values too (§10). None of these fired on the
# twelve captures this repo was built from; they are here so that stays true.
LEAK_PATTERNS = (
    (r"\bsessionId\b", "a session id"),
    (r"anti-csrftoken", "a CSRF token"),
    (r'"authorization"\s*:\s*"[^"]{8,}', "an authorization header"),
    (r"\b0x[0-9a-fA-F]{40}\b(?=[^\"]*\"\s*:\s*\"(?:proxy|wallet)Address)",
     "a user wallet address"),
    (r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.", "a JWT"),
    (r"\b[0-9a-f]{32}\b(?=[^\n]{0,40}(?:key|token|secret))", "an API key"),
)


def scan_for_leaks(name, text):
    problems = []
    for pattern, what in LEAK_PATTERNS:
        if re.search(pattern, text):
            problems.append(f"{name}: looks like it carries {what}")
    return problems


def main():
    fixtures = {"_URLS": {}}
    problems = []
    for name, (filename, url, keep) in SOURCES.items():
        trimmed, error = build(name, filename, url, keep)
        if error:
            print(f"  SKIP  {name}: {error}")
            problems.append(f"{name}: {error}")
            continue
        leaks = scan_for_leaks(name, trimmed)
        if leaks:
            problems.extend(leaks)
            for leak in leaks:
                print("  LEAK  " + leak)
            continue
        fixtures[name] = trimmed
        fixtures["_URLS"][name] = url
        rows = P.parse_markets(trimmed, url)
        print(f"  OK    {name:16} {len(trimmed):8,} bytes  {len(rows):3} row(s)")

    if problems:
        print("\n%d problem(s); nothing written." % len(problems))
        return 1
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(fixtures, handle, ensure_ascii=False)
    total = os.path.getsize(OUT_PATH)
    print(f"\nWrote {OUT_PATH} ({total:,} bytes, {len(fixtures) - 1} fixtures).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
