#!/usr/bin/env python3
"""
Connection tester
=================
Run this once after setting your credentials to confirm everything works
BEFORE you deploy. It:
  1. Sends a test Telegram message (so you know pushes reach your phone)
  2. Gets an SP-API access token (confirms your Amazon credentials work)
  3. Lists how many orders exist in the last 24 hours

Usage (local):
    pip install -r requirements.txt
    # load your .env first, then:
    python test_connection.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# Load .env if python-dotenv is available or parse it manually
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from fba_notifier import (  # noqa: E402
    get_access_token, sp_api_get, send_telegram,
    MARKETPLACE_ID, REGION, REGION_ENDPOINTS,
)


def main():
    print("1/3  Sending a test Telegram message...")
    ok = send_telegram("✅ Test from your FBA Sale Notifier. If you can read this, "
                       "Telegram notifications are working!")
    if ok:
        print("     ✅ Telegram message sent. Check your phone.")
    else:
        print("     ❌ Telegram failed. Double-check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(1)

    print("2/3  Getting Amazon SP-API access token...")
    if REGION not in REGION_ENDPOINTS:
        print(f"     ❌ SP_API_REGION '{REGION}' invalid. Use na, eu, or fe.")
        sys.exit(1)
    token = get_access_token()
    print("     ✅ Amazon credentials accepted (got an access token).")

    print("3/3  Fetching orders from the last 24 hours...")
    created_after = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = sp_api_get(token, "/orders/v0/orders",
                      {"MarketplaceIds": MARKETPLACE_ID, "CreatedAfter": created_after})
    orders = data.get("payload", {}).get("Orders", [])
    fba = [o for o in orders if o.get("FulfillmentChannel") == "AFN"]
    print(f"     ✅ Amazon returned {len(orders)} order(s) total, {len(fba)} FBA, in the last 24h.")
    print("\nAll checks passed. You're ready to deploy. 🎉")


if __name__ == "__main__":
    main()
