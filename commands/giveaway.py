import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import main as core


DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}
MAX_DURATION_SECONDS = 30 * 24 * 60 * 60
ENTRY_COOLDOWN_SECONDS = 180
entry_cooldowns = {}


def parse_duration(value):
    value = value.strip().lower()
    if not value or not re.fullmatch(r"(?:\d+\s*[smhdw]\s*)+", value):
        raise ValueError("Use a duration such as `30m`, `2h`, or `1d 6h`.")
    parts = re.findall(r"(\d+)\s*([smhdw])", value)
    if len({unit for _, unit in parts}) != len(parts):
        raise ValueError("Use each duration unit only once, for example `1d 6h`.")
    seconds = sum(int(number) * DURATION_UNITS[unit] for number, unit in parts)
    if seconds < 10:
        raise ValueError("A giveaway must run for at least 10 seconds.")
    if seconds > MAX_DURATION_SECONDS:
        raise ValueError("A giveaway cannot run longer than 30 days.")
    return seconds


def parse_iso(value):
    return datetime.fromisoformat(value)


def discord_timestamp(value, style="R"):
    return f"<t:{int(parse_iso(value).timestamp())}:{style}>"


def get_giveaway(giveaway_id):
    with core.db_session() as connection:
        row = connection.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ?", (giveaway_id,)
        ).fetchone()
    return dict(row) if row else None


def get_entry_count(giveaway_id):
    with core.db_session() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway_id,),
        ).fetchone()
    return row["count"]


def get_entries(giveaway_id):
    with core.db_session() as connection:
        rows = connection.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway_id,),
        ).fetchall()
    return [row["user_id"] for row in rows]


