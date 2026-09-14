from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from flask import Flask, flash, redirect, render_template, request, url_for

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "resellradar.db")
SLICKDEALS_RSS = "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"
USER_AGENT = "ResellRadar/0.3 (+local personal resale scanner)"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DEFAULTS = {
    "bankroll": 500.0,
    "min_profit": 40.0,
    "min_roi": 20.0,
    "default_fee_pct": 13.25,
    "reserve_cash": 100.0,
    "scan_interval_min": 5.0,
    "max_item_cost": 225.0,
    "min_demand_score": 60.0,
}

SCANNER = {
    "running": False,
    "last_scan": None,
    "last_error": None,
    "last_found": 0,
}
SCANNER_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
EBAY_TOKEN = {"access_token": None, "expires_at": 0.0}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table: str, column: str, definition: str):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bankroll REAL NOT NULL,
                min_profit REAL NOT NULL,
                min_roi REAL NOT NULL,
                default_fee_pct REAL NOT NULL,
                reserve_cash REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                item_name TEXT NOT NULL,
                category TEXT,
                source TEXT,
                sell_marketplace TEXT,
                size_variant TEXT,
                buy_price REAL NOT NULL,
                sales_tax REAL NOT NULL DEFAULT 0,
                inbound_shipping REAL NOT NULL DEFAULT 0,
                expected_sell_price REAL NOT NULL,
                fee_pct REAL NOT NULL,
                outbound_shipping REAL NOT NULL DEFAULT 0,
                other_costs REAL NOT NULL DEFAULT 0,
                recent_sales INTEGER NOT NULL DEFAULT 0,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS comp_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                keyword TEXT NOT NULL UNIQUE,
                expected_sell_price REAL NOT NULL,
                recent_sales INTEGER NOT NULL DEFAULT 0,
                sell_marketplace TEXT NOT NULL DEFAULT 'eBay',
                fee_pct REAL NOT NULL DEFAULT 13.25,
                outbound_shipping REAL NOT NULL DEFAULT 0,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS marketplace_comps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                keyword TEXT NOT NULL,
                expected_sell_price REAL NOT NULL,
                recent_sales INTEGER NOT NULL DEFAULT 0,
                sell_marketplace TEXT NOT NULL DEFAULT 'eBay',
                fee_pct REAL NOT NULL DEFAULT 13.25,
                outbound_shipping REAL NOT NULL DEFAULT 0,
                notes TEXT,
                UNIQUE(keyword, sell_marketplace)
            );

            CREATE TABLE IF NOT EXISTS scanner_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                deal_url TEXT NOT NULL,
                buy_price REAL,
                market_estimate REAL,
                comp_count INTEGER NOT NULL DEFAULT 0,
                comp_source TEXT,
                fee_pct REAL NOT NULL DEFAULT 13.25,
                outbound_shipping REAL NOT NULL DEFAULT 0,
                potential_profit REAL,
                potential_roi REAL,
                status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                raw_description TEXT
            );
            """
        )
        ensure_column(conn, "settings", "scan_interval_min", "REAL NOT NULL DEFAULT 5")
        ensure_column(conn, "settings", "max_item_cost", "REAL NOT NULL DEFAULT 225")
        ensure_column(conn, "settings", "min_demand_score", "REAL NOT NULL DEFAULT 60")
        ensure_column(conn, "scanner_hits", "demand_score", "REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "scanner_hits", "demand_label", "TEXT NOT NULL DEFAULT 'UNVERIFIED'")
        ensure_column(conn, "scanner_hits", "best_marketplace", "TEXT")
        ensure_column(conn, "scanner_hits", "market_notes", "TEXT")
        ensure_column(conn, "scanner_hits", "stockx_bid", "REAL")
        ensure_column(conn, "scanner_hits", "stockx_ask", "REAL")

        row = conn.execute("SELECT id FROM settings WHERE id=1").fetchone()
        if not row:
            conn.execute(
                """INSERT INTO settings
                (id, bankroll, min_profit, min_roi, default_fee_pct, reserve_cash, scan_interval_min, max_item_cost, min_demand_score)
                VALUES (1,?,?,?,?,?,?,?,?)""",
                (
                    DEFAULTS["bankroll"], DEFAULTS["min_profit"], DEFAULTS["min_roi"],
                    DEFAULTS["default_fee_pct"], DEFAULTS["reserve_cash"],
                    DEFAULTS["scan_interval_min"], DEFAULTS["max_item_cost"], DEFAULTS["min_demand_score"],
                ),
            )

        # One-time compatibility migration from v0.2's single-marketplace comp table.
        old_count = conn.execute("SELECT COUNT(*) AS n FROM comp_rules").fetchone()["n"]
        new_count = conn.execute("SELECT COUNT(*) AS n FROM marketplace_comps").fetchone()["n"]
        if old_count and not new_count:
            conn.execute(
                """INSERT OR IGNORE INTO marketplace_comps
                (created_at,keyword,expected_sell_price,recent_sales,sell_marketplace,fee_pct,outbound_shipping,notes)
                SELECT created_at,keyword,expected_sell_price,recent_sales,sell_marketplace,fee_pct,outbound_shipping,notes
                FROM comp_rules"""
            )


def get_settings():
    with db() as conn:
        return conn.execute("SELECT * FROM settings WHERE id=1").fetchone()


def metrics(row, settings):
    total_cost = row["buy_price"] + row["sales_tax"] + row["inbound_shipping"] + row["other_costs"]
    fees = row["expected_sell_price"] * (row["fee_pct"] / 100.0)
    payout = row["expected_sell_price"] - fees - row["outbound_shipping"]
    profit = payout - total_cost
    roi = (profit / total_cost * 100.0) if total_cost > 0 else 0.0
    liquid = row["recent_sales"] >= 5
    qualifies = profit >= settings["min_profit"] and roi >= settings["min_roi"] and liquid

    if qualifies and profit >= 70 and roi >= 30:
        grade = "A+"
    elif qualifies and profit >= 50 and roi >= 25:
        grade = "A"
    elif qualifies:
        grade = "B"
    else:
        grade = "SKIP"

    score = min(100, max(0,
        (min(max(profit, 0), 100) / 100) * 35
        + (min(max(roi, 0), 60) / 60) * 35
        + (min(row["recent_sales"], 30) / 30) * 30
    ))
    return {
        "total_cost": total_cost, "fees": fees, "payout": payout,
        "profit": profit, "roi": roi, "qualifies": qualifies,
        "grade": grade, "score": round(score),
    }


def extract_price(text: str) -> float | None:
    if not text:
        return None
    matches = re.findall(r"\$\s*([0-9]{1,5}(?:,[0-9]{3})*(?:\.\d{1,2})?)", text)
    vals = []
    for m in matches:
        try:
            v = float(m.replace(",", ""))
            if 3 <= v <= 5000:
                vals.append(v)
        except ValueError:
            pass
    return vals[0] if vals else None


def clean_query(title: str) -> str:
    q = re.sub(r"\$\s*[0-9,.]+", " ", title)
    q = re.sub(r"\b(w/|with|free shipping|select accounts|coupon|code|save|off|deal)\b.*$", " ", q, flags=re.I)
    q = re.sub(r"[^A-Za-z0-9+\-(). '&]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:100]


def meaningful_tokens(text: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "new", "sale", "free", "shipping", "select", "more", "pack", "bundle"}
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3 and t not in stop}


def title_similarity(a: str, b: str) -> float:
    aa, bb = meaningful_tokens(a), meaningful_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))


def demand_from_sales(recent_sales: int) -> tuple[float, str]:
    if recent_sales >= 40:
        return 100.0, "VERY HIGH"
    if recent_sales >= 20:
        return 90.0, "HIGH"
    if recent_sales >= 10:
        return 78.0, "GOOD"
    if recent_sales >= 5:
        return 65.0, "ENOUGH"
    if recent_sales > 0:
        return 35.0, "LOW"
    return 0.0, "UNVERIFIED"


def demand_label(score: float) -> str:
    if score >= 90:
        return "VERY HIGH"
    if score >= 75:
        return "HIGH"
    if score >= 60:
        return "GOOD"
    if score >= 35:
        return "LOW"
    return "UNVERIFIED"


def get_ebay_token() -> str | None:
    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    now = time.time()
    if EBAY_TOKEN["access_token"] and EBAY_TOKEN["expires_at"] > now + 60:
        return EBAY_TOKEN["access_token"]

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=12,
    )
    r.raise_for_status()
    data = r.json()
    EBAY_TOKEN["access_token"] = data["access_token"]
    EBAY_TOKEN["expires_at"] = now + int(data.get("expires_in", 7200))
    return EBAY_TOKEN["access_token"]


def ebay_active_estimate(title: str) -> tuple[float | None, int]:
    """Conservative active-listing estimate. This is NOT sold-history demand data."""
    token = get_ebay_token()
    if not token:
        return None, 0
    q = clean_query(title)
    if len(q) < 4:
        return None, 0
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "User-Agent": USER_AGENT,
        },
        params={"q": q, "limit": "20", "filter": "buyingOptions:{FIXED_PRICE}"},
        timeout=12,
    )
    r.raise_for_status()
    items = r.json().get("itemSummaries", [])
    prices = []
    for item in items:
        try:
            p = float(item["price"]["value"])
            ship = 0.0
            shipping = item.get("shippingOptions") or []
            if shipping and shipping[0].get("shippingCost"):
                ship = float(shipping[0]["shippingCost"]["value"])
            if p > 0:
                prices.append(p + ship)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    if len(prices) < 3:
        return None, len(prices)
    prices.sort()
    if len(prices) >= 8:
        cut = max(1, len(prices) // 10)
        prices = prices[cut:-cut]
    return round(statistics.median(prices), 2), len(prices)


def stockx_enabled() -> bool:
    return bool(os.environ.get("STOCKX_API_KEY") and os.environ.get("STOCKX_ACCESS_TOKEN"))


def stockx_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('STOCKX_ACCESS_TOKEN', '').strip()}",
        "x-api-key": os.environ.get("STOCKX_API_KEY", "").strip(),
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _num(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def stockx_market_estimate(title: str) -> dict | None:
    """Use official StockX catalog + market data. Highest bids are treated as demand, not sold history."""
    if not stockx_enabled():
        return None
    q = clean_query(title)
    if len(q) < 3:
        return None

    r = requests.get(
        "https://api.stockx.com/v2/catalog/search",
        headers=stockx_headers(),
        params={"query": q, "pageNumber": 1, "pageSize": 10},
        timeout=15,
    )
    r.raise_for_status()
    products = r.json().get("products", [])
    if not products:
        return None

    ranked = sorted(
        ((title_similarity(title, (p.get("title") or "") + " " + (p.get("styleId") or "")), p) for p in products),
        key=lambda x: x[0], reverse=True,
    )
    similarity, product = ranked[0]
    if similarity < 0.34:
        return None

    product_id = product.get("productId")
    if not product_id:
        return None
    m = requests.get(
        f"https://api.stockx.com/v2/catalog/products/{product_id}/market-data",
        headers=stockx_headers(),
        params={"currencyCode": "USD"},
        timeout=15,
    )
    m.raise_for_status()
    rows = m.json()
    if isinstance(rows, dict):
        rows = [rows]

    bids, asks, sell_faster = [], [], []
    total_variants = 0
    variants_with_bid = 0
    for row in rows or []:
        total_variants += 1
        bid = _num(row.get("highestBidAmount"))
        ask = _num(row.get("lowestAskAmount"))
        faster = _num(row.get("sellFasterAmount"))
        if bid:
            bids.append(bid)
            variants_with_bid += 1
        if ask:
            asks.append(ask)
        if faster:
            sell_faster.append(faster)

    if not bids:
        return {
            "estimate": None,
            "demand_score": 15.0,
            "demand_label": "LOW",
            "highest_bid": None,
            "lowest_ask": round(statistics.median(asks), 2) if asks else None,
            "marketplace": "StockX",
            "source": "StockX market",
            "notes": f"Matched {product.get('title') or q}; no live bids found",
        }

    median_bid = round(statistics.median(bids), 2)
    median_ask = round(statistics.median(asks), 2) if asks else None
    coverage = variants_with_bid / max(1, total_variants)
    ratio = (median_bid / median_ask) if median_ask and median_ask > 0 else 0.75

    # A live highest bid is a real buyer-side demand signal. Tight bid/ask spread + bid coverage raises confidence.
    score = 48.0 + min(25.0, coverage * 25.0)
    if ratio >= 0.90:
        score += 20
    elif ratio >= 0.80:
        score += 14
    elif ratio >= 0.70:
        score += 8
    elif ratio >= 0.55:
        score += 3
    score = min(95.0, score)

    return {
        "estimate": median_bid,
        "demand_score": round(score, 1),
        "demand_label": demand_label(score),
        "highest_bid": median_bid,
        "lowest_ask": median_ask,
        "marketplace": "StockX",
        "source": "StockX highest bids",
        "notes": f"Matched {product.get('title') or q}; {variants_with_bid}/{max(1,total_variants)} variants have bids",
    }


def find_marketplace_comps(title: str) -> list[sqlite3.Row]:
    title_l = title.lower()
    with db() as conn:
        rules = conn.execute("SELECT * FROM marketplace_comps ORDER BY LENGTH(keyword) DESC").fetchall()
    return [r for r in rules if r["keyword"].lower() in title_l]


def best_manual_comp(title: str, buy_price: float) -> dict | None:
    rules = find_marketplace_comps(title)
    if not rules:
        return None
    candidates = []
    for rule in rules:
        payout = rule["expected_sell_price"] * (1.0 - rule["fee_pct"] / 100.0) - rule["outbound_shipping"]
        profit = payout - buy_price
        demand_score, label = demand_from_sales(rule["recent_sales"])
        candidates.append({
            "estimate": rule["expected_sell_price"],
            "comp_count": rule["recent_sales"],
            "comp_source": "verified sold comp",
            "fee_pct": rule["fee_pct"],
            "outbound_shipping": rule["outbound_shipping"],
            "marketplace": rule["sell_marketplace"],
            "demand_score": demand_score,
            "demand_label": label,
            "market_notes": rule["notes"] or f"Verified {rule['sell_marketplace']} sold comps",
            "expected_profit_before_tax": profit,
            "stockx_bid": None,
            "stockx_ask": None,
        })
    # Best expected net payout, while still letting demand gate the main radar.
    candidates.sort(key=lambda x: x["expected_profit_before_tax"], reverse=True)
    return candidates[0]


def score_scanner_hit(buy_price: float | None, estimate: float | None, fee_pct: float,
                      outbound_shipping: float, comp_count: int, comp_source: str | None,
                      demand_score_value: float, settings):
    if buy_price is None:
        return None, None, "NO PRICE"
    if estimate is None:
        return None, None, "LOW DEMAND" if demand_score_value < settings["min_demand_score"] else "UNVERIFIED"

    fees = estimate * fee_pct / 100.0
    profit = estimate - fees - outbound_shipping - buy_price
    roi = profit / buy_price * 100 if buy_price > 0 else 0
    demand_ok = demand_score_value >= settings["min_demand_score"]
    verified = comp_source == "verified sold comp"
    liquid = comp_count >= 5

    if not demand_ok:
        status = "LOW DEMAND"
    elif verified and liquid and profit >= settings["min_profit"] and roi >= settings["min_roi"]:
        status = "BUY"
    elif profit >= settings["min_profit"] and roi >= settings["min_roi"]:
        status = "CHECK SOLD"
    else:
        status = "SKIP"
    return round(profit, 2), round(roi, 1), status


def fetch_slickdeals() -> list[dict]:
    r = requests.get(SLICKDEALS_RSS, headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    deals = []
    for item in root.findall("./channel/item")[:40]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not title or not link:
            continue
        deals.append({
            "title": title,
            "link": link,
            "description": desc,
            "buy_price": extract_price(title) or extract_price(desc),
        })
    return deals


def choose_market_candidate(item: dict, settings) -> dict:
    buy_price = item["buy_price"]
    manual = best_manual_comp(item["title"], buy_price)
    if manual:
        return manual

    # StockX is preferred over asking-price-only eBay data because a live bid is buyer-side demand.
    if stockx_enabled():
        try:
            sx = stockx_market_estimate(item["title"])
            if sx:
                return {
                    "estimate": sx["estimate"],
                    "comp_count": 0,
                    "comp_source": sx["source"],
                    "fee_pct": float(os.environ.get("STOCKX_FEE_PCT", "12") or 12),
                    "outbound_shipping": 0.0,
                    "marketplace": sx["marketplace"],
                    "demand_score": sx["demand_score"],
                    "demand_label": sx["demand_label"],
                    "market_notes": sx["notes"],
                    "stockx_bid": sx["highest_bid"],
                    "stockx_ask": sx["lowest_ask"],
                }
        except Exception:
            pass

    ebay_enabled = bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))
    if ebay_enabled:
        try:
            estimate, count = ebay_active_estimate(item["title"])
            if estimate is not None:
                return {
                    "estimate": estimate,
                    "comp_count": count,
                    "comp_source": "eBay active median",
                    "fee_pct": settings["default_fee_pct"],
                    "outbound_shipping": 0.0,
                    "marketplace": "eBay",
                    # Active listings show supply/competition, not actual sell-through demand.
                    "demand_score": 25.0,
                    "demand_label": "UNVERIFIED",
                    "market_notes": "Active eBay asking prices only; demand not verified",
                    "stockx_bid": None,
                    "stockx_ask": None,
                }
        except Exception:
            pass

    # Slickdeals frontpage is useful discovery interest, but not proof that an item has resale demand.
    return {
        "estimate": None,
        "comp_count": 0,
        "comp_source": None,
        "fee_pct": settings["default_fee_pct"],
        "outbound_shipping": 0.0,
        "marketplace": None,
        "demand_score": 20.0,
        "demand_label": "UNVERIFIED",
        "market_notes": "Frontpage deal discovered; resale demand not yet verified",
        "stockx_bid": None,
        "stockx_ask": None,
    }


def run_scan() -> int:
    with SCANNER_LOCK:
        if SCANNER["running"]:
            return 0
        SCANNER["running"] = True
        SCANNER["last_error"] = None
    found = 0
    try:
        settings = get_settings()
        feed = fetch_slickdeals()
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        for item in feed:
            buy_price = item["buy_price"]
            if buy_price is None or buy_price > settings["max_item_cost"]:
                continue

            market = choose_market_candidate(item, settings)
            profit, roi, status = score_scanner_hit(
                buy_price, market["estimate"], market["fee_pct"], market["outbound_shipping"],
                market["comp_count"], market["comp_source"], market["demand_score"], settings
            )
            fingerprint = hashlib.sha1(item["link"].encode()).hexdigest()
            with db() as conn:
                existing = conn.execute("SELECT id FROM scanner_hits WHERE fingerprint=?", (fingerprint,)).fetchone()
                values = (
                    now, item["title"], buy_price, market["estimate"], market["comp_count"], market["comp_source"],
                    market["fee_pct"], market["outbound_shipping"], profit, roi, status, item["description"],
                    market["demand_score"], market["demand_label"], market["marketplace"], market["market_notes"],
                    market["stockx_bid"], market["stockx_ask"], fingerprint,
                )
                if existing:
                    conn.execute(
                        """UPDATE scanner_hits SET last_seen=?, title=?, buy_price=?, market_estimate=?, comp_count=?,
                        comp_source=?, fee_pct=?, outbound_shipping=?, potential_profit=?, potential_roi=?, status=?,
                        raw_description=?, demand_score=?, demand_label=?, best_marketplace=?, market_notes=?,
                        stockx_bid=?, stockx_ask=? WHERE fingerprint=?""",
                        values,
                    )
                else:
                    conn.execute(
                        """INSERT INTO scanner_hits
                        (last_seen,title,buy_price,market_estimate,comp_count,comp_source,fee_pct,outbound_shipping,
                        potential_profit,potential_roi,status,raw_description,demand_score,demand_label,best_marketplace,
                        market_notes,stockx_bid,stockx_ask,fingerprint,first_seen,source,deal_url)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        values[:-1] + (fingerprint, now, "Slickdeals", item["link"]),
                    )
                    found += 1

        with SCANNER_LOCK:
            SCANNER["last_scan"] = datetime.now().astimezone().isoformat(timespec="seconds")
            SCANNER["last_found"] = found
        return found
    except Exception as exc:
        with SCANNER_LOCK:
            SCANNER["last_error"] = f"{type(exc).__name__}: {exc}"
        return 0
    finally:
        with SCANNER_LOCK:
            SCANNER["running"] = False


