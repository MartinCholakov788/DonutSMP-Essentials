import discord
from discord import app_commands
from discord.ext import commands

import main as core


class Help(commands.Cog):
    @app_commands.command(name="help", description="Show the bot command guide and workflow overview.")
    async def help(self, interaction):
        embed = core.make_embed(
            "Bot guide",
            "A quick guide to DonutSMP Essentials Assistant. Use slash commands from Discord's command picker.",
        )
        embed.add_field(
            name="🛰️ General",
            value=(
                "`/help` Show this guide.\n"
                "`/embed create` Admin-only; build, preview, add fields to, and publish custom embeds.\n"
                "`/giveaway create` Admin-only; publish a timed giveaway with a prize, host, winners, conditions, and optional required role.\n"
                "`/ticket create` Admin-only; publish the private support panel.\n"
                "`/profile <member>` Show reputation, completed deals, vouches, warnings, and join date.\n"
                "`/invitecount [member]` Show tracked invite totals for yourself or another member.\n"
                "`/health` Show service and worker health.\n"
                "`/apply` Admin-only; post the guided staff application panel.\n"
                "`/verify` Admin-only; post the member verification panel. New members complete a Minecraft username and CAPTCHA check before access is restored.\n"
                "`/appeal <reason>` Open a private appeal ticket. Administrators can use `/warn`, `/kick`, `/ban`, and `/timeout`.\n"
                "`/settings view` and `/settings set` let staff configure protection switches and ticket timing.\n"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎉 Giveaways",
            value=(
                "Only administrators can manage giveaways. Use `/giveaway create` with a duration such as `30m`, `2h`, or `1d 6h`; choose the prize, number of winners, optional conditions, required Discord role, and destination channel.\n"
                "Members enter with the button, and each member can enter once. Required roles are enforced automatically. The bot updates the entry count, ends giveaways automatically, announces randomly selected winners, and restores active buttons after restarts.\n"
                "Admins can use `/giveaway active`, `/giveaway end`, `/giveaway reroll`, and `/giveaway cancel`; members can use `/giveaway info` to inspect a giveaway and their entry status."
            ),
            inline=False,
        )
        embed.add_field(
            name="🤝 Middleman tickets",
            value=(
                "`/middleman panel` Admin-only; post the create-only ticket panel. `/middleman status`, `active`, `verify`, `release`, and `close` manage the same workflow from one organized command group.\n"
                "Ticket setup collects the creator's IGN, agreed price (`7k`, `7m`, `7b` supported), and seller/buyer role before creating the channel. The other trader then confirms their IGN and the starting price.\n"
                "Both participants must accept any proposed price change, and both must click **Close Deal** to lock and rename the channel.\n"
                "The deal embed shows live status. Configured support staff use **Verify payment**, **Record release**, and **Staff close** buttons inside the deal; the staff dashboard shows the open queue.\n"
                "Closing archives the full HTML transcript to the configured transcript channel.\n"
                "Inactive tickets receive participant reminders and are flagged for support review using configurable hour thresholds. Use the ticket buttons for IGN, price, payment report, delivery, and issue escalation."
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ Support tickets and staff tools",
            value=(
                "`/ticket create` posts the support dropdown for General Problems, Report a Player, Giveaways, and Partnership Requests. The user must explain the issue before a private channel is created.\n"
                "Support staff can use `/ticket claim`, `/ticket close`, `/ticket reopen`, `/ticket active`, `/ticket priority`, `/ticket add-member`, and `/ticket remove-member`; deletion always requires confirmation and a 10-second cancellable countdown.\n"
                "`/profile <member>` is public. Administrators can use `/warn`, `/kick`, `/ban`, and `/timeout`; `/staff warnings` and `/dashboard refresh` are available to configured staff. Audit events are stored and published to the configured audit channel."
            ),
            inline=False,
        )
        embed.add_field(
            name="⭐ Community vouches",
            value=(
                "`/vouch <member>` Select a server member, choose Legit or Scammer and 1-5 stars, then submit a reason. One vouch per member/target.\n"
                "`/vouchcount <member>` Show total, Legit, Scammer, and average rating for a selected member. Vouches are user-submitted, not verified."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔔 Transactions and safety",
            value=(
                "`/transactionhistory [player]` Support-only; query both sides of events received by this bot's authenticated webhook, not live DonutSMP history.\n"
                "`POST /transaction` Requires a stable unique `event_id`, accepts `from_player`, `to_player`, and `amount`, and queues an alert with automatic retries. Same-ID retries do not duplicate records.\n"
                "The bot cannot access DonutSMP balances, hold coins, or transfer funds. Staff must verify and move coins manually in-game."
            ),
            inline=False,
        )
        embed.set_footer(text="Ticket visibility is limited by Discord permissions; server administrators can bypass channel overwrites.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help())