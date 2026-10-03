import discord
from discord import app_commands
from discord.ext import commands

import main as core


class Middleman(commands.GroupCog, group_name="middleman", group_description="Manage private middleman deals"):
    @app_commands.command(name="panel", description="Post the middleman deal panel in the configured channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction: discord.Interaction):
        channel = await core.resolve_channel(core.PANEL_CHANNEL_ID)
        embed = core.make_embed(
            "Middleman",
            "1. Press **Start a deal** and fill in one form.\n"
            "2. One of you puts the money into the bot. The other does their side of the trade.\n"
            "3. Once they get it, whoever paid presses **Release** and the bot pays the other.\n"
            "**0% fee.** Staff handle disputes.",
            "success",
        )
        if core.MIDDLEMAN_BANNER_URL:
            embed.set_image(url=core.MIDDLEMAN_BANNER_URL)
        await channel.send(embed=embed, view=core.PanelView())
        await core.send_ephemeral_embed(
            interaction, "Panel posted", f"The middleman panel is live in {channel.mention}.", "success"
        )

    @app_commands.command(name="status", description="Show the status of the middleman deal in this channel.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        ticket = core.get_ticket(channel_id=interaction.channel_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Ticket not found", "Use this command inside a middleman deal.", "warning")
            return
        if not core.is_party(ticket, interaction.user.id) and not core.is_support(interaction.user):
            await core.send_ephemeral_embed(interaction, "Access denied", "You cannot view this deal's status.", "error")
            return
        await interaction.response.send_message(embed=core.ticket_embed(ticket), ephemeral=True)

    @app_commands.command(name="active", description="Show open middleman deals awaiting staff attention.")
    @app_commands.guild_only()
    async def active(self, interaction: discord.Interaction):
        if not core.is_support(interaction.user):
            await core.send_ephemeral_embed(interaction, "Support access required", "Only middleman support can view the deal queue.", "error")
            return
        with core.db_session() as connection:
            tickets = connection.execute(
                """SELECT ticket_number, channel_id, seller_id, buyer_id, amount, activity_at
                   FROM tickets WHERE guild_id = ? AND status = 'open' ORDER BY activity_at ASC LIMIT 15""",
                (interaction.guild_id,),
            ).fetchall()
        description = "There are no open middleman deals." if not tickets else "\n\n".join(
            f"**Deal #{ticket['ticket_number']}** • <#{ticket['channel_id']}> • "
            f"{core.format_coin_amount(ticket['amount']) if ticket['amount'] else 'price pending'} coins\n"
            f"<@{ticket['seller_id']}> → <@{ticket['buyer_id']}> • last activity `{ticket['activity_at'][:16].replace('T', ' ')}` UTC"
            for ticket in tickets
        )
        embed = core.make_embed("Active middleman queue", description, "info")
        embed.set_footer(text="Oldest activity appears first • Staff controls live in each deal")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="verify", description="Record that support verified payment in-game.")
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction):
        ticket = core.get_ticket(channel_id=interaction.channel_id)
        if not core.is_support(interaction.user):
            await core.send_ephemeral_embed(interaction, "Support access required", "Only configured support staff can verify payment.", "error")
            return
        if not ticket or ticket["status"] != "open" or not ticket["payment_reported"]:
            await core.send_ephemeral_embed(interaction, "No payment report", "An open deal with a buyer payment report is required.", "warning")
            return
        core.update_ticket(ticket["ticket_number"], payment_verified=1)
        await core.log_ticket_event(interaction.channel, ticket["ticket_number"], "Payment manually verified", f"Support member <@{interaction.user.id}> recorded an in-game payment check.", "success")
        await core.send_audit_event(interaction.guild, "middleman_payment_verified", interaction.user, interaction.channel, f"Verified payment for deal #{ticket['ticket_number']}.")
        await core.send_ephemeral_embed(interaction, "Payment verification recorded", "This records the staff member's in-game check. The bot does not inspect DonutSMP or move coins.", "success")
        await core.refresh_ticket_message(ticket["ticket_number"])

    @app_commands.command(name="release", description="Record a manual in-game coin release after delivery confirmation.")
    @app_commands.guild_only()
    async def release(self, interaction: discord.Interaction):
        ticket = core.get_ticket(channel_id=interaction.channel_id)
        if not core.is_support(interaction.user):
            await core.send_ephemeral_embed(interaction, "Support access required", "Only configured support staff can record a release.", "error")
            return
        if not ticket or ticket["status"] != "open" or not ticket["delivery_confirmed"]:
            await core.send_ephemeral_embed(interaction, "Delivery confirmation required", "The buyer must confirm receipt before support records a release.", "warning")
            return
        core.update_ticket(ticket["ticket_number"], funds_released=1, status="completed")
        await core.log_ticket_event(interaction.channel, ticket["ticket_number"], "Manual release recorded", f"Support member <@{interaction.user.id}> recorded the manual in-game transfer.", "success")
        await core.send_audit_event(interaction.guild, "middleman_release_recorded", interaction.user, interaction.channel, f"Recorded release for deal #{ticket['ticket_number']}.")
        await core.send_ephemeral_embed(interaction, "Manual release recorded", "This records a transfer staff already completed in-game. The bot did not move any coins.", "success")
        await core.refresh_ticket_message(ticket["ticket_number"])

    @app_commands.command(name="close", description="Close this middleman deal after support review.")
    @app_commands.guild_only()
    async def close(self, interaction: discord.Interaction):
        ticket = core.get_ticket(channel_id=interaction.channel_id)
        if not ticket or not core.is_support(interaction.user):
            await core.send_ephemeral_embed(interaction, "Support access required", "Only support staff can close middleman deals.", "error")
            return
        if ticket["status"] != "open":
            await core.send_ephemeral_embed(interaction, "Already closed", "This deal is no longer open.", "warning")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await core.log_ticket_event(interaction.channel, ticket["ticket_number"], "Closed by support", f"Support member <@{interaction.user.id}> closed this deal after review.", "warning")
        await core.close_ticket_channel(ticket, interaction.channel)
        await core.send_audit_event(interaction.guild, "middleman_closed_by_staff", interaction.user, interaction.channel, f"Closed deal #{ticket['ticket_number']}.")
        await core.send_ephemeral_embed(interaction, "Deal closed by support", f"The channel was locked and renamed `closed-deal-{ticket['ticket_number']}`.", "success")
        await core.refresh_ticket_message(ticket["ticket_number"])







async def setup(bot):
    await bot.add_cog(Middleman())