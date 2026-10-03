import random
import re
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

import discord
from discord import app_commands
from discord.ext import commands

import config
import main as core


VERIFICATION_CHANNEL_ID = config.VERIFICATION_CHANNEL_ID
VERIFIED_ROLE_IDS = config.VERIFIED_ROLE_IDS or ()
PENDING_VERIFICATION_ROLE_ID = config.PENDING_VERIFICATION_ROLE_ID
MINECRAFT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


async def restrict_member(member):
    verification_channel = member.guild.get_channel(VERIFICATION_CHANNEL_ID)
    if verification_channel is None:
        raise RuntimeError("The configured verification channel does not exist.")
    for channel in member.guild.channels:
        if channel.id == VERIFICATION_CHANNEL_ID:
            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                reason="New member must complete verification",
            )
        else:
            # Remove stale member-specific denies created by older onboarding code.
            await channel.set_permissions(member, overwrite=None, reason="Reset stale onboarding permissions")


async def restore_member_access(member):
    await hide_verification_channel(member)


async def hide_verification_channel(member):
    verification_channel = member.guild.get_channel(VERIFICATION_CHANNEL_ID)
    if verification_channel is not None:
        await verification_channel.set_permissions(
            member,
            view_channel=False,
            send_messages=False,
            read_message_history=False,
            reason="Verified member no longer needs verification",
        )


def is_verified(member):
    return any(role.id in VERIFIED_ROLE_IDS for role in getattr(member, "roles", ()))


def register_attempt(guild_id, user_id):
    now = datetime.now(timezone.utc)
    with core.db_session() as connection:
        row = connection.execute(
            "SELECT attempts, locked_until FROM verification_attempts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row and row["locked_until"]:
            locked_until = datetime.fromisoformat(row["locked_until"])
            if locked_until > now:
                return False, int((locked_until - now).total_seconds()) + 1
        attempts = row["attempts"] if row else 0
        if row and row["locked_until"] and datetime.fromisoformat(row["locked_until"]) <= now:
            attempts = 0
        attempts += 1
        locked_until = now + timedelta(minutes=15) if attempts >= 5 else None
        connection.execute(
            """INSERT INTO verification_attempts (guild_id, user_id, attempts, last_attempt_at, locked_until)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET attempts = excluded.attempts,
               last_attempt_at = excluded.last_attempt_at, locked_until = excluded.locked_until""",
            (guild_id, user_id, attempts, now.isoformat(), locked_until.isoformat() if locked_until else None),
        )
    return True, 0


def verified_account_owner(guild_id, minecraft_name):
    with core.db_session() as connection:
        row = connection.execute(
            "SELECT user_id FROM verified_accounts WHERE guild_id = ? AND minecraft_name = ? COLLATE NOCASE",
            (guild_id, minecraft_name),
        ).fetchone()
    return row["user_id"] if row else None


def has_verified_account(guild_id, user_id):
    with core.db_session() as connection:
        row = connection.execute(
            "SELECT 1 FROM verified_accounts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


async def minecraft_name_exists(minecraft_name):
    endpoint = os.getenv("DONUTSMP_PLAYER_API_URL")
    if not endpoint:
        return True, None

    def request_player():
        with urlopen(f"{endpoint.rstrip('/')}/{quote(minecraft_name)}", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict) and "exists" in payload:
            return bool(payload["exists"])
        return bool(payload)

    try:
        return await asyncio.to_thread(request_player), None
    except HTTPError as error:
        if error.code == 404:
            return False, None
        return False, "The Minecraft account service rejected the request."
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return False, "The Minecraft account service is unavailable."


def permission_blockers(member, roles):
    bot_member = member.guild.me
    if bot_member is None:
        return ["The bot's member record is not available yet."]
    blockers = []
    permissions = bot_member.guild_permissions
    for permission in ("manage_channels", "manage_nicknames", "manage_roles"):
        if not getattr(permissions, permission):
            blockers.append(f"Missing server permission: {permission.replace('_', ' ').title()}.")
    if member.id != member.guild.owner_id and member.top_role >= bot_member.top_role:
        blockers.append(f"The bot's highest role must be above {member.top_role.mention} to change this member's nickname.")
    for role in roles:
        if role >= bot_member.top_role:
            blockers.append(f"The bot's highest role must be above {role.mention} to assign that role.")
    return blockers


class VerifyModal(discord.ui.Modal, title="Minecraft verification"):
    def __init__(self, first_number, second_number):
        super().__init__()
        self.expected_answer = first_number + second_number
        self.minecraft_name = discord.ui.TextInput(
            label="Minecraft username",
            placeholder="Your exact Minecraft Java username",
            min_length=3,
            max_length=16,
        )
        self.captcha_answer = discord.ui.TextInput(
            label=f"Captcha: {first_number} + {second_number} = ?",
            placeholder="Enter the answer shown above",
            min_length=1,
            max_length=3,
        )
        self.add_item(self.minecraft_name)
        self.add_item(self.captcha_answer)

    async def on_submit(self, interaction):
        minecraft_name = str(self.minecraft_name.value).strip()
        captcha_answer = str(self.captcha_answer.value).strip()
        if is_verified(interaction.user) or has_verified_account(interaction.guild_id, interaction.user.id):
            await hide_verification_channel(interaction.user)
            await core.send_ephemeral_embed(interaction, "Already verified", "Your Minecraft account is already verified on this server.", "info")
            return
        allowed, remaining = register_attempt(interaction.guild_id, interaction.user.id)
        if not allowed:
            await core.send_ephemeral_embed(
                interaction,
                "Verification temporarily locked",
                f"Too many attempts. Try again in **{remaining // 60}m {remaining % 60}s**.",
                "warning",
            )
            return
        if not MINECRAFT_NAME_PATTERN.fullmatch(minecraft_name):
            await core.send_ephemeral_embed(
                interaction,
                "Invalid Minecraft username",
                "Use 3-16 letters, numbers, or underscores only.",
                "warning",
            )
            return
        if captcha_answer != str(self.expected_answer):
            await core.send_ephemeral_embed(
                interaction,
                "Captcha failed",
                "That answer was incorrect. Press Verify again to receive a new challenge.",
                "warning",
            )
            return
        exists, api_error = await minecraft_name_exists(minecraft_name)
        if api_error:
            await core.send_ephemeral_embed(interaction, "Verification service unavailable", api_error, "error")
            return
        if not exists:
            await core.send_ephemeral_embed(interaction, "Minecraft account not found", "That Minecraft username could not be verified by the configured account service.", "warning")
            return
        owner_id = verified_account_owner(interaction.guild_id, minecraft_name)
        if owner_id is not None and owner_id != interaction.user.id:
            await core.send_ephemeral_embed(
                interaction,
                "Minecraft account already verified",
                "That Minecraft username is already linked to another member on this server.",
                "error",
            )
            return
        roles = [interaction.guild.get_role(role_id) for role_id in VERIFIED_ROLE_IDS]
        roles = [role for role in roles if role is not None]
        if len(roles) != len(VERIFIED_ROLE_IDS):
            await core.send_ephemeral_embed(
                interaction,
                "Verification is unavailable",
                "The configured verified roles are missing. Please contact an administrator.",
                "error",
            )
            return
        pending_role = interaction.guild.get_role(PENDING_VERIFICATION_ROLE_ID)
        hierarchy_roles = roles + ([pending_role] if pending_role else [])
        blockers = permission_blockers(interaction.user, hierarchy_roles)
        if blockers:
            await core.send_ephemeral_embed(
                interaction,
                "Verification permissions need attention",
                "\n".join(f"• {blocker}" for blocker in blockers),
                "error",
            )
            return
        try:
            await interaction.user.edit(nick=minecraft_name, reason="Completed Minecraft verification")
        except discord.Forbidden:
            await core.send_ephemeral_embed(
                interaction,
                "Nickname update failed",
                "Discord rejected the nickname change. Move the bot's highest role above this member and ensure Manage Nicknames is enabled.",
                "error",
            )
            return
        try:
            await interaction.user.add_roles(*roles, reason="Completed Minecraft verification")
        except discord.Forbidden:
            await core.send_ephemeral_embed(
                interaction,
                "Role assignment failed",
                "Discord rejected the role assignment. Move the bot's highest role above both verified roles and ensure Manage Roles is enabled.",
                "error",
            )
            return
        if pending_role and pending_role in interaction.user.roles:
            try:
                await interaction.user.remove_roles(
                    pending_role,
                    reason="Completed Minecraft verification",
                )
            except discord.Forbidden:
                await core.send_ephemeral_embed(
                    interaction,
                    "Pending role removal failed",
                    "Verification roles were added, but Discord rejected removal of the pending verification role. Move the bot's highest role above that role and ensure Manage Roles is enabled.",
                    "error",
                )
                return
        try:
            await restore_member_access(interaction.user)
        except discord.Forbidden:
            await core.send_ephemeral_embed(
                interaction,
                "Access restoration failed",
                "Discord rejected a channel permission update. Ensure the bot can manage channel permissions in every server channel.",
                "error",
            )
            return
        with core.db_session() as connection:
            connection.execute(
                """INSERT INTO verified_accounts (guild_id, user_id, minecraft_name, verified_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET minecraft_name = excluded.minecraft_name,
                   verified_at = excluded.verified_at""",
                (interaction.guild_id, interaction.user.id, minecraft_name, datetime.now(timezone.utc).isoformat()),
            )
        await interaction.response.send_message(
            embed=core.make_embed(
                "Verification complete",
                f"Your nickname is now **{minecraft_name}** and your verified roles have been assigned. Welcome to {interaction.guild.name}!",
                "success",
            ),
            ephemeral=True,
        )
        await core.send_audit_event(
            interaction.guild,
            "member_verified",
            actor=interaction.user,
            target=interaction.user,
            details=f"Member completed verification with Minecraft username `{minecraft_name}`.",
        )


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="member-verification:start")
    async def verify(self, interaction, button):
        if is_verified(interaction.user):
            try:
                await hide_verification_channel(interaction.user)
            except discord.Forbidden:
                core.app.logger.warning("Could not hide verification channel from verified member %s", interaction.user.id)
            await interaction.response.send_message(
                embed=core.make_embed(
                    "Already verified",
                    "You have already completed verification. This button is no longer available to you.",
                    "info",
                ),
                ephemeral=True,
            )
            return
        first_number = random.SystemRandom().randint(2, 9)
        second_number = random.SystemRandom().randint(2, 9)
        await interaction.response.send_modal(VerifyModal(first_number, second_number))


class Verify(commands.Cog):
    @app_commands.command(name="verify", description="Post the member verification panel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def verify(self, interaction):
        channel = await core.resolve_channel(VERIFICATION_CHANNEL_ID)
        embed = core.make_embed(
            "DonutSMP Member Verification",
            "Welcome to the server. Verify your Minecraft Java username to unlock the rest of the community.",
            "info",
        )
        embed.add_field(
            name="How it works",
            value="Select **Verify**, enter your exact Minecraft username, and solve the short anti-bot challenge. Your nickname and verified roles will be updated automatically.",
            inline=False,
        )
        embed.set_footer(text="If verification fails, contact a server administrator.")
        await channel.send(embed=embed, view=VerifyView())
        await core.send_ephemeral_embed(interaction, "Verification panel posted", f"The verification panel is live in {channel.mention}.", "success")


async def setup(bot):
    await bot.add_cog(Verify())
