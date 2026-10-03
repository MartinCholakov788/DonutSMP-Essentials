import discord
from discord import app_commands
from discord.ext import commands

import main as core


class VouchCount(commands.Cog):
    @app_commands.command(name="vouchcount", description="Show community vouch totals for a Minecraft player.")
    @app_commands.guild_only()
    @app_commands.describe(player="Discord server member")
    async def vouchcount(self, interaction, player: discord.Member):
        with core.db_session() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS total,
                    SUM(CASE WHEN verdict = 'legit' THEN 1 ELSE 0 END) AS legit,
                    SUM(CASE WHEN verdict = 'scammer' THEN 1 ELSE 0 END) AS scammer,
                    AVG(stars) AS average_stars
                FROM player_vouches WHERE guild_id = ? AND target_id = ?""",
                (interaction.guild_id, player.id),
            ).fetchone()
        total = row["total"]
        legit = row["legit"] or 0
        scammer = row["scammer"] or 0
        average = f"{row['average_stars']:.1f}/5" if row["average_stars"] is not None else "No ratings"
        embed = core.make_embed(
            f"Community reputation • {player.display_name}",
            "Counts reflect submitted community opinions, not verified DonutSMP records.",
            "info",
        )
        embed.add_field(name="🎮 Player", value=f"{player.mention} ({player.display_name})", inline=False)
        embed.add_field(name="📊 Total vouches", value=str(total), inline=True)
        embed.add_field(name="✅ Legit", value=str(legit), inline=True)
        embed.add_field(name="🚨 Scammer", value=str(scammer), inline=True)
        embed.add_field(name="⭐ Average rating", value=average, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(VouchCount())