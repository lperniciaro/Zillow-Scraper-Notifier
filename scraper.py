"""
Zillow Rental Scraper
=====================
Scrapes Zillow rental listings anonymously via Gluetun/PIA VPN proxy.

Strategy: parse the embedded __NEXT_DATA__ JSON from the HTML search results
page rather than hitting the XHR API directly. This is far more robust against
403 blocks because it mimics a normal browser page load.

Filters:
  - Minimum beds: 4
  - Minimum baths: 2.5 (full + half counted as 0.5)
  - Price range: configurable min/max monthly rent
  - Availability date: May 1 – Jul 15 (current year)
  - Zip codes: defined in config.json

Output: results/listings_<timestamp>.json  +  results/listings_latest.json
Alerts:  SMS + email via notifier.py for new matches (deduped by zpid)
"""

import argparse
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from notifier import notify_new_listings

# Load .env automatically – works locally; in Docker the env vars are already
# injected by docker-compose so this is a no-op there.
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.json"))
OUTPUT_DIR  = Path(os.getenv("OUTPUT_DIR",  "/app/results"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Filters (can also be overridden in config.json)
# ---------------------------------------------------------------------------
AVAIL_START = date(date.today().year, 5, 1)   # May 1
AVAIL_END   = date(date.today().year, 7, 15)  # Jul 15
MIN_BEDS    = 4
MIN_BATHS   = 2.5   # full + 0.5 per half-bath
MIN_PRICE: float | None = None        # monthly rent, None = no lower bound
MAX_PRICE: float | None = None        # monthly rent, None = no upper bound
# None = no filter; otherwise a set of Zillow homeType strings e.g. {"SINGLE_FAMILY"}
ALLOWED_HOME_TYPES: set[str] | None = None

# ---------------------------------------------------------------------------
# Zillow endpoints
# ---------------------------------------------------------------------------
ZILLOW_BASE = "https://www.zillow.com"

# ---------------------------------------------------------------------------
# Rotating user agents – cycle through several to avoid fingerprinting
# ---------------------------------------------------------------------------
USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

_ua_index = 0

def _next_user_agent() -> str:
    """Round-robin through user agents."""
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua


def _base_headers(ua: str | None = None) -> dict:
    ua = ua or _next_user_agent()
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def build_session() -> requests.Session:
    """Build a fresh requests session with retry logic and a new user agent."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    session.headers.update(_base_headers())
    return session


def zillow_search_url(zip_code: str) -> str:
    return f"{ZILLOW_BASE}/{zip_code}/rentals/"


def zillow_city_search_url(page: int = 1) -> str:
    """City-level rental search URL for San Diego.
    Zillow paginates with /N_p/ suffix for page N > 1.
    """
    base = f"{ZILLOW_BASE}/san-diego-ca/rentals/"
    return base if page == 1 else f"{ZILLOW_BASE}/san-diego-ca/rentals/{page}_p/"


def _extract_next_data(html: str) -> dict | None:
    """
    Pull the JSON blob from <script id="__NEXT_DATA__" ...>...</script>.
    This is the same data the page renders from – no XHR needed.
    """
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _extract_search_results(next_data: dict) -> list[dict]:
    """
    Navigate the __NEXT_DATA__ structure to find the map/list results.
    Zillow nests them under several possible paths depending on page version.
    """
    # Path 1: props.pageProps.searchPageState.cat1.searchResults
    try:
        state = next_data["props"]["pageProps"]["searchPageState"]
        return (
            state.get("cat1", {})
                 .get("searchResults", {})
                 .get("mapResults", [])
            or
            state.get("cat1", {})
                 .get("searchResults", {})
                 .get("listResults", [])
        )
    except (KeyError, TypeError):
        pass

    # Path 2: props.pageProps.gdpClientCache (individual listing pages)
    try:
        cache = next_data["props"]["pageProps"]["gdpClientCache"]
        # This is a dict of zpid → listing data; flatten to list
        results = []
        for v in json.loads(cache).values():
            if isinstance(v, dict) and "property" in v:
                results.append(v["property"])
        if results:
            return results
    except (KeyError, TypeError, json.JSONDecodeError):
        pass

    return []


def fetch_listings_for_zip(zip_code: str, cfg: dict) -> list[dict]:
    """
    Fetch rental listings for a zip code by loading the Zillow search HTML
    page and extracting the embedded __NEXT_DATA__ JSON.

    Creates a fresh session per zip to rotate cookies and user agents.
    Retries with backoff on 403/429.
    """
    search_url = zillow_search_url(zip_code)
    max_attempts = cfg.get("max_attempts_per_zip", 3)
    base_delay   = cfg.get("delay_between_zips_seconds", 8)

    for attempt in range(1, max_attempts + 1):
        session = build_session()  # fresh cookies + new UA each attempt

        log.info("Fetching zip %s (attempt %d/%d) → %s", zip_code, attempt, max_attempts, search_url)

        # Warm up: hit the Zillow homepage first so we look like a real browser
        # navigation (only on first attempt to save time)
        if attempt == 1:
            try:
                session.get(ZILLOW_BASE, timeout=20)
                time.sleep(random.uniform(1.5, 3.0))
            except requests.RequestException:
                pass  # non-fatal

        try:
            resp = session.get(
                search_url,
                headers={**_base_headers(session.headers.get("User-Agent")), "Referer": ZILLOW_BASE + "/"},
                timeout=30,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            log.error("Network error for zip %s: %s", zip_code, exc)
            if attempt < max_attempts:
                sleep = base_delay * attempt * 2
                log.info("Retrying in %ss...", sleep)
                time.sleep(sleep)
            continue

        if resp.status_code == 403:
            log.warning("403 Forbidden for zip %s (attempt %d). Backing off...", zip_code, attempt)
            if attempt < max_attempts:
                sleep = base_delay * attempt * 3 + random.uniform(5, 15)
                log.info("Sleeping %.0fs before retry...", sleep)
                time.sleep(sleep)
            continue

        if resp.status_code == 429:
            log.warning("429 Rate limited for zip %s. Backing off heavily...", zip_code)
            sleep = 60 * attempt + random.uniform(10, 30)
            log.info("Sleeping %.0fs...", sleep)
            time.sleep(sleep)
            continue

        if resp.status_code != 200:
            log.error("Unexpected status %d for zip %s", resp.status_code, zip_code)
            continue

        next_data = _extract_next_data(resp.text)
        if next_data is None:
            log.warning("Could not find __NEXT_DATA__ in response for zip %s", zip_code)
            # Could be a bot-challenge page; back off and retry
            if attempt < max_attempts:
                time.sleep(base_delay * attempt * 2)
            continue

        results = _extract_search_results(next_data)
        log.info("  → %d raw results for zip %s", len(results), zip_code)
        return results

    log.error("All %d attempts failed for zip %s – skipping.", max_attempts, zip_code)
    return []


def fetch_listings_for_city(cfg: dict) -> list[dict]:
    """
    Fetch ALL rental listings for San Diego by scraping the city-level search
    page and walking through every page of results.  Zillow typically shows
    ~40 listings per page and caps at ~20 pages (≈800 listings), which is
    enough to cover all north/central SD zip codes in a single run.

    Returns a flat list of raw listing dicts (not yet filtered/formatted).
    """
    max_attempts = cfg.get("max_attempts_per_zip", 3)
    base_delay   = cfg.get("delay_between_zips_seconds", 8)
    all_results: list[dict] = []
    seen_zpids:  set[str]   = set()

    page = 1
    while True:
        url = zillow_city_search_url(page)
        log.info("Fetching city page %d → %s", page, url)

        listings = []
        for attempt in range(1, max_attempts + 1):
            session = build_session()

            # Warm up on first page / first attempt only
            if page == 1 and attempt == 1:
                try:
                    session.get(ZILLOW_BASE, timeout=20)
                    time.sleep(random.uniform(1.5, 3.0))
                except requests.RequestException:
                    pass

            try:
                resp = session.get(
                    url,
                    headers={**_base_headers(session.headers.get("User-Agent")),
                              "Referer": ZILLOW_BASE + "/san-diego-ca/rentals/"},
                    timeout=30,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                log.error("Network error on city page %d: %s", page, exc)
                if attempt < max_attempts:
                    time.sleep(base_delay * attempt * 2)
                continue

            if resp.status_code == 403:
                log.warning("403 on city page %d (attempt %d). Backing off...", page, attempt)
                if attempt < max_attempts:
                    sleep = base_delay * attempt * 3 + random.uniform(5, 15)
                    log.info("Sleeping %.0fs...", sleep)
                    time.sleep(sleep)
                continue

            if resp.status_code == 429:
                log.warning("429 rate-limited on city page %d.", page)
                sleep = 60 * attempt + random.uniform(10, 30)
                log.info("Sleeping %.0fs...", sleep)
                time.sleep(sleep)
                continue

            if resp.status_code != 200:
                log.error("Unexpected status %d on city page %d", resp.status_code, page)
                break

            next_data = _extract_next_data(resp.text)
            if next_data is None:
                log.warning("No __NEXT_DATA__ on city page %d", page)
                if attempt < max_attempts:
                    time.sleep(base_delay * attempt * 2)
                continue

            listings = _extract_search_results(next_data)
            log.info("  → %d raw results on city page %d", len(listings), page)
            break  # success

        if not listings:
            log.info("No listings returned for city page %d – stopping pagination.", page)
            break

        new_on_page = 0
        for listing in listings:
            zpid = str(listing.get("zpid", ""))
            if zpid and zpid not in seen_zpids:
                seen_zpids.add(zpid)
                all_results.append(listing)
                new_on_page += 1

        log.info("  → %d new unique listings (total so far: %d)", new_on_page, len(all_results))

        # If we got fewer new listings than expected, we've likely hit the last page
        if new_on_page == 0:
            log.info("No new listings on page %d – done paginating.", page)
            break

        page += 1
        # Polite delay between pages
        delay = base_delay + random.uniform(0, base_delay * 0.5)
        log.info("Sleeping %.0fs before next page...", delay)
        time.sleep(delay)

    log.info("City search complete: %d total unique raw listings.", len(all_results))
    return all_results


# ---------------------------------------------------------------------------
# Filtering & formatting
# ---------------------------------------------------------------------------

def parse_baths(listing: dict) -> float:
    """
    Calculate total bathrooms.
    Prefers factsAndFeatures breakdown; falls back to top-level baths field.
    """
    facts = listing.get("factsAndFeatures", {})
    full  = facts.get("fullBathroomCount")
    half  = facts.get("halfBathroomCount")

    if full is not None:
        return float(full) + (0.5 * float(half or 0))

    return float(listing.get("baths") or 0)


def parse_availability(listing: dict) -> date | None:
    raw = listing.get("availabilityDate")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def passes_filters(listing: dict, allowed_zips: set[str] | None = None) -> bool:
    """
    Return True if the listing meets all configured filters.

    allowed_zips – when provided (city-search mode) the listing's zip code must
                   be in this set.  Pass None to skip the zip check (zip-search
                   mode already constrains the query by zip).
    """
    beds      = listing.get("beds") or listing.get("hdpData", {}).get("homeInfo", {}).get("bedrooms", 0)
    baths     = parse_baths(listing)
    avail     = parse_availability(listing)
    price     = listing.get("hdpData", {}).get("homeInfo", {}).get("price")
    home_type = (
        listing.get("hdpData", {}).get("homeInfo", {}).get("homeType")
        or listing.get("homeType")
        or ""
    ).upper()

    if (beds or 0) < MIN_BEDS:
        return False

    if baths < MIN_BATHS:
        return False

    if price is not None:
        if MIN_PRICE is not None and price < MIN_PRICE:
            return False
        if MAX_PRICE is not None and price > MAX_PRICE:
            return False

    # --- Home type ---
    if ALLOWED_HOME_TYPES is not None and home_type:
        if home_type not in ALLOWED_HOME_TYPES:
            log.debug("Skipping %s – home type %s not in allowed list", listing.get("address"), home_type)
            return False

    # Require a known availability date within the target window.
    # Listings with no date are "available now" and not useful for future planning.
    if avail is None or not (AVAIL_START <= avail <= AVAIL_END):
        return False

    # --- Zip code ---
    listing_zip = (
        listing.get("hdpData", {}).get("homeInfo", {}).get("zipcode", "") or ""
    )
    if allowed_zips is not None:
        # City-search mode: only keep listings in our target zip codes
        if listing_zip not in allowed_zips:
            return False
    else:
        # Legacy zip-search mode: zip was already baked into the query URL,
        # but sanity-check if the listing reports a different zip
        pass  # no extra check needed

    return True


def _build_url(detail_url: str) -> str:
    """Always return a clean absolute Zillow URL regardless of whether
    detailUrl is a full URL or a bare path."""
    if not detail_url:
        return ZILLOW_BASE
    # Strip any leading full origin so we never double-up
    path = detail_url.replace("https://www.zillow.com", "").replace("http://www.zillow.com", "")
    return ZILLOW_BASE + path


def format_listing(listing: dict, zip_code: str) -> dict:
    home_info = listing.get("hdpData", {}).get("homeInfo", {})
    avail = parse_availability(listing)
    baths = parse_baths(listing)

    return {
        "zpid":               listing.get("zpid"),
        "address":            listing.get("address"),
        "zip_code":           zip_code,
        "url":                _build_url(listing.get("detailUrl", "")),
        "price":              listing.get("price"),
        "price_monthly":      home_info.get("price"),
        "beds":               listing.get("beds") or home_info.get("bedrooms"),
        "baths_total":        baths,
        "baths_full":         listing.get("factsAndFeatures", {}).get("fullBathroomCount"),
        "baths_half":         listing.get("factsAndFeatures", {}).get("halfBathroomCount"),
        "area_sqft":          listing.get("area") or home_info.get("livingArea"),
        "home_type":          home_info.get("homeType"),
        "status":             listing.get("statusText"),
        "availability_date":  str(avail) if avail else None,
        "availability_known": avail is not None,
        "image":              listing.get("imgSrc"),
        "is_featured":        listing.get("isFeaturedListing", False),
        "has_pool":           listing.get("factsAndFeatures", {}).get("hasPool", False),
        "has_ac":             listing.get("factsAndFeatures", {}).get("hasAirConditioning", False),
        "has_fireplace":      listing.get("factsAndFeatures", {}).get("hasFireplace", False),
        "latitude":           listing.get("latLong", {}).get("latitude"),
        "longitude":          listing.get("latLong", {}).get("longitude"),
        "rent_zestimate":     home_info.get("rentZestimate"),
        "days_on_zillow":     home_info.get("daysOnZillow"),
        "scraped_at":         datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def save_results(listings: list[dict]) -> None:
    now         = datetime.now(timezone.utc)
    ts          = now.strftime("%Y%m%d_%H%M%S")
    timestamped = OUTPUT_DIR / f"listings_{ts}.json"
    latest      = OUTPUT_DIR / "listings_latest.json"

    payload = {
        "scraped_at": now.isoformat().replace("+00:00", "Z"),
        "filters": {
            "min_beds":   MIN_BEDS,
            "min_baths":  MIN_BATHS,
            "min_price":  MIN_PRICE,
            "max_price":  MAX_PRICE,
            "avail_start": str(AVAIL_START),
            "avail_end":   str(AVAIL_END),
        },
        "count":    len(listings),
        "listings": listings,
    }

    for path in (timestamped, latest):
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        log.info("Saved %d listings → %s", len(listings), path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Zillow Rental Scraper")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Ignore saved notified-zpids and send alerts for all current matches (does not overwrite the saved list).",
    )
    args = parser.parse_args()

    cfg = load_config()

    zip_codes: list[str] = cfg.get("zip_codes", [])
    if not zip_codes:
        log.error("No zip_codes defined in config.json")
        return

    global MIN_BEDS, MIN_BATHS, MIN_PRICE, MAX_PRICE, AVAIL_START, AVAIL_END, ALLOWED_HOME_TYPES
    MIN_BEDS  = cfg.get("min_beds",  MIN_BEDS)
    MIN_BATHS = cfg.get("min_baths", MIN_BATHS)
    MIN_PRICE = cfg.get("min_price", MIN_PRICE)
    MAX_PRICE = cfg.get("max_price", MAX_PRICE)
    raw_types = cfg.get("allowed_home_types", None)
    ALLOWED_HOME_TYPES = {t.upper() for t in raw_types} if raw_types else None
    if "avail_start" in cfg:
        AVAIL_START = datetime.strptime(cfg["avail_start"], "%Y-%m-%d").date()
    if "avail_end" in cfg:
        AVAIL_END = datetime.strptime(cfg["avail_end"], "%Y-%m-%d").date()

    use_city_search: bool = cfg.get("use_city_search", False)

    log.info("=== Zillow Scraper Starting ===")
    log.info("Search mode: %s", "city (San Diego)" if use_city_search else "per-zip")
    log.info("Zip codes  : %s", zip_codes)
    log.info("Min beds   : %s", MIN_BEDS)
    log.info("Min baths  : %s", MIN_BATHS)
    log.info("Price range: %s → %s",
             f"${MIN_PRICE:,.0f}" if MIN_PRICE else "any",
             f"${MAX_PRICE:,.0f}" if MAX_PRICE else "any")
    log.info("Home types : %s", sorted(ALLOWED_HOME_TYPES) if ALLOWED_HOME_TYPES else "any")
    log.info("Avail range: %s → %s", AVAIL_START, AVAIL_END)

    all_matched: list[dict] = []
    seen_zpids:  set[str]   = set()
    allowed_zips = set(zip_codes)

    if use_city_search:
        # ── City-search mode ──────────────────────────────────────────────────
        # One query for all of San Diego, then filter by zip locally.
        raw_listings = fetch_listings_for_city(cfg)
        for listing in raw_listings:
            zpid = str(listing.get("zpid", ""))
            if zpid in seen_zpids:
                continue
            seen_zpids.add(zpid)

            if passes_filters(listing, allowed_zips=allowed_zips):
                listing_zip = (
                    listing.get("hdpData", {}).get("homeInfo", {}).get("zipcode", "")
                    or listing.get("addressZipcode", "")
                    or "unknown"
                )
                all_matched.append(format_listing(listing, listing_zip))

        log.info("  → %d matched filters across all pages", len(all_matched))
    else:
        # ── Per-zip mode (legacy) ─────────────────────────────────────────────
        for i, zip_code in enumerate(zip_codes):
            raw_listings = fetch_listings_for_zip(zip_code, cfg)

            matched = 0
            for listing in raw_listings:
                zpid = str(listing.get("zpid", ""))
                if zpid in seen_zpids:
                    continue
                seen_zpids.add(zpid)

                if passes_filters(listing):
                    all_matched.append(format_listing(listing, zip_code))
                    matched += 1

            log.info("  → %d matched filters for zip %s", matched, zip_code)

            if i < len(zip_codes) - 1:
                delay = cfg.get("delay_between_zips_seconds", 8)
                jitter = random.uniform(0, delay * 0.5)
                log.info("Sleeping %.0fs before next zip...", delay + jitter)
                time.sleep(delay + jitter)

    log.info("=== Done: %d total matching listings ===", len(all_matched))
    save_results(all_matched)

    if args.test:
        log.info("TEST MODE – bypassing saved notified-zpids, alerting all %d match(es).", len(all_matched))

    newly_alerted = notify_new_listings(all_matched, test_mode=args.test)
    if newly_alerted:
        log.info("Sent alerts for %d new listing(s).", len(newly_alerted))
    else:
        log.info("No new listings to alert this run.")


if __name__ == "__main__":
    main()
