"""
test_notify.py
==============
One-shot test: loads listings_latest.json, clears the notified cache so
every listing is treated as "new", and fires the full notification pipeline.

Usage:
    python test_notify.py
"""

import json
import logging
import os
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Point to local files (not /app/... Docker paths)
os.environ.setdefault("CONFIG_PATH",    r".\config.json")
os.environ.setdefault("OUTPUT_DIR",     r".\results")
os.environ.setdefault("NOTIFIED_PATH",  r".\results\notified_zpids.json")
os.environ.setdefault("TOKEN_PATH",     r".\token.json")
os.environ.setdefault("CREDENTIALS_PATH", r".\credentials.json")

# Load .env manually (simple key=value parser, no dotenv dep needed)
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from notifier import notify_new_listings, NOTIFIED_PATH  # noqa: E402

# Load the latest results
results_file = Path("results") / "listings_latest.json"
if not results_file.exists():
    print("No results/listings_latest.json found. Run the scraper first.")
    raise SystemExit(1)

with open(results_file) as f:
    data = json.load(f)

listings = data.get("listings", [])
print(f"Loaded {len(listings)} listing(s) from {results_file}")

# Wipe the notified cache so all listings are treated as new for this test
if NOTIFIED_PATH.exists():
    print(f"Clearing notified cache: {NOTIFIED_PATH}")
    NOTIFIED_PATH.unlink()

print("\nFiring notifications...\n")
alerted = notify_new_listings(listings)
print(f"\nDone — alerted {len(alerted)} listing(s).")
