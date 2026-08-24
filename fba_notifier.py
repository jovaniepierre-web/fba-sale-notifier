#!/usr/bin/env python3
"""
FBA Sale Notifier
=================
Checks your Amazon Selling Partner account for new FBA (Fulfilled by Amazon)
orders and sends you a Telegram push notification for each new sale.

It is designed to run on a schedule (e.g. every 5 minutes via GitHub Actions).
Between runs it remembers which orders it has already told you about, so you
only get notified once per sale.

Everything is configured through environment variables (see .env.example).
No credentials are ever stored in this file.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------------- #
# Configuration (from environment variables)
# --------------------------------------------------------------------------- #

LWA_CLIENT_ID = os.environ.get("LWA_CLIENT_ID", "").strip()
LWA_CLIENT_SECRET = os.environ.get("LWA_CLIENT_SECRET", "").strip()
SP_API_REFRESH_TOKEN = os.environ.get("SP_API_REFRESH_TOKEN", "").strip()

# Amazon marketplace + region.
# Marketplace IDs: https://developer-docs.amazon.com/sp-api/docs/marketplace-ids
#   US = ATVPDKIKX0DER, CA = A2EUQ1WTGCTBG2, MX = A1AM78C64UM0Y8
#   UK = A1F83G8C2ARO7P, DE = A1PA6795UKMFR9, etc.
MARKETPLACE_ID = os.environ.get("MARKETPLACE_ID", "ATVPDKIKX0DER").strip()

# Region endpoint: na (North America), eu (Europe), fe (Far East)
REGION = os.environ.get("SP_API_REGION", "na").strip().lower()

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# How far back to look for new orders on each run (minutes).
# Keep this comfortably larger than your schedule interval so nothing slips
# through the cracks if a run is delayed. Duplicates are prevented by the
# seen-orders state file regardless.
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES") or "120")

# Where the "already notified" state is stored.
STATE_FILE = os.environ.get("STATE_FILE", "seen_orders.json")

# Only notify for FBA orders? (True = FBA only, False = FBA + merchant-fulfilled)
FBA_ONLY = (os.environ.get("FBA_ONLY") or "true").strip().lower() in ("1", "true", "yes")

# Optional label shown at the top of each notification, e.g. a store/region name.
# Useful when running more than one store (e.g. "US" and "UK") into one chat.
STORE_LABEL = os.environ.get("STORE_LABEL", "").strip()

# Alert as soon as an order is placed, even while Amazon still shows it "Pending"
# (payment not yet confirmed). Each order still only alerts once. Canceled orders
# are never alerted.
INCLUDE_PENDING = (os.environ.get("INCLUDE_PENDING") or "true").strip().lower() in ("1", "true", "yes")

REGION_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}", flush=True)


def require_config():
    missing = []
    for name, val in [
        ("LWA_CLIENT_ID", LWA_CLIENT_ID),
        ("LWA_CLIENT_SECRET", LWA_CLIENT_SECRET),
        ("SP_API_REFRESH_TOKEN", SP_API_REFRESH_TOKEN),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ]:
        if not val:
            missing.append(name)
    if REGION not in REGION_ENDPOINTS:
        log(f"ERROR: SP_API_REGION must be one of {list(REGION_ENDPOINTS)}, got '{REGION}'")
        sys.exit(1)
    if missing:
        log("ERROR: missing required environment variables: " + ", ".join(missing))
        log("Set them in your .env file (local) or GitHub repository Secrets (cloud).")
        sys.exit(1)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None  # None signals "first run / no baseline yet"
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # {order_id: iso_timestamp_first_seen}
        return dict(data.get("seen", {}))
    except Exception as e:
        log(f"WARNING: could not read state file ({e}); starting fresh.")
        return {}


def save_state(seen):
    # Prune anything older than 14 days to keep the file small.
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    pruned = {}
    for oid, ts in seen.items():
        try:
            when = datetime.fromisoformat(ts)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            when = datetime.now(timezone.utc)
        if when >= cutoff:
            pruned[oid] = ts
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": pruned, "updated": datetime.now(timezone.utc).isoformat()}, f, indent=2)


# --------------------------------------------------------------------------- #
# SP-API
# --------------------------------------------------------------------------- #

def get_access_token():
    """Exchange the long-lived refresh token for a short-lived access token."""
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": SP_API_REFRESH_TOKEN,
            "client_id": LWA_CLIENT_ID,
            "client_secret": LWA_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"ERROR getting LWA access token: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    return resp.json()["access_token"]


def sp_api_get(access_token, path, params=None, max_retries=5):
    """GET against the SP-API with basic 429 backoff handling."""
    base = REGION_ENDPOINTS[REGION]
    url = base + path
    headers = {
        "x-amz-access-token": access_token,
        "Accept": "application/json",
    }
    delay = 2
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            log(f"Rate limited on {path}; backing off {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code != 200:
            log(f"ERROR {resp.status_code} on {path}: {resp.text[:500]}")
            resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Gave up after {max_retries} retries on {path}")


def fetch_recent_orders(access_token):
    # Use LastUpdatedAfter (not CreatedAfter): Amazon marks new orders "Pending"
    # first, so filtering by creation time misses them once they flip to a
    # confirmed status outside the window. LastUpdatedAfter re-surfaces an order
    # the moment its status changes, so confirmed sales are reliably caught.
    updated_after = (
        datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "MarketplaceIds": MARKETPLACE_ID,
        "LastUpdatedAfter": updated_after,
    }
    orders = []
    next_token = None
    while True:
        if next_token:
            data = sp_api_get(access_token, "/orders/v0/orders",
                              {"MarketplaceIds": MARKETPLACE_ID, "NextToken": next_token})
        else:
            data = sp_api_get(access_token, "/orders/v0/orders", params)
        payload = data.get("payload", {})
        orders.extend(payload.get("Orders", []))
        next_token = payload.get("NextToken")
        if not next_token:
            break
        time.sleep(1)  # be gentle on rate limits when paginating
    return orders


def fetch_order_items(access_token, order_id):
    """Return a list of (title, sku, qty) for the products in an order."""
    try:
        data = sp_api_get(access_token, f"/orders/v0/orders/{order_id}/orderItems")
    except Exception as e:
        log(f"WARNING: could not fetch items for {order_id}: {e}")
        return []
    items = []
    for it in data.get("payload", {}).get("OrderItems", []):
        items.append((
            it.get("Title", "(unknown item)"),
            it.get("SellerSKU", ""),
            it.get("QuantityOrdered", 0),
        ))
    return items


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"ERROR sending Telegram message: {resp.status_code} {resp.text}")
    return resp.status_code == 200


def format_notification(order, items):
    oid = order.get("AmazonOrderId", "?")
    total = order.get("OrderTotal") or {}
    amount = total.get("Amount")
    currency = total.get("CurrencyCode", "")
    status = order.get("OrderStatus", "")
    is_pending = status == "Pending"
    channel = order.get("FulfillmentChannel", "")
    channel_label = ("FBA" if channel == "AFN" else "Merchant-fulfilled"
                     if channel == "MFN" else (channel or "TBD"))
    purchase = order.get("PurchaseDate", "")

    header = "\U0001F4B0 <b>New Amazon sale!</b>"
    if STORE_LABEL:
        header += f"  ({STORE_LABEL})"
    lines = [header]
    if is_pending:
        lines.append("⏳ <i>Pending — order placed, not yet confirmed</i>")
    if items:
        for title, sku, qty in items:
            qty_str = f"{qty}× " if qty and qty != 1 else ""
            sku_str = f" <code>{sku}</code>" if sku else ""
            lines.append(f"• {qty_str}{title}{sku_str}")
    else:
        n_items = (order.get("NumberOfItemsShipped", 0) or 0) + \
                  (order.get("NumberOfItemsUnshipped", 0) or 0)
        lines.append(f"• {n_items} item(s)")

    if amount:
        lines.append(f"\n<b>Total:</b> {amount} {currency}")
    elif is_pending:
        lines.append("\n<b>Total:</b> <i>pending</i>")
    lines.append(f"<b>Channel:</b> {channel_label}")
    if status:
        lines.append(f"<b>Status:</b> {status}")
    lines.append(f"<b>Order:</b> <code>{oid}</code>")
    if purchase:
        lines.append(f"<b>Placed:</b> {purchase}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    require_config()
    log(f"Checking for new orders (region={REGION}, marketplace={MARKETPLACE_ID}, "
        f"lookback={LOOKBACK_MINUTES}m, FBA_ONLY={FBA_ONLY})")

    seen = load_state()
    first_run = (seen is None) or (len(seen) == 0)
    if seen is None:
        seen = {}
    if first_run:
        log("Baseline run: recording current orders as a baseline WITHOUT sending "
            "notifications (so you don't get a flood of old orders). Future runs notify new sales.")

    access_token = get_access_token()
    orders = fetch_recent_orders(access_token)
    log(f"Fetched {len(orders)} order(s) in the last {LOOKBACK_MINUTES} minutes.")

    now_iso = datetime.now(timezone.utc).isoformat()
    new_count = 0
    stats = {"notified": 0, "pending": 0, "non_fba": 0, "already_seen": 0, "canceled": 0}
    channels = {}

    for order in orders:
        oid = order.get("AmazonOrderId")
        ch_val = order.get("FulfillmentChannel")
        status = order.get("OrderStatus")
        channels[f"{ch_val or 'unset'}/{status or 'unknown'}"] = \
            channels.get(f"{ch_val or 'unset'}/{status or 'unknown'}", 0) + 1
        if not oid:
            continue
        # Never alert for orders that were canceled.
        if status == "Canceled":
            stats["canceled"] += 1
            continue
        # Only skip on fulfillment channel when we actually know it's not FBA.
        # (Fresh "Pending" orders sometimes have no channel set yet.)
        if FBA_ONLY and ch_val and ch_val != "AFN":
            stats["non_fba"] += 1
            continue
        if status == "Pending" and not INCLUDE_PENDING:
            stats["pending"] += 1
            continue
        if oid in seen:
            stats["already_seen"] += 1
            continue

        # New confirmed order we haven't notified about
        seen[oid] = now_iso
        new_count += 1
        if first_run:
            continue  # baseline only

        items = fetch_order_items(access_token, oid)
        msg = format_notification(order, items)
        if send_telegram(msg):
            stats["notified"] += 1
            log(f"Notified for order {oid}")
        time.sleep(1)  # gentle pacing between item lookups / messages

    log(f"Breakdown — fetched={len(orders)} by channel/status={channels} | "
        f"notified={stats['notified']} pending-skipped={stats['pending']} "
        f"non-FBA-skipped={stats['non_fba']} canceled-skipped={stats['canceled']} "
        f"already-seen={stats['already_seen']}")
    save_state(seen)

    if first_run:
        log(f"Baseline recorded ({new_count} order(s)). Future runs will notify on new sales.")
    else:
        log(f"Done. Sent {new_count} new notification(s).")


if __name__ == "__main__":
    main()
