#!/usr/bin/env python3
"""
Daily FBA Sales Report
======================
Runs once a day (early morning), pulls the previous day's orders for the US and
UK stores, updates a rolling history, and delivers to Telegram:
  1. a trend chart image + a text summary (units, orders, revenue, AOV, and
     day-over-day / 7-day-average comparisons)
  2. a self-contained HTML dashboard (as a document you can open in a browser)

Only uses the Orders API — no extra Amazon roles required. Trends build up over
time in daily_history.json (committed back to the repo by the workflow).
"""

import base64
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
LWA_CLIENT_ID = os.environ.get("LWA_CLIENT_ID", "").strip()
LWA_CLIENT_SECRET = os.environ.get("LWA_CLIENT_SECRET", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
REPORT_TZ = os.environ.get("REPORT_TZ", "America/New_York").strip()
HISTORY_FILE = os.environ.get("HISTORY_FILE", "daily_history.json")
TREND_DAYS = int(os.environ.get("TREND_DAYS") or "21")
SKU_TREND_DAYS = int(os.environ.get("SKU_TREND_DAYS") or "14")

STORES = [
    {"key": "US", "label": "\U0001F1FA\U0001F1F8 US", "region": "na",
     "marketplace": "ATVPDKIKX0DER", "currency": "USD", "symbol": "$",
     "refresh_token": os.environ.get("SP_API_REFRESH_TOKEN_US", "").strip()},
    {"key": "UK", "label": "\U0001F1EC\U0001F1E7 UK", "region": "eu",
     "marketplace": "A1F83G8C2ARO7P", "currency": "GBP", "symbol": "£",
     "refresh_token": os.environ.get("SP_API_REFRESH_TOKEN_UK", "").strip()},
]
REGION_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
LWA_URL = "https://api.amazon.com/auth/o2/token"

# Brand-ish palette
C_UNITS = "#2f6df6"
C_ORDERS = "#12a594"
C_US = "#2f6df6"
C_UK = "#e5484d"
C_AVG = "#8b8d98"


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# SP-API
# --------------------------------------------------------------------------- #
def get_access_token(refresh_token):
    r = requests.post(LWA_URL, data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": LWA_CLIENT_ID, "client_secret": LWA_CLIENT_SECRET,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def sp_get(region, access_token, path, params, max_retries=6):
    url = REGION_ENDPOINTS[region] + path
    headers = {"x-amz-access-token": access_token, "Accept": "application/json"}
    delay = 2
    for _ in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=40)
        if r.status_code == 429:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Too many retries on {path}")


def fetch_orders_since(store, access_token, created_after_iso):
    """All orders created at/after the given UTC time for one store."""
    orders, next_token, pages = [], None, 0
    while True:
        if next_token:
            params = {"MarketplaceIds": store["marketplace"], "NextToken": next_token}
        else:
            params = {"MarketplaceIds": store["marketplace"], "CreatedAfter": created_after_iso}
        data = sp_get(store["region"], access_token, "/orders/v0/orders", params)
        payload = data.get("payload", {})
        orders.extend(payload.get("Orders", []))
        next_token = payload.get("NextToken")
        pages += 1
        if not next_token:
            break
        if pages >= 60:
            log(f"WARNING: stopped paginating {store['key']} at {pages} pages "
                f"({len(orders)} orders) to stay within limits; older data may be incomplete.")
            break
        time.sleep(2)
    return orders


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def local_day(purchase_date_iso, tz):
    dt = datetime.fromisoformat(purchase_date_iso.replace("Z", "+00:00"))
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def blank_day():
    return {s["key"]: {"units": 0, "orders": 0, "revenue": 0.0} for s in STORES}


def aggregate_into(history, store, orders, tz):
    for o in orders:
        status = o.get("OrderStatus", "")
        # Count "ordered" (include Pending) to match Amazon's Sales dashboard and
        # the alerts; only genuinely canceled orders are dropped.
        if status == "Canceled":
            continue
        pd = o.get("PurchaseDate")
        if not pd:
            continue
        d = local_day(pd, tz)
        bucket = history.setdefault(d, blank_day())[store["key"]]
        bucket["orders"] += 1
        bucket["units"] += (o.get("NumberOfItemsShipped", 0) or 0) + \
                           (o.get("NumberOfItemsUnshipped", 0) or 0)
        total = o.get("OrderTotal", {})
        try:
            bucket["revenue"] += float(total.get("Amount", 0) or 0)
        except (TypeError, ValueError):
            pass


def fetch_order_items(store, access_token, order_id):
    """Return [(sku, title, qty), ...] for an order's line items."""
    try:
        data = sp_get(store["region"], access_token,
                      f"/orders/v0/orders/{order_id}/orderItems", {})
    except Exception as e:
        log(f"WARNING: order items for {order_id}: {e}")
        return []
    out = []
    for it in data.get("payload", {}).get("OrderItems", []):
        price = it.get("ItemPrice") or {}
        try:
            amt = float(price.get("Amount", 0) or 0)   # line total (price × qty), pre-tax
        except (TypeError, ValueError):
            amt = 0.0
        out.append((it.get("SellerSKU", "(no SKU)"),
                    it.get("Title", ""),
                    int(it.get("QuantityOrdered", 0) or 0),
                    amt))
    return out


def update_sku_history(store, access_token, orders, tz, days_needed, history):
    """For each day in days_needed, fetch order-item SKUs for that day's confirmed
    orders and store per-SKU unit counts in history[day]['skus'][sku] = {title, US, UK}.
    Only days present in `orders` can be filled (the fetch window)."""
    from collections import defaultdict
    by_day = defaultdict(list)
    for o in orders:
        if o.get("OrderStatus") == "Canceled":  # include Pending; drop only canceled
            continue
        pd = o.get("PurchaseDate")
        oid = o.get("AmazonOrderId")
        if not pd or not oid:
            continue
        d = local_day(pd, tz)
        if d in days_needed:
            by_day[d].append(oid)
    for d, oids in by_day.items():
        entry = history.setdefault(d, blank_day())
        skus = entry.setdefault("skus", {})
        rev = 0.0
        for oid in oids:
            for sku, title, qty, price in fetch_order_items(store, access_token, oid):
                rec = skus.setdefault(sku, {"title": title, "US": 0, "UK": 0})
                rec[store["key"]] = rec.get(store["key"], 0) + qty
                if title and not rec.get("title"):
                    rec["title"] = title
                rev += price
            time.sleep(2.0)  # stay under the getOrderItems steady rate limit
        # Revenue from line items (filled in even for pending orders, and pre-tax
        # like Amazon's "ordered product sales") overrides the order-total figure.
        entry.setdefault(store["key"], {"units": 0, "orders": 0, "revenue": 0.0})["revenue"] = round(rev, 2)
    if by_day:
        log(f"{store['key']}: fetched SKU items for {len(by_day)} day(s): {sorted(by_day)}")


def sku_rows_sorted(skus):
    """SKUs sorted by total units desc: [(sku, rec, total), ...]."""
    rows = [(sku, rec, rec.get("US", 0) + rec.get("UK", 0)) for sku, rec in skus.items()]
    rows.sort(key=lambda r: -r[2])
    return rows


_BARS = "▁▂▃▄▅▆▇█"
def sparkline(vals):
    if not vals or max(vals) == 0:
        return "▁" * len(vals)
    mx = max(vals)
    return "".join(_BARS[min(len(_BARS) - 1, int(v / mx * (len(_BARS) - 1)))] for v in vals)


def sku_window_series(history, target_date):
    """Return (day_labels, {sku: {title, US, UK, total, daily:[...]}}) over the
    last SKU_TREND_DAYS days, from the per-day SKU tallies stored in history."""
    days = [d.strftime("%Y-%m-%d") for d in date_range(target_date, SKU_TREND_DAYS)]
    skus = {}
    for i, d in enumerate(days):
        for sku, rec in history.get(d, {}).get("skus", {}).items():
            s = skus.setdefault(sku, {"title": rec.get("title", ""), "US": 0, "UK": 0,
                                      "daily": [0] * len(days)})
            us = rec.get("US", 0)
            uk = rec.get("UK", 0)
            s["daily"][i] += us + uk
            s["US"] += us
            s["UK"] += uk
            if rec.get("title") and not s["title"]:
                s["title"] = rec["title"]
    for s in skus.values():
        s["total"] = s["US"] + s["UK"]
    return days, skus


def esc(txt):
    return (str(txt).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def date_range(end_date, days):
    return [(end_date - timedelta(days=i)) for i in range(days - 1, -1, -1)]


def day_val(history, dstr, store_key, field):
    return history.get(dstr, {}).get(store_key, {}).get(field, 0)


def combined(history, dstr, field):
    return sum(day_val(history, dstr, s["key"], field) for s in STORES)


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def moving_avg(values, window=7):
    out = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        seg = values[lo:i + 1]
        out.append(sum(seg) / len(seg) if seg else 0)
    return out


def make_sku_trend_png(days, skus, topn=6):
    """Line chart: daily units (US+UK) for the top-N SKUs over the window."""
    top = sorted(skus.items(), key=lambda kv: -kv[1]["total"])[:topn]
    fig, ax = plt.subplots(figsize=(8.2, 4.0), dpi=130)
    x = range(len(days))
    if top:
        for sku, s in top:
            ax.plot(x, s["daily"], marker="o", ms=2.5, lw=1.7, label=sku)
        step = max(1, len(days) // 10)
        ax.set_xticks(list(x)[::step])
        ax.set_xticklabels([d[5:] for d in days][::step])
        ax.legend(fontsize=7, frameon=False, ncol=2)
    else:
        ax.text(0.5, 0.5, "Per-SKU trend builds up over the next couple of weeks",
                ha="center", va="center", color="#8b8d98", transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(f"Top SKUs — units per day (last {len(days)} days)",
                 loc="left", fontweight="bold")
    ax.grid(True, color="#ececf0", lw=0.8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def make_chart_png(history, target_date):
    days = date_range(target_date, TREND_DAYS)
    dstrs = [d.strftime("%Y-%m-%d") for d in days]
    labels = [d.strftime("%b %d") for d in days]

    units = [combined(history, d, "units") for d in dstrs]
    orders = [combined(history, d, "orders") for d in dstrs]
    rev_us = [day_val(history, d, "US", "revenue") for d in dstrs]
    rev_uk = [day_val(history, d, "UK", "revenue") for d in dstrs]

    plt.rcParams.update({"font.size": 10, "axes.edgecolor": "#d0d0d5",
                         "axes.grid": True, "grid.color": "#ececf0",
                         "grid.linewidth": 0.8, "figure.dpi": 130})
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(8.2, 9.6))
    x = range(len(dstrs))
    step = max(1, len(dstrs) // 10)
    ticks = list(x)[::step]
    ticklabels = labels[::step]

    # 1) Units per day + 7-day avg
    ax1.bar(x, units, color=C_UNITS, alpha=0.85, label="Units")
    ax1.plot(x, moving_avg(units), color=C_AVG, lw=2, label="7-day avg")
    ax1.set_title("Units sold per day", loc="left", fontweight="bold")
    ax1.set_xticks(ticks); ax1.set_xticklabels(ticklabels)
    ax1.legend(frameon=False, fontsize=8)

    # 2) Revenue per day (US $ left axis, UK £ right axis)
    ax2.plot(x, rev_us, color=C_US, lw=2, marker="o", ms=3, label="US ($)")
    ax2.set_ylabel("US $", color=C_US)
    ax2.tick_params(axis="y", labelcolor=C_US)
    ax2b = ax2.twinx()
    ax2b.plot(x, rev_uk, color=C_UK, lw=2, marker="o", ms=3, label="UK (£)")
    ax2b.set_ylabel("UK £", color=C_UK)
    ax2b.tick_params(axis="y", labelcolor=C_UK)
    ax2b.grid(False)
    ax2.set_title("Revenue per day (native currency)", loc="left", fontweight="bold")
    ax2.set_xticks(ticks); ax2.set_xticklabels(ticklabels)

    # 3) Orders per day + 7-day avg
    ax3.bar(x, orders, color=C_ORDERS, alpha=0.85, label="Orders")
    ax3.plot(x, moving_avg(orders), color=C_AVG, lw=2, label="7-day avg")
    ax3.set_title("Orders per day", loc="left", fontweight="bold")
    ax3.set_xticks(ticks); ax3.set_xticklabels(ticklabels)
    ax3.legend(frameon=False, fontsize=8)

    fig.suptitle(f"RevHeads — Sales trends (last {TREND_DAYS} days)",
                 fontsize=13, fontweight="bold", x=0.02, ha="left")
    fig.tight_layout(rect=[0, 0, 1, 0.98])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# --------------------------------------------------------------------------- #
# Summary + HTML
# --------------------------------------------------------------------------- #
def pct_change(cur, prev):
    if prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def arrow(p):
    if p is None:
        return "–"
    if p > 0.5:
        return f"▲ {p:+.0f}%"
    if p < -0.5:
        return f"▼ {p:+.0f}%"
    return "≈ flat"


def money(sym, val):
    return f"{sym}{val:,.2f}"


def compute_summary(history, target_date):
    t = target_date.strftime("%Y-%m-%d")
    prev = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    prior7 = [d.strftime("%Y-%m-%d") for d in date_range(target_date - timedelta(days=1), 7)]

    u = combined(history, t, "units")
    o = combined(history, t, "orders")
    u_prev = combined(history, prev, "units")
    o_prev = combined(history, prev, "orders")
    u_avg7 = sum(combined(history, d, "units") for d in prior7) / 7.0
    o_avg7 = sum(combined(history, d, "orders") for d in prior7) / 7.0

    per_store = {}
    for s in STORES:
        rev = day_val(history, t, s["key"], "revenue")
        ordc = day_val(history, t, s["key"], "orders")
        per_store[s["key"]] = {
            "revenue": rev, "orders": ordc,
            "aov": (rev / ordc) if ordc else 0.0,
            "symbol": s["symbol"], "label": s["label"], "units": day_val(history, t, s["key"], "units"),
        }
    return {
        "date": t, "units": u, "orders": o,
        "units_dod": pct_change(u, u_prev), "orders_dod": pct_change(o, o_prev),
        "units_v7": pct_change(u, u_avg7), "orders_v7": pct_change(o, o_avg7),
        "per_store": per_store,
    }


def build_summary_text(s, sku_data):
    lines = [f"\U0001F4CA <b>Daily sales report</b> — {s['date']}", ""]
    lines.append(f"<b>{s['units']}</b> units · <b>{s['orders']}</b> orders")
    lines.append(f"vs prior day: units {arrow(s['units_dod'])}, orders {arrow(s['orders_dod'])}")
    lines.append(f"vs 7-day avg: units {arrow(s['units_v7'])}, orders {arrow(s['orders_v7'])}")
    lines.append("")
    for key in ("US", "UK"):
        ps = s["per_store"][key]
        lines.append(f"{ps['label']}: {money(ps['symbol'], ps['revenue'])} "
                     f"· {ps['orders']} orders · AOV {money(ps['symbol'], ps['aov'])}")
    top = sku_rows_sorted(sku_data)[:5]
    if top:
        lines.append("\n<b>Top SKUs (units US/UK):</b>")
        for sku, rec, total in top:
            lines.append(f"• <code>{esc(sku)}</code> — {total} ({rec.get('US',0)}/{rec.get('UK',0)})")
    lines.append("\n<i>Full per-SKU breakdown in the attached dashboard.</i>")
    return "\n".join(lines)


def build_html(history, target_date, summary, chart_png):
    chart_b64 = base64.b64encode(chart_png).decode()
    sku_data = history.get(target_date.strftime("%Y-%m-%d"), {}).get("skus", {})

    # --- yesterday's per-SKU table ---
    sku_rows = ""
    for sku, rec, total in sku_rows_sorted(sku_data):
        sku_rows += (f"<tr><td><code>{esc(sku)}</code></td>"
                     f"<td class='prod'>{esc(rec.get('title') or '')}</td>"
                     f"<td>{rec.get('US', 0)}</td><td>{rec.get('UK', 0)}</td>"
                     f"<td><b>{total}</b></td></tr>")
    if not sku_rows:
        sku_rows = "<tr><td colspan='5' style='color:var(--mut)'>No confirmed unit sales for this day.</td></tr>"

    # --- 14-day per-SKU/per-country trend ---
    sku_days, sku_series = sku_window_series(history, target_date)
    sku_trend_b64 = base64.b64encode(make_sku_trend_png(sku_days, sku_series)).decode()
    trend_rows = ""
    for sku, rec, total in sku_rows_sorted(sku_series):
        trend_rows += (f"<tr><td><code>{esc(sku)}</code></td>"
                       f"<td class='prod'>{esc(rec.get('title') or '')}</td>"
                       f"<td class='spark'>{sparkline(rec['daily'])}</td>"
                       f"<td>{rec['US']}</td><td>{rec['UK']}</td>"
                       f"<td><b>{total}</b></td></tr>")
    if not trend_rows:
        trend_rows = ("<tr><td colspan='6' style='color:var(--mut)'>Per-SKU history is still "
                      "building — it fills in over the next couple of weeks.</td></tr>")

    days = date_range(target_date, min(TREND_DAYS, 14))
    rows = ""
    for d in reversed(days):
        ds = d.strftime("%Y-%m-%d")
        rows += (f"<tr><td>{d:%b %d}</td>"
                 f"<td>{combined(history, ds, 'units')}</td>"
                 f"<td>{combined(history, ds, 'orders')}</td>"
                 f"<td>${day_val(history, ds, 'US', 'revenue'):,.2f}</td>"
                 f"<td>£{day_val(history, ds, 'UK', 'revenue'):,.2f}</td></tr>")

    def card(title, big, sub):
        return (f"<div class='card'><div class='t'>{title}</div>"
                f"<div class='b'>{big}</div><div class='s'>{sub}</div></div>")

    us = summary["per_store"]["US"]
    uk = summary["per_store"]["UK"]
    cards = "".join([
        card("Units", summary["units"], f"prior day {arrow(summary['units_dod'])}"),
        card("Orders", summary["orders"], f"prior day {arrow(summary['orders_dod'])}"),
        card("US revenue", money('$', us['revenue']), f"AOV {money('$', us['aov'])}"),
        card("UK revenue", money('£', uk['revenue']), f"AOV {money('£', uk['aov'])}"),
    ])

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>RevHeads Daily Report {summary['date']}</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1a1a1f;--mut:#6b6b76;--card:#fff;--line:#e6e6ec;--accent:#2f6df6}}
@media(prefers-color-scheme:dark){{:root{{--bg:#121316;--fg:#ececed;--mut:#9a9aa4;--card:#1c1d21;--line:#2a2b30}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 2px}}.date{{color:var(--mut);margin-bottom:20px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.card .t{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.card .b{{font-size:26px;font-weight:700;margin:4px 0}}
.card .s{{color:var(--mut);font-size:13px}}
img{{width:100%;border:1px solid var(--line);border-radius:12px;background:#fff}}
table{{width:100%;border-collapse:collapse;margin-top:22px;font-size:14px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:600}}td:not(:first-child){{text-align:right}}
h2{{font-size:15px;margin:26px 0 6px}}
td.prod{{max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
td.spark{{font-family:monospace;letter-spacing:1px;color:var(--accent);text-align:left}}
code{{font-size:12px}}
</style></head><body><div class='wrap'>
<h1>RevHeads — Daily Sales Report</h1>
<div class='date'>{summary['date']} · US + UK</div>
<div class='cards'>{cards}</div>
<img src='data:image/png;base64,{chart_b64}' alt='trend charts'>
<h2>Units sold by SKU · {summary['date']}</h2>
<table><thead><tr><th>SKU</th><th>Product</th><th>US</th><th>UK</th><th>Total</th></tr></thead>
<tbody>{sku_rows}</tbody></table>
<h2>Per-SKU trend · last {SKU_TREND_DAYS} days</h2>
<img src='data:image/png;base64,{sku_trend_b64}' alt='per-SKU trend'>
<table><thead><tr><th>SKU</th><th>Product</th><th>Trend</th><th>US {SKU_TREND_DAYS}d</th><th>UK {SKU_TREND_DAYS}d</th><th>Total</th></tr></thead>
<tbody>{trend_rows}</tbody></table>
<h2>Last {min(TREND_DAYS,14)} days</h2>
<table><thead><tr><th>Date</th><th>Units</th><th>Orders</th><th>US rev</th><th>UK rev</th></tr></thead>
<tbody>{rows}</tbody></table>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg_send_photo(png_bytes, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption,
                                 "parse_mode": "HTML"},
                      files={"photo": ("trends.png", png_bytes, "image/png")}, timeout=60)
    if r.status_code != 200:
        log(f"ERROR sendPhoto: {r.status_code} {r.text[:300]}")


def tg_send_document(html_str, filename, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                      files={"document": (filename, html_str.encode("utf-8"), "text/html")},
                      timeout=60)
    if r.status_code != 200:
        log(f"ERROR sendDocument: {r.status_code} {r.text[:300]}")


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"WARNING: could not read history ({e}); starting fresh.")
    return {}


def save_history(history, target_date):
    cutoff = (target_date - timedelta(days=max(TREND_DAYS, 40))).strftime("%Y-%m-%d")
    pruned = {d: v for d, v in history.items() if d >= cutoff}
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    for name, val in [("LWA_CLIENT_ID", LWA_CLIENT_ID), ("LWA_CLIENT_SECRET", LWA_CLIENT_SECRET),
                      ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)]:
        if not val:
            log(f"ERROR: missing {name}")
            sys.exit(1)

    tz = ZoneInfo(REPORT_TZ)
    today_local = datetime.now(tz).date()
    target_date = today_local - timedelta(days=1)   # yesterday
    log(f"Building report for {target_date} ({REPORT_TZ})")

    history = load_history()

    # Decide how far back to fetch: full window on first run, else just recent days.
    window_start = target_date - timedelta(days=TREND_DAYS - 1)
    have_window = all((window_start + timedelta(days=i)).strftime("%Y-%m-%d") in history
                      for i in range(TREND_DAYS))
    fetch_from = window_start if not have_window else (target_date - timedelta(days=2))
    created_after = datetime(fetch_from.year, fetch_from.month, fetch_from.day,
                             tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"Fetching orders created since {created_after} (first_run={not have_window})")

    # Clear the days we're about to recompute so re-runs don't double count.
    for i in range((target_date - fetch_from).days + 1):
        d = (fetch_from + timedelta(days=i)).strftime("%Y-%m-%d")
        history[d] = blank_day()

    # Which days still need per-SKU item lookups. We fetch items across the whole
    # trend window (not just the SKU-trend window) so units AND revenue come from
    # line items consistently for every day on the charts. Full backfill on first
    # run, just the recomputed recent days thereafter.
    item_window_days = [(target_date - timedelta(days=i)).strftime("%Y-%m-%d")
                        for i in range(TREND_DAYS)]
    sku_days_needed = {d for d in item_window_days if "skus" not in history.get(d, {})}

    for store in STORES:
        if not store["refresh_token"]:
            log(f"Skipping {store['key']}: no refresh token set.")
            continue
        try:
            token = get_access_token(store["refresh_token"])
            orders = fetch_orders_since(store, token, created_after)
            aggregate_into(history, store, orders, tz)
            log(f"{store['key']}: fetched {len(orders)} orders since {created_after}")
            update_sku_history(store, token, orders, tz, sku_days_needed, history)
        except Exception as e:
            log(f"ERROR for {store['key']}: {e}")

    # For the days we freshly pulled line items, use the summed ordered
    # quantities as the unit count — accurate even for pending orders (whose
    # order-level shipped/unshipped counts are often 0).
    for d in sku_days_needed:
        entry = history.get(d)
        if not entry:
            continue
        skus = entry.get("skus", {})
        for s in STORES:
            units = sum(rec.get(s["key"], 0) for rec in skus.values())
            entry.setdefault(s["key"], {"units": 0, "orders": 0, "revenue": 0.0})["units"] = units

    save_history(history, target_date)

    sku_today = history.get(target_date.strftime("%Y-%m-%d"), {}).get("skus", {})
    summary = compute_summary(history, target_date)
    chart = make_chart_png(history, target_date)
    text = build_summary_text(summary, sku_today)
    html = build_html(history, target_date, summary, chart)

    tg_send_photo(chart, text)
    tg_send_document(html, f"RevHeads-daily-report-{summary['date']}.html",
                     caption="Full dashboard — open in a browser")
    log("Report sent.")


if __name__ == "__main__":
    main()
