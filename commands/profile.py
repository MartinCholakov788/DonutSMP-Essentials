import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import main as core


MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60


def parse_timeout_duration(value):
    value = value.strip().lower()
    if not re.fullmatch(r"\d+\s*[smhd](?:\s+\d+\s*[smhd])*", value):
        raise ValueError("Use a duration such as `1m`, `2h`, or `7d`.")
    parts = re.findall(r"(\d+)\s*([smhd])", value)
    if len({unit for _, unit in parts}) != len(parts):
        raise ValueError("Use each duration unit only once, for example `1d 6h`.")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = sum(int(amount) * multipliers[unit] for amount, unit in parts)
    if seconds < 1:
        raise ValueError("The timeout must be at least 1 second.")
    if seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError("Discord timeouts cannot be longer than 28 days.")
    return seconds


def has_moderation_access(member):
    return bool(member and member.guild_permissions.administrator)


async def notify_member(member, title, description, tone="warning"):
    try:
        await member.send(embed=core.make_embed(title, description, tone))
    except (discord.Forbidden, discord.HTTPException):
        core.app.logger.info("Could not DM moderation notice to member %s", member.id)


async def validate_moderation_target(interaction, member):
    if not has_moderation_access(interaction.user):
        await core.send_ephemeral_embed(
            interaction,
            "Moderation access required",
            "Only server administrators can use this command.",
            "error",
        )
        return False
    if member.id == interaction.user.id:
        await core.send_ephemeral_embed(interaction, "Invalid target", "You cannot moderate yourself.", "warning")
        return False
    if member.id == interaction.guild.owner_id:
        await core.send_ephemeral_embed(interaction, "Invalid target", "The server owner cannot be moderated by this bot.", "warning")
        return False
    if not interaction.user.guild_permissions.administrator and member.top_role >= interaction.user.top_role:
        await core.send_ephemeral_embed(
            interaction,
            "Role hierarchy prevents this",
            f"Your highest role must be above {member.mention}'s highest role before you can moderate them.",
            "error",
        )
        return False
    return True


