import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import main as core


def database_status():
    try:
        with core.db_session() as connection:
            connection.execute("SELECT 1").fetchone()
        return "✅ Operational"
    except sqlite3.Error:
        core.app.logger.exception("Health check database failure")
        return "❌ Unavailable"


def transaction_status():
    if not core.WEBHOOK_SECRET:
        return "⚪ Disabled (no secret configured)"
    try:
        with core.db_session() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM webhook_outbox WHERE delivered_at IS NULL"
            ).fetchone()[0]
        return f"✅ Enabled • {pending:,} pending delivery(ies)"
    except sqlite3.Error:
        return "❌ Storage unavailable"


class Health(commands.Cog):
    @app_commands.command(name="health", description="Show bot, database, webhook, and worker health.")
    async def health(self, interaction: discord.Interaction):
        latency = core.bot.latency * 1000
        latency_text = f"{latency:.0f} ms" if latency >= 0 else "Connecting"
        worker_names = {
            "Tickets": core.ticket_reminder_worker,
            "Webhook outbox": core.webhook_outbox_worker,
            "Giveaways": core.giveaway_worker,
        }
        worker_status = "\n".join(f"{name}: {'✅ Running' if worker.is_running() else '❌ Stopped'}" for name, worker in worker_names.items())
        try:
            with core.db_session() as connection:
                last_delivery = connection.execute(
                    "SELECT delivered_at FROM webhook_outbox WHERE delivered_at IS NOT NULL ORDER BY delivered_at DESC LIMIT 1"
                ).fetchone()
            last_delivery_text = last_delivery["delivered_at"] if last_delivery else "No successful delivery recorded"
        except sqlite3.Error:
            last_delivery_text = "Unavailable"
        embed = core.make_embed("System health", "Operational status for the bot's local services.", "success")
        embed.add_field(name="📡 Discord gateway", value=f"✅ Connected\nLatency: {latency_text}", inline=True)
        embed.add_field(name="🗄️ Database", value=database_status(), inline=True)
        embed.add_field(name="🔔 Transaction webhook", value=transaction_status(), inline=True)
        embed.add_field(name="⚙️ Background workers", value=worker_status, inline=False)
        embed.add_field(name="📬 Last transaction delivery", value=last_delivery_text, inline=False)
        embed.add_field(name="🧩 DonutSMP integration", value="✅ Configured" if core.DONUTSMP_API_URL else "⚪ Webhook-only mode; no API/plugin configured", inline=False)
        if core.error_counts:
            errors = "\n".join(f"{name}: {count}" for name, count in sorted(core.error_counts.items()))
        else:
            errors = "No application-command errors recorded since startup."
        embed.add_field(name="⚠️ Error counters", value=errors[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="reset-member-permissions",
        description="Reset explicit member permissions for a channel or player.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Remove member-specific overwrites from this channel.",
        player="Remove this player's explicit overwrites from every channel.",
    )
    async def reset_member_permissions(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        player: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if (channel is None) == (player is None):
            await core.send_ephemeral_embed(
                interaction,
                "Choose one target",
                "Provide either `channel` or `player`, but not both.",
                "warning",
            )
            return
        removed = 0
        skipped = []
        bot_member = interaction.guild.me
        channels = [channel] if channel is not None else interaction.guild.channels
        for current_channel in channels:
            targets = [
                target
                for target in current_channel.overwrites
                if isinstance(target, discord.Member)
                and (bot_member is None or target.id != bot_member.id)
                and (player is None or target.id == player.id)
            ]
            for target in targets:
                try:
                    await current_channel.set_permissions(
                        target,
                        overwrite=None,
                        reason=f"Temporary member permission reset by {interaction.user} ({interaction.user.id})",
                    )
                    removed += 1
                except discord.Forbidden:
                    skipped.append(current_channel.mention)
                except discord.HTTPException:
                    skipped.append(current_channel.mention)
        scope = f"channel {channel.mention}" if channel else f"player {player.mention}"
        details = f"Removed {removed} explicit member permission overwrite(s) for {scope}."
        if skipped:
            details += f" Could not update {len(set(skipped))} channel(s): {', '.join(sorted(set(skipped))[:10])}."
        await core.send_audit_event(
            interaction.guild,
            "member_channel_permissions_reset",
            interaction.user,
            details=details,
        )
        await core.send_ephemeral_embed(interaction, "Member permissions reset", details, "success" if not skipped else "warning")


def _role_snapshot(role):
    return {
        "id": role.id,
        "name": role.name,
        "position": role.position,
        "colour": role.colour.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "permissions": role.permissions.value,
    }


def _channel_snapshot(channel):
    snapshot = {
        "id": channel.id,
        "type": str(channel.type),
        "name": channel.name,
        "position": getattr(channel, "position", None),
        "category_id": getattr(channel, "category_id", None),
        "topic": getattr(channel, "topic", None),
        "nsfw": getattr(channel, "nsfw", None),
        "slowmode_delay": getattr(channel, "slowmode_delay", None),
        "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", None),
        "default_thread_slowmode_delay": getattr(channel, "default_thread_slowmode_delay", None),
        "bitrate": getattr(channel, "bitrate", None),
        "user_limit": getattr(channel, "user_limit", None),
        "rtc_region": getattr(channel, "rtc_region", None),
        "default_sort_order": str(getattr(channel, "default_sort_order", "")) if getattr(channel, "default_sort_order", None) else None,
        "default_forum_layout": str(getattr(channel, "default_forum_layout", "")) if getattr(channel, "default_forum_layout", None) else None,
    }
    if hasattr(channel, "available_tags"):
        snapshot["available_tags"] = [
            {
                "id": tag.id,
                "name": tag.name,
                "moderated": tag.moderated,
                "emoji_id": tag.emoji_id,
                "emoji_name": tag.emoji_name,
            }
            for tag in channel.available_tags
        ]
    return snapshot


def _member_snapshot(member):
    return {
        "id": member.id,
        "name": member.name,
        "display_name": member.display_name,
        "global_name": member.global_name,
        "bot": member.bot,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "role_ids": [role.id for role in member.roles if role.is_default() is False],
    }


def build_guild_snapshot(guild):
    return {
        "format": "donutsmp-essentials-discord-backup-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "permissions_included": False,
        "guild": {
            "id": guild.id,
            "name": guild.name,
            "description": guild.description,
            "owner_id": guild.owner_id,
            "verification_level": str(guild.verification_level),
            "default_notifications": str(guild.default_notifications),
            "explicit_content_filter": str(guild.explicit_content_filter),
            "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
            "afk_timeout": guild.afk_timeout,
            "system_channel_id": guild.system_channel.id if guild.system_channel else None,
            "rules_channel_id": guild.rules_channel.id if guild.rules_channel else None,
            "public_updates_channel_id": guild.public_updates_channel.id if guild.public_updates_channel else None,
            "preferred_locale": str(guild.preferred_locale),
            "features": sorted(guild.features),
        },
        "roles": [_role_snapshot(role) for role in guild.roles],
        "channels": [_channel_snapshot(channel) for channel in guild.channels],
        "members": [_member_snapshot(member) for member in guild.members],
        "emojis": [
            {
                "id": emoji.id,
                "name": emoji.name,
                "animated": emoji.animated,
                "managed": emoji.managed,
                "available": emoji.available,
            }
            for emoji in guild.emojis
        ],
        "stickers": [
            {
                "id": sticker.id,
                "name": sticker.name,
                "description": sticker.description,
                "format": str(sticker.format),
                "available": sticker.available,
            }
            for sticker in guild.stickers
        ],
    }


def read_backup_snapshot(backup_file):
    backup_path = Path(core.DISCORD_BACKUP_DIRECTORY) / Path(backup_file).name
    if backup_path.suffix.lower() != ".json":
        raise ValueError("Backups must be JSON files created by `/backup create`.")
    if not backup_path.is_file():
        raise FileNotFoundError(f"Backup file `{backup_path.name}` was not found in `{core.DISCORD_BACKUP_DIRECTORY}`.")
    snapshot = json.loads(backup_path.read_text(encoding="utf-8"))
    if snapshot.get("format") != "donutsmp-essentials-discord-backup-v1":
        raise ValueError("That file is not a compatible DonutSMP Essentials backup.")
    if snapshot.get("permissions_included"):
        raise ValueError("This backup includes channel permissions and cannot be loaded by the permission-safe restore.")
    return snapshot, backup_path


async def restore_guild_snapshot(guild, snapshot):
    created_roles = 0
    created_channels = 0
    assigned_roles = 0
    skipped_channels = []
    skipped_roles = []
    role_map = {}
    saved_role_order = []
    bot_member = guild.me

    for saved_role in sorted(snapshot.get("roles", []), key=lambda role: role.get("position", 0)):
        if saved_role.get("managed") or saved_role.get("name") == "@everyone":
            continue
        role = discord.utils.find(lambda item: item.name == saved_role.get("name"), guild.roles)
        if role is None:
            role = await guild.create_role(
                name=saved_role["name"],
                permissions=discord.Permissions(saved_role.get("permissions", 0)),
                colour=discord.Colour(saved_role.get("colour", 0)),
                hoist=bool(saved_role.get("hoist", False)),
                mentionable=bool(saved_role.get("mentionable", False)),
                reason="Restore DonutSMP Essentials backup",
            )
            created_roles += 1
        else:
            if bot_member is not None and role >= bot_member.top_role:
                skipped_roles.append(f"{role.name} (above the bot's highest role)")
            else:
                try:
                    await role.edit(
                        permissions=discord.Permissions(saved_role.get("permissions", 0)),
                        colour=discord.Colour(saved_role.get("colour", 0)),
                        hoist=bool(saved_role.get("hoist", False)),
                        mentionable=bool(saved_role.get("mentionable", False)),
                        reason="Restore DonutSMP Essentials role settings",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    skipped_roles.append(f"{role.name} (Discord denied role settings)")
        role_map[saved_role["id"]] = role
        if not role.managed and (bot_member is None or role < bot_member.top_role):
            saved_role_order.append((int(saved_role.get("position", 1)), role))

    if saved_role_order:
        maximum_position = max(1, len(guild.roles) - 1)
        role_positions = {
            role: min(maximum_position, index)
            for index, (_, role) in enumerate(
                sorted(saved_role_order, key=lambda item: item[0], reverse=True),
                start=1,
            )
        }
        try:
            await guild.edit_role_positions(
                positions=role_positions,
                reason="Restore DonutSMP Essentials role order",
            )
        except (discord.Forbidden, discord.HTTPException):
            skipped_roles.append("role order (Discord denied role position changes)")

    saved_channels = snapshot.get("channels", [])
    category_map = {}
    for saved_channel in sorted(saved_channels, key=lambda channel: channel.get("position", 0)):
        if saved_channel.get("type") != "category":
            continue
        channel = discord.utils.find(lambda item: item.name == saved_channel.get("name"), guild.categories)
        if channel is None:
            channel = await guild.create_category(
                saved_channel["name"],
                reason="Restore DonutSMP Essentials backup without channel permissions",
            )
            created_channels += 1
        category_map[saved_channel["id"]] = channel

    for saved_channel in sorted(saved_channels, key=lambda channel: channel.get("position", 0)):
        channel_type = saved_channel.get("type")
        if channel_type == "category":
            continue
        category = category_map.get(saved_channel.get("category_id"))
        existing = discord.utils.find(
            lambda item: item.name == saved_channel.get("name") and str(item.type) == channel_type,
            guild.channels,
        )
        if existing is not None:
            continue
        kwargs = {"category": category, "reason": "Restore DonutSMP Essentials backup without channel permissions"}
        if channel_type in {"text", "news"}:
            kwargs.update(
                topic=saved_channel.get("topic"),
                nsfw=bool(saved_channel.get("nsfw", False)),
                slowmode_delay=saved_channel.get("slowmode_delay") or 0,
            )
            creator = guild.create_text_channel if channel_type == "text" else guild.create_text_channel
        elif channel_type == "voice":
            kwargs.update(
                bitrate=saved_channel.get("bitrate"),
                user_limit=saved_channel.get("user_limit") or 0,
            )
            creator = guild.create_voice_channel
        elif channel_type == "stage_voice":
            creator = guild.create_stage_channel
        else:
            skipped_channels.append(f"{saved_channel.get('name')} ({channel_type})")
            continue
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        await creator(saved_channel["name"], **kwargs)
        created_channels += 1

    for saved_member in snapshot.get("members", []):
        member = guild.get_member(saved_member.get("id"))
        if member is None or member.bot:
            continue
        roles = [role_map[role_id] for role_id in saved_member.get("role_ids", []) if role_id in role_map]
        if not roles:
            continue
        try:
            await member.add_roles(*roles, reason="Restore DonutSMP Essentials backup role assignments")
            assigned_roles += len(roles)
        except (discord.Forbidden, discord.HTTPException):
            continue

    return created_roles, created_channels, assigned_roles, skipped_channels, skipped_roles


class Backup(commands.GroupCog, group_name="backup", group_description="Create Discord and bot data backups"):
    @app_commands.command(name="create", description="Back up server structure and bot data without channel permissions.")
    @app_commands.guild_only()
    async def create(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await asyncio.to_thread(build_guild_snapshot, interaction.guild)
            backup_directory = Path(core.DISCORD_BACKUP_DIRECTORY)
            backup_directory.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            snapshot_path = backup_directory / f"guild-{interaction.guild.id}-{timestamp}.json"
            await asyncio.to_thread(
                snapshot_path.write_text,
                json.dumps(snapshot, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
            await asyncio.to_thread(core.backup_database)
        except OSError as error:
            core.app.logger.exception("Could not write Discord backup")
            await core.send_ephemeral_embed(
                interaction,
                "Backup failed",
                f"The backup could not be written to `{core.DISCORD_BACKUP_DIRECTORY}`: {error}",
                "error",
            )
            return
        except Exception:
            core.app.logger.exception("Could not create Discord backup")
            await core.send_ephemeral_embed(
                interaction,
                "Backup failed",
                "The bot could not create the server snapshot. Check the bot logs for the exact error.",
                "error",
            )
            return
        await core.send_audit_event(
            interaction.guild,
            "discord_backup_created",
            interaction.user,
            details=f"Created {snapshot_path} without channel permission overwrites.",
        )
        await core.send_ephemeral_embed(
            interaction,
            "Backup created",
            f"Saved `{snapshot_path}` and the SQLite database backup. Channel permissions were intentionally excluded.",
            "success",
        )

    @app_commands.command(name="restore", description="Load a saved server structure backup without channel permissions.")
    @app_commands.guild_only()
    @app_commands.describe(backup_file="JSON filename inside the configured backup directory")
    async def restore(self, interaction: discord.Interaction, backup_file: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot, backup_path = await asyncio.to_thread(read_backup_snapshot, backup_file)
            source_guild_id = snapshot.get("guild", {}).get("id")
            result = await restore_guild_snapshot(interaction.guild, snapshot)
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as error:
            await core.send_ephemeral_embed(interaction, "Restore failed", str(error), "error")
            return
        except (discord.Forbidden, discord.HTTPException) as error:
            error_detail = getattr(error, "text", None) or str(error)
            await core.send_ephemeral_embed(
                interaction,
                "Discord restore failed",
                f"Discord rejected part of the restore with HTTP {getattr(error, 'status', 'permission error')}: {error_detail[:500]} No channel permissions were changed.",
                "error",
            )
            return
        except Exception:
            core.app.logger.exception("Could not restore Discord backup")
            await core.send_ephemeral_embed(interaction, "Restore failed", "An unexpected restore error occurred. Check the bot logs.", "error")
            return
        created_roles, created_channels, assigned_roles, skipped_channels, skipped_roles = result
        details = f"Created {created_roles} role(s), {created_channels} channel(s), and restored {assigned_roles} member role assignment(s) from `{backup_path.name}` (source guild `{source_guild_id}`)."
        if skipped_channels:
            details += f" Unsupported channel types skipped: {', '.join(skipped_channels[:10])}."
        if skipped_roles:
            details += f" Roles skipped because Discord denied access: {', '.join(skipped_roles[:10])}."
        await core.send_audit_event(interaction.guild, "discord_backup_restored", interaction.user, details=details)
        await core.send_ephemeral_embed(
            interaction,
            "Backup restored",
            details + " Channel permission overwrites were not loaded or changed.",
            "success" if not skipped_channels else "warning",
        )


class Dashboard(commands.GroupCog, group_name="dashboard", group_description="Publish staff operations dashboards"):
    @app_commands.command(name="refresh", description="Publish a current staff operations dashboard.")
    @app_commands.guild_only()
    async def refresh(self, interaction: discord.Interaction):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only configured support staff can publish the dashboard.", "error")
            return
        with core.db_session() as connection:
            support_open = connection.execute("SELECT COUNT(*) FROM support_tickets WHERE guild_id = ? AND status = 'open'", (interaction.guild_id,)).fetchone()[0]
            support_unclaimed = connection.execute("SELECT COUNT(*) FROM support_tickets WHERE guild_id = ? AND status = 'open' AND claimed_by IS NULL", (interaction.guild_id,)).fetchone()[0]
            middleman_open = connection.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'", (interaction.guild_id,)).fetchone()[0]
            giveaways = connection.execute("SELECT COUNT(*) FROM giveaways WHERE guild_id = ? AND status = 'active'", (interaction.guild_id,)).fetchone()[0]
            warnings = connection.execute("SELECT COUNT(*) FROM staff_warnings WHERE guild_id = ? AND active = 1", (interaction.guild_id,)).fetchone()[0]
            warning_points = connection.execute("SELECT COALESCE(SUM(points), 0) FROM staff_warnings WHERE guild_id = ? AND active = 1", (interaction.guild_id,)).fetchone()[0]
            pending_applications = connection.execute("SELECT COUNT(*) FROM staff_applications WHERE guild_id = ? AND status = 'pending'", (interaction.guild_id,)).fetchone()[0]
            pending_webhooks = connection.execute("SELECT COUNT(*) FROM webhook_outbox WHERE delivered_at IS NULL").fetchone()[0]
            mismatches = connection.execute("SELECT COUNT(*) FROM transaction_matches WHERE match_status = 'mismatch'").fetchone()[0]
        channel = await core.resolve_channel(core.STAFF_DASHBOARD_CHANNEL_ID)
        embed = core.make_embed("Staff operations dashboard", "Live queues and moderation signals for this server.", "info")
        embed.add_field(name="🎫 Support tickets", value=f"{support_open:,} open", inline=True)
        embed.add_field(name="📥 Unclaimed tickets", value=f"{support_unclaimed:,}", inline=True)
        embed.add_field(name="🤝 Middleman deals", value=f"{middleman_open:,} open", inline=True)
        embed.add_field(name="🎉 Giveaways", value=f"{giveaways:,} active", inline=True)
        embed.add_field(name="📝 Pending applications", value=f"{pending_applications:,}", inline=True)
        embed.add_field(name="⚠️ Active warnings", value=f"{warnings:,} ({warning_points:,} points)", inline=True)
        embed.add_field(name="🔔 Webhook outbox", value=f"{pending_webhooks:,} pending", inline=True)
        embed.add_field(name="⚠️ Amount mismatches", value=f"{mismatches:,}", inline=True)
        embed.set_footer(text=f"Refreshed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} • Staff access only")
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await core.send_audit_event(interaction.guild, "staff_dashboard_refreshed", interaction.user, channel, f"Dashboard published as message {message.id}.")
        await core.send_ephemeral_embed(interaction, "Dashboard refreshed", f"The staff dashboard is live in {channel.mention}.", "success")


async def setup(bot):
    await bot.add_cog(Health())
    await bot.add_cog(Dashboard())
