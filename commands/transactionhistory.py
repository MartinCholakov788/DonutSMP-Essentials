import re

from discord import app_commands
from discord.ext import commands

import main as core


class TransactionHistory(commands.Cog):
    @app_commands.command(
        name="transactionhistory",
        description="Query events received by this bot's transaction webhook.",
    )
    @app_commands.guild_only()
    @app_commands.describe(player="Minecraft player name", limit="Number of recent events (1-20)")
    async def transactionhistory(
        self,
        interaction,
        player: str = "zlorbie788",
        limit: app_commands.Range[int, 1, 20] = 10,
    ):
        if not core.is_support(interaction.user):
            await core.send_ephemeral_embed(
                interaction, "Support access required", "Only middleman support can view transaction history.", "error"
            )
            return
        player = player.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
            await core.send_ephemeral_embed(
                interaction, "Invalid Minecraft IGN", "Enter 1-16 letters, numbers, or underscores.", "warning"
            )
            return
        with core.db_session() as connection:
            rows = connection.execute(
                     """SELECT event_id, amount, received_at, from_player, to_player FROM transaction_events
                     WHERE player = ? COLLATE NOCASE
                         OR from_player = ? COLLATE NOCASE
                         OR to_player = ? COLLATE NOCASE
                     ORDER BY id DESC LIMIT ?""",
                     (player, player, player, limit),
            ).fetchall()
        if not rows:
            description = (
                f"No webhook events are stored for **{player}**. This is not a live DonutSMP account query."
            )
        else:
            description = "\n".join(
                f"[{row['event_id'] or 'legacy'}] {row['received_at'][:19].replace('T', ' ')} UTC: "
                f"{row['from_player'] or '?'} -> {row['to_player'] or row['player']} ({row['amount']})"
                for row in rows
            )
        embed = core.make_embed(f"Transaction activity • {player}", description[:4000])
        embed.add_field(
            name="📚 Source",
            value="Events received by this bot's authenticated webhook",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(TransactionHistory())