class Profile(commands.Cog):
    @app_commands.command(name="profile", description="Show a member's reputation and activity summary.")
    @app_commands.guild_only()
    @app_commands.describe(member="The Discord member to inspect")
    async def profile(self, interaction: discord.Interaction, member: discord.Member):
        summary = core.member_reputation_summary(interaction.guild_id, member.id)
        joined = member.joined_at.astimezone(timezone.utc).strftime("%Y-%m-%d") if member.joined_at else "Unknown"
        embed = core.make_embed(
            f"Member profile • {member.display_name}",
            "Community reputation and bot-recorded activity. Vouches are user-submitted and are not independently verified.",
            "info",
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="👤 Discord member", value=f"{member.mention}\nID: `{member.id}`", inline=False)
        embed.add_field(name="📅 Joined server", value=joined, inline=True)
        embed.add_field(name="🤝 Completed deals", value=f"{summary['completed_deals']:,}", inline=True)
        embed.add_field(name="⚠️ Active staff warnings", value=f"{summary['active_warnings']:,}", inline=True)
        embed.add_field(name="⭐ Vouches", value=f"{summary['vouches']:,} total\n{summary['average']:.1f}/5 average", inline=True)
        embed.add_field(name="✅ Legit", value=f"{summary['legit']:,}", inline=True)
        embed.add_field(name="🚩 Scammer", value=f"{summary['scammer']:,}", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="warn", description="Add a staff warning to a member's profile.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to warn", reason="Reason for the staff warning")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not has_moderation_access(interaction.user):
            await core.send_ephemeral_embed(
                interaction,
                "Moderation access required",
                "Only server administrators can use this command.",
                "error",
            )
            return
        reason = reason.strip()
        if len(reason) < 5 or len(reason) > 1000:
            await core.send_ephemeral_embed(interaction, "Invalid reason", "Warning reasons must be 5-1,000 characters.", "warning")
            return
        with core.db_session() as connection:
            connection.execute(
                "INSERT INTO staff_warnings (guild_id, user_id, staff_id, reason, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (interaction.guild_id, member.id, interaction.user.id, reason),
            )
        await notify_member(
            member,
            "You received a staff warning",
            f"A staff warning was added to your profile in **{interaction.guild.name}**.\n\n**Reason:** {reason}",
        )
        await core.send_audit_event(interaction.guild, "staff_warning_added", interaction.user, member, reason)
        await core.send_ephemeral_embed(interaction, "Warning saved", f"The warning was added to {member.mention}'s profile.", "success")

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await validate_moderation_target(interaction, member):
            return
        reason = reason.strip() or "No reason provided"
        try:
            await member.kick(reason=f"{interaction.user} ({interaction.user.id}): {reason}")
        except discord.Forbidden:
            await core.send_ephemeral_embed(interaction, "Kick denied", "Discord denied the kick. Check the bot's Kick Members permission and role hierarchy.", "error")
            return
        except discord.HTTPException as error:
            await core.send_ephemeral_embed(interaction, "Kick failed", f"Discord returned HTTP {error.status} while kicking {member.mention}.", "error")
            return
        await notify_member(
            member,
            "You were kicked",
            f"You were kicked from **{interaction.guild.name}**.\n\n**Reason:** {reason}",
        )
        await core.send_audit_event(interaction.guild, "member_kicked", interaction.user, member, reason)
        await core.send_ephemeral_embed(interaction, "Member kicked", f"{member.mention} was kicked from the server.", "success")

    @app_commands.command(name="ban", description="Permanently ban a member from the server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to ban", reason="Reason for the ban")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await validate_moderation_target(interaction, member):
            return
        reason = reason.strip() or "No reason provided"
        try:
            await member.ban(reason=f"{interaction.user} ({interaction.user.id}): {reason}")
        except discord.Forbidden:
            await core.send_ephemeral_embed(
                interaction,
                "Ban denied",
                "Discord denied the ban. Check the bot's Ban Members permission and role hierarchy.",
                "error",
            )
            return
        except discord.HTTPException as error:
            await core.send_ephemeral_embed(
                interaction,
                "Ban failed",
                f"Discord returned HTTP {error.status} while banning {member.mention}.",
                "error",
            )
            return
        await notify_member(
            member,
            "You were banned",
            f"You were banned from **{interaction.guild.name}**.\n\n**Reason:** {reason}",
        )
        await core.send_audit_event(interaction.guild, "member_banned", interaction.user, member, reason)
        await core.send_ephemeral_embed(interaction, "Member banned", f"{member.mention} was banned from the server.", "success")

    @app_commands.command(name="timeout", description="Temporarily timeout a member.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Member to timeout", duration="Duration such as 1m, 2h, or 7d", reason="Reason for the timeout")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
        if not await validate_moderation_target(interaction, member):
            return
        try:
            duration_seconds = parse_timeout_duration(duration)
        except ValueError as error:
            await core.send_ephemeral_embed(interaction, "Invalid timeout duration", str(error), "warning")
            return
        reason = reason.strip() or "No reason provided"
        until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        try:
            await member.edit(
                timed_out_until=until,
                reason=f"{interaction.user} ({interaction.user.id}): {reason}",
            )
        except discord.Forbidden:
            await core.send_ephemeral_embed(interaction, "Timeout denied", "Discord denied the timeout. Check the bot's Moderate Members permission and role hierarchy.", "error")
            return
        except discord.HTTPException as error:
            await core.send_ephemeral_embed(interaction, "Timeout failed", f"Discord returned HTTP {error.status} while timing out {member.mention}.", "error")
            return
        await notify_member(
            member,
            "You were timed out",
            f"You were timed out in **{interaction.guild.name}** for **{duration}**.\n\n**Reason:** {reason}",
        )
        await core.send_audit_event(interaction.guild, "member_timed_out", interaction.user, member, f"{duration}: {reason}")
        await core.send_ephemeral_embed(interaction, "Member timed out", f"{member.mention} was timed out for **{duration}**.", "success")

class Staff(commands.GroupCog, group_name="staff", group_description="Manage staff-only member records"):
    @app_commands.command(name="warnings", description="View a member's active staff warnings.")
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only configured support staff can view staff warnings.", "error")
            return
        with core.db_session() as connection:
            rows = connection.execute(
                "SELECT reason, staff_id, created_at FROM staff_warnings WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id DESC LIMIT 10",
                (interaction.guild_id, member.id),
            ).fetchall()
        if not rows:
            description = f"{member.mention} has no active staff warnings."
        else:
            description = "\n\n".join(f"**{row['created_at'][:10]}** • <@{row['staff_id']}>\n{row['reason']}" for row in rows)
        await interaction.response.send_message(embed=core.make_embed(f"Warnings • {member.display_name}", description, "warning"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Profile())
    await bot.add_cog(Staff())
