from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

import resell_core as core

from dotenv import load_dotenv

load_dotenv()

BOT_VERSION = "1.0"


def money(v) -> str:
    return "—" if v is None else f"${float(v):,.2f}"


def pct(v) -> str:
    return "—" if v is None else f"{float(v):.1f}%"


def grade_color(grade: str) -> discord.Color:
    return {"A+": discord.Color.green(), "A": discord.Color.green(), "B": discord.Color.gold()}.get(grade, discord.Color.greyple())


def hit_embed(hit: dict, alert: bool = False) -> discord.Embed:
    prefix = "🚨 A+ FLIP" if hit["grade"] == "A+" else "🔥 A FLIP" if hit["grade"] == "A" else "🟢 B FLIP"
    embed = discord.Embed(
        title=f"{prefix} · {hit['risk_label']} RISK" if alert else f"{hit['grade']} · {hit['risk_label']} RISK",
        description=hit["title"][:4000], url=hit["deal_url"], color=grade_color(hit["grade"]),
    )
    embed.add_field(name="Buy", value=money(hit["buy_price"]), inline=True)
    embed.add_field(name="Market", value=money(hit["market_estimate"]), inline=True)
    embed.add_field(name="Net / unit", value=money(hit["potential_profit"]), inline=True)
    embed.add_field(name="ROI", value=pct(hit["potential_roi"]), inline=True)
    embed.add_field(name="Demand", value=f"{hit['demand_score']:.0f}/100", inline=True)
    embed.add_field(name="Risk", value=f"{hit['risk_label']} · {hit['risk_score']:.0f}/100", inline=True)
    sold = f"{hit['comp_count']} recent sales" if hit["comp_count"] else "buyer-side signal" if hit["comp_source"] == "StockX highest bid" else "unverified"
    embed.add_field(name="Liquidity", value=sold, inline=True)
    embed.add_field(name="Sell-through", value=pct(hit["sell_through"]), inline=True)
    embed.add_field(name="Est. days to sell", value="—" if hit["est_days_to_sell"] is None else f"~{hit['est_days_to_sell']:.1f}d", inline=True)
    trend = "—" if hit["trend_pct"] is None else f"{hit['trend_pct']:+.1f}%"
    embed.add_field(name="Price trend", value=trend, inline=True)
    embed.add_field(name="Match confidence", value=f"{hit['match_score']*100:.0f}%", inline=True)
    embed.add_field(name="Best market", value=hit["best_marketplace"] or "—", inline=True)
    q = int(hit["recommended_qty"] or 0)
    embed.add_field(
        name="Bankroll recommendation",
        value=(f"Buy **{q}** · use **{money(hit['recommended_capital'])}** · projected total **+{money(hit['recommended_profit']).replace('$','')}**"
               if q else "No purchase recommended with current available bankroll."), inline=False,
    )
    if hit.get("market_notes"):
        embed.add_field(name="Why", value=str(hit["market_notes"])[:900], inline=False)
    if hit.get("source_logo_url"):
        embed.set_thumbnail(url=hit["source_logo_url"])
    embed.set_footer(text=f"{hit['source']} · ResellRadar {BOT_VERSION}" + (" · WATCHLIST" if hit.get("watched") else ""))
    return embed


class HitView(discord.ui.View):
    def __init__(self, hit: dict):
        super().__init__(timeout=None)
        self.hit_id = int(hit["id"])
        self.recommended_qty = max(1, int(hit.get("recommended_qty") or 1))
        self.add_item(discord.ui.Button(label="Open deal", style=discord.ButtonStyle.link, url=hit["deal_url"]))
        links = core.market_links(hit["title"])
        for name in ("eBay", "StockX", "GOAT"):
            self.add_item(discord.ui.Button(label=name, style=discord.ButtonStyle.link, url=links[name]))
        button = discord.ui.Button(
            label=f"Mark purchased ×{self.recommended_qty}", style=discord.ButtonStyle.success,
            custom_id=f"resellradar:purchase:{self.hit_id}",
        )
        button.callback = self._mark_purchased
        self.add_item(button)

    async def _mark_purchased(self, interaction: discord.Interaction):
        try:
            inventory_id = core.purchase_from_hit(self.hit_id, self.recommended_qty)
        except Exception as exc:
            await interaction.response.send_message(f"Could not record purchase: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Recorded inventory #{inventory_id}: **{self.recommended_qty}** unit(s). Use `/inventory` to see it and `/sold` when it sells.",
            ephemeral=True,
        )


class ResellRadarBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.last_scan_monotonic = 0.0
        self.last_scan_result = None
        self.health_server = None

    async def setup_hook(self):
        guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        # Re-register recent purchase buttons so alert buttons survive bot restarts.
        with core.db() as conn:
            recent = conn.execute("SELECT * FROM scanner_hits WHERE status='BUY' ORDER BY id DESC LIMIT 100").fetchall()
        for row in recent:
            self.add_view(HitView(dict(row)))

        port = os.environ.get("PORT")
        if port:
            self.health_server = await asyncio.start_server(self._health, "0.0.0.0", int(port))
        background_scan.start()

    async def _health(self, reader, writer):
        try:
            await reader.read(4096)
            body = b"ResellRadar OK"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 14\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def on_ready(self):
        print(f"Logged in as {self.user} · ResellRadar {BOT_VERSION}")


bot = ResellRadarBot()


async def configured_alert_channel():
    channel_id = core.get_settings()["discord_alert_channel_id"]
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.DiscordException:
            return None
    return channel


async def send_alerts() -> int:
    channel = await configured_alert_channel()
    if channel is None:
        return 0
    sent = 0
    for hit in core.unalerted_alerts():
        try:
            view = HitView(hit)
            bot.add_view(view)
            await channel.send(embed=hit_embed(hit, alert=True), view=view)
            core.mark_alerted(hit["id"])
            sent += 1
        except discord.DiscordException as exc:
            print(f"alert failure {hit['id']}: {exc}")
    return sent


@tasks.loop(seconds=30)
async def background_scan():
    settings = core.get_settings()
    interval = max(60, float(settings["scan_interval_min"]) * 60)
    now = time.monotonic()
    if now - bot.last_scan_monotonic < interval:
        return
    bot.last_scan_monotonic = now
    bot.last_scan_result = await asyncio.to_thread(core.run_scan)
    await send_alerts()


@background_scan.before_loop
async def before_background_scan():
    await bot.wait_until_ready()


@bot.tree.command(name="status", description="Show scanner, data-source, bankroll and API status.")
async def status_cmd(interaction: discord.Interaction):
    s = core.get_settings(); pnl = core.pnl_summary(); sources = core.list_sources()
    embed = discord.Embed(title="ResellRadar Status", color=discord.Color.blurple())
    embed.add_field(name="Bankroll", value=money(s["bankroll"]), inline=True)
    embed.add_field(name="Available", value=money(core.available_capital(s)), inline=True)
    embed.add_field(name="Open inventory", value=money(pnl["open_cost"]), inline=True)
    embed.add_field(name="Min flip", value=f"{money(s['min_profit'])} / {s['min_roi']:.0f}% ROI", inline=True)
    embed.add_field(name="Demand floor", value=f"{s['min_demand_score']:.0f}/100", inline=True)
    embed.add_field(name="Push alerts", value=f"{s['alert_min_grade']}+ (watchlist: B+)", inline=True)
    embed.add_field(name="eBay", value="✅ connected" if core.ebay_enabled() else "⏳ not configured", inline=True)
    embed.add_field(name="StockX", value="✅ connected" if core.stockx_enabled() else "⏳ not configured", inline=True)
    embed.add_field(name="Best Buy", value="✅ connected" if core.bestbuy_enabled() else "optional API key", inline=True)
    embed.add_field(name="Sources", value=", ".join(x["name"] for x in sources)[:1000], inline=False)
    if bot.last_scan_result:
        embed.add_field(name="Last scan", value=f"{bot.last_scan_result['evaluated']} evaluated · {bot.last_scan_result['new']} new", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="scan", description="Run all configured deal sources now and send qualifying alerts.")
