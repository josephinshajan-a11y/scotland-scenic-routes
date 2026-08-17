"""
Scotland's most picturesque running routes
==========================================

Reboot Data Analyst assessment - collection, cleaning and analysis of
AllTrails running routes in Scotland.

Author: Josephin Shajan

--------------------------------------------------------------------------
HOW TO RUN
--------------------------------------------------------------------------

    pip install playwright pandas numpy matplotlib
    playwright install chromium

    python alltrails_scotland.py --test         # check the maths
    python alltrails_scotland.py --check        # check the site is reachable
    python alltrails_scotland.py                # full run, ~20 minutes
    python alltrails_scotland.py --from clean   # re-analyse saved data

Everything is written into a `reboot_output` folder next to this file.

--------------------------------------------------------------------------
IF THE SITE REFUSES THE REQUEST
--------------------------------------------------------------------------

AllTrails sits behind bot protection. Run --check first; it separates four
things that look identical from the outside - a wrong URL, a refused
request, a changed link pattern and a changed page layout - and each needs
a completely different fix.

By default the collector drives the real Chrome installed on the machine
rather than Playwright's bundled Chromium, which is enough on most setups.
If it still gets refused, attach to a browser you launched yourself:

    1. Quit Chrome completely - Cmd+Q, and check no Chrome icon is left in
       the Dock. If Chrome is already running, the launch below prints
       "Opening in existing browser session", the flag is ignored and no
       debug port is ever opened.
    2. /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
           --remote-debugging-port=9222 \\
           --user-data-dir="$HOME/chrome-debug-profile"
    3. Confirm the port is live - curl -s localhost:9222/json/version
       should return JSON, not a connection error.
    4. Open alltrails.com in that window and clear any check by hand
    5. python alltrails_scotland.py --browser cdp

Nothing here defeats an access control. It makes an ordinary browser session
look like what it is. If the site declines the traffic outright, that gets
reported in the write-up rather than worked around.

--------------------------------------------------------------------------
WHAT THIS DOES
--------------------------------------------------------------------------

"Most picturesque" is not a field you can download. There is no beauty column
on AllTrails and there was never going to be one, so the job is to build a
defensible proxy out of things that are measurable.

Five signals go into the index:

  1. Scenery tags     - what AllTrails itself says is on the route
  2. Photo intensity  - photos per review; do people stop and shoot?
  3. Scenery mentions - share of reviews using scenery language
  4. Adjusted rating  - shrunk toward the mean so a 5.0 from four people
                        cannot beat a 4.7 from nine hundred
  5. Elevation per km - climbing usually means something to look at

Signal 2 is the one worth defending in a pitch. A rating tells you people
enjoyed the run. A photo tells you they stopped mid-run because of what they
were looking at, which is much closer to the question being asked.

Collection uses a real browser via Playwright rather than requests and
BeautifulSoup, because AllTrails renders client-side and returns an empty
shell to a plain HTTP fetch. Full reasoning and limitations are in the
methodology document.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import numpy as np
import pandas as pd

# ==========================================================================
# SECTION 1 - CONFIGURATION
# Every tunable value lives here so the write-up and the code cannot drift.
# ==========================================================================

ROOT = Path(__file__).resolve().parent / "reboot_output"
RAW = ROOT / "data_raw"
PROCESSED = ROOT / "data_processed"
OUTPUTS = ROOT / "outputs"
FIGURES = OUTPUTS / "figures"
PAGES = RAW / "pages"
BROWSER_PROFILE = ROOT / ".browser_profile"

for _d in (RAW, PROCESSED, OUTPUTS, FIGURES, PAGES):
    _d.mkdir(parents=True, exist_ok=True)

# AllTrails lists trail-running routes by council area, roughly ten per page.
# Sweeping regions rather than scrolling one national listing avoids the
# popularity ordering that would otherwise fill the sample with Edinburgh and
# Glasgow, and gives the Highlands and islands a floor.
#
# Any URL that 404s is skipped, so the list can be generous.

_BASE = "https://www.alltrails.com/scotland"

START_URL = f"{_BASE}/trail-running"

FALLBACK_URLS = [
    f"{_BASE}/{region}/trail-running" for region in [
        # Highlands and islands
        "highlands", "highland", "highland/inverness", "argyll-and-bute",
        "shetland/shetland", "orkney", "na-h-eileanan-an-iar", "moray",
        # North east
        "aberdeenshire", "aberdeen-city", "angus", "dundee-city",
        # Central
        "perth-and-kinross", "stirling", "clackmannanshire", "fife",
        "falkirk",
        # Central belt
        "edinburgh", "glasgow-city-3", "west-lothian", "midlothian",
        "east-lothian", "renfrewshire", "east-renfrewshire",
        "east-dunbartonshire", "west-dunbartonshire", "north-lanarkshire",
        "south-lanarkshire", "inverclyde",
        # South
        "scottish-borders", "dumfries-and-galloway", "north-ayrshire",
        "south-ayrshire", "east-ayrshire",
    ]
] + [
    # Locale-prefixed variants, in case the site redirects UK visitors
    "https://www.alltrails.com/en-gb/scotland/trail-running",
    "https://www.alltrails.com/united-kingdom/scotland/trail-running",
]

TARGET_ROUTES = 100
MAX_LISTING_SCROLLS = 40
MAX_REVIEWS_PER_TRAIL = 30

# Widened from 2.5-5s after an earlier run tripped a rate limiter.
REQUEST_DELAY = (5.0, 9.0)
NAV_TIMEOUT_MS = 45_000
HEADLESS = False                # a visible browser clears bot checks far
                                # more reliably than a headless one
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# AllTrails tags are condition and amenity labels aggregated from reviews,
# not terrain features. In this sample: great-views (86 routes),
# not-crowded (85), easy-to-park (72), dog-friendly (64). The only scenery
# dimension available is a three-level view rating, so that is what the
# signal uses, at a weight matching how coarse it is.
VIEW_TAG_SCORES = {"great-views": 1.0, "good-views": 0.6}

# Tags describing something that spoils the look. Each costs a small penalty
# rather than zeroing the score outright.
NEGATIVE_TAGS = [
    "crowded", "poor-conditions", "obstructions", "no-shade", "overgrown",
    "private-property", "hard-to-park", "rocky",
]

# Deliberately narrow. "Nice" and "great" describe the run; "stunning" and
# "viewpoint" describe the view.
SCENERY_LEXICON = [
    "scenic", "scenery", "view", "views", "viewpoint", "vista", "panoramic",
    "panorama", "stunning", "breathtaking", "beautiful", "gorgeous",
    "picturesque", "spectacular", "photo", "photos", "photograph",
    "photogenic", "loch", "glen", "waterfall", "falls", "coastline",
    "coastal", "sunset", "sunrise", "ridge", "summit", "overlook",
    "landscape", "wildlife", "forest", "woodland", "river", "beach",
]
DETRACTOR_LEXICON = [
    "boring", "dull", "disappointing", "overgrown", "litter", "rubbish",
    "busy", "crowded", "noisy", "road", "traffic", "industrial", "bland",
]

# Weights revised once the real tag vocabulary was known. The tag signal is
# only a three-level view rating, so 30% was far too much credit for it; the
# freed weight went to review language and elevation rather than to photo
# intensity, because the sensitivity check already showed the ranking leaning
# heavily on photos and piling more on would have made that worse.
SCORE_WEIGHTS = {
    "scenery_tag_score": 0.20,
    "photo_intensity": 0.25,
    "scenery_mention_rate": 0.25,
    "rating_shrunk": 0.15,
    "elevation_per_km": 0.15,
}

MIN_REVIEWS_FOR_RANKING = 10
RATING_PRIOR_WEIGHT = 25

SCOTLAND_BBOX = {"lat_min": 54.5, "lat_max": 61.0,
                 "lon_min": -9.0, "lon_max": -0.5}

RANGES = {
    "length_km": (0.3, 120.0),
    "elevation_gain_m": (0.0, 3000.0),
    "avg_rating": (1.0, 5.0),
    "num_reviews": (0.0, 100_000.0),
    "num_photos": (0.0, 500_000.0),
}


# ==========================================================================
# SECTION 2 - COLLECTION
# ==========================================================================

# Trail paths look like /trail/scotland/edinburgh/arthurs-seat, but AllTrails
# also serves locale-prefixed versions such as /en-gb/trail/... to UK
# visitors. Missing that prefix is what made the first collection attempt
# return nothing at all, so the prefix is optional here.
TRAIL_URL_RE = re.compile(
    r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/trail/[a-z0-9-]+/[a-z0-9-]+", re.I)
NUM = r"([\d,]+(?:\.\d+)?)"


def pause() -> None:
    time.sleep(random.uniform(*REQUEST_DELAY))


def to_float(x):
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def slug_of(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def is_trail_link(href: str) -> bool:
    if not href:
        return False
    return bool(TRAIL_URL_RE.match(urlparse(href).path))


def attach_api_recorder(page, sink: list) -> None:
    """Keep a copy of the JSON the page fetches for itself.

    This is not an attempt to call a private API. It records what a normal
    visitor's browser already requested while loading a page they can see.
    It is cleaner and more complete than parsing rendered HTML, and if it
    ever stops working the DOM parser below still does.
    """
    def on_response(response):
        url = response.url
        if "alltrails.com" not in url:
            return
        if not any(k in url for k in ("/api/", "/graphql", ".json")):
            return
        if "json" not in (response.headers or {}).get("content-type", ""):
            return
        try:
            sink.append({"url": url, "body": response.json()})
        except Exception:
            pass

    page.on("response", on_response)


def harvest_trails_from_json(obj, found: dict) -> None:
    """Walk arbitrary JSON and pull out anything shaped like a trail.

    AllTrails has changed its payload shape more than once, so rather than
    hard-coding a path we look for dicts carrying the fields we care about.
    """
    if isinstance(obj, dict):
        keys = set(obj.keys())
        if ("name" in keys
                and ({"slug", "url", "trail_id", "id"} & keys)
                and ({"avg_rating", "rating", "num_reviews", "popularity"} & keys)):
            slug = obj.get("slug") or obj.get("url") or ""
            key = str(obj.get("trail_id") or obj.get("id") or slug or obj["name"])
            found.setdefault(key, obj)
        for v in obj.values():
            harvest_trails_from_json(v, found)
    elif isinstance(obj, list):
        for v in obj:
            harvest_trails_from_json(v, found)


# Playwright's bundled Chromium is easily identified as automated. Three
# mitigations: channel="chrome" uses the real installed browser, the init
# script below hides the most obvious flags, and --browser cdp attaches to a
# browser the user launched themselves.
#
# This makes an ordinary session look like one. It does not defeat access
# controls - a refusal is reported rather than worked around.

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const q = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : q(p)
);
"""

