"""
notifier.py
===========
Sends SMS alerts for new Zillow listings via Gmail API (OAuth 2.0) →
Verizon email-to-SMS gateway (number@vtext.com).

Tracks which listings have already been alerted so duplicates are never sent.
The "notified" state is persisted to a JSON file so it survives container
restarts.

OAuth 2.0 setup (one-time, on your host machine):
  1. Go to https://console.cloud.google.com/
  2. Create a project (or select an existing one).
  3. Enable the Gmail API:
       APIs & Services → Enable APIs → search "Gmail API" → Enable
  4. Create OAuth credentials:
       APIs & Services → Credentials → Create Credentials →
       OAuth client ID → Desktop app → Download JSON →
       save as  credentials.json  in this repo folder.
  5. Run the one-time auth helper (on your host, NOT in Docker):
       python oauth_setup.py
     This opens a browser, you log in, and it writes token.json.
  6. Both credentials.json and token.json are volume-mounted into the
     container (read-only for credentials, read-write for token so it
     can be auto-refreshed).

Environment variables (set in .env):
  GMAIL_USER       – your Gmail address (used as From + To for email alerts)
  VERIZON_NUMBER   – 10-digit Verizon number for SMS (digits only)
  CREDENTIALS_PATH – path to credentials.json  (default: /app/credentials.json)
  TOKEN_PATH       – path to token.json         (default: /app/token.json)
  NOTIFIED_PATH    – path to notified_zpids.json (default: /app/results/notified_zpids.json)
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Load .env automatically – no-op when env vars are already set (Docker).
load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
GMAIL_USER       = os.getenv("GMAIL_USER", "")
VERIZON_NUMBER   = os.getenv("VERIZON_NUMBER", "")
CREDENTIALS_PATH = Path(os.getenv("CREDENTIALS_PATH", "/app/credentials.json"))
TOKEN_PATH       = Path(os.getenv("TOKEN_PATH",       "/app/token.json"))
NOTIFIED_PATH    = Path(os.getenv("NOTIFIED_PATH",    "/app/results/notified_zpids.json"))

# Gmail API scope – send-only, no read access needed
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Verizon email-to-SMS gateway
VERIZON_SMS_GATEWAY = "vtext.com"

# ---------------------------------------------------------------------------
# OAuth 2.0 credential management
# ---------------------------------------------------------------------------

def _get_credentials() -> Credentials | None:
    """
    Load credentials from token.json, refreshing the access token if expired.
    Returns None if credentials are missing or broken (alerts will be skipped).
    """
    if not TOKEN_PATH.exists():
        log.error(
            "token.json not found at %s. "
            "Run  python oauth_setup.py  on your host machine first.",
            TOKEN_PATH,
        )
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception as exc:
        log.error("Failed to load token.json: %s", exc)
        return None

    if creds.expired and creds.refresh_token:
        try:
            log.info("OAuth token expired – refreshing...")
            creds.refresh(Request())
            # Persist the refreshed token
            TOKEN_PATH.write_text(creds.to_json())
            log.info("OAuth token refreshed and saved.")
        except Exception as exc:
            log.error("Failed to refresh OAuth token: %s", exc)
            return None

    if not creds.valid:
        log.error("OAuth credentials are invalid. Re-run oauth_setup.py.")
        return None

    return creds


def _get_gmail_service():
    """Return an authenticated Gmail API service object, or None on failure."""
    creds = _get_credentials()
    if creds is None:
        return None
    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        log.error("Failed to build Gmail service: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Notified-zpids persistence
# ---------------------------------------------------------------------------

def load_notified() -> set[str]:
    """Load the set of zpids we have already sent an alert for."""
    if not NOTIFIED_PATH.exists():
        return set()
    try:
        with open(NOTIFIED_PATH) as f:
            data = json.load(f)
        return set(data.get("notified_zpids", []))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load notified zpids file: %s", exc)
        return set()


def save_notified(notified: set[str]) -> None:
    """Persist the notified zpids set to disk."""
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(notified),
        "notified_zpids": sorted(notified),
    }
    try:
        with open(NOTIFIED_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        log.debug("Saved %d notified zpids → %s", len(notified), NOTIFIED_PATH)
    except OSError as exc:
        log.error("Could not save notified zpids: %s", exc)


# ---------------------------------------------------------------------------
# Configuration check
# ---------------------------------------------------------------------------

def _is_configured() -> bool:
    if not GMAIL_USER:
        log.warning("GMAIL_USER not set – notifications disabled.")
        return False
    if not VERIZON_NUMBER:
        log.warning("VERIZON_NUMBER not set – notifications disabled.")
        return False
    if not TOKEN_PATH.exists():
        log.warning("token.json not found at %s – notifications disabled. Run oauth_setup.py first.", TOKEN_PATH)
        return False
    return True


def _sms_address() -> str:
    digits = "".join(c for c in VERIZON_NUMBER if c.isdigit())
    return f"{digits}@{VERIZON_SMS_GATEWAY}"


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _build_sms_body(listing: dict) -> str:
    """
    SMS body with just the Zillow link.
    """
    raw_url = listing.get("url", "")
    url = "https://www.zillow.com" + raw_url.replace("https://www.zillow.com", "").replace("http://www.zillow.com", "") if raw_url else ""
    return url


def _build_email_html(listing: dict) -> str:
    """Rich HTML email body."""
    beds  = listing.get("beds", "?")
    baths = listing.get("baths_total", "?")
    price = listing.get("price", "?")
    addr  = listing.get("address", "?")
    avail = listing.get("availability_date") or "Not specified"
    sqft  = listing.get("area_sqft", "?")
    # Sanitize URL – strip any accidental double-prefix from old cached data
    raw_url = listing.get("url", "")
    url = "https://www.zillow.com" + raw_url.replace("https://www.zillow.com", "").replace("http://www.zillow.com", "") if raw_url else ""
    img   = listing.get("image", "")
    pool  = "✅" if listing.get("has_pool") else "❌"
    ac    = "✅" if listing.get("has_ac")   else "❌"
    zpid  = listing.get("zpid", "")

    img_tag = f"<img src='{img}' style='width:100%;border-radius:8px;margin-bottom:12px;'/>" if img else ""

    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
  <h2 style="color:#006aff;">🏠 New Zillow Rental Match</h2>
  {img_tag}
  <table style="width:100%;border-collapse:collapse;">
    <tr><td style="padding:6px;font-weight:bold;">Address</td><td style="padding:6px;">{addr}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:6px;font-weight:bold;">Price</td><td style="padding:6px;">{price}</td></tr>
    <tr><td style="padding:6px;font-weight:bold;">Beds / Baths</td><td style="padding:6px;">{beds} bed / {baths} bath</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:6px;font-weight:bold;">Sq Ft</td><td style="padding:6px;">{sqft}</td></tr>
    <tr><td style="padding:6px;font-weight:bold;">Available</td><td style="padding:6px;">{avail}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:6px;font-weight:bold;">Pool</td><td style="padding:6px;">{pool}</td></tr>
    <tr><td style="padding:6px;font-weight:bold;">A/C</td><td style="padding:6px;">{ac}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:6px;font-weight:bold;">ZPID</td><td style="padding:6px;">{zpid}</td></tr>
  </table>
  <p style="margin-top:16px;">
    <a href="{url}" style="background:#006aff;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:bold;">
      View on Zillow →
    </a>
  </p>
  <p style="color:#999;font-size:12px;">Scraped at {listing.get('scraped_at','')}</p>
</body></html>
"""