def scanner_loop():
    if STOP_EVENT.wait(1.0):
        return
    while not STOP_EVENT.is_set():
        run_scan()
        try:
            interval = max(1.0, float(get_settings()["scan_interval_min"])) * 60.0
        except Exception:
            interval = 300.0
        STOP_EVENT.wait(interval)


@app.route("/")
def index():
    settings = get_settings()
    with db() as conn:
        rows = conn.execute("SELECT * FROM deals ORDER BY id DESC").fetchall()
        hits = conn.execute(
            """SELECT * FROM scanner_hits
            WHERE demand_score >= ?
            ORDER BY CASE status WHEN 'BUY' THEN 1 WHEN 'CHECK SOLD' THEN 2 WHEN 'SKIP' THEN 3 ELSE 4 END,
            demand_score DESC, COALESCE(potential_profit,-9999) DESC, id DESC LIMIT 100""",
            (settings["min_demand_score"],),
        ).fetchall()
        filtered_count = conn.execute(
            "SELECT COUNT(*) AS n FROM scanner_hits WHERE demand_score < ?", (settings["min_demand_score"],)
        ).fetchone()["n"]
        rules = conn.execute("SELECT * FROM marketplace_comps ORDER BY keyword, sell_marketplace").fetchall()
    deals = []
    committed = 0.0
    for row in rows:
        m = metrics(row, settings)
        d = dict(row)
        d.update(m)
        deals.append(d)
        if m["qualifies"]:
            committed += m["total_cost"]
    available = max(0.0, settings["bankroll"] - settings["reserve_cash"] - committed)
    with SCANNER_LOCK:
        scanner_state = dict(SCANNER)
    ebay_enabled = bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))
    return render_template(
        "index.html", deals=deals, settings=settings, available=available, committed=committed,
        hits=hits, rules=rules, scanner=scanner_state, ebay_enabled=ebay_enabled,
        stockx_enabled=stockx_enabled(), filtered_count=filtered_count,
    )