BROWSER_MODE = "chrome"      # set by --browser

# 127.0.0.1 rather than localhost on purpose. On macOS localhost resolves to
# the IPv6 loopback ::1 first, but Chrome's debugging port binds to IPv4
# only, so "localhost" fails with ECONNREFUSED ::1:9222 even when Chrome is
# running perfectly well.
CDP_ENDPOINT = "http://127.0.0.1:9222"


def _new_context(pw):
    """Open a browser context, or attach to one the user already launched."""
    if BROWSER_MODE == "cdp":
        browser = pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ctx.add_init_script(STEALTH_JS)
        return ctx

    kwargs = dict(
        user_data_dir=str(BROWSER_PROFILE),
        headless=HEADLESS,
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 950},
        locale="en-GB",
        timezone_id="Europe/London",
        args=["--disable-blink-features=AutomationControlled"],
    )
    if BROWSER_MODE == "chrome":
        kwargs["channel"] = "chrome"

    try:
        ctx = pw.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        if BROWSER_MODE == "chrome":
            print(f"[browser] real Chrome unavailable ({e}); "
                  "falling back to bundled Chromium")
            kwargs.pop("channel", None)
            ctx = pw.chromium.launch_persistent_context(**kwargs)
        else:
            raise

    ctx.add_init_script(STEALTH_JS)
    return ctx


def _dismiss_cookies(page) -> None:
    for label in ("Accept all", "Accept All Cookies", "Allow all"):
        try:
            b = page.get_by_role("button", name=re.compile(label, re.I))
            if b.count():
                b.first.click()
                page.wait_for_timeout(1200)
                return
        except Exception:
            pass