async def scan_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await asyncio.to_thread(core.run_scan)
    bot.last_scan_result = result; bot.last_scan_monotonic = time.monotonic()
    alerts = await send_alerts()
    msg = f"✅ Scan complete: **{result['evaluated']}** evaluated · **{result['new']}** new · **{alerts}** alert(s)."
    if result["errors"]:
        msg += "\nSome adapters had errors: " + " | ".join(result["errors"][:3])
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="radar", description="Show the best demand-qualified flips.")
@app_commands.describe(limit="1-10 opportunities", include_b="Include B-tier flips")
async def radar_cmd(interaction: discord.Interaction, limit: app_commands.Range[int,1,10]=5, include_b: bool=True):
    rows = core.radar(limit, include_b)
    if not rows:
        await interaction.response.send_message("No current flips clear your profit + demand rules.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    for hit in rows:
        await interaction.followup.send(embed=hit_embed(hit), view=HitView(hit), ephemeral=True)


@bot.tree.command(name="setup_alerts", description="Use this channel for profitable-flip alerts.")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_alerts_cmd(interaction: discord.Interaction):
    core.update_setting(discord_alert_channel_id=interaction.channel_id)
    await interaction.response.send_message(f"✅ Alerts will post in <#{interaction.channel_id}>.", ephemeral=True)


@bot.tree.command(name="test_alert", description="Send a test ResellRadar alert to the configured alert channel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def test_alert_cmd(interaction: discord.Interaction):
    channel = await configured_alert_channel()
    if channel is None:
        await interaction.response.send_message("Run `/setup_alerts` first.", ephemeral=True); return
    await channel.send(embed=discord.Embed(title="✅ ResellRadar test alert", description="Alerts are configured correctly.", color=discord.Color.green()))
    await interaction.response.send_message("Sent.", ephemeral=True)


@bot.tree.command(name="add_comp", description="Add verified resale comps from eBay, StockX, GOAT, etc.")
async def add_comp_cmd(
    interaction: discord.Interaction, keyword: str, marketplace: str,
    sell_price: app_commands.Range[float,0.01,100000.0], recent_sales: app_commands.Range[int,0,100000],
    fee_pct: app_commands.Range[float,0.0,100.0]=13.25, shipping: app_commands.Range[float,0.0,10000.0]=0,
    window_days: app_commands.Range[int,1,365]=30, identifier: Optional[str]=None,
):
    core.add_comp(keyword, marketplace, sell_price, recent_sales, fee_pct, shipping, window_days, identifier)
    score, label, days = core.demand_from_sales(recent_sales, window_days)
    await interaction.response.send_message(
        f"✅ Saved **{marketplace}** comp for **{keyword}** · demand **{score:.0f}/100 ({label})**" + (f" · est. ~{days:.1f}d/sale" if days else ""), ephemeral=True)


@bot.tree.command(name="watch", description="Prioritize a product/category; B-tier watched flips can alert.")
async def watch_cmd(interaction: discord.Interaction, term: str, category: Optional[str]=None, priority: app_commands.Range[int,1,3]=1):
    core.add_watch(term, category, priority)
    await interaction.response.send_message(f"👀 Watching **{term}** (priority {priority}).", ephemeral=True)


@bot.tree.command(name="unwatch", description="Remove a watchlist term.")
async def unwatch_cmd(interaction: discord.Interaction, term: str):
    ok = core.remove_watch(term)
    await interaction.response.send_message("✅ Removed." if ok else "That term wasn't on the watchlist.", ephemeral=True)


@bot.tree.command(name="watchlist", description="Show prioritized products/categories.")
async def watchlist_cmd(interaction: discord.Interaction):
    rows = core.list_watch()
    text = "\n".join(f"• **{r['term']}** · priority {r['priority']}" + (f" · {r['category']}" if r['category'] else "") for r in rows) or "Watchlist is empty."
    await interaction.response.send_message(text[:1900], ephemeral=True)


@bot.tree.command(name="source_add", description="Add an approved RSS/Atom deal feed.")
@app_commands.checks.has_permissions(manage_guild=True)
async def source_add_cmd(interaction: discord.Interaction, name: str, url: str):
    if not url.startswith(("https://", "http://")):
        await interaction.response.send_message("URL must start with http:// or https://", ephemeral=True); return
    core.add_source(name, url)
    await interaction.response.send_message(f"✅ Added deal feed **{name}**.", ephemeral=True)


@bot.tree.command(name="sources", description="Show active deal-discovery sources.")
async def sources_cmd(interaction: discord.Interaction):
    rows = core.list_sources()
    await interaction.response.send_message("\n".join(f"• **{r['name']}** · {r['kind']}" for r in rows)[:1900], ephemeral=True)


@bot.tree.command(name="bought", description="Record a purchase in inventory.")
async def bought_cmd(interaction: discord.Interaction, item: str, buy_price: app_commands.Range[float,0.01,100000.0], qty: app_commands.Range[int,1,100]=1, source: Optional[str]=None):
    iid = core.record_purchase(None, item, buy_price, qty, source)
    await interaction.response.send_message(f"✅ Inventory **#{iid}** recorded · {qty} × {money(buy_price)}.", ephemeral=True)


@bot.tree.command(name="inventory", description="Show open resale inventory and tied-up capital.")
async def inventory_cmd(interaction: discord.Interaction):
    rows = core.inventory_rows(True); pnl = core.pnl_summary()
    if not rows:
        await interaction.response.send_message("No open inventory.", ephemeral=True); return
    lines = [f"**#{r['id']}** · {r['qty']}× {r['item_name']} · cost {money(r['purchase_cost'])}" for r in rows[:20]]
    lines.append(f"\n**Capital tied up:** {money(pnl['open_cost'])} · **Available:** {money(core.available_capital())}")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@bot.tree.command(name="sold", description="Mark an inventory lot sold and calculate realized profit.")
async def sold_cmd(interaction: discord.Interaction, inventory_id: int, sale_price: app_commands.Range[float,0.01,100000.0], marketplace: str, fees: app_commands.Range[float,0.0,100000.0]=0, shipping: app_commands.Range[float,0.0,100000.0]=0):
    try:
        profit = core.mark_sold(inventory_id, sale_price, marketplace, fees, shipping)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True); return
    await interaction.response.send_message(f"✅ Inventory #{inventory_id} sold · realized profit **{money(profit)}**.", ephemeral=True)


@bot.tree.command(name="pnl", description="Show realized profit and current inventory exposure.")
async def pnl_cmd(interaction: discord.Interaction):
    p = core.pnl_summary(); s = core.get_settings()
    await interaction.response.send_message(
        f"**Realized P&L:** {money(p['realized'])}\n**Open inventory cost:** {money(p['open_cost'])}\n**Open units:** {p['open_units']}\n**Sold units:** {p['sold_units']}\n**Available bankroll:** {money(core.available_capital(s))}", ephemeral=True)


@bot.tree.command(name="strategy", description="Show current bankroll and alert rules.")
async def strategy_cmd(interaction: discord.Interaction):
    s = core.get_settings()
    await interaction.response.send_message(
        f"Bankroll **{money(s['bankroll'])}** · reserve **{money(s['reserve_cash'])}** · min profit **{money(s['min_profit'])}** · min ROI **{s['min_roi']:.1f}%**\n"
        f"Demand floor **{s['min_demand_score']:.0f}/100** · normal push alerts **{s['alert_min_grade']}+** · max same item **{s['max_same_item_qty']}** · concentration cap **{s['max_concentration_pct']:.0f}%**.", ephemeral=True)


@bot.tree.command(name="set_strategy", description="Update bankroll and flip thresholds.")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_strategy_cmd(
    interaction: discord.Interaction, bankroll: Optional[float]=None, reserve: Optional[float]=None,
    min_profit: Optional[float]=None, min_roi: Optional[float]=None, max_item_cost: Optional[float]=None,
    demand_floor: Optional[float]=None, alert_min_grade: Optional[str]=None,
):
    grade = alert_min_grade.upper() if alert_min_grade else None
    if grade and grade not in {"B","A","A+"}:
        await interaction.response.send_message("alert_min_grade must be B, A, or A+.", ephemeral=True); return
    core.update_setting(bankroll=bankroll, reserve_cash=reserve, min_profit=min_profit, min_roi=min_roi,
                        max_item_cost=max_item_cost, min_demand_score=demand_floor, alert_min_grade=grade)
    await interaction.response.send_message("✅ Strategy updated.", ephemeral=True)


async def on_permission_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need **Manage Server** to use that command."
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)
        return
    raise error

for command in (setup_alerts_cmd, test_alert_cmd, source_add_cmd, set_strategy_cmd):
    command.error(on_permission_error)


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN before starting the bot.")
    bot.run(token)


if __name__ == "__main__":
    main()