def winner_ids(giveaway):
    try:
        return json.loads(giveaway.get("winner_ids") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []


def giveaway_embed(giveaway, entry_count=None):
    status = giveaway["status"]
    tone = "info" if status == "active" else "success" if status == "ended" else "error"
    if status == "active":
        description = "Press **Enter Giveaway** below for a chance to win. One entry per member."
    elif status == "cancelled":
        description = "This giveaway was cancelled by an administrator. No winner was selected."
    else:
        description = "This giveaway has ended. Thank you to everyone who participated."
    embed = core.make_embed(f"Giveaway #{giveaway['giveaway_id']}", description, tone)
    embed.add_field(name="🎁 Prize", value=giveaway["prize"], inline=False)
    embed.add_field(name="👤 Hosted by", value=f"<@{giveaway['host_id']}>")
    embed.add_field(name="🏆 Winners", value=str(giveaway["winner_count"]))
    if status == "active":
        embed.add_field(
            name="⏳ Ends",
            value=f"{discord_timestamp(giveaway['ends_at'])}\n{discord_timestamp(giveaway['ends_at'], 'f')}",
        )
    elif giveaway.get("ended_at"):
        embed.add_field(name="🕒 Ended", value=discord_timestamp(giveaway["ended_at"], "f"))
    entry_count = entry_count if entry_count is not None else get_entry_count(giveaway["giveaway_id"])
    embed.add_field(name="👥 Entries", value=f"{entry_count:,}")
    conditions = giveaway.get("conditions") or "No additional conditions. Good luck to everyone!"
    embed.add_field(name="📋 Conditions", value=conditions, inline=False)
    required_role_id = giveaway.get("required_role_id")
    embed.add_field(
        name="🔐 Entry requirement",
        value=f"Members must have <@&{required_role_id}>" if required_role_id else "Open to everyone",
        inline=False,
    )
    if status == "ended":
        winners = winner_ids(giveaway)
        embed.add_field(
            name="🎉 Winner" if len(winners) == 1 else "🎉 Winners",
            value=" ".join(f"<@{user_id}>" for user_id in winners) if winners else "No eligible entries.",
            inline=False,
        )
    embed.set_footer(text="Giveaway system • Only one entry per member")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id, disabled=False):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.enter_button = self.enter_giveaway
        self.enter_button.custom_id = f"giveaway:enter:{giveaway_id}"
        self.enter_button.disabled = disabled

    @discord.ui.button(
        label="🎉 Enter Giveaway",
        style=discord.ButtonStyle.success,
        custom_id="giveaway:enter",
    )
    async def enter_giveaway(self, interaction, button):
        if interaction.user.bot:
            await core.send_ephemeral_embed(
                interaction, "Entry unavailable", "Bot accounts cannot enter giveaways.", "warning"
            )
            return
        now_monotonic = time.monotonic()
        last_entry = entry_cooldowns.get((self.giveaway_id, interaction.user.id))
        if last_entry is not None and now_monotonic - last_entry < ENTRY_COOLDOWN_SECONDS:
            remaining = int(ENTRY_COOLDOWN_SECONDS - (now_monotonic - last_entry)) + 1
            await core.send_ephemeral_embed(interaction, "Entry cooldown", f"Please wait **{remaining} seconds** before trying this giveaway again.", "warning")
            return
        entry_cooldowns[(self.giveaway_id, interaction.user.id)] = now_monotonic
        giveaway = get_giveaway(self.giveaway_id)
        if not giveaway or giveaway["status"] != "active":
            await core.send_ephemeral_embed(
                interaction, "Giveaway ended", "This giveaway is no longer accepting entries.", "warning"
            )
            return
        now = datetime.now(timezone.utc)
        if parse_iso(giveaway["ends_at"]) <= now:
            await core.send_ephemeral_embed(
                interaction, "Giveaway ending", "This giveaway is being finalized. Please try another giveaway.", "warning"
            )
            return
        required_role_id = giveaway.get("required_role_id")
        if required_role_id and not any(role.id == required_role_id for role in interaction.user.roles):
            await core.send_ephemeral_embed(
                interaction,
                "Role required",
                f"You need <@&{required_role_id}> to enter this giveaway.",
                "warning",
            )
            return
        with core.db_session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id, entered_at)
                   SELECT giveaway_id, ?, ? FROM giveaways
                   WHERE giveaway_id = ? AND status = 'active' AND ends_at > ?""",
                (interaction.user.id, now.isoformat(), self.giveaway_id, now.isoformat()),
            )
        if not cursor.rowcount:
            await core.send_ephemeral_embed(
                interaction, "Already entered", "You already have one entry in this giveaway.", "info"
            )
            return
        await core.send_ephemeral_embed(
            interaction,
            "Entry confirmed",
            f"You are entered in **Giveaway #{self.giveaway_id}**. Good luck!",
            "success",
        )
        try:
            await interaction.message.edit(
                embed=giveaway_embed(giveaway, get_entry_count(self.giveaway_id)),
                view=GiveawayView(self.giveaway_id),
            )
        except discord.HTTPException:
            core.app.logger.exception("Could not refresh giveaway %s", self.giveaway_id)


def disabled_giveaway_view(giveaway_id):
    return GiveawayView(giveaway_id, disabled=True)


async def publish_result(giveaway, winners, action="ended"):
    try:
        channel = await core.resolve_channel(giveaway["channel_id"])
        message = await channel.fetch_message(giveaway["message_id"])
        await message.edit(embed=giveaway_embed(giveaway), view=disabled_giveaway_view(giveaway["giveaway_id"]))
        if winners:
            mentions = " ".join(f"<@{user_id}>" for user_id in winners)
            text = (
                f"🎉 Congratulations {mentions}! You won **{giveaway['prize']}**. "
                f"Open the support panel in <#{core.SUPPORT_TICKET_PANEL_CHANNEL_ID}> and choose **Giveaways** to claim your prize safely."
            )
        else:
            text = f"The giveaway for **{giveaway['prize']}** ended without any eligible entries."
        if action == "rerolled":
            text = f"🔄 New winner selected! {text}"
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id) for user_id in winners]))
        with core.db_session() as connection:
            connection.execute(
                "UPDATE giveaways SET outcome_status = 'published', outcome_published_at = ?, outcome_error = NULL WHERE giveaway_id = ?",
                (datetime.now(timezone.utc).isoformat(), giveaway["giveaway_id"]),
            )
    except (discord.HTTPException, discord.NotFound):
        with core.db_session() as connection:
            connection.execute(
                "UPDATE giveaways SET outcome_status = 'failed', outcome_attempts = COALESCE(outcome_attempts, 0) + 1, outcome_error = ?, recovery_at = ? WHERE giveaway_id = ?",
                ("Discord result publication failed", datetime.now(timezone.utc).isoformat(), giveaway["giveaway_id"]),
            )
        core.app.logger.exception("Could not publish result for giveaway %s", giveaway["giveaway_id"])


def finalize_giveaway(giveaway_id, actor_id, guild_id=None, reroll=False):
    with core.db_session() as connection:
        if guild_id is None:
            giveaway = connection.execute(
                "SELECT * FROM giveaways WHERE giveaway_id = ?", (giveaway_id,)
            ).fetchone()
        else:
            giveaway = connection.execute(
                "SELECT * FROM giveaways WHERE giveaway_id = ? AND guild_id = ?",
                (giveaway_id, guild_id),
            ).fetchone()
        if not giveaway:
            return None, None, "missing"
        if reroll and giveaway["status"] != "ended":
            return dict(giveaway), [], "not_ended"
        if not reroll and giveaway["status"] != "active":
            return dict(giveaway), winner_ids(dict(giveaway)), "already_finished"
        entries = [row[0] for row in connection.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,)
        ).fetchall()]
        excluded = set(winner_ids(dict(giveaway))) if reroll else set()
        eligible = [user_id for user_id in entries if user_id not in excluded]
        winners = secrets.SystemRandom().sample(eligible, min(giveaway["winner_count"], len(eligible)))
        ended_at = datetime.now(timezone.utc).isoformat()
        if reroll and not winners:
            return dict(giveaway), [], "no_new_entries"
        connection.execute(
            """UPDATE giveaways SET status = 'ended', winner_ids = ?, ended_at = ?, ended_by = ?,
               outcome_status = 'pending', outcome_attempts = 0, outcome_error = NULL, recovery_at = NULL
               WHERE giveaway_id = ?""",
            (json.dumps(winners), ended_at, actor_id, giveaway_id),
        )
        updated = connection.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ?", (giveaway_id,)
        ).fetchone()
    return dict(updated), winners, "ok"


async def process_expired_giveaways():
    now = datetime.now(timezone.utc).isoformat()
    with core.db_session() as connection:
        rows = connection.execute(
            "SELECT giveaway_id FROM giveaways WHERE status = 'active' AND ends_at <= ?",
            (now,),
        ).fetchall()
    for row in rows:
        giveaway, winners, result = finalize_giveaway(row["giveaway_id"], 0)
        if result == "ok":
            await publish_result(giveaway, winners)
    with core.db_session() as connection:
        recoveries = connection.execute(
            "SELECT * FROM giveaways WHERE status = 'ended' AND outcome_status IN ('pending', 'failed') AND (recovery_at IS NULL OR recovery_at <= ?)",
            (now,),
        ).fetchall()
    for giveaway in recoveries:
        await publish_result(dict(giveaway), winner_ids(dict(giveaway)))


class Giveaway(commands.GroupCog, group_name="giveaway", group_description="Create and manage professional giveaways"):
    @app_commands.command(name="create", description="Create an admin-hosted giveaway with a timed entry button.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        prize="What the winner(s) will receive",
        duration="How long it runs, such as 30m, 2h, or 1d 6h",
        winners="Number of winners (1-20)",
        conditions="Optional eligibility rules or extra instructions",
        required_role="Optional Discord role required to enter",
        channel="Where to publish it; defaults to this channel",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        conditions: str = None,
        required_role: discord.Role = None,
        channel: discord.TextChannel = None,
    ):
        prize = prize.strip()
        conditions = conditions.strip() if conditions else None
        if not prize or len(prize) > 256:
            await core.send_ephemeral_embed(interaction, "Invalid prize", "Prize text must be 1-256 characters.", "warning")
            return
        if conditions and len(conditions) > 1000:
            await core.send_ephemeral_embed(interaction, "Conditions too long", "Conditions must be 1,000 characters or fewer.", "warning")
            return
        try:
            duration_seconds = parse_duration(duration)
        except ValueError as error:
            await core.send_ephemeral_embed(interaction, "Invalid duration", str(error), "warning")
            return
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await core.send_ephemeral_embed(interaction, "Invalid channel", "Choose a standard text channel for the giveaway.", "warning")
            return
        now = datetime.now(timezone.utc)
        ends_at = (now + timedelta(seconds=duration_seconds)).isoformat()
        with core.db_session() as connection:
            cursor = connection.execute(
                """INSERT INTO giveaways
                   (guild_id, channel_id, host_id, prize, conditions, required_role_id, winner_count, ends_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction.guild_id,
                    channel.id,
                    interaction.user.id,
                    prize,
                    conditions,
                    required_role.id if required_role else None,
                    winners,
                    ends_at,
                    now.isoformat(),
                ),
            )
            giveaway_id = cursor.lastrowid
        giveaway = get_giveaway(giveaway_id)
        try:
            message = await channel.send(
                embed=giveaway_embed(giveaway, 0),
                view=GiveawayView(giveaway_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            with core.db_session() as connection:
                connection.execute("UPDATE giveaways SET message_id = ? WHERE giveaway_id = ?", (message.id, giveaway_id))
        except Exception:
            with core.db_session() as connection:
                connection.execute("DELETE FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
            raise
        await core.send_audit_event(
            interaction.guild,
            "giveaway_created",
            interaction.user,
            message,
            f"Created giveaway #{giveaway_id} for {prize}; duration `{duration}`; {winners} winner(s).",
        )
        await core.send_ephemeral_embed(
            interaction,
            "Giveaway published",
            f"Giveaway **#{giveaway_id}** is live in {channel.mention} and ends {discord_timestamp(ends_at)}.",
            "success",
        )

    @app_commands.command(name="info", description="Show details and entry status for a giveaway in this server.")
    @app_commands.guild_only()
    @app_commands.describe(giveaway_id="The giveaway number shown in its embed")
    async def info(self, interaction: discord.Interaction, giveaway_id: int):
        giveaway = get_giveaway(giveaway_id)
        if not giveaway or giveaway["guild_id"] != interaction.guild_id:
            await core.send_ephemeral_embed(
                interaction, "Giveaway not found", "That giveaway does not exist in this server.", "warning"
            )
            return
        with core.db_session() as connection:
            entry = connection.execute(
                "SELECT 1 FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, interaction.user.id),
            ).fetchone()
        embed = giveaway_embed(giveaway)
        if giveaway["status"] == "active":
            embed.add_field(name="Your entry", value="✅ Entered" if entry else "⏳ Not entered yet", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="end", description="End an active giveaway now and select winner(s).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def end(self, interaction: discord.Interaction, giveaway_id: int):
        giveaway, winners, result = finalize_giveaway(giveaway_id, interaction.user.id, interaction.guild_id)
        if result == "missing":
            await core.send_ephemeral_embed(interaction, "Giveaway not found", "Check the giveaway ID and try again.", "warning")
            return
        if result != "ok":
            await core.send_ephemeral_embed(interaction, "Already ended", "That giveaway has already been finalized.", "warning")
            return
        await publish_result(giveaway, winners)
        await core.send_audit_event(
            interaction.guild,
            "giveaway_ended",
            interaction.user,
            giveaway_id,
            f"Ended giveaway #{giveaway_id}; selected {len(winners)} winner(s).",
        )
        await core.send_ephemeral_embed(interaction, "Giveaway ended", f"Giveaway **#{giveaway_id}** has been finalized.", "success")

    @app_commands.command(name="reroll", description="Choose new winner(s) for a completed giveaway.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def reroll(self, interaction: discord.Interaction, giveaway_id: int):
        giveaway, winners, result = finalize_giveaway(
            giveaway_id, interaction.user.id, interaction.guild_id, reroll=True
        )
        if result == "missing":
            await core.send_ephemeral_embed(interaction, "Giveaway not found", "Check the giveaway ID and try again.", "warning")
            return
        if result == "not_ended":
            await core.send_ephemeral_embed(interaction, "Giveaway is active", "End the giveaway before rerolling it.", "warning")
            return
        if result == "no_new_entries":
            await core.send_ephemeral_embed(interaction, "No eligible entries", "There are no new eligible entrants to select.", "warning")
            return
        await publish_result(giveaway, winners, "rerolled")
        await core.send_audit_event(
            interaction.guild,
            "giveaway_rerolled",
            interaction.user,
            giveaway_id,
            f"Rerolled giveaway #{giveaway_id}; selected {len(winners)} replacement winner(s).",
        )
        await core.send_ephemeral_embed(interaction, "Giveaway rerolled", f"New winner(s) selected for giveaway **#{giveaway_id}**.", "success")

    @app_commands.command(name="cancel", description="Cancel an active giveaway without selecting a winner.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def cancel(self, interaction: discord.Interaction, giveaway_id: int):
        with core.db_session() as connection:
            cursor = connection.execute(
                     """UPDATE giveaways SET status = 'cancelled', ended_at = ?, ended_by = ?
                         WHERE giveaway_id = ? AND guild_id = ? AND status = 'active'""",
                     (datetime.now(timezone.utc).isoformat(), interaction.user.id, giveaway_id, interaction.guild_id),
            )
        if not cursor.rowcount:
            await core.send_ephemeral_embed(interaction, "Giveaway not active", "That giveaway was not found or has already ended.", "warning")
            return
        giveaway = get_giveaway(giveaway_id)
        try:
            channel = await core.resolve_channel(giveaway["channel_id"])
            message = await channel.fetch_message(giveaway["message_id"])
            await message.edit(embed=giveaway_embed(giveaway), view=disabled_giveaway_view(giveaway_id))
        except (discord.HTTPException, discord.NotFound):
            core.app.logger.exception("Could not update cancelled giveaway %s", giveaway_id)
        await core.send_audit_event(
            interaction.guild,
            "giveaway_cancelled",
            interaction.user,
            giveaway_id,
            f"Cancelled giveaway #{giveaway_id} without selecting a winner.",
        )
        await core.send_ephemeral_embed(interaction, "Giveaway cancelled", f"Giveaway **#{giveaway_id}** was cancelled.", "success")

    @app_commands.command(name="active", description="List active giveaways hosted in this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def active(self, interaction: discord.Interaction):
        with core.db_session() as connection:
            giveaways = connection.execute(
                """SELECT giveaway_id, channel_id, prize, ends_at, winner_count
                   FROM giveaways WHERE guild_id = ? AND status = 'active' ORDER BY ends_at ASC LIMIT 20""",
                (interaction.guild_id,),
            ).fetchall()
        if not giveaways:
            description = "There are no active giveaways in this server."
        else:
            description = "\n".join(
                f"**#{giveaway['giveaway_id']}** • <#{giveaway['channel_id']}> • {giveaway['prize']} • "
                f"{giveaway['winner_count']} winner(s) • ends {discord_timestamp(giveaway['ends_at'])}"
                for giveaway in giveaways
            )
        await interaction.response.send_message(
            embed=core.make_embed("Active giveaways", description, "info"),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Giveaway())