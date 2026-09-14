# ResellRadar v0.2 — Live Scanner

ResellRadar is a local resale opportunity scanner for a small starting bankroll.

## What changed in v0.2
- Automatically scans the Slickdeals front-page RSS feed.
- Background scan interval is editable (default: every 5 minutes).
- Manual **Scan now** button.
- Extracts deal prices and ignores items above your max purchase cost (default: $225).
- Deduplicates deals in SQLite.
- Verified sold-comp rules can turn matching live deals into BUY / SKIP decisions.
- Optional eBay Browse API integration uses active fixed-price listings as a rough asking-price estimate.
- **Important:** active eBay listings are not sold comps, so they can only produce `CHECK SOLD`, never an automatic `BUY`.
- Browser refreshes every 60 seconds while the app is open.

## Default strategy
- Bankroll: $500
- Cash reserve: $100
- Minimum projected net profit: $40
- Minimum ROI: 20%
- Maximum purchase cost: $225
- Requires at least 5 verified recent sold comps for `BUY`

## Install / update on Windows
From the `resellradar` folder:

```powershell
py -m pip install -r requirements.txt
py app.py
```

If `py` behaves strangely on your machine but `py app.py` already works, use the same Python command that worked previously.

Open:

http://127.0.0.1:5000

## How the live scanner works
1. Pulls the latest Slickdeals front-page feed.
2. Extracts a buy price from each deal.
3. Drops items over the configured max item cost.
4. Looks for a matching verified sold-comp keyword/SKU.
5. Computes estimated fees, shipping, net profit, and ROI.
6. Marks the result:
   - `BUY`: verified sold comps >= 5 and thresholds pass.
   - `CHECK SOLD`: potential thresholds pass but resale estimate is not verified sold history.
   - `SKIP`: estimate exists but thresholds fail.
   - `NEEDS COMPS`: no resale estimate yet.

## Optional eBay active-listing estimates
The official eBay Browse API requires an Application access token. Create eBay developer credentials and set these in PowerShell before running:

```powershell
$env:EBAY_CLIENT_ID="your-client-id"
$env:EBAY_CLIENT_SECRET="your-client-secret"
py app.py
```

ResellRadar will then use a trimmed median of active fixed-price listings as a rough market estimate. It deliberately labels that source `eBay active median`, because asking prices are not proof that an item actually sells there.

## Verified comp rules
Use the dashboard section **Verified sold-comp rules** for products/SKUs you personally verify from actual completed sales. Example:

- Keyword: `LEGO 75367`
- Expected sold price: `625`
- Recent sold count: `12`
- eBay fee: `13.25`
- Shipping: `25`

Any live deal whose title contains `LEGO 75367` is automatically rescored against that verified comp.

## Safety / reliability
The app does not bypass CAPTCHAs, queues, rate limits, or retailer anti-bot systems. It is a deal/resale scanner, not an anti-bot checkout tool.