@app.route("/scan", methods=["POST"])
def scan_now():
    found = run_scan()
    with SCANNER_LOCK:
        err = SCANNER["last_error"]
    if err:
        flash(f"Scan finished with an error: {err}", "error")
    else:
        flash(f"Scan complete. {found} new deal(s) added; low-demand items stay hidden from the radar.", "ok")
    return redirect(url_for("index"))


@app.route("/scanner/clear", methods=["POST"])
def clear_scanner():
    with db() as conn:
        conn.execute("DELETE FROM scanner_hits")
    flash("Scanner results cleared.", "ok")
    return redirect(url_for("index"))


@app.route("/comps/add", methods=["POST"])
def add_comp():
    settings = get_settings()
    try:
        keyword = request.form["keyword"].strip()
        expected_sell_price = float(request.form["expected_sell_price"])
        recent_sales = int(request.form.get("recent_sales", 0) or 0)
        marketplace = request.form.get("sell_marketplace", "eBay").strip() or "eBay"
        fee_pct = float(request.form.get("fee_pct", settings["default_fee_pct"]) or settings["default_fee_pct"])
        shipping = float(request.form.get("outbound_shipping", 0) or 0)
        notes = request.form.get("notes", "").strip()
    except ValueError:
        flash("Comp fields contain an invalid number.", "error")
        return redirect(url_for("index"))
    if not keyword or expected_sell_price <= 0:
        flash("Comp keyword and sell price are required.", "error")
        return redirect(url_for("index"))
    with db() as conn:
        conn.execute(
            """INSERT INTO marketplace_comps
            (created_at,keyword,expected_sell_price,recent_sales,sell_marketplace,fee_pct,outbound_shipping,notes)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(keyword,sell_marketplace) DO UPDATE SET expected_sell_price=excluded.expected_sell_price,
            recent_sales=excluded.recent_sales,fee_pct=excluded.fee_pct,
            outbound_shipping=excluded.outbound_shipping,notes=excluded.notes""",
            (datetime.now().isoformat(timespec="seconds"), keyword, expected_sell_price, recent_sales,
             marketplace, fee_pct, shipping, notes),
        )
    run_scan()
    flash(f"{marketplace} comp saved. ResellRadar rescored the matching deals.", "ok")
    return redirect(url_for("index"))