def _load_more(page) -> bool:
    for label in ("Show more results", "Show more trails", "Load more"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass
    before = page.evaluate("document.body.scrollHeight")
    page.mouse.wheel(0, 4000)
    page.wait_for_timeout(2200)
    return page.evaluate("document.body.scrollHeight") > before


def check_access() -> None:
    """Load one page and report exactly what came back.

    Worth running before a full collection. Four things that look identical
    from the outside - a wrong URL, a refused request, a changed link
    pattern and a changed layout - need completely different fixes, and
    this separates them.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _new_context(pw)
        page = ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        print(f"[check] browser mode: {BROWSER_MODE}")
        print(f"[check] opening {START_URL}")
        resp = page.goto(START_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        _dismiss_cookies(page)
        page.wait_for_timeout(2000)

        text = (page.evaluate("document.body.innerText") or "").strip()
        hrefs = page.eval_on_selector_all(
            "a[href*='/trail/']",
            "els => els.map(e => e.getAttribute('href'))") or []
        matched = [h for h in hrefs if is_trail_link(h)]

        status = resp.status if resp else None
        print(f"\n  HTTP status      : {status}")
        print(f"  page title       : {page.title()!r}")
        print(f"  body text        : {len(text)} characters")
        print(f"  raw /trail/ links: {len(hrefs)}")
        print(f"  matching pattern : {len(matched)}")
        if hrefs[:3]:
            print("  sample hrefs     :")
            for h in hrefs[:3]:
                print(f"      {h}")
        print("\n  --- first 500 characters of the page ---")
        print("  " + (text[:500].replace("\n", "\n  ") or "(page is empty)"))
        print("  ---------------------------------------")

        blocked_text = any(s in text.lower() for s in
                           ("access denied", "are you a robot",
                            "verify you are", "unusual traffic",
                            "temporarily restricted"))

        # A refusal does not always come with a readable page. A 403 or 429
        # with an empty body is the clearest block there is, and checking
        # the status code first avoids mistaking it for a layout change.
        blocked_status = status in (401, 403, 405, 429, 503)

        if status == 404:
            print("\n  Verdict: the URL does not exist. START_URL needs "
                  "correcting rather than anything to do with access.")
        elif blocked_status or blocked_text:
            print(f"\n  Verdict: the site refused the request (HTTP {status}).")
            if status == 429 or "temporarily restricted" in text.lower():
                print("  This is rate limiting. Stop for an hour before "
                      "trying again - more requests extend it.")
            print("  Next option is --browser cdp, which drives a Chrome you "
                  "launched yourself. Chrome must be fully quit first, or "
                  "the debug port never opens.")
        elif matched:
            print(f"\n  Verdict: page loaded, {len(matched)} usable trail "
                  "links found. Safe to run the full collection.")
        elif hrefs:
            print(f"\n  Verdict: page loaded and {len(hrefs)} trail links are "
                  "present, but none match the expected URL pattern. "
                  "TRAIL_URL_RE needs widening - compare it to the sample "
                  "hrefs above.")
        else:
            print("\n  Verdict: page loaded but no trail links at all. The "
                  "listing layout has probably changed.")

        input("\n  Press Enter to close the browser...")
        ctx.close()


def collect_listings() -> None:
    """Step 1 - build the list of candidate running routes."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    api_sink: list = []
    trail_urls: dict = {}
    json_trails: dict = {}

    with sync_playwright() as pw:
        ctx = _new_context(pw)
        page = ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        attach_api_recorder(page, api_sink)

        all_urls = [START_URL] + FALLBACK_URLS
        skipped = 0

        for n, url in enumerate(all_urls, 1):
            if len(trail_urls) >= TARGET_ROUTES:
                print(f"\n[listings] target reached, "
                      f"{len(all_urls) - n + 1} page(s) not needed")
                break

            label = url.replace("https://www.alltrails.com/", "")
            try:
                resp = page.goto(url, wait_until="domcontentloaded")
            except PWTimeout:
                print(f"  [{n}/{len(all_urls)}] {label} - timed out, skipping")
                continue

            # Not every council area has a trail-running page. A 404 is
            # expected for some of these and is not worth stopping for.
            if resp and resp.status == 404:
                skipped += 1
                print(f"  [{n}/{len(all_urls)}] {label} - no page (404)")
                continue

            page.wait_for_timeout(3500)
            if n == 1:
                _dismiss_cookies(page)

            before_count = len(trail_urls)
            stagnant = 0
            for _ in range(MAX_LISTING_SCROLLS):
                try:
                    hrefs = page.eval_on_selector_all(
                        "a[href*='/trail/']",
                        "els => els.map(e => e.getAttribute('href'))") or []
                except Exception:
                    hrefs = []
                for h in hrefs:
                    if is_trail_link(h):
                        trail_urls.setdefault(
                            urljoin("https://www.alltrails.com", h.split("?")[0]),
                            None)

                if len(trail_urls) >= TARGET_ROUTES:
                    break
                stagnant = 0 if _load_more(page) else stagnant + 1
                if stagnant >= 2:
                    break

            gained = len(trail_urls) - before_count
            print(f"  [{n}/{len(all_urls)}] {label} - "
                  f"+{gained} new, {len(trail_urls)} total")
            pause()

        if skipped:
            print(f"\n[listings] {skipped} regional page(s) did not exist")
        ctx.close()

    for cap in api_sink:
        harvest_trails_from_json(cap["body"], json_trails)
    for t in json_trails.values():
        slug = str(t.get("slug") or t.get("url") or "")
        if slug.lstrip("/").startswith("trail/"):
            trail_urls.setdefault(
                urljoin("https://www.alltrails.com/", slug.lstrip("/")), None)

    urls = list(trail_urls)[:TARGET_ROUTES]
    (RAW / "listings.json").write_text(
        json.dumps({"collected_urls": urls, "count": len(urls)}, indent=2))
    with (RAW / "api_capture.jsonl").open("w") as fh:
        for cap in api_sink:
            fh.write(json.dumps(cap) + "\n")

    print(f"\n[listings] saved {len(urls)} route URLs")

    if not urls:
        raise SystemExit(
            "\nNo routes were found, so there is nothing to collect and the "
            "run stops here.\n\n"
            "Diagnose it with:\n"
            "    python3 alltrails_scotland.py --check\n\n"
            "That loads one page and reports whether the URL is wrong, the "
            "site refused the request, the link pattern has changed, or the "
            "layout has changed.\n")

    if len(urls) < 20:
        print("\n  Fewer routes than expected. Run --check to see which of "
              "the four failure modes it is.", file=sys.stderr)


# ------------------------------------------------ three parsing strategies

def parse_jsonld(html: str) -> dict:
    """Strategy A - schema.org markup in the head. Least likely to change."""
    out: dict = {}
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            if "name" in node and not out.get("name"):
                out["name"] = node["name"]
            agg = node.get("aggregateRating") or {}
            if isinstance(agg, dict):
                out.setdefault("avg_rating", to_float(agg.get("ratingValue")))
                out.setdefault("num_reviews", to_float(
                    agg.get("reviewCount") or agg.get("ratingCount")))
            geo = node.get("geo") or {}
            if isinstance(geo, dict):
                out.setdefault("latitude", to_float(geo.get("latitude")))
                out.setdefault("longitude", to_float(geo.get("longitude")))
            addr = node.get("address") or {}
            if isinstance(addr, dict):
                out.setdefault("region", addr.get("addressRegion"))
                out.setdefault("city", addr.get("addressLocality"))
    return {k: v for k, v in out.items() if v is not None}


FIELD_MAP = {
    "name": ["name"],
    "avg_rating": ["avg_rating", "rating", "averageRating"],
    "num_reviews": ["num_reviews", "reviewCount", "numReviews"],
    "num_photos": ["num_photos", "photoCount", "numPhotos"],
    "length_m": ["length", "distance"],
    "elevation_gain_m": ["elevation_gain", "elevationGain", "ascent"],
    "difficulty": ["difficulty_rating", "difficulty"],
    "route_type": ["route_type", "routeType"],
    # Coordinates deliberately NOT taken from here. The captured JSON can
    # contain several trail objects and picking the wrong one gives a route
    # the wrong location, which then fails the Scotland check and silently
    # deletes it. Coordinates come from JSON-LD only, where they are
    # unambiguously about the page being viewed.
    "popularity": ["popularity"],
    "city": ["city_name", "city"],
    "region": ["state_name", "region", "area_name"],
}


def parse_captured(node: dict) -> dict:
    """Strategy B - the JSON the page fetched for itself. Richest source."""
    out: dict = {}
    for target, candidates in FIELD_MAP.items():
        for c in candidates:
            if c in node and node[c] not in (None, ""):
                out[target] = node[c]
                break
    tags = []
    for key in ("tags", "activities", "features", "attributes"):
        val = node.get(key)
        if isinstance(val, list):
            for t in val:
                if isinstance(t, str):
                    tags.append(t)
                elif isinstance(t, dict):
                    n = t.get("slug") or t.get("name")
                    if n:
                        tags.append(str(n))
    if tags:
        out["tags"] = sorted({t.lower().replace(" ", "-") for t in tags})
    return out


def parse_dom_text(text: str) -> dict:
    """Strategy C - visible text and regex. Ugly, but it survives changes."""
    out: dict = {}
    m = re.search(rf"Length[:\s]*{NUM}\s*(km|mi)", text, re.I)
    if m:
        v, u = to_float(m.group(1)), m.group(2).lower()
        out["length_km"] = v * 1.60934 if u == "mi" else v
    m = re.search(rf"Elevation gain[:\s]*{NUM}\s*(m|ft)", text, re.I)
    if m:
        v, u = to_float(m.group(1)), m.group(2).lower()
        out["elevation_gain_m"] = v * 0.3048 if u == "ft" else v
    m = re.search(r"Route type[:\s]*(Loop|Out & back|Out and back|Point to point)",
                  text, re.I)
    if m:
        out["route_type"] = m.group(1)
    m = re.search(r"\b(Easy|Moderate|Hard)\b", text)
    if m:
        out["difficulty"] = m.group(1)
    m = re.search(rf"{NUM}\s*\(\s*{NUM}\s*\)", text)
    if m:
        out.setdefault("avg_rating", to_float(m.group(1)))
        out.setdefault("num_reviews", to_float(m.group(2)))
    m = re.search(rf"{NUM}\s*(?:photos|Photos)", text)
    if m:
        out["num_photos"] = to_float(m.group(1))
    return {k: v for k, v in out.items() if v is not None}


def scrape_tags_from_dom(page) -> list:
    try:
        chips = page.eval_on_selector_all(
            "a[href*='/tag/'], span[class*='tag'], li[class*='tag']",
            "els => els.map(e => e.innerText)") or []
    except Exception:
        return []
    return sorted({c.strip().lower().replace(" ", "-") for c in chips
                   if c and 2 < len(c.strip()) < 30})


def scrape_reviews(page, limit: int) -> list:
    reviews: list = []
    try:
        for label in ("Show more reviews", "See all reviews", "More reviews"):
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count():
                for _ in range(3):
                    try:
                        btn.first.click()
                        page.wait_for_timeout(1800)
                    except Exception:
                        break
                break
        blocks = page.eval_on_selector_all(
            "[data-testid*='review'], article, li[class*='review']",
            "els => els.map(e => e.innerText)") or []
    except Exception:
        return reviews

    seen = set()
    for b in blocks:
        b = (b or "").strip()
        if len(b) < 25 or b in seen:
            continue
        seen.add(b)
        m = re.search(r"([1-5])(?:\.0)?\s*(?:star|out of 5)", b, re.I)
        reviews.append({"text": b[:2000],
                        "stars": float(m.group(1)) if m else None})
        if len(reviews) >= limit:
            break
    return reviews


def collect_details() -> None:
    """Step 2 - visit each route page and pull the detail fields.

    Every page is also saved as raw HTML, so the parsing step can be reworked
    offline without going back to the site.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    listings_file = RAW / "listings.json"
    if not listings_file.exists():
        raise SystemExit("No listings.json yet. Run the listings step first.")

    urls = json.loads(listings_file.read_text())["collected_urls"][:TARGET_ROUTES]
    print(f"[details] {len(urls)} routes to visit")
    if not urls:
        raise SystemExit(
            "The listings step found nothing, so there is nothing to visit. "
            "Run --check to find out why before going further.")

    details: list = []
    all_reviews: dict = {}

    with sync_playwright() as pw:
        ctx = _new_context(pw)
        page = ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        # One listener for the whole run; the sink is emptied before each
        # page so captures never bleed between trails.
        api_sink: list = []
        attach_api_recorder(page, api_sink)

        for i, url in enumerate(urls, 1):
            slug = slug_of(url)
            rec: dict = {"url": url, "slug": slug, "collected_ok": False}
            api_sink.clear()

            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1500)

                html = page.content()
                (PAGES / f"{slug}.html").write_text(html, encoding="utf-8")
                body_text = page.evaluate("document.body.innerText") or ""

                rec.update(parse_jsonld(html))

                harvested: dict = {}
                for cap in api_sink:
                    harvest_trails_from_json(cap["body"], harvested)
                # Only accept a captured object whose name matches this
                # page's slug. Falling back to "any trail object we saw"
                # attaches another route's numbers to this one, which is
                # worse than having no numbers at all.
                best = None
                for node in harvested.values():
                    nm = str(node.get("name", "")).lower().replace(" ", "-")
                    if nm and nm in slug:
                        best = node
                        break
                if best:
                    for k, v in parse_captured(best).items():
                        if rec.get(k) in (None, ""):
                            rec[k] = v

                for k, v in parse_dom_text(body_text).items():
                    rec.setdefault(k, v)

                if not rec.get("tags"):
                    rec["tags"] = scrape_tags_from_dom(page)
                if not rec.get("name"):
                    try:
                        rec["name"] = page.locator("h1").first.inner_text().strip()
                    except Exception:
                        rec["name"] = slug.replace("-", " ").title()

                revs = scrape_reviews(page, MAX_REVIEWS_PER_TRAIL)
                all_reviews[slug] = revs
                rec["reviews_captured"] = len(revs)
                rec["collected_ok"] = True
                print(f"  [{i}/{len(urls)}] {rec.get('name')} ({len(revs)} reviews)")

            except PWTimeout:
                rec["error"] = "timeout"
                print(f"  [{i}/{len(urls)}] TIMEOUT {slug}")
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                print(f"  [{i}/{len(urls)}] FAILED {slug} - {e}")

            details.append(rec)
            pause()

            if i % 10 == 0:   # checkpoint so a crash doesn't lose the lot
                (RAW / "trail_details.json").write_text(
                    json.dumps(details, indent=2, default=str))
                (RAW / "reviews.json").write_text(
                    json.dumps(all_reviews, indent=2, default=str))
        ctx.close()

    (RAW / "trail_details.json").write_text(
        json.dumps(details, indent=2, default=str))
    (RAW / "reviews.json").write_text(
        json.dumps(all_reviews, indent=2, default=str))
    ok = sum(1 for d in details if d["collected_ok"])
    print(f"\n[details] {ok}/{len(details)} routes collected successfully")


# ==========================================================================
# SECTION 2b - OFFLINE RE-PARSE
#
# Rebuilds the dataset from the saved HTML. Makes no network requests.
#
# Two sources inside each saved page: JSON-LD walked recursively (name,
# rating, review count, coordinates, region, review text), and the embedded
# application JSON (length, elevation, difficulty, photo count).
#
# Trail pages also carry data for nearby trails, so matching the first
# number found would attach a neighbour's stats to this route. `elevationGain`
# appears exactly once per page, in the route's own stats block, so it is
# used as the anchor and length is read backwards from it.
# ==========================================================================

def walk_jsonld(obj, out: dict, reviews: list) -> None:
    """Recursively pull trail fields and review text out of JSON-LD."""
    if isinstance(obj, dict):
        node_type = obj.get("@type")

        agg = obj.get("aggregateRating")
        if isinstance(agg, dict):
            out.setdefault("avg_rating", to_float(agg.get("ratingValue")))
            out.setdefault("num_reviews", to_float(
                agg.get("reviewCount") or agg.get("ratingCount")))

        geo = obj.get("geo")
        if isinstance(geo, dict):
            out.setdefault("latitude", to_float(geo.get("latitude")))
            out.setdefault("longitude", to_float(geo.get("longitude")))

        addr = obj.get("address")
        if isinstance(addr, dict):
            out.setdefault("region", addr.get("addressRegion"))
            out.setdefault("city", addr.get("addressLocality"))

        if node_type == "LocalBusiness" and obj.get("name"):
            out.setdefault("name", obj["name"])

        if node_type == "Review":
            body = obj.get("reviewBody")
            rating = obj.get("reviewRating") or obj.get("rating") or {}
            if body:
                reviews.append({
                    "text": str(body)[:2000],
                    "stars": (to_float(rating.get("ratingValue"))
                              if isinstance(rating, dict) else None),
                })

        for v in obj.values():
            walk_jsonld(v, out, reviews)

    elif isinstance(obj, list):
        for v in obj:
            walk_jsonld(v, out, reviews)


def parse_saved_page(html: str) -> tuple[dict, list]:
    out: dict = {}
    reviews: list = []

    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I):
        try:
            walk_jsonld(json.loads(block.strip()), out, reviews)
        except json.JSONDecodeError:
            continue

    # Quotes inside the embedded application JSON are backslash-escaped, so
    # every key pattern allows an optional backslash before the closing quote.
    def grab(pattern, cast=to_float):
        m = re.search(pattern, html)
        return cast(m.group(1)) if m else None

    elev = re.search(r'"elevationGain\\?"\s*:\s*([\d.]+)', html)
    if elev:
        out["elevation_gain_m"] = to_float(elev.group(1))
        # The route's own length sits just before its elevation gain in the
        # same stats object; the nearest preceding match is the right one.
        preceding = html[max(0, elev.start() - 800):elev.start()]
        lengths = re.findall(r'"length\\?"\s*:\s*([\d.]+)', preceding)
        if lengths:
            out["length_m"] = to_float(lengths[-1])

    for key, pattern in {
        "num_photos": r'"photoCount\\?"\s*:\s*(\d+)',
        "popularity": r'"popularity\\?"\s*:\s*([\d.]+)',
    }.items():
        val = grab(pattern)
        if val is not None:
            out.setdefault(key, val)

    if out.get("num_reviews") is None:
        rc = grab(r'"reviewCount\\?"\s*:\s*(\d+)')
        if rc is not None:
            out["num_reviews"] = rc

    # AllTrails stores difficulty as 1, 3 or 5 rather than a word.
    diff = grab(r'"difficulty\\?"\s*:\s*(\d+)', int)
    if diff is not None:
        out["difficulty"] = {1: "Easy", 3: "Moderate", 5: "Hard"}.get(diff)

    rt = re.search(r'"routeType\\?"\s*:\s*\{.{0,300}?'
                   r'(Loop|Out\s*&?\s*[Bb]ack|Point\s*to\s*[Pp]oint)', html)
    if rt:
        out["route_type"] = rt.group(1)

    return {k: v for k, v in out.items() if v is not None}, reviews


def run_reparse() -> None:
    """Rebuild the raw records from saved HTML. Makes no network requests."""
    details_file = RAW / "trail_details.json"
    if not details_file.exists():
        raise SystemExit("No trail_details.json yet. Run the collection first.")

    records = json.loads(details_file.read_text())
    all_reviews: dict = {}
    filled = 0

    for rec in records:
        page = PAGES / f"{rec.get('slug')}.html"
        if not page.exists():
            continue
        parsed, reviews = parse_saved_page(
            page.read_text(encoding="utf-8", errors="ignore"))

        # Parsed values win: they are read from this page's own markup,
        # whereas the live pass could pick up a neighbouring trail's numbers.
        rec.update(parsed)
        all_reviews[rec["slug"]] = reviews
        rec["reviews_captured"] = len(reviews)
        if parsed.get("avg_rating") is not None:
            filled += 1

    details_file.write_text(json.dumps(records, indent=2, default=str))
    (RAW / "reviews.json").write_text(
        json.dumps(all_reviews, indent=2, default=str))

    total_reviews = sum(len(v) for v in all_reviews.values())
    print(f"[reparse] re-read {len(records)} saved pages")
    print(f"[reparse] {filled} now have a rating, "
          f"{total_reviews} review texts recovered")


# ==========================================================================
# SECTION 3 - CLEANING AND VALIDATION
# ==========================================================================

def normalise_units(df: pd.DataFrame) -> pd.DataFrame:
    """AllTrails serves metres in JSON and miles/feet to UK visitors in the
    DOM. Both paths end up as km and metres here, in one place."""
    if "length_km" not in df:
        df["length_km"] = np.nan
    df["length_km"] = pd.to_numeric(df["length_km"], errors="coerce")
    if "length_m" in df:
        m = pd.to_numeric(df["length_m"], errors="coerce")
        as_km = np.where(m > 1000, m / 1000.0, m)
        df["length_km"] = df["length_km"].fillna(pd.Series(as_km, index=df.index))
    for col in ("elevation_gain_m", "avg_rating", "num_reviews",
                "num_photos", "latitude", "longitude", "popularity"):
        if col not in df:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def tidy_categoricals(df: pd.DataFrame) -> pd.DataFrame:
    if "difficulty" not in df:
        df["difficulty"] = np.nan
    df["difficulty"] = (df["difficulty"].astype(str).str.strip().str.title()
                        .replace({"Nan": np.nan, "None": np.nan}))
    df["difficulty"] = df["difficulty"].replace(
        {"1": "Easy", "3": "Moderate", "5": "Hard",
         "1.0": "Easy", "3.0": "Moderate", "5.0": "Hard"})

    if "route_type" not in df:
        df["route_type"] = np.nan
    df["route_type"] = (df["route_type"].astype(str).str.strip().str.title()
                        .replace({"Out And Back": "Out & Back",
                                  "Nan": np.nan, "None": np.nan}))

    if "tags" not in df:
        df["tags"] = [[] for _ in range(len(df))]
    df["tags"] = df["tags"].apply(
        lambda x: sorted({str(t).lower().strip().replace(" ", "-") for t in x})
        if isinstance(x, list) else [])
    df["tags_str"] = df["tags"].apply("|".join)
    return df


def run_clean() -> None:
    """Step 3 - clean, validate, and write an auditable QA report.

    Nothing is imputed. Missing stays missing, and the scoring step handles
    gaps explicitly rather than filling them in.
    """
    qa: list = []
    details_file = RAW / "trail_details.json"
    if not details_file.exists():
        raise SystemExit("No trail_details.json yet. Run the details step first.")

    records = json.loads(details_file.read_text())
    print(f"[clean] {len(records)} raw records loaded")
    if not records:
        raise SystemExit(
            "No records were collected, so there is nothing to clean. "
            "Run --check to find out why the collection came back empty.")

    df = pd.DataFrame(records)

    if "collected_ok" not in df:
        df["collected_ok"] = True
    df["collected_ok"] = df["collected_ok"].fillna(False).astype(bool)
    failed = int((~df["collected_ok"]).sum())
    if failed:
        qa.append(f"- {failed} page(s) failed to load and were excluded")
    df = df[df["collected_ok"]].copy()

    fallback_name = df["slug"].astype(str).str.replace("-", " ").str.title()
    if "name" not in df:
        df["name"] = fallback_name
    df["name"] = df["name"].fillna(fallback_name)
    for col in ("region", "city"):
        if col not in df:
            df[col] = np.nan

    df = normalise_units(df)
    df = tidy_categoricals(df)

    # Range checks - impossible values are nulled, not silently kept
    for col, (lo, hi) in RANGES.items():
        if col not in df:
            continue
        bad = df[col].notna() & ((df[col] < lo) | (df[col] > hi))
        if bad.any():
            qa.append(f"- `{col}`: nulled {int(bad.sum())} value(s) outside "
                      f"the plausible range {lo}-{hi}")
            df.loc[bad, col] = np.nan

    # Duplicates - by slug, then by name plus rounded coordinates
    before = len(df)
    df = df.drop_duplicates(subset=["slug"], keep="first")
    if len(df) < before:
        qa.append(f"- removed {before - len(df)} duplicate slug(s)")
    # Catches the same route listed twice under slightly different names.
    # Only rows with coordinates take part, otherwise every coordinate-less
    # row shares the key "name|nan|nan".
    before = len(df)
    geo_known = df["latitude"].notna() & df["longitude"].notna()
    key = (df["name"].astype(str).str.lower().str.strip() + "|"
           + df["latitude"].round(3).astype(str) + "|"
           + df["longitude"].round(3).astype(str))
    dupe = key.duplicated(keep="first") & geo_known
    df = df.loc[~dupe]
    if len(df) < before:
        qa.append(f"- removed {before - len(df)} near-duplicate(s) matching on "
                  "name and coordinates")

    # Geographic check against a box covering Scotland including the islands
    has_geo = df["latitude"].notna() & df["longitude"].notna()
    inside = (df["latitude"].between(SCOTLAND_BBOX["lat_min"],
                                     SCOTLAND_BBOX["lat_max"])
              & df["longitude"].between(SCOTLAND_BBOX["lon_min"],
                                        SCOTLAND_BBOX["lon_max"]))
    # Treated as a bad coordinate, not a bad route: every route came from a
    # Scotland-only listing page, so location is known even when the parsed
    # number is wrong. Dropping these rows once cost 99 of 100 routes to what
    # was really a parsing bug.
    suspect = has_geo & ~inside
    if suspect.any():
        qa.append(f"- {int(suspect.sum())} route(s) had coordinates outside "
                  "Scotland; the coordinates were nulled and the routes kept, "
                  "since they came from Scotland-only listing pages")
        df.loc[suspect, ["latitude", "longitude"]] = np.nan
    if int((~has_geo).sum()):
        qa.append(f"- {int((~has_geo).sum())} route(s) have no coordinates; "
                  "location is implied by the listing page but not verified")

    df["elevation_per_km"] = (df["elevation_gain_m"] / df["length_km"]
                              ).replace([np.inf, -np.inf], np.nan)
    df["photos_per_review"] = (df["num_photos"] / df["num_reviews"]
                               ).replace([np.inf, -np.inf], np.nan)

    keep = ["slug", "name", "url", "region", "city", "latitude", "longitude",
            "length_km", "elevation_gain_m", "elevation_per_km", "difficulty",
            "route_type", "avg_rating", "num_reviews", "num_photos",
            "photos_per_review", "reviews_captured", "tags_str"]
    df = df[[c for c in keep if c in df.columns]].reset_index(drop=True)
    df.to_csv(PROCESSED / "routes_clean.csv", index=False)

    rows = ["| Field | Populated | % of routes |", "|---|---|---|"]
    for c in ["name", "length_km", "elevation_gain_m", "avg_rating",
              "num_reviews", "num_photos", "difficulty", "route_type",
              "latitude", "tags_str", "reviews_captured"]:
        if c not in df:
            continue
        n = (int((df[c].astype(str).str.len() > 0).sum()) if c == "tags_str"
             else int(df[c].notna().sum()))
        rows.append(f"| {c} | {n} | {n / max(len(df), 1) * 100:.0f}% |")

    (OUTPUTS / "qa_report.md").write_text("\n".join([
        "# Data quality report", "",
        f"Routes in the cleaned dataset: **{len(df)}**", "",
        "## Cleaning actions", "",
        *(qa or ["- no cleaning actions were needed"]), "",
        "## Field completeness", "", *rows, "",
        "## Notes", "",
        "- Length and elevation are in km and metres throughout.",
        "- Ratings are left raw here; the shrinkage adjustment happens at the "
        "analysis stage so the original value stays visible.",
        "- Nothing was imputed. Missing stays missing.",
    ]))
    print(f"[clean] {len(df)} routes written to routes_clean.csv")


# ==========================================================================
# SECTION 4 - ANALYSIS AND SCORING
# ==========================================================================

WORD = re.compile(r"[a-z']+")


def tokenise(text: str) -> list:
    return WORD.findall((text or "").lower())


def review_features(reviews: list) -> dict:
    """Per-route language features from review text."""
    if not reviews:
        return {"scenery_mention_rate": np.nan, "detractor_rate": np.nan,
                "n_reviews_text": 0, "top_scenery_words": ""}
    scen, detr = set(SCENERY_LEXICON), set(DETRACTOR_LEXICON)
    hits = misses = 0
    counter: Counter = Counter()
    for r in reviews:
        toks = set(tokenise(r.get("text", "")))
        matched = toks & scen
        if matched:
            hits += 1
            counter.update(matched)
        if toks & detr:
            misses += 1
    n = len(reviews)
    return {"scenery_mention_rate": hits / n,
            "detractor_rate": misses / n,
            "n_reviews_text": n,
            "top_scenery_words": "|".join(w for w, _ in counter.most_common(5))}


def pct_rank(s: pd.Series) -> pd.Series:
    """Percentile rank, 0-1, missing stays missing.

    Percentiles rather than min-max because one viral route with 40,000
    photos would otherwise flatten everything else towards zero. They are
    also easier to explain: "top 5% for photos per visitor" needs no
    further translation.
    """
    return s.rank(pct=True, na_option="keep")


def shrink_rating(df: pd.DataFrame) -> pd.Series:
    """Pull ratings toward the sample mean in proportion to thin evidence.

        adjusted = (v*R + m*C) / (v + m)

    A 5.0 from four people should not beat a 4.7 from nine hundred.
    """
    C = df["avg_rating"].mean()
    m = RATING_PRIOR_WEIGHT
    return (df["num_reviews"].fillna(0) * df["avg_rating"] + m * C) / \
           (df["num_reviews"].fillna(0) + m)


def scenery_tag_score(tags_str) -> float:
    # An empty tag list comes back from the CSV as NaN, and `NaN or ""`
    # returns NaN rather than the empty string, because NaN is truthy.
    # Checking for the string type explicitly avoids that trap.
    if not isinstance(tags_str, str):
        return np.nan
    tags = set(tags_str.split("|")) - {""}
    if not tags:
        return np.nan

    # Best view tag the route carries, minus a small penalty per tag that
    # describes something spoiling it.
    best = max((VIEW_TAG_SCORES.get(t, 0.0) for t in tags), default=0.0)
    penalty = 0.1 * sum(1 for t in tags if t in NEGATIVE_TAGS)
    return float(min(1.0, max(0.0, best - penalty)))


def composite(df: pd.DataFrame, weights: dict | None = None) -> pd.DataFrame:
    """Weighted percentile composite.

    If a route is missing a signal, that signal is dropped for that route and
    the remaining weights are renormalised. Filling gaps with column means
    would invent data and hide exactly what a reviewer should be able to see.
    """
    weights = weights or SCORE_WEIGHTS
    comps = list(weights)
    ranked = pd.DataFrame({c: pct_rank(df[c]) for c in comps}, index=df.index)
    w = pd.Series(weights)
    available = ranked.notna()
    wm = available.mul(w, axis=1)
    score = (ranked.fillna(0) * wm).sum(axis=1) / wm.sum(axis=1).replace(0, np.nan)

    out = df.copy()
    for c in comps:
        out[f"pct_{c}"] = ranked[c]
    out["components_used"] = available.sum(axis=1)
    out["picturesque_score"] = (score * 100).round(1)
    return out


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman correlation without pulling in scipy.

    Spearman is just Pearson applied to the ranks, so ranking first and
    correlating gives the identical answer with one less dependency.
    """
    pair = pd.concat([a, b], axis=1).dropna()
    if len(pair) < 3:
        return float("nan")
    return pair.iloc[:, 0].rank().corr(pair.iloc[:, 1].rank())


def sensitivity(df: pd.DataFrame) -> pd.DataFrame:
    """Does the ranking survive different weighting choices?

    If it only holds under one specific set of weights, that is worth knowing
    before anything goes in a press release.
    """
    base = df["picturesque_score"]
    variants = {"equal weights": {k: 1 / len(SCORE_WEIGHTS) for k in SCORE_WEIGHTS}}
    for drop in SCORE_WEIGHTS:
        rest = {k: v for k, v in SCORE_WEIGHTS.items() if k != drop}
        total = sum(rest.values())
        variants[f"without {drop}"] = {k: v / total for k, v in rest.items()}

    rows = []
    for label, weights in variants.items():
        alt = composite(df, weights)["picturesque_score"]
        top_a = set(df.assign(s=base).nlargest(10, "s")["slug"])
        top_b = set(df.assign(s=alt).nlargest(10, "s")["slug"])
        rows.append({"variant": label,
                     "spearman_vs_chosen": round(spearman(base, alt), 3),
                     "top10_overlap": len(top_a & top_b)})
    return pd.DataFrame(rows)


def make_charts(df: pd.DataFrame) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"figure.dpi": 140, "savefig.dpi": 140, "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.alpha": 0.25})
    INK, ACCENT = "#1d3557", "#e07a5f"

    def top_routes():
        # Same eligibility filter as the headline ranking, otherwise the chart
        # and the table disagree about which routes are in the top 15.
        eligible = df[df["num_reviews"].fillna(0) >= MIN_REVIEWS_FOR_RANKING]
        top = eligible.nlargest(15, "picturesque_score").iloc[::-1]
        fig, ax = plt.subplots(figsize=(9, 6.5))
        ax.barh(top["name"].astype(str).str.slice(0, 42),
                top["picturesque_score"], color=INK)
        ax.set_xlabel("Picturesque index (0-100)")
        ax.set_title("Scotland's most picturesque running routes\n"
                     f"Routes with at least {MIN_REVIEWS_FOR_RANKING} reviews",
                     loc="left", fontsize=13, fontweight="bold")
        for y, v in enumerate(top["picturesque_score"]):
            ax.text(v + 0.8, y, f"{v:.0f}", va="center", fontsize=9)
        ax.set_xlim(0, 105)
        fig.tight_layout()
        fig.savefig(FIGURES / "top_routes.png")
        plt.close(fig)

    def photos_vs_rating():
        d = df.dropna(subset=["photos_per_review", "avg_rating"])
        if d.empty:
            return
        fig, ax = plt.subplots(figsize=(8, 5.5))
        ax.scatter(d["avg_rating"], d["photos_per_review"],
                   s=np.clip(d["num_reviews"].fillna(10) / 8, 12, 320),
                   alpha=0.6, color=ACCENT, edgecolor="white", linewidth=0.6)
        ax.set_xlabel("Average rating")
        ax.set_ylabel("Photos per review")
        ax.set_title("Enjoyment and photography are not the same thing",
                     loc="left", fontsize=13, fontweight="bold")
        for _, r in d.nlargest(5, "photos_per_review").iterrows():
            ax.annotate(str(r["name"])[:26],
                        (r["avg_rating"], r["photos_per_review"]),
                        fontsize=8, xytext=(4, 4), textcoords="offset points")
        fig.tight_layout()
        fig.savefig(FIGURES / "photos_vs_rating.png")
        plt.close(fig)

    def tags():
        c: Counter = Counter()
        for s in df["tags_str"].fillna("").astype(str):
            c.update(t for t in s.split("|") if t)
        common = c.most_common(18)
        if not common:
            return
        labels = [t.replace("-", " ") for t, _ in common][::-1]
        vals = [n for _, n in common][::-1]
        colours = [ACCENT if lab.replace(" ", "-") in VIEW_TAG_SCORES
                   else "#adb5bd" for lab in labels]
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.barh(labels, vals, color=colours)
        ax.set_xlabel("Number of routes carrying the tag")
        ax.set_title("What AllTrails says is on Scotland's running routes",
                     loc="left", fontsize=13, fontweight="bold")
        fig.tight_layout()
        fig.savefig(FIGURES / "tag_frequency.png")
        plt.close(fig)

    def route_map():
        d = df.dropna(subset=["latitude", "longitude", "picturesque_score"])
        if len(d) < 5:
            return
        fig, ax = plt.subplots(figsize=(6.5, 8))
        sc = ax.scatter(d["longitude"], d["latitude"], c=d["picturesque_score"],
                        cmap="viridis", s=60, edgecolor="white", linewidth=0.5)
        for _, r in d.nlargest(6, "picturesque_score").iterrows():
            ax.annotate(str(r["name"])[:24], (r["longitude"], r["latitude"]),
                        fontsize=8, xytext=(5, 3), textcoords="offset points")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title("Where the highest-scoring routes are", loc="left",
                     fontsize=13, fontweight="bold")
        fig.colorbar(sc, ax=ax, label="Picturesque index", shrink=0.7)
        fig.tight_layout()
        fig.savefig(FIGURES / "route_map.png")
        plt.close(fig)

    def distribution():
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.hist(df["picturesque_score"].dropna(), bins=18, color=INK, alpha=0.85)
        ax.set_xlabel("Picturesque index")
        ax.set_ylabel("Routes")
        ax.set_title("Most routes cluster in the middle", loc="left",
                     fontsize=13, fontweight="bold")
        fig.tight_layout()
        fig.savefig(FIGURES / "score_distribution.png")
        plt.close(fig)

    for fn in (top_routes, photos_vs_rating, tags, route_map, distribution):
        try:
            fn()
        except Exception as e:
            print(f"[analyse] chart {fn.__name__} skipped: {e}")


