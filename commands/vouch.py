from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import main as core


class VouchFormView(discord.ui.View):
    def __init__(self, player, author_id, evidence=None):
        super().__init__(timeout=300)
        self.player = player
        self.player_id = player.id
        self.author_id = author_id
        self.evidence = evidence
        self.verdict = None
        self.stars = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await core.send_ephemeral_embed(
                interaction, "This form is private", "Only the member who started this vouch can use it.", "error"
            )
            return False
        return True

    def form_embed(self):
        verdict = self.verdict.title() if self.verdict else "Choose Legit or Scammer"
        stars = f"{'⭐' * self.stars} ({self.stars}/5)" if self.stars else "Choose a 1-5 star rating"
        return core.make_embed(
            f"Vouch for {self.player.display_name}",
            "Select a rating category and star score, then add a short reason. Your completed vouch will be posted publicly.",
        ).add_field(name="🎮 Player", value=self.player.mention).add_field(
            name="🛡️ Verdict", value=verdict
        ).add_field(name="⭐ Rating", value=stars)

    async def refresh(self, interaction):
        await interaction.response.edit_message(embed=self.form_embed(), view=self)

    @discord.ui.button(label="✍️ Add reason and submit", style=discord.ButtonStyle.success, row=2)
    async def submit(self, interaction, button):
        if not self.verdict or not self.stars:
            await core.send_ephemeral_embed(
                interaction,
                "Finish your rating first",
                "Choose both a verdict and a star rating before adding your reason.",
                "warning",
            )
            return
        await interaction.response.send_modal(
            VouchReasonModal(self.player, self.author_id, self.verdict, self.stars, self.evidence)
        )


class VerdictSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="🛡️ Choose Legit or Scammer",
            min_values=1,
            max_values=1,
            row=0,
            options=[
                discord.SelectOption(label="Legit", value="legit", emoji="✅"),
                discord.SelectOption(label="Scammer", value="scammer", emoji="🚨"),
            ],
        )

    async def callback(self, interaction):
        self.view.verdict = self.values[0]
        await self.view.refresh(interaction)


class StarSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="⭐ Choose a rating from 1 to 5",
            min_values=1,
            max_values=1,
            row=1,
            options=[
                discord.SelectOption(label=f"{stars} star{'s' if stars != 1 else ''}", value=str(stars), emoji="⭐")
                for stars in range(1, 6)
            ],
        )

    async def callback(self, interaction):
        self.view.stars = int(self.values[0])
        await self.view.refresh(interaction)


class VouchForm(VouchFormView):
    def __init__(self, player, author_id, evidence=None):
        super().__init__(player, author_id, evidence)
        self.add_item(VerdictSelect())
        self.add_item(StarSelect())


