from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import sqlite3
import statistics
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import requests

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("RESELLRADAR_DB", os.path.join(APP_DIR, "resellradar.db"))
USER_AGENT = "ResellRadar/1.0 (+personal resale opportunity scanner)"
SLICKDEALS_RSS = "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"
BESTBUY_LOGO = "https://developer.bestbuy.com/images/bestbuy-logo.png"

DEFAULTS = {
    "bankroll": 500.0,
    "min_profit": 40.0,
    "min_roi": 20.0,
    "default_fee_pct": 13.25,
    "reserve_cash": 100.0,
    "scan_interval_min": 5.0,
    "max_item_cost": 225.0,
    "min_demand_score": 60.0,
    "alert_min_grade": "A",
    "max_same_item_qty": 2,
    "max_concentration_pct": 45.0,
}

EBAY_TOKEN = {"access_token": None, "expires_at": 0.0}


@dataclass
class DealCandidate:
    title: str
    source: str
    deal_url: str
    buy_price: float
    description: str = ""
    identifier: str | None = None
    category: str | None = None
    source_logo_url: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bankroll REAL NOT NULL DEFAULT 500,
                min_profit REAL NOT NULL DEFAULT 40,
                min_roi REAL NOT NULL DEFAULT 20,
                default_fee_pct REAL NOT NULL DEFAULT 13.25,
                reserve_cash REAL NOT NULL DEFAULT 100
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

            CREATE TABLE IF NOT EXISTS marketplace_comps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                keyword TEXT NOT NULL,
                identifier TEXT,
                expected_sell_price REAL NOT NULL,
                recent_sales INTEGER NOT NULL DEFAULT 0,
                window_days INTEGER NOT NULL DEFAULT 30,
                sell_marketplace TEXT NOT NULL DEFAULT 'eBay',
                fee_pct REAL NOT NULL DEFAULT 13.25,
                outbound_shipping REAL NOT NULL DEFAULT 0,
                highest_bid REAL,
                lowest_ask REAL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                notes TEXT,
                UNIQUE(keyword, sell_marketplace)
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                term TEXT NOT NULL UNIQUE COLLATE NOCASE,
                category TEXT,
                priority INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS custom_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                kind TEXT NOT NULL DEFAULT 'rss',
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hit_id INTEGER,
                item_name TEXT NOT NULL,
                variant TEXT,
                source TEXT,
                buy_price REAL NOT NULL,
                qty INTEGER NOT NULL DEFAULT 1,
                purchase_cost REAL NOT NULL,
                purchased_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                sold_price REAL,
                sell_marketplace TEXT,
                fees REAL NOT NULL DEFAULT 0,
                shipping REAL NOT NULL DEFAULT 0,
                sold_at TEXT,
                realized_profit REAL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS market_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_key TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                market_price REAL,
                highest_bid REAL,
                lowest_ask REAL,
                UNIQUE(product_key, marketplace, observed_at)
            );
            """
        )

        # Settings migration.
        ensure_column(conn, "settings", "scan_interval_min", "REAL NOT NULL DEFAULT 5")
        ensure_column(conn, "settings", "max_item_cost", "REAL NOT NULL DEFAULT 225")
        ensure_column(conn, "settings", "min_demand_score", "REAL NOT NULL DEFAULT 60")
        ensure_column(conn, "settings", "alert_min_grade", "TEXT NOT NULL DEFAULT 'A'")
        ensure_column(conn, "settings", "max_same_item_qty", "INTEGER NOT NULL DEFAULT 2")
        ensure_column(conn, "settings", "max_concentration_pct", "REAL NOT NULL DEFAULT 45")
        ensure_column(conn, "settings", "discord_alert_channel_id", "INTEGER")

        # Scanner migration.
        scanner_cols = {
            "product_key": "TEXT", "product_identifier": "TEXT", "category": "TEXT",
            "source_logo_url": "TEXT", "match_score": "REAL NOT NULL DEFAULT 0",
            "best_marketplace": "TEXT", "demand_score": "REAL NOT NULL DEFAULT 0",
            "demand_label": "TEXT NOT NULL DEFAULT 'UNVERIFIED'", "supply_count": "INTEGER NOT NULL DEFAULT 0",
            "sell_through": "REAL", "est_days_to_sell": "REAL", "trend_pct": "REAL",
            "volatility_pct": "REAL", "risk_score": "REAL NOT NULL DEFAULT 100",
            "risk_label": "TEXT NOT NULL DEFAULT 'HIGH'", "grade": "TEXT NOT NULL DEFAULT 'SKIP'",
            "recommended_qty": "INTEGER NOT NULL DEFAULT 0", "recommended_capital": "REAL NOT NULL DEFAULT 0",
            "recommended_profit": "REAL NOT NULL DEFAULT 0", "watched": "INTEGER NOT NULL DEFAULT 0",
            "market_notes": "TEXT", "stockx_bid": "REAL", "stockx_ask": "REAL",
            "discord_alerted_at": "TEXT",
        }
        for col, definition in scanner_cols.items():
            ensure_column(conn, "scanner_hits", col, definition)

        # Marketplace comp migration from older v0.3 schema.
        for col, definition in {
            "updated_at": "TEXT", "identifier": "TEXT", "window_days": "INTEGER NOT NULL DEFAULT 30",
            "highest_bid": "REAL", "lowest_ask": "REAL", "source_type": "TEXT NOT NULL DEFAULT 'manual'",
        }.items():
            ensure_column(conn, "marketplace_comps", col, definition)

        row = conn.execute("SELECT id FROM settings WHERE id=1").fetchone()
        if not row:
            conn.execute(
                """INSERT INTO settings
                (id,bankroll,min_profit,min_roi,default_fee_pct,reserve_cash,scan_interval_min,max_item_cost,
                 min_demand_score,alert_min_grade,max_same_item_qty,max_concentration_pct)
                VALUES (1,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    DEFAULTS["bankroll"], DEFAULTS["min_profit"], DEFAULTS["min_roi"],
                    DEFAULTS["default_fee_pct"], DEFAULTS["reserve_cash"], DEFAULTS["scan_interval_min"],
                    DEFAULTS["max_item_cost"], DEFAULTS["min_demand_score"], DEFAULTS["alert_min_grade"],
                    DEFAULTS["max_same_item_qty"], DEFAULTS["max_concentration_pct"],
                ),
            )

        # Copy old single-market comp rules when present.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "comp_rules" in tables:
            old_count = conn.execute("SELECT COUNT(*) FROM comp_rules").fetchone()[0]
            new_count = conn.execute("SELECT COUNT(*) FROM marketplace_comps").fetchone()[0]
            if old_count and not new_count:
                conn.execute(
                    """INSERT OR IGNORE INTO marketplace_comps
                    (created_at,updated_at,keyword,expected_sell_price,recent_sales,sell_marketplace,fee_pct,outbound_shipping,notes)
                    SELECT created_at,created_at,keyword,expected_sell_price,recent_sales,sell_marketplace,fee_pct,outbound_shipping,notes
                    FROM comp_rules"""
                )


