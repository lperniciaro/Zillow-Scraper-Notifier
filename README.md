# Zillow Rental Scraper

Automated Zillow rental listing scraper with SMS/email alerts, VPN rotation, and advanced filtering.

## Features

### Core Functionality
- **Anonymous Scraping**: Routes all traffic through PIA VPN via Gluetun with automatic IP rotation
- **Smart Parsing**: Extracts data from Zillow's `__NEXT_DATA__` JSON (more reliable than XHR API)
- **Duplicate Prevention**: Tracks notified listings by ZPID to avoid repeat alerts
- **Dual Notifications**: 
  - SMS via Verizon email-to-text gateway (link only)
  - Rich HTML email with full property details
- **Persistent Results**: Saves timestamped JSON files + latest snapshot

### Unique Filter Features

#### Property Filters
- **Bedrooms**: Minimum bed count (default: 4)
- **Bathrooms**: Minimum total baths with half-bath support (default: 2.5)
- **Price Range**: Min/max monthly rent filtering
- **Home Type**: Filter by property type (SINGLE_FAMILY, TOWNHOUSE, CONDO, APARTMENT, etc.)
- **Availability Window**: Only show listings available within specific date range
- **Geographic**: Target specific zip codes

#### Advanced Features
- **Availability Date Requirement**: Filters out "available now" listings - only shows properties with known future availability dates
- **Bathroom Calculation**: Intelligently parses full + half bathrooms (half = 0.5)
- **Amenity Detection**: Tracks pool, A/C, fireplace availability
- **Rent Zestimate**: Includes Zillow's rent estimate for comparison

### Search Modes
1. **Per-Zip Mode** (default): Queries each zip code individually - faster, fewer pages
2. **City-Wide Mode**: Scrapes all San Diego rentals, then filters by zip locally - comprehensive but slower

## Setup

### Prerequisites
- Python 3.10+
- Docker & Docker Compose (for VPN mode)
- PIA VPN account (for anonymous scraping)
- Gmail account with API access (for notifications)

### Installation

1. **Clone and install dependencies**:
```bash
cd "Zillow Scraper"
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. **Configure filters** - Edit `config.json`:
```json
{
  "use_city_search": false,
  "zip_codes": ["92126", "92127", "92128"],
  "min_beds": 4,
  "min_baths": 2.5,
  "allowed_home_types": ["SINGLE_FAMILY"],
  "min_price": 3000,
  "max_price": 6000,
  "avail_start": "2026-05-01",
  "avail_end": "2026-07-15",
  "delay_between_zips_seconds": 8
}
```

**Filter Options**:
- `use_city_search`: `true` = city-wide search, `false` = per-zip (faster)
- `zip_codes`: Array of target zip codes
- `min_beds`: Minimum bedrooms (integer)
- `min_baths`: Minimum bathrooms (supports decimals for half-baths)
- `allowed_home_types`: Array of home types or `null` for all types
  - Valid types: `SINGLE_FAMILY`, `TOWNHOUSE`, `CONDO`, `APARTMENT`, `MANUFACTURED`, `LOT`, `MULTI_FAMILY`
- `min_price`/`max_price`: Monthly rent range (null = no limit)
- `avail_start`/`avail_end`: Date range for availability (YYYY-MM-DD)
- `delay_between_zips_seconds`: Polite delay between requests

3. **Set up Gmail API** (for notifications):
```bash
# 1. Go to https://console.cloud.google.com/
# 2. Create project → Enable Gmail API
# 3. Create OAuth credentials (Desktop app)
# 4. Download credentials.json to project root
# 5. Run one-time auth:
python oauth_setup.py
# This opens browser for login and creates token.json
```

4. **Configure environment** - Edit `.env`:
```bash
# Gmail notifications
GMAIL_USER=your-email@gmail.com
VERIZON_NUMBER=1234567890

# PIA VPN (for Docker mode)
PIA_USERNAME=your_pia_username
PIA_PASSWORD=your_pia_password
```

## Usage

### Local Mode (Direct)
```bash
source .venv/bin/activate
python scraper.py
```

### Local Mode (Cron)
Set up automated runs every 2 hours from 9AM-9PM:
```bash
crontab -e
# Add this line:
0 9-21/2 * * * /Users/lugworm/Desktop/Zillow\ Scraper/run_scraper.sh
```

**View logs**:
```bash
# Real-time
tail -f /Users/lugworm/Desktop/Zillow\ Scraper/scraper.log

