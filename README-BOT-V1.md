# ResellRadar Discord Bot v1.0

Discord-first resale scanner for a small bankroll. It separates **deal discovery** from **resale demand** so a cheap item does not become a BUY unless there is a credible buyer/liquidity signal.

## Implemented

- Multi-source discovery: Slickdeals, custom RSS/Atom feeds, optional Best Buy official Products API (watchlist-driven).
- Marketplace valuation: manual verified eBay/GOAT/StockX comps; eBay active-listing supply estimate; optional official StockX highest-bid/lowest-ask integration.
- Safer product matching with identifiers, model/variant guardrails, and title similarity.
- A+ / A / B flip tiers.
- Demand score, sell-through estimate, estimated days-to-sell, price trend, volatility, and LOW/MEDIUM/HIGH risk.
- Bankroll-aware recommended quantity with reserve and concentration limits.
- Inventory: /bought, /inventory, /sold, /pnl.
- Watchlists: /watch, /unwatch, /watchlist. Watched B-tier flips are eligible for alerts.
- Alert buttons: Open deal, eBay, StockX, GOAT, Mark purchased.
- Custom deal feeds: /source_add, /sources.
- 24/7 deployment files: Dockerfile, Procfile, render.yaml, built-in HTTP health endpoint.

## Update your existing folder

Copy the new files over your existing project **without deleting `resellradar.db`**.

```powershell
py -m pip install -r requirements.txt
py discord_bot.py
```

The database migrates automatically.

## Key commands

```text
/status
/scan
/radar
/setup_alerts
/add_comp
/watch
/unwatch
/watchlist
/source_add
/sources
/bought
/inventory
/sold
/pnl
/strategy
/set_strategy
```

## Current default strategy

- Bankroll: $500
- Reserve: $100
- Minimum profit: $40
- Minimum ROI: 20%
- Max purchase cost per unit: $225
- Demand floor: 60/100
- Normal push alerts: A and A+
- Watchlist push alerts: B, A, A+
- Max same item: 2 units
- Max concentration: 45% of bankroll

## Marketplace notes

**eBay:** active listings are treated as supply/asking-price information, not completed-sale demand. Add verified sold comps with `/add_comp` until your available API access can supply stronger history.

**StockX:** if `STOCKX_API_KEY` and `STOCKX_ACCESS_TOKEN` are set, ResellRadar can use official highest-bid/lowest-ask market data. A highest bid is buyer-side demand, but it is still not the same as a completed sale.

**GOAT:** use `/add_comp` with verified GOAT sales; no unsupported private API scraping is included.

## Best Buy adapter

Set `BESTBUY_API_KEY` to enable the official Products API. It is watchlist-driven to avoid scanning the full catalog and wasting API calls. Best Buy API-sourced scanner hits are pruned after 72 hours. Follow Best Buy's API terms/branding requirements when enabling this adapter.

## 24/7 hosting

The bot exposes a tiny health endpoint when the host supplies `PORT`, so `render.yaml` can run it as a web service. Set secrets in the hosting provider's environment settings, never in Git.

For truly continuous Discord operation, choose a hosting plan/service that does not suspend the process for inactivity.