class VouchReasonModal(discord.ui.Modal, title="✍️ Add a vouch reason"):
    reason = discord.ui.TextInput(
        label="Why are you giving this rating?",
        placeholder="Share a concise, factual reason.",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=500,
    )

    def __init__(self, player, author_id, verdict=None, stars=None, evidence=None):
        super().__init__()
        self.player = player
        self.player_id = player.id
        self.author_id = author_id
        self.verdict = verdict
        self.stars = stars
        self.evidence = evidence
        if self.verdict is None or self.stars is None:
            self.verdict_select = discord.ui.Select(
                custom_id="vouch:verdict",
                placeholder="Choose Legit or Scammer",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(label="Legit", value="legit", emoji="✅"),
                    discord.SelectOption(label="Scammer", value="scammer", emoji="🚨"),
                ],
            )
            self.stars_select = discord.ui.Select(
                custom_id="vouch:stars",
                placeholder="Choose a rating from 1 to 5",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=f"{rating} star{'s' if rating != 1 else ''}",
                        value=str(rating),
                        emoji="⭐",
                    )
                    for rating in range(1, 6)
                ],
            )
            self.add_item(
                discord.ui.Label(
                    text="What is your vouch?",
                    component=self.verdict_select,
                )
            )
            self.add_item(
                discord.ui.Label(
                    text="What rating would you give?",
                    component=self.stars_select,
                )
            )

    async def on_submit(self, interaction):
        if interaction.user.id != self.author_id:
            await core.send_ephemeral_embed(
                interaction, "This form is private", "Only the member who started this vouch can submit it.", "error"
            )
            return
        if interaction.guild is None:
            await core.send_ephemeral_embed(
                interaction, "Server required", "Vouches can only be submitted inside a server.", "error"
            )
            return
        if self.verdict is None or self.stars is None:
            self.verdict = self.verdict_select.values[0]
            self.stars = int(self.stars_select.values[0])
        reason = str(self.reason.value).strip()
        if len(reason) < 5:
            await core.send_ephemeral_embed(
                interaction, "Reason too short", "Please provide at least five characters of detail.", "warning"
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        with core.db_session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO player_vouches
                (guild_id, player, target_id, author_id, verdict, stars, reason, evidence_url, evidence_filename, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction.guild_id,
                    str(self.player_id),
                    self.player_id,
                    interaction.user.id,
                    self.verdict,
                    self.stars,
                    reason,
                    self.evidence.url if self.evidence else None,
                    self.evidence.filename if self.evidence else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            vouch_id = cursor.lastrowid if cursor.rowcount else None
        if not vouch_id:
            await core.send_ephemeral_embed(
                interaction,
                "Vouch already submitted",
                f"You have already vouched for **{self.player.display_name}**. Each Discord member can submit one vouch per player.",
                "warning",
            )
            return

        channel = None
        try:
            channel = await core.resolve_channel(core.VOUCH_CHANNEL_ID)
            tone = "success" if self.verdict == "legit" else "error"
            embed = core.make_embed(
                f"Community vouch • {self.player.display_name}",
                "Community-submitted feedback. This report is not independently verified.",
                tone,
            )
            embed.add_field(name="🎮 Player", value=f"{self.player.mention} ({self.player.display_name})", inline=True)
            embed.add_field(name="🛡️ Verdict", value="✅ Legit" if self.verdict == "legit" else "🚨 Scammer", inline=True)
            embed.add_field(name="⭐ Rating", value=f"{'⭐' * self.stars} ({self.stars}/5)", inline=True)
            embed.add_field(name="📝 Reason", value=reason, inline=False)
            embed.add_field(name="👤 Vouched by", value=interaction.user.display_name, inline=False)
            if self.evidence:
                embed.add_field(name="📎 Evidence", value=f"[Open {self.evidence.filename}]({self.evidence.url})", inline=False)
                if self.evidence.content_type and self.evidence.content_type.startswith("image/"):
                    embed.set_image(url=self.evidence.url)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            with core.db_session() as connection:
                connection.execute("DELETE FROM player_vouches WHERE id = ?", (vouch_id,))
            core.app.logger.exception("Could not publish vouch")
            await core.send_ephemeral_embed(
                interaction,
                "Vouch could not be posted",
                "Check that the configured vouch channel exists and the bot can send embeds there, then retry.",
                "error",
            )
            return
        vouch_count = core.member_reputation_summary(interaction.guild_id, self.player_id)["vouches"]
        milestone_roles = []
        if vouch_count > 50:
            milestone_roles.append(core.VOUCH_OVER_50_ROLE_ID)
        if vouch_count > 75:
            milestone_roles.append(core.VOUCH_OVER_75_ROLE_ID)
        if milestone_roles:
            roles = [interaction.guild.get_role(role_id) for role_id in milestone_roles]
            roles = [role for role in roles if role is not None]
            if len(roles) != len(milestone_roles):
                core.app.logger.error(
                    "One or more configured vouch milestone roles are missing at count %s",
                    vouch_count,
                )
            try:
                if roles:
                    await self.player.add_roles(
                        *roles,
                        reason=f"Reached {vouch_count} community vouches",
                    )
            except discord.Forbidden:
                core.app.logger.warning(
                    "Could not assign vouch milestone role(s) to member %s at count %s",
                    self.player_id,
                    vouch_count,
                )
            except discord.HTTPException:
                core.app.logger.exception(
                    "Discord rejected vouch milestone role assignment for member %s at count %s",
                    self.player_id,
                    vouch_count,
                )
        await core.send_ephemeral_embed(
            interaction,
            "Vouch posted",
            f"Your rating for **{self.player.display_name}** was posted in {channel.mention}.",
            "success",
        )


class Vouch(commands.Cog):
    @app_commands.command(name="vouch", description="Submit a community rating for a Minecraft player.")
    @app_commands.checks.cooldown(1, 180, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    @app_commands.describe(player="Discord server member to rate", evidence="Optional screenshot or file supporting this vouch")
    async def vouch(self, interaction, player: discord.Member, evidence: discord.Attachment = None):
        if player.bot:
            await core.send_ephemeral_embed(
                interaction, "Choose a human member", "Bot accounts cannot receive community vouches.", "warning"
            )
            return
        if player.id == interaction.user.id:
            await core.send_ephemeral_embed(
                interaction, "Self-vouch unavailable", "Choose another server member to rate.", "warning"
            )
            return
        embed = core.make_embed(
            f"Vouch for {player.display_name}",
            "Choose **Legit** or **Scammer**, select one to five stars, and add a concise reason. Each member may vouch once per player."
            + (f" Evidence attached: **{evidence.filename}**." if evidence else " You can attach optional evidence with this command."),
        )
        embed.add_field(name="🎮 Selected member", value=f"{player.mention} ({player.display_name})")
        embed.add_field(name="📢 Published in", value=f"<#{core.VOUCH_CHANNEL_ID}>")
        await interaction.response.send_modal(
            VouchReasonModal(player, interaction.user.id, evidence=evidence)
        )


async def setup(bot):
    await bot.add_cog(Vouch())