def get_settings() -> sqlite3.Row:
    with db() as conn:
        return conn.execute("SELECT * FROM settings WHERE id=1").fetchone()


def update_setting(**changes) -> None:
    allowed = {
        "bankroll", "min_profit", "min_roi", "default_fee_pct", "reserve_cash", "scan_interval_min",
        "max_item_cost", "min_demand_score", "alert_min_grade", "max_same_item_qty",
        "max_concentration_pct", "discord_alert_channel_id",
    }
    clean = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if not clean:
        return
    with db() as conn:
        conn.execute(
            f"UPDATE settings SET {', '.join(f'{k}=?' for k in clean)} WHERE id=1",
            tuple(clean.values()),
        )


def extract_price(text: str) -> float | None:
    if not text:
        return None
    vals = []
    for raw in re.findall(r"\$\s*([0-9]{1,5}(?:,[0-9]{3})*(?:\.\d{1,2})?)", text):
        try:
            value = float(raw.replace(",", ""))
            if 3 <= value <= 100000:
                vals.append(value)
        except ValueError:
            pass
    return vals[0] if vals else None


def normalize(text: str) -> str:
    text = text.lower().replace("™", " ").replace("®", " ")
    text = re.sub(r"\$\s*[\d,.]+", " ", text)
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


STOPWORDS = {
    "the", "and", "for", "with", "from", "new", "sale", "free", "shipping", "select", "more",
    "pack", "bundle", "deal", "off", "black", "white", "color", "version", "edition",
}


def meaningful_tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) >= 2 and t not in STOPWORDS}


def critical_tokens(text: str) -> set[str]:
    out = set()
    for token in normalize(text).split():
        if any(c.isdigit() for c in token) and len(token) >= 3:
            out.add(token)
        elif token in {"ti", "super", "pro", "max", "ultra", "oled", "slim"}:
            out.add(token)
    return out


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    aa, bb = meaningful_tokens(na), meaningful_tokens(nb)
    if not aa or not bb:
        return 0.0
    jaccard = len(aa & bb) / max(1, len(aa | bb))
    seq = SequenceMatcher(None, na, nb).ratio()
    return 0.65 * jaccard + 0.35 * seq


def safe_match_score(title: str, keyword: str, identifier: str | None = None, deal_identifier: str | None = None) -> float:
    title_n = normalize(title)
    keyword_n = normalize(keyword)
    if identifier:
        ident = normalize(identifier).replace(" ", "")
        haystacks = [title_n.replace(" ", ""), normalize(deal_identifier or "").replace(" ", "")]
        if ident and any(ident in h for h in haystacks):
            return 1.0
    if keyword_n and keyword_n in title_n:
        base = 0.96
    else:
        base = title_similarity(title, keyword)

    # Variant/model guardrail: important model tokens in the comp must appear in the deal.
    comp_critical = critical_tokens(keyword)
    deal_critical = critical_tokens(title)
    missing = comp_critical - deal_critical
    if missing:
        base -= min(0.55, 0.18 * len(missing))
    return max(0.0, min(1.0, base))


