# ResellRadar Discord Migration

Copy these files into your existing `resellradar` folder.

## Install

```powershell
py -m pip install -r requirements.txt
```

## Environment variables

```powershell
$env:DISCORD_BOT_TOKEN="YOUR_TOKEN"
$env:DISCORD_GUILD_ID="YOUR_SERVER_ID"
```

Optional eBay credentials:

```powershell
$env:EBAY_CLIENT_ID="YOUR_CLIENT_ID"
$env:EBAY_CLIENT_SECRET="YOUR_CLIENT_SECRET"
```

## Run

```powershell
py discord_bot.py
```

Then in Discord:

- `/setup_alerts`
- `/test_alert`
- `/status`
- `/scan`
- `/radar`
- `/add_comp`
- `/strategy`
- `/set_strategy`

The bot reuses your existing `app.py` scanner and local `resellradar.db`.
