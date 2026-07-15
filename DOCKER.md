# Docker Deployment Guide

## Headless Mode Issue

**Current Status:** Cloudflare blocks headless Chrome with Turnstile challenge. 

**Workaround:** Use `HEADLESS=false` or run locally with visible browser.

## Local Running (Recommended)

```bash
# Continuous running
python main.py --max-accounts 0

# With config
python main.py --batch-size 5 --delay-account 30
```

## Docker Setup (Limited - headless blocked)

### Build
```bash
docker build -t bluk-cf .
```

### Run
```bash
docker run -d \
  -e GMAIL_EMAIL="your@gmail.com" \
  -e GMAIL_APP_PASSWORD="your_app_password" \
  -e NINE_ROUTER_PASSWORD="your_password" \
  -e NINE_ROUTER_URL="https://my-9router-or-omniroute.com/api" \
  -e DOMAINS="domain1.com,domain2.org" \
  -e MAX_ACCOUNTS=0 \
  -e HEADLESS=false \
  bluk-cf
```

## Environment Variables

See `.env.example`.

## GitHub Container Registry

Push to `master` → auto-builds to `ghcr.io/YOUR_USERNAME/bluk-cf:latest`

## Persist Data to Host

Mount volume to save accounts CSV to host machine:

```bash
docker run -d \
  -v $(pwd)/data:/app/data \
  -e GMAIL_EMAIL="your@gmail.com" \
  -e GMAIL_APP_PASSWORD="your_password" \
  -e NINE_ROUTER_PASSWORD="your_password" \
  -e CSV_OUTPUT_PATH="/app/data/accounts.csv" \
  ghcr.io/YOUR_USERNAME/bluk-cf:latest
```

Creates `./data/accounts.csv` on host machine.

### Docker Compose with Volume

```yaml
version: '3.8'
services:
  bluk-cf:
    image: ghcr.io/YOUR_USERNAME/bluk-cf:latest
    volumes:
      - ./data:/app/data
    environment:
      - CSV_OUTPUT_PATH=/app/data/accounts.csv
      - GMAIL_EMAIL=${GMAIL_EMAIL}
      - GMAIL_APP_PASSWORD=${GMAIL_APP_PASSWORD}
      - NINE_ROUTER_PASSWORD=${NINE_ROUTER_PASSWORD}
    restart: unless-stopped
```