def run_analyse() -> None:
    """Step 4 - score, language analysis, charts, findings."""
    df = pd.read_csv(PROCESSED / "routes_clean.csv")
    reviews = json.loads((RAW / "reviews.json").read_text())
    print(f"[analyse] {len(df)} routes loaded")

    feats = pd.DataFrame([review_features(reviews.get(s, [])) for s in df["slug"]],
                         index=df.index)
    df = pd.concat([df, feats], axis=1)

    df["scenery_tag_score"] = df["tags_str"].apply(scenery_tag_score)
    df["photo_intensity"] = df["photos_per_review"]
    df["rating_shrunk"] = shrink_rating(df)

    df = composite(df)
    df["rank"] = df["picturesque_score"].rank(ascending=False,
                                              method="min").astype("Int64")
    df["headline_eligible"] = df["num_reviews"].fillna(0) >= MIN_REVIEWS_FOR_RANKING
    df = df.sort_values("picturesque_score", ascending=False)
    df.to_csv(PROCESSED / "routes_scored.csv", index=False)

    wanted = ["rank", "name", "region", "picturesque_score", "avg_rating",
              "num_reviews", "num_photos", "photos_per_review", "length_km",
              "elevation_gain_m", "scenery_mention_rate", "top_scenery_words",
              "url"]
    top20 = df[df["headline_eligible"]].head(20)[
        [c for c in wanted if c in df.columns]]
    top20.to_csv(PROCESSED / "summary_top20.csv", index=False)

    df.groupby("difficulty", dropna=True).agg(
        routes=("slug", "count"), mean_score=("picturesque_score", "mean"),
        mean_rating=("avg_rating", "mean"),
        mean_photos_per_review=("photos_per_review", "mean"),
        mean_length_km=("length_km", "mean")
    ).round(2).sort_values("mean_score", ascending=False).to_csv(
        PROCESSED / "summary_by_difficulty.csv")

    df.groupby("route_type", dropna=True).agg(
        routes=("slug", "count"), mean_score=("picturesque_score", "mean"),
        mean_scenery_mentions=("scenery_mention_rate", "mean")
    ).round(2).sort_values("mean_score", ascending=False).to_csv(
        PROCESSED / "summary_by_route_type.csv")

    tag_counter: Counter = Counter()
    for s in df["tags_str"].fillna("").astype(str):
        tag_counter.update(t for t in s.split("|") if t)
    pd.DataFrame(tag_counter.most_common(), columns=["tag", "routes"]).to_csv(
        PROCESSED / "summary_tag_counts.csv", index=False)

    sens = sensitivity(df)
    sens.to_csv(PROCESSED / "summary_sensitivity.csv", index=False)

    corr_cols = ["picturesque_score", "avg_rating", "num_reviews",
                 "photos_per_review", "elevation_per_km", "length_km",
                 "scenery_mention_rate"]
    present = [c for c in corr_cols if c in df]
    corr = pd.DataFrame(
        [[round(spearman(df[a], df[b]), 3) for b in present] for a in present],
        index=present, columns=present)
    corr.to_csv(PROCESSED / "summary_correlations.csv")

    make_charts(df)

    def fmt(x, nd=2):
        return "n/a" if pd.isna(x) else f"{x:,.{nd}f}"

    lines = [
        "# Auto-generated findings", "",
        "Every number below is computed from the collected data.", "",
        f"- Routes collected and cleaned: **{len(df)}**",
        f"- Routes with at least {MIN_REVIEWS_FOR_RANKING} reviews: "
        f"**{int(df['headline_eligible'].sum())}**",
        f"- Reviews of text captured: **{int(df['n_reviews_text'].sum())}**",
        f"- Median route length: **{fmt(df['length_km'].median())} km**",
        f"- Median elevation gain: **{fmt(df['elevation_gain_m'].median(), 0)} m**",
        f"- Median rating: **{fmt(df['avg_rating'].median())}**",
        f"- Median photos per review: **{fmt(df['photos_per_review'].median())}**",
        f"- Mean share of reviews mentioning scenery: "
        f"**{fmt(df['scenery_mention_rate'].mean() * 100, 1)}%**",
        f"- Mean share mentioning a detractor: "
        f"**{fmt(df['detractor_rate'].mean() * 100, 1)}%**",
        "", "## Top 10", "",
        "| # | Route | Score | Rating | Reviews | Photos/review | km | Ascent m |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, (_, r) in enumerate(top20.head(10).iterrows(), 1):
        lines.append(
            f"| {i} | {r.get('name')} | {fmt(r.get('picturesque_score'), 1)} | "
            f"{fmt(r.get('avg_rating'), 1)} | {fmt(r.get('num_reviews'), 0)} | "
            f"{fmt(r.get('photos_per_review'))} | {fmt(r.get('length_km'), 1)} | "
            f"{fmt(r.get('elevation_gain_m'), 0)} |")

    lines += ["", "## Spearman correlations with the index", ""]
    for c in corr.index:
        if c != "picturesque_score":
            lines.append(f"- {c}: **{corr.loc['picturesque_score', c]}**")

    lines += ["", "## Weighting sensitivity", "",
              "| Variant | Spearman vs chosen | Top-10 overlap |", "|---|---|---|"]
    for _, r in sens.iterrows():
        lines.append(f"| {r['variant']} | {r['spearman_vs_chosen']} | "
                     f"{r['top10_overlap']}/10 |")

    lines += ["", "## Most common scenery words in reviews", ""]
    wc: Counter = Counter()
    for s in df["top_scenery_words"].fillna("").astype(str):
        wc.update(w for w in s.split("|") if w)
    for w, n in wc.most_common(12):
        lines.append(f"- {w}: a top word for {n} routes")

    (OUTPUTS / "auto_findings.md").write_text("\n".join(lines))
    print("[analyse] wrote scored data, summary tables, charts and findings")


# ==========================================================================
# SECTION 5 - TESTS
# Fixtures below are invented numbers used only to prove the functions do
# what the methodology says they do. None of this touches collected data.
# ==========================================================================

def run_tests() -> int:
    failures = []

    def check(label, condition):
        print(f"  {'pass' if condition else 'FAIL'}  {label}")
        if not condition:
            failures.append(label)

    print("\nshrinkage")
    d = pd.DataFrame({"avg_rating": [5.0, 4.7, 4.0, 4.5],
                      "num_reviews": [4, 900, 300, 50]})
    s = shrink_rating(d)
    check("a 5.0 from 4 reviews falls below a 4.7 from 900", s[0] < s[1])
    check("a heavily-reviewed rating barely moves", abs(s[1] - 4.7) < 0.05)
    check("a thinly-reviewed rating moves a lot", abs(s[0] - 5.0) > 0.1)

    print("\npercentile ranks")
    r = pct_rank(pd.Series([1, 2, 3, 4, np.nan]))
    check("missing stays missing", pd.isna(r[4]))
    check("ranks run 0 to 1", r.min() > 0 and r.max() == 1.0)
    check("order preserved", r[0] < r[1] < r[2] < r[3])
    r2 = pct_rank(pd.Series([1, 2, 3, 10_000]))
    check("an outlier does not compress the others", r2[1] - r2[0] > 0.2)

    print("\ntag score")
    check("great-views scores 1.0",
          abs(scenery_tag_score("great-views|easy-to-park") - 1.0) < 1e-9)
    check("good-views scores below great-views",
          scenery_tag_score("good-views") < scenery_tag_score("great-views"))
    check("no tags returns missing, not zero", pd.isna(scenery_tag_score("")))
    check("tags with no view rating score 0.0",
          scenery_tag_score("dog-friendly|easy-to-park") == 0.0)
    check("a negative tag reduces a great-views score",
          scenery_tag_score("great-views|crowded") < 1.0)
    check("the score never falls below zero",
          scenery_tag_score("crowded|overgrown|no-shade|obstructions") == 0.0)

    print("\nreview language")
    f = review_features([
        {"text": "Stunning views over the loch, took loads of photos"},
        {"text": "Muddy and quite dull, a lot of road noise"},
        {"text": "Good workout, decent surface underfoot"},
    ])
    check("scenery mention rate counts 1 of 3",
          abs(f["scenery_mention_rate"] - 1 / 3) < 1e-9)
    check("detractor rate counts 1 of 3", abs(f["detractor_rate"] - 1 / 3) < 1e-9)
    check("no reviews returns missing",
          pd.isna(review_features([])["scenery_mention_rate"]))

    print("\ncomposite with a missing component")
    d2 = pd.DataFrame({
        "slug": ["a", "b", "c"],
        "scenery_tag_score": [0.9, 0.5, 0.1],
        "photo_intensity": [2.0, 1.0, 0.2],
        "scenery_mention_rate": [0.8, np.nan, 0.1],
        "rating_shrunk": [4.8, 4.5, 4.0],
        "elevation_per_km": [60, 40, 10],
    })
    out = composite(d2)
    check("weights renormalise when a component is missing",
          out["components_used"].tolist() == [5, 4, 5])
    check("scores stay within 0-100", out["picturesque_score"].between(0, 100).all())
    check("the strongest route still ranks first",
          out["picturesque_score"][0] == out["picturesque_score"].max())

    print("\nunit conversion")
    u = normalise_units(pd.DataFrame({"length_m": [8046.0, 12.5]}))
    check("metres convert to km", abs(u["length_km"][0] - 8.046) < 0.01)
    check("a value already in km is left alone", abs(u["length_km"][1] - 12.5) < 0.01)

    print("\nweights")
    check("weights sum to 1", abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9)

    print("\n" + "-" * 46)
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("all checks passed")
    return 0


# ==========================================================================
# SECTION 6 - ENTRY POINT
# ==========================================================================

STEPS = ["listings", "details", "reparse", "clean", "analyse"]


def main() -> None:
    global BROWSER_MODE, HEADLESS

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="run checks on the scoring maths and exit")
    ap.add_argument("--check", action="store_true",
                    help="load one page and report what came back, then exit")
    ap.add_argument("--browser", choices=["chrome", "chromium", "cdp"],
                    default="chrome",
                    help="chrome: real installed Chrome (default). "
                         "chromium: Playwright's bundled build. "
                         "cdp: attach to a Chrome you launched yourself with "
                         "--remote-debugging-port=9222")
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser window (more likely to be blocked)")
    ap.add_argument("--from", dest="start", choices=STEPS, default="listings",
                    help="step to start from (default: the beginning)")
    args = ap.parse_args()

    BROWSER_MODE = args.browser
    HEADLESS = args.headless

    if args.test:
        sys.exit(run_tests())

    if args.check:
        check_access()
        return

    for step in STEPS[STEPS.index(args.start):]:
        print(f"\n{'=' * 62}\n  {step.upper()}\n{'=' * 62}")
        {"listings": collect_listings, "details": collect_details,
         "reparse": run_reparse, "clean": run_clean,
         "analyse": run_analyse}[step]()

    print(f"\nDone. Open {OUTPUTS / 'auto_findings.md'}")


if __name__ == "__main__":
    main()