def product_key(title: str, identifier: str | None = None) -> str:
    if identifier:
        return f"id:{normalize(identifier).replace(' ', '')}"
    toks = sorted(meaningful_tokens(title))[:12]
    return "title:" + hashlib.sha1(" ".join(toks).encode()).hexdigest()[:16]


def is_watched(title: str) -> bool:
    n = normalize(title)
    with db() as conn:
        rows = conn.execute("SELECT term FROM watchlist WHERE enabled=1").fetchall()
    return any(normalize(r["term"]) in n for r in rows)


def demand_from_sales(recent_sales: int, window_days: int = 30) -> tuple[float, str, float | None]:
    recent_sales = max(0, int(recent_sales))
    window_days = max(1, int(window_days))
    if recent_sales >= 40:
        score, label = 100.0, "VERY HIGH"
    elif recent_sales >= 20:
        score, label = 90.0, "HIGH"
    elif recent_sales >= 10:
        score, label = 78.0, "GOOD"
    elif recent_sales >= 5:
        score, label = 65.0, "ENOUGH"
    elif recent_sales > 0:
        score, label = 35.0, "LOW"
    else:
        score, label = 0.0, "UNVERIFIED"
    days = round(window_days / recent_sales, 1) if recent_sales else None
    return score, label, days


def ebay_enabled() -> bool:
    return bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))


def get_ebay_token() -> str | None:
    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        return None
    now = time.time()
    if EBAY_TOKEN["access_token"] and EBAY_TOKEN["expires_at"] > now + 60:
        return EBAY_TOKEN["access_token"]
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    EBAY_TOKEN["access_token"] = data["access_token"]
    EBAY_TOKEN["expires_at"] = now + int(data.get("expires_in", 7200))
    return EBAY_TOKEN["access_token"]