@app.route("/comps/delete/<int:rule_id>", methods=["POST"])
def delete_comp(rule_id):
    with db() as conn:
        conn.execute("DELETE FROM marketplace_comps WHERE id=?", (rule_id,))
    flash("Marketplace comp deleted.", "ok")
    return redirect(url_for("index"))


@app.route("/add", methods=["POST"])
def add_deal():
    settings = get_settings()
    try:
        values = {
            "item_name": request.form["item_name"].strip(),
            "category": request.form.get("category", "").strip(),
            "source": request.form.get("source", "").strip(),
            "sell_marketplace": request.form.get("sell_marketplace", "").strip(),
            "size_variant": request.form.get("size_variant", "").strip(),
            "buy_price": float(request.form.get("buy_price", 0) or 0),
            "sales_tax": float(request.form.get("sales_tax", 0) or 0),
            "inbound_shipping": float(request.form.get("inbound_shipping", 0) or 0),
            "expected_sell_price": float(request.form.get("expected_sell_price", 0) or 0),
            "fee_pct": float(request.form.get("fee_pct", settings["default_fee_pct"]) or settings["default_fee_pct"]),
            "outbound_shipping": float(request.form.get("outbound_shipping", 0) or 0),
            "other_costs": float(request.form.get("other_costs", 0) or 0),
            "recent_sales": int(request.form.get("recent_sales", 0) or 0),
            "notes": request.form.get("notes", "").strip(),
        }
    except ValueError:
        flash("One of the numeric fields is invalid.", "error")
        return redirect(url_for("index"))

    if not values["item_name"] or values["buy_price"] <= 0 or values["expected_sell_price"] <= 0:
        flash("Item name, buy price, and expected sell price are required.", "error")
        return redirect(url_for("index"))

    with db() as conn:
        conn.execute(
            """INSERT INTO deals (
                created_at,item_name,category,source,sell_marketplace,size_variant,buy_price,sales_tax,
                inbound_shipping,expected_sell_price,fee_pct,outbound_shipping,other_costs,recent_sales,notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"), values["item_name"], values["category"], values["source"],
                values["sell_marketplace"], values["size_variant"], values["buy_price"], values["sales_tax"],
                values["inbound_shipping"], values["expected_sell_price"], values["fee_pct"], values["outbound_shipping"],
                values["other_costs"], values["recent_sales"], values["notes"]
            ),
        )
    flash("Deal added.", "ok")
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def update_settings():
    try:
        vals = (
            float(request.form["bankroll"]), float(request.form["min_profit"]), float(request.form["min_roi"]),
            float(request.form["default_fee_pct"]), float(request.form["reserve_cash"]),
            float(request.form.get("scan_interval_min", 5) or 5), float(request.form.get("max_item_cost", 225) or 225),
            float(request.form.get("min_demand_score", 60) or 60),
        )
    except ValueError:
        flash("Settings must be valid numbers.", "error")
        return redirect(url_for("index"))
    with db() as conn:
        conn.execute(
            """UPDATE settings SET bankroll=?, min_profit=?, min_roi=?, default_fee_pct=?, reserve_cash=?,
            scan_interval_min=?, max_item_cost=?, min_demand_score=? WHERE id=1""", vals,
        )
    flash("Settings updated.", "ok")
    return redirect(url_for("index"))


@app.route("/delete/<int:deal_id>", methods=["POST"])
def delete_deal(deal_id):
    with db() as conn:
        conn.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=scanner_loop, name="resellradar-scanner", daemon=True)
    t.start()
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