def _encode_message(msg: MIMEMultipart) -> dict:
    """Encode a MIMEMultipart message for the Gmail API."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def _send_via_gmail_api(service, to: str, subject: str, plain: str, html: str | None = None) -> bool:
    """
    Send an email via the Gmail API.
    Returns True on success, False on failure.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = to
    msg.attach(MIMEText(plain, "plain"))
    if html:
        msg.attach(MIMEText(html, "html"))

    try:
        service.users().messages().send(
            userId="me",
            body=_encode_message(msg),
        ).execute()
        return True
    except HttpError as exc:
        log.error("Gmail API error sending to %s: %s", to, exc)
        return False
    except Exception as exc:
        log.error("Unexpected error sending to %s: %s", to, exc)
        return False


def send_sms_alert(listing: dict, service) -> bool:
    """Send compact SMS via Verizon email-to-text gateway."""
    to      = _sms_address()
    subject = f"Zillow Alert"
    body    = _build_sms_body(listing)

    ok = _send_via_gmail_api(service, to, subject, body)
    if ok:
        log.info("SMS alert sent → %s  (%s)", to, listing.get("address"))
    return ok


def send_email_alert(listing: dict, service) -> bool:
    """Send rich HTML email alert to yourself as a record."""
    addr    = listing.get("address", "New Listing")
    subject = f"🏠 Zillow Match: {addr}"
    plain   = _build_sms_body(listing)
    html    = _build_email_html(listing)

    ok = _send_via_gmail_api(service, GMAIL_USER, subject, plain, html)
    if ok:
        log.info("Email alert sent → %s  (%s)", GMAIL_USER, addr)
    return ok


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def notify_new_listings(listings: list[dict], test_mode: bool = False) -> list[dict]:
    """
    Given a list of matched listings, send SMS + email alerts for any that
    have not been notified before.  Updates and persists the notified set.

    test_mode – if True, ignore the saved notified-zpids and alert for every
                listing in the list.  The saved list is NOT updated, so normal
                runs afterwards still deduplicate correctly.

    Returns the list of listings that were newly alerted.
    """
    if not listings:
        return []

    if not _is_configured():
        return []

    service = _get_gmail_service()
    if service is None:
        log.error("Could not initialise Gmail API service – skipping notifications.")
        return []

    notified = set() if test_mode else load_notified()
    if test_mode:
        log.info("TEST MODE – notified-zpids cache ignored, will alert all %d listing(s).", len(listings))

    newly_alerted: list[dict] = []

    for listing in listings:
        zpid = str(listing.get("zpid", ""))
        if not zpid:
            continue
        if zpid in notified:
            log.debug("Already notified zpid %s – skipping.", zpid)
            continue

        log.info("New listing – sending alerts for zpid %s (%s)", zpid, listing.get("address"))

        sms_ok   = send_sms_alert(listing, service)
        email_ok = send_email_alert(listing, service)

        if sms_ok or email_ok:
            newly_alerted.append(listing)
            if not test_mode:
                notified.add(zpid)
        else:
            log.warning("All notification channels failed for zpid %s – will retry next run.", zpid)

    if newly_alerted and not test_mode:
        save_notified(notified)
        log.info("Alerted %d new listing(s). Total notified: %d.", len(newly_alerted), len(notified))
    elif newly_alerted and test_mode:
        log.info("TEST MODE – alerted %d listing(s). Notified cache unchanged.", len(newly_alerted))
    else:
        log.info("No new listings to alert.")

    return newly_alerted
