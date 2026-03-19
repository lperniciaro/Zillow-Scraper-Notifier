# =============================================================================
# Zillow Scraper – Docker Image
# =============================================================================
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy scraper source
COPY scraper.py .
COPY notifier.py .

# Results volume mount point
RUN mkdir -p /app/results

# Default: run once and exit.
# The docker-compose schedule (or restart policy) handles repetition.
CMD ["python", "-u", "scraper.py"]
