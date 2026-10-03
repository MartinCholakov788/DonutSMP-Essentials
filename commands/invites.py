import discord
from discord import app_commands
from discord.ext import commands

import main as core


class Invites(commands.Cog):
    @app_commands.command(name="invitecount", description="Show how many members a player invited.")
    @app_commands.guild_only()
    @app_commands.describe(player="Player to check; defaults to yourself")
    async def invitecount(self, interaction: discord.Interaction, player: discord.Member = None):
        player = player or interaction.user
        with core.db_session() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM invite_uses WHERE guild_id = ? AND inviter_id = ?",
                (interaction.guild_id, player.id),
            ).fetchone()[0]
        embed = core.make_embed(
            f"Invite count • {player.display_name}",
            f"{player.mention} has invited **{count:,}** member{'s' if count != 1 else ''} that the bot could track.",
            "info",
        )
        embed.set_thumbnail(url=player.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Invites())
