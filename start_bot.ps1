if (-not $env:DISCORD_BOT_TOKEN) {
    Write-Host "DISCORD_BOT_TOKEN is not set." -ForegroundColor Red
    Write-Host '$env:DISCORD_BOT_TOKEN="YOUR_TOKEN"'
    exit 1
}
py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
py discord_bot.py