# Last 50 lines
tail -n 50 /Users/lugworm/Desktop/Zillow\ Scraper/scraper.log

# Search for errors
grep -i error /Users/lugworm/Desktop/Zillow\ Scraper/scraper.log
```

### Docker Mode (VPN + Auto-rotation)
```bash
# Start stack
docker compose up -d

# View logs
docker logs -f zillow-scraper

# Stop stack
docker compose down
```

**Docker Architecture**:
- `gluetun`: PIA VPN tunnel with killswitch
- `ip-rotator`: Rotates VPN IP every 5 minutes
- `zillow-scraper`: Python scraper (all traffic through VPN)

### Test Mode
Send alerts for all current matches (ignores notification history):
```bash
python scraper.py --test
```

## Output

### Results Files
- `results/listings_YYYYMMDD_HHMMSS.json` - Timestamped snapshot
- `results/listings_latest.json` - Always current results
- `results/notified_zpids.json` - Tracks alerted listings

### Sample Output
```json
{
  "scraped_at": "2026-03-18T08:13:10.612346Z",
  "filters": {
    "min_beds": 4,
    "min_baths": 2.5,
    "min_price": 3000,
    "max_price": 6000,
    "avail_start": "2026-05-01",
    "avail_end": "2026-07-15"
  },
  "count": 2,
  "listings": [
    {
      "zpid": "16812556",
      "address": "San Diego, CA 92128",
      "url": "https://www.zillow.com/homedetails/...",
      "price": "$5,495/mo",
      "price_monthly": 5495,
      "beds": 5,
      "baths_total": 3.0,
      "area_sqft": 2136,
      "home_type": "SINGLE_FAMILY",
      "availability_date": "2026-07-03",
      "has_pool": false,
      "has_ac": false,
      "rent_zestimate": 4931,
      "days_on_zillow": 9
    }
  ]
}
```

## Notifications

### SMS (Text Message)
- Sent to Verizon number via email-to-SMS gateway
- Contains only the Zillow listing URL
- Instant notification for new matches

### Email
- Sent to your Gmail address
- Rich HTML format with:
  - Property image
  - Full details (beds, baths, sqft, price)
  - Amenities (pool, A/C)
  - Availability date
  - Direct link to listing

## Anti-Ban Measures

1. **VPN Rotation**: Fresh IP every 5 minutes via Gluetun
2. **User Agent Rotation**: Cycles through Chrome/Firefox/Edge on Windows/Mac
3. **Polite Delays**: Configurable delays between requests
4. **Browser Mimicry**: Sends realistic headers and referers
5. **Retry Logic**: Exponential backoff on 403/429 errors
6. **Homepage Warmup**: Visits Zillow homepage before search

## Troubleshooting

### No listings found
- Check if filters are too restrictive
- Verify zip codes are correct
- Try `use_city_search: true` for broader coverage

### 403 Forbidden errors
- Increase `delay_between_zips_seconds` in config.json
- Ensure VPN is working (Docker mode)
- Try different time of day

### Notifications not working
- Verify Gmail API setup: `python test_notify.py`
- Check token.json exists and is valid
- Confirm GMAIL_USER and VERIZON_NUMBER in .env

### Cron job not running
- Check cron logs: `grep CRON /var/log/system.log` (macOS)
- Verify script is executable: `chmod +x run_scraper.sh`
- Test script manually: `./run_scraper.sh`

## File Structure

```
Zillow Scraper/
├── scraper.py              # Main scraper logic
├── notifier.py             # SMS/email alerts via Gmail API
├── oauth_setup.py          # One-time Gmail OAuth setup
├── test_notify.py          # Test notification system
├── config.json             # Filter configuration
├── .env                    # Environment variables (credentials)
├── credentials.json        # Gmail API credentials (from Google Cloud)
├── token.json              # Gmail OAuth token (auto-generated)
├── requirements.txt        # Python dependencies
├── run_scraper.sh          # Cron wrapper script
├── docker-compose.yml      # Docker stack definition
├── Dockerfile              # Scraper container image
├── rotate_ip.sh            # VPN IP rotation script
├── results/                # Output directory
│   ├── listings_*.json     # Timestamped results
│   ├── listings_latest.json
│   └── notified_zpids.json # Notification tracking
└── gluetun-data/           # VPN configuration cache
```

## License

Personal use only. Respect Zillow's Terms of Service and robots.txt.