def ebay_active_estimate(title: str) -> tuple[float | None, int]:
    token = get_ebay_token()
    if not token:
        return None, 0
    q = " ".join(list(meaningful_tokens(title))[:10]) or normalize(title)
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "User-Agent": USER_AGENT},
        params={"q": q[:120], "limit": "30", "filter": "buyingOptions:{FIXED_PRICE}"},
        timeout=15,
    )
    r.raise_for_status()
    prices = []
    for item in r.json().get("itemSummaries", []):
        name = item.get("title") or ""
        if title_similarity(title, name) < 0.38:
            continue
        try:
            price = float(item["price"]["value"])
            shipping = item.get("shippingOptions") or []
            ship = float(shipping[0]["shippingCost"]["value"]) if shipping and shipping[0].get("shippingCost") else 0.0
            if price > 0:
                prices.append(price + ship)
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    if len(prices) < 3:
        return None, len(prices)
    prices.sort()
    if len(prices) >= 10:
        cut = max(1, len(prices) // 10)
        prices = prices[cut:-cut]
    return round(statistics.median(prices), 2), len(prices)


def stockx_enabled() -> bool:
    return bool(os.environ.get("STOCKX_API_KEY") and os.environ.get("STOCKX_ACCESS_TOKEN"))


def stockx_market_estimate(title: str) -> dict | None:
    if not stockx_enabled():
        return None
    headers = {
        "Authorization": f"Bearer {os.environ['STOCKX_ACCESS_TOKEN'].strip()}",
        "x-api-key": os.environ["STOCKX_API_KEY"].strip(),
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    q = normalize(title)[:100]
    r = requests.get("https://api.stockx.com/v2/catalog/search", headers=headers, params={"query": q, "pageNumber": 1, "pageSize": 10}, timeout=15)
    r.raise_for_status()
    products = r.json().get("products", [])
    if not products:
        return None
    ranked = sorted(((title_similarity(title, (p.get("title") or "") + " " + (p.get("styleId") or "")), p) for p in products), reverse=True, key=lambda x: x[0])
    score, product = ranked[0]
    if score < 0.45 or not product.get("productId"):
        return None
    m = requests.get(
        f"https://api.stockx.com/v2/catalog/products/{product['productId']}/market-data",
        headers=headers, params={"currencyCode": "USD"}, timeout=15,
    )
    m.raise_for_status()
    rows = m.json()
    if isinstance(rows, dict):
        rows = [rows]
    bids, asks = [], []
    variants, bid_variants = 0, 0
    for row in rows or []:
        variants += 1
        try:
            bid = float(row.get("highestBidAmount")) if row.get("highestBidAmount") else None
        except (TypeError, ValueError):
            bid = None
        try:
            ask = float(row.get("lowestAskAmount")) if row.get("lowestAskAmount") else None
        except (TypeError, ValueError):
            ask = None
        if bid and bid > 0:
            bids.append(bid); bid_variants += 1
        if ask and ask > 0:
            asks.append(ask)
    med_bid = round(statistics.median(bids), 2) if bids else None
    med_ask = round(statistics.median(asks), 2) if asks else None
    if not med_bid:
        return {"estimate": None, "demand_score": 15.0, "highest_bid": None, "lowest_ask": med_ask, "match_score": score,
                "notes": f"StockX match {product.get('title')}; no live bids"}
    coverage = bid_variants / max(1, variants)
    ratio = med_bid / med_ask if med_ask else 0.75
    demand = 48 + min(25, coverage * 25) + (20 if ratio >= .9 else 14 if ratio >= .8 else 8 if ratio >= .7 else 3 if ratio >= .55 else 0)
    return {
        "estimate": med_bid, "demand_score": round(min(95, demand), 1), "highest_bid": med_bid,
        "lowest_ask": med_ask, "match_score": score,
        "notes": f"StockX match {product.get('title')}; {bid_variants}/{max(1,variants)} variants have bids",
    }


def best_manual_comp(title: str, deal_identifier: str | None, buy_price: float) -> dict | None:
    with db() as conn:
        rows = conn.execute("SELECT * FROM marketplace_comps").fetchall()
    candidates = []
    for row in rows:
        mscore = safe_match_score(title, row["keyword"], row["identifier"], deal_identifier)
        if mscore < 0.55:
            continue
        demand, label, days = demand_from_sales(row["recent_sales"], row["window_days"])
        payout = row["expected_sell_price"] * (1 - row["fee_pct"] / 100) - row["outbound_shipping"]
        candidates.append({
            "estimate": row["expected_sell_price"], "comp_count": row["recent_sales"], "comp_source": "verified sold comp",
            "fee_pct": row["fee_pct"], "outbound_shipping": row["outbound_shipping"], "marketplace": row["sell_marketplace"],
            "demand_score": demand, "demand_label": label, "est_days_to_sell": days, "match_score": mscore,
            "highest_bid": row["highest_bid"], "lowest_ask": row["lowest_ask"], "window_days": row["window_days"],
            "market_notes": row["notes"] or f"Verified {row['sell_marketplace']} sold comps",
            "expected_profit_before_tax": payout - buy_price,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x["expected_profit_before_tax"], x["demand_score"], x["match_score"]), reverse=True)
    return candidates[0]


def record_history(pkey: str, marketplace: str, market_price: float | None, bid: float | None = None, ask: float | None = None) -> None:
    if market_price is None and bid is None and ask is None:
        return
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO market_history(product_key,marketplace,observed_at,market_price,highest_bid,lowest_ask) VALUES (?,?,?,?,?,?)",
            (pkey, marketplace, now_iso(), market_price, bid, ask),
        )
        # Keep a compact rolling history.
        conn.execute(
            """DELETE FROM market_history WHERE id IN (
                 SELECT id FROM market_history WHERE product_key=? AND marketplace=? ORDER BY id DESC LIMIT -1 OFFSET 60
               )""", (pkey, marketplace),
        )


def history_metrics(pkey: str, marketplace: str) -> tuple[float | None, float | None]:
    with db() as conn:
        rows = conn.execute(
            "SELECT market_price FROM market_history WHERE product_key=? AND marketplace=? AND market_price IS NOT NULL ORDER BY id DESC LIMIT 12",
            (pkey, marketplace),
        ).fetchall()
    prices = [float(r["market_price"]) for r in reversed(rows)]
    if len(prices) < 2:
        return None, None
    trend = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else None
    volatility = statistics.pstdev(prices) / statistics.mean(prices) * 100 if len(prices) >= 3 and statistics.mean(prices) else None
    return round(trend, 1) if trend is not None else None, round(volatility, 1) if volatility is not None else None


def grade_for(profit: float | None, roi: float | None, settings) -> str:
    if profit is None or roi is None or profit < settings["min_profit"] or roi < settings["min_roi"]:
        return "SKIP"
    if profit >= max(70, settings["min_profit"] + 30) and roi >= max(30, settings["min_roi"] + 10):
        return "A+"
    if profit >= max(50, settings["min_profit"] + 10) and roi >= max(25, settings["min_roi"] + 5):
        return "A"
    return "B"


def risk_metrics(demand: float, verified: bool, trend: float | None, volatility: float | None,
                 sell_through: float | None, profit: float | None, roi: float | None) -> tuple[float, str]:
    risk = 58.0
    risk -= min(30, demand * 0.30)
    if verified:
        risk -= 12
    else:
        risk += 8
    if trend is not None:
        if trend < -10: risk += 18
        elif trend < -3: risk += 9
        elif trend > 8: risk -= 5
    if volatility is not None:
        risk += min(18, volatility * 0.75)
    if sell_through is not None:
        if sell_through < 15: risk += 15
        elif sell_through < 30: risk += 8
        elif sell_through > 60: risk -= 8
    if profit is not None and roi is not None:
        if profit >= 70 and roi >= 30: risk -= 8
        elif profit < 45 or roi < 22: risk += 7
    risk = round(max(0, min(100, risk)), 1)
    label = "LOW" if risk <= 30 else "MEDIUM" if risk <= 55 else "HIGH"
    return risk, label


def open_inventory_cost() -> float:
    with db() as conn:
        value = conn.execute("SELECT COALESCE(SUM(purchase_cost),0) AS total FROM inventory WHERE status='OPEN'").fetchone()["total"]
    return float(value or 0)


def available_capital(settings=None) -> float:
    settings = settings or get_settings()
    return max(0.0, float(settings["bankroll"]) - float(settings["reserve_cash"]) - open_inventory_cost())


def recommendation(buy_price: float, profit_per_unit: float | None, grade: str, settings=None) -> tuple[int, float, float]:
    settings = settings or get_settings()
    if buy_price <= 0 or not profit_per_unit or grade == "SKIP":
        return 0, 0.0, 0.0
    capital = available_capital(settings)
    max_budget = math.floor(capital / buy_price)
    concentration = float(settings["bankroll"]) * float(settings["max_concentration_pct"]) / 100.0
    max_concentration = max(1, math.floor(concentration / buy_price)) if buy_price <= concentration else 1
    grade_cap = 2 if grade in {"A", "A+"} else 1
    qty = max(0, min(int(settings["max_same_item_qty"]), grade_cap, max_budget, max_concentration))
    return qty, round(qty * buy_price, 2), round(qty * profit_per_unit, 2)


def choose_market(candidate: DealCandidate, settings) -> dict:
    pkey = product_key(candidate.title, candidate.identifier)
    manual = best_manual_comp(candidate.title, candidate.identifier, candidate.buy_price)
    ebay_estimate, ebay_supply = (None, 0)
    if ebay_enabled():
        try:
            ebay_estimate, ebay_supply = ebay_active_estimate(candidate.title)
        except Exception:
            pass

    if manual:
        manual["supply_count"] = ebay_supply if manual["marketplace"].lower() == "ebay" else 0
        if manual["comp_count"] > 0 and manual["supply_count"] > 0:
            manual["sell_through"] = round(manual["comp_count"] / (manual["comp_count"] + manual["supply_count"]) * 100, 1)
        else:
            manual["sell_through"] = None
        return manual

    if stockx_enabled():
        try:
            sx = stockx_market_estimate(candidate.title)
            if sx and sx.get("estimate"):
                return {
                    "estimate": sx["estimate"], "comp_count": 0, "comp_source": "StockX highest bid",
                    "fee_pct": float(os.environ.get("STOCKX_FEE_PCT", "12") or 12), "outbound_shipping": 0.0,
                    "marketplace": "StockX", "demand_score": sx["demand_score"], "demand_label": "BUYER BIDS",
                    "est_days_to_sell": None, "match_score": sx["match_score"], "highest_bid": sx["highest_bid"],
                    "lowest_ask": sx["lowest_ask"], "supply_count": 0, "sell_through": None, "window_days": 30,
                    "market_notes": sx["notes"],
                }
        except Exception:
            pass

    if ebay_estimate is not None:
        return {
            "estimate": ebay_estimate, "comp_count": 0, "comp_source": "eBay active median",
            "fee_pct": settings["default_fee_pct"], "outbound_shipping": 0.0, "marketplace": "eBay",
            "demand_score": 25.0, "demand_label": "UNVERIFIED", "est_days_to_sell": None,
            "match_score": 0.55, "highest_bid": None, "lowest_ask": None, "supply_count": ebay_supply,
            "sell_through": None, "window_days": 30, "market_notes": "Active asking prices only; completed-sale demand not verified",
        }

    return {
        "estimate": None, "comp_count": 0, "comp_source": None, "fee_pct": settings["default_fee_pct"],
        "outbound_shipping": 0.0, "marketplace": None, "demand_score": 0.0, "demand_label": "UNVERIFIED",
        "est_days_to_sell": None, "match_score": 0.0, "highest_bid": None, "lowest_ask": None,
        "supply_count": 0, "sell_through": None, "window_days": 30,
        "market_notes": "Deal discovered; no resale demand source matched yet",
    }


def score_candidate(candidate: DealCandidate, settings=None) -> dict:
    settings = settings or get_settings()
    market = choose_market(candidate, settings)
    estimate = market["estimate"]
    fees = estimate * market["fee_pct"] / 100 if estimate is not None else None
    profit = estimate - fees - market["outbound_shipping"] - candidate.buy_price if estimate is not None else None
    roi = profit / candidate.buy_price * 100 if profit is not None and candidate.buy_price > 0 else None
    pkey = product_key(candidate.title, candidate.identifier)

    if estimate is not None and market["marketplace"]:
        record_history(pkey, market["marketplace"], estimate, market["highest_bid"], market["lowest_ask"])
    trend, volatility = history_metrics(pkey, market["marketplace"] or "unknown")

    demand_ok = market["demand_score"] >= float(settings["min_demand_score"])
    grade = grade_for(profit, roi, settings)
    verified = market["comp_source"] == "verified sold comp"
    risk_score, risk_label = risk_metrics(
        market["demand_score"], verified, trend, volatility, market["sell_through"], profit, roi,
    )
    if not demand_ok:
        status = "LOW DEMAND"
        grade = "SKIP"
    elif grade == "SKIP":
        status = "SKIP"
    elif verified or market["comp_source"] == "StockX highest bid":
        status = "BUY"
    else:
        status = "CHECK SOLD"

    qty, capital, total_profit = recommendation(candidate.buy_price, profit, grade, settings)
    return {
        **market, "product_key": pkey, "profit": round(profit, 2) if profit is not None else None,
        "roi": round(roi, 1) if roi is not None else None, "status": status, "grade": grade,
        "trend_pct": trend, "volatility_pct": volatility, "risk_score": risk_score, "risk_label": risk_label,
        "recommended_qty": qty, "recommended_capital": capital, "recommended_profit": total_profit,
        "watched": 1 if is_watched(candidate.title) else 0,
    }


def fetch_rss(url: str, source_name: str) -> list[DealCandidate]:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = root.findall("./channel/item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for item in items[:60]:
        title = (item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link:
            atom_link = item.find("{http://www.w3.org/2005/Atom}link")
            if atom_link is not None:
                link = (atom_link.attrib.get("href") or "").strip()
        desc = (item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
        price = extract_price(title) or extract_price(desc)
        if title and link and price:
            out.append(DealCandidate(title=title, source=source_name, deal_url=link, buy_price=price, description=desc))
    return out


def fetch_slickdeals() -> list[DealCandidate]:
    return fetch_rss(SLICKDEALS_RSS, "Slickdeals")


def bestbuy_enabled() -> bool:
    return bool(os.environ.get("BESTBUY_API_KEY"))


def fetch_bestbuy_watchlist(settings) -> list[DealCandidate]:
    """Official Best Buy Products API adapter. Results are watchlist-driven to control rate use."""
    api_key = os.environ.get("BESTBUY_API_KEY", "").strip()
    if not api_key:
        return []
    with db() as conn:
        terms = [r["term"] for r in conn.execute("SELECT term FROM watchlist WHERE enabled=1 ORDER BY priority DESC,id LIMIT 8").fetchall()]
    out: list[DealCandidate] = []
    for term in terms:
        words = [w for w in normalize(term).split() if len(w) >= 2][:5]
        if not words:
            continue
        search_expr = "&".join(f"search={w}" for w in words)
        url = f"https://api.bestbuy.com/v1/products({search_expr})"
        params = {
            "format": "json", "show": "sku,name,salePrice,regularPrice,url,onlineAvailability,customerReviewAverage,customerReviewCount",
            "pageSize": 20, "apiKey": api_key,
        }
        try:
            r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
        except Exception:
            continue
        for p in r.json().get("products", []):
            try:
                price = float(p.get("salePrice"))
            except (TypeError, ValueError):
                continue
            if price <= 0 or price > settings["max_item_cost"]:
                continue
            if p.get("onlineAvailability") is False:
                continue
            sku = str(p.get("sku") or "") or None
            name = p.get("name") or term
            product_url = p.get("url") or f"https://www.bestbuy.com/site/searchpage.jsp?st={quote_plus(name)}"
            out.append(DealCandidate(
                title=name, source="Best Buy", deal_url=product_url, buy_price=price,
                description=f"Best Buy SKU {sku or 'unknown'}; regular ${p.get('regularPrice') or '—'}",
                identifier=sku, category="Electronics", source_logo_url=BESTBUY_LOGO,
            ))
    # API content cache guard: dedupe this scan; old Best Buy hits are pruned after 72 hours below.
    unique = {}
    for d in out:
        unique[(d.identifier, d.buy_price)] = d
    return list(unique.values())


def discover_deals(settings=None) -> list[DealCandidate]:
    settings = settings or get_settings()
    all_deals: list[DealCandidate] = []
    try:
        all_deals.extend(fetch_slickdeals())
    except Exception:
        pass
    with db() as conn:
        sources = conn.execute("SELECT name,url FROM custom_sources WHERE enabled=1 AND kind='rss'").fetchall()
    for source in sources:
        try:
            all_deals.extend(fetch_rss(source["url"], source["name"]))
        except Exception:
            continue
    if bestbuy_enabled():
        all_deals.extend(fetch_bestbuy_watchlist(settings))
    return all_deals


def upsert_hit(candidate: DealCandidate, scored: dict) -> tuple[int, bool]:
    fingerprint = hashlib.sha1(f"{candidate.source}|{candidate.identifier or ''}|{candidate.deal_url}".encode()).hexdigest()
    timestamp = now_iso()
    values = {
        "last_seen": timestamp, "title": candidate.title, "source": candidate.source, "deal_url": candidate.deal_url,
        "buy_price": candidate.buy_price, "market_estimate": scored["estimate"], "comp_count": scored["comp_count"],
        "comp_source": scored["comp_source"], "fee_pct": scored["fee_pct"], "outbound_shipping": scored["outbound_shipping"],
        "potential_profit": scored["profit"], "potential_roi": scored["roi"], "status": scored["status"],
        "raw_description": candidate.description, "product_key": scored["product_key"], "product_identifier": candidate.identifier,
        "category": candidate.category, "source_logo_url": candidate.source_logo_url, "match_score": scored["match_score"],
        "best_marketplace": scored["marketplace"], "demand_score": scored["demand_score"], "demand_label": scored["demand_label"],
        "supply_count": scored["supply_count"], "sell_through": scored["sell_through"], "est_days_to_sell": scored["est_days_to_sell"],
        "trend_pct": scored["trend_pct"], "volatility_pct": scored["volatility_pct"], "risk_score": scored["risk_score"],
        "risk_label": scored["risk_label"], "grade": scored["grade"], "recommended_qty": scored["recommended_qty"],
        "recommended_capital": scored["recommended_capital"], "recommended_profit": scored["recommended_profit"],
        "watched": scored["watched"], "market_notes": scored["market_notes"], "stockx_bid": scored["highest_bid"],
        "stockx_ask": scored["lowest_ask"],
    }
    with db() as conn:
        row = conn.execute("SELECT id FROM scanner_hits WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row:
            assignments = ",".join(f"{k}=?" for k in values)
            conn.execute(f"UPDATE scanner_hits SET {assignments} WHERE fingerprint=?", (*values.values(), fingerprint))
            return int(row["id"]), False
        cols = ["fingerprint", "first_seen", *values.keys()]
        vals = [fingerprint, timestamp, *values.values()]
        conn.execute(f"INSERT INTO scanner_hits({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", vals)
        hit_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return int(hit_id), True


def prune_source_cache() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).astimezone().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("DELETE FROM scanner_hits WHERE source='Best Buy' AND last_seen < ?", (cutoff,))


def run_scan() -> dict:
    settings = get_settings()
    candidates = discover_deals(settings)
    new = updated = evaluated = 0
    errors = []
    for candidate in candidates:
        if candidate.buy_price <= 0 or candidate.buy_price > settings["max_item_cost"]:
            continue
        try:
            scored = score_candidate(candidate, settings)
            _, created = upsert_hit(candidate, scored)
            new += int(created); updated += int(not created); evaluated += 1
        except Exception as exc:
            errors.append(f"{candidate.source}: {type(exc).__name__}: {exc}")
    prune_source_cache()
    return {"new": new, "updated": updated, "evaluated": evaluated, "errors": errors[:5], "at": now_iso()}


def radar(limit: int = 10, include_b: bool = True) -> list[dict]:
    settings = get_settings()
    grades = ("A+", "A", "B") if include_b else ("A+", "A")
    placeholders = ",".join("?" for _ in grades)
    with db() as conn:
        rows = conn.execute(
            f"""SELECT * FROM scanner_hits WHERE status='BUY' AND demand_score>=? AND grade IN ({placeholders})
                ORDER BY CASE grade WHEN 'A+' THEN 1 WHEN 'A' THEN 2 ELSE 3 END,
                         watched DESC, risk_score ASC, potential_profit DESC, id DESC LIMIT ?""",
            (settings["min_demand_score"], *grades, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def grade_rank(grade: str) -> int:
    return {"SKIP": 0, "B": 1, "A": 2, "A+": 3}.get(grade, 0)


def unalerted_alerts(limit: int = 20) -> list[dict]:
    settings = get_settings()
    min_rank = grade_rank(settings["alert_min_grade"])
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM scanner_hits WHERE status='BUY' AND discord_alerted_at IS NULL
               AND demand_score>=? ORDER BY watched DESC, potential_profit DESC, id ASC LIMIT ?""",
            (settings["min_demand_score"], limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        # Watched products get B-tier push alerts; normal products respect configured minimum grade.
        required = 1 if item["watched"] else min_rank
        if grade_rank(item["grade"]) >= required and item["recommended_qty"] > 0:
            out.append(item)
    return out


def mark_alerted(hit_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE scanner_hits SET discord_alerted_at=? WHERE id=?", (now_iso(), hit_id))


def add_watch(term: str, category: str | None = None, priority: int = 1) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO watchlist(created_at,term,category,priority,enabled) VALUES (?,?,?,?,1)
               ON CONFLICT(term) DO UPDATE SET category=excluded.category,priority=excluded.priority,enabled=1""",
            (now_iso(), term.strip(), category, priority),
        )


def remove_watch(term: str) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE lower(term)=lower(?)", (term.strip(),))
        return cur.rowcount > 0


def list_watch() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM watchlist WHERE enabled=1 ORDER BY priority DESC,term").fetchall()]


def add_source(name: str, url: str, kind: str = "rss") -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO custom_sources(created_at,name,kind,url,enabled) VALUES (?,?,?,?,1)
               ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,url=excluded.url,enabled=1""",
            (now_iso(), name.strip(), kind, url.strip()),
        )


def list_sources() -> list[dict]:
    base = [{"name": "Slickdeals", "kind": "rss", "url": SLICKDEALS_RSS, "enabled": 1, "built_in": True}]
    if bestbuy_enabled():
        base.append({"name": "Best Buy", "kind": "api", "url": "official Products API", "enabled": 1, "built_in": True})
    with db() as conn:
        base.extend({**dict(r), "built_in": False} for r in conn.execute("SELECT * FROM custom_sources ORDER BY name").fetchall())
    return base


def add_comp(keyword: str, marketplace: str, sell_price: float, recent_sales: int, fee_pct: float,
             shipping: float = 0.0, window_days: int = 30, identifier: str | None = None,
             highest_bid: float | None = None, lowest_ask: float | None = None, notes: str = "") -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO marketplace_comps
               (created_at,updated_at,keyword,identifier,expected_sell_price,recent_sales,window_days,sell_marketplace,
                fee_pct,outbound_shipping,highest_bid,lowest_ask,source_type,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(keyword,sell_marketplace) DO UPDATE SET
                 updated_at=excluded.updated_at,identifier=excluded.identifier,expected_sell_price=excluded.expected_sell_price,
                 recent_sales=excluded.recent_sales,window_days=excluded.window_days,fee_pct=excluded.fee_pct,
                 outbound_shipping=excluded.outbound_shipping,highest_bid=excluded.highest_bid,lowest_ask=excluded.lowest_ask,
                 notes=excluded.notes""",
            (now_iso(), now_iso(), keyword.strip(), identifier, sell_price, recent_sales, window_days, marketplace.strip(),
             fee_pct, shipping, highest_bid, lowest_ask, "manual", notes),
        )


def record_purchase(hit_id: int | None, item_name: str, buy_price: float, qty: int = 1,
                    source: str | None = None, variant: str | None = None, notes: str = "") -> int:
    qty = max(1, int(qty))
    cost = round(float(buy_price) * qty, 2)
    with db() as conn:
        conn.execute(
            """INSERT INTO inventory(hit_id,item_name,variant,source,buy_price,qty,purchase_cost,purchased_at,status,notes)
               VALUES (?,?,?,?,?,?,?,?, 'OPEN', ?)""",
            (hit_id, item_name, variant, source, buy_price, qty, cost, now_iso(), notes),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def purchase_from_hit(hit_id: int, qty: int | None = None) -> int:
    with db() as conn:
        hit = conn.execute("SELECT * FROM scanner_hits WHERE id=?", (hit_id,)).fetchone()
    if not hit:
        raise ValueError("Scanner hit not found")
    q = int(qty or hit["recommended_qty"] or 1)
    return record_purchase(hit_id, hit["title"], hit["buy_price"], q, hit["source"], notes="Marked purchased from Discord alert")


def mark_sold(inventory_id: int, sale_price: float, marketplace: str, fees: float = 0.0, shipping: float = 0.0) -> float:
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND status='OPEN'", (inventory_id,)).fetchone()
        if not item:
            raise ValueError("Open inventory item not found")
        revenue = float(sale_price) * int(item["qty"])
        profit = round(revenue - float(fees) - float(shipping) - float(item["purchase_cost"]), 2)
        conn.execute(
            """UPDATE inventory SET status='SOLD',sold_price=?,sell_marketplace=?,fees=?,shipping=?,sold_at=?,realized_profit=?
               WHERE id=?""",
            (sale_price, marketplace, fees, shipping, now_iso(), profit, inventory_id),
        )
    return profit


def inventory_rows(open_only: bool = True) -> list[dict]:
    with db() as conn:
        if open_only:
            rows = conn.execute("SELECT * FROM inventory WHERE status='OPEN' ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM inventory ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def pnl_summary() -> dict:
    with db() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='SOLD' THEN realized_profit ELSE 0 END),0) realized,
                      COALESCE(SUM(CASE WHEN status='OPEN' THEN purchase_cost ELSE 0 END),0) open_cost,
                      SUM(CASE WHEN status='OPEN' THEN qty ELSE 0 END) open_units,
                      SUM(CASE WHEN status='SOLD' THEN qty ELSE 0 END) sold_units
               FROM inventory"""
        ).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def market_links(title: str) -> dict[str, str]:
    q = quote_plus(title)
    return {
        "eBay": f"https://www.ebay.com/sch/i.html?_nkw={q}",
        "StockX": f"https://stockx.com/search?s={q}",
        "GOAT": f"https://www.goat.com/search?query={q}",
    }


init_db()
