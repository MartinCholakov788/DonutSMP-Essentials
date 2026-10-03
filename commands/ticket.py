import asyncio
import io
import re
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands

import main as core


class TicketType(Enum):
    GENERAL = "general"
    REPORT = "report"
    GIVEAWAY = "giveaway"
    PARTNERSHIP = "partnership"


class TicketStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    DELETED = "deleted"


class TicketPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


TICKET_LABELS: Dict[str, str] = {
    TicketType.GENERAL.value: "General Problems Ticket",
    TicketType.REPORT.value: "Report a Player Ticket",
    TicketType.GIVEAWAY.value: "Giveaways",
    TicketType.PARTNERSHIP.value: "Partnership Request",
}
TICKET_CREATE_COOLDOWN_SECONDS = 180
ticket_creation_cooldowns = {}


def ticket_creation_cooldown_remaining(user_id: int) -> int:
    now = time.monotonic()
    last_created = ticket_creation_cooldowns.get(user_id)
    if last_created is not None:
        remaining = TICKET_CREATE_COOLDOWN_SECONDS - (now - last_created)
        if remaining > 0:
            return int(remaining) + 1
    ticket_creation_cooldowns[user_id] = now
    return 0


def get_support_ticket(
    ticket_number: Optional[int] = None, 
    channel_id: Optional[int] = None, 
    guild_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Fetches a support ticket from the database by ticket number or channel ID."""
    if ticket_number is None and channel_id is None:
        return None
        
    column, value = ("ticket_number", ticket_number) if ticket_number is not None else ("channel_id", channel_id)
    query = f"SELECT * FROM support_tickets WHERE {column} = ?"
    values: List[Any] = [value]
    
    if guild_id is not None:
        query += " AND guild_id = ?"
        values.append(guild_id)
        
    with core.db_session() as connection:
        row = connection.execute(query, values).fetchone()
        
    return dict(row) if row else None


def safe_channel_name(value: str) -> str:
    """Sanitizes a string to be safe for Discord channel names."""
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (value or "member")[:38]


def ticket_channel_name(ticket_type: str, member: discord.Member, number: int) -> str:
    """Generates a standardized channel name for a new ticket."""
    name = safe_channel_name(member.display_name)
    prefixes = {
        TicketType.GENERAL.value: "general-ticket",
        TicketType.REPORT.value: "report-player",
        TicketType.GIVEAWAY.value: "giveaway",
        TicketType.PARTNERSHIP.value: "partnership"
    }
    prefix = prefixes.get(ticket_type, "ticket")
    return f"{prefix}-{name}-{number}"[:95]


async def generate_transcript(channel: discord.TextChannel) -> discord.File:
    """Generates a comprehensive text transcript of a channel's history."""
    lines = [
        f"Transcript for Support Channel: {channel.name}",
        f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "=" * 60
    ]
    
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = msg.author.display_name
            content = msg.clean_content or "[No text content]"
            
            lines.append(f"\n[{time_str}] {author}: {content}")
            
            if msg.attachments:
                lines.append(f"    📎 Attachments: {', '.join(a.url for a in msg.attachments)}")
            if msg.embeds:
                lines.append(f"    📄 [Embed Included]")
    except discord.Forbidden:
        lines.append("\n[Error: Bot lacked permission to read full message history]")
        
    file_bytes = io.BytesIO("\n".join(lines).encode('utf-8'))
    return discord.File(file_bytes, filename=f"transcript-{channel.name}.txt")


def ticket_embeds(ticket: Dict[str, Any]) -> List[discord.Embed]:
    """Generates the main informational embeds for a support ticket."""
    status = ticket["status"]
    
    if status == TicketStatus.OPEN.value:
        tone = "info"
    elif status == TicketStatus.CLOSED.value:
        tone = "warning"
    else:
        tone = "error"

    main_embed = core.make_embed(
        title=f"{TICKET_LABELS.get(ticket['ticket_type'], 'Ticket')} #{ticket['ticket_number']}",
        description="Welcome to your support ticket. Please describe your issue clearly. A staff member will assist you shortly.\n\n**⚠️ Please do not ping any staff members.**",
        tone=tone,
    )
    
    claimed_by_text = f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "*Unclaimed*"
    priority_display = str(ticket.get("priority", TicketPriority.NORMAL.value)).upper()
    
    main_embed.add_field(name="👤 Created by", value=f"<@{ticket['creator_id']}>", inline=True)
    main_embed.add_field(name="📌 Status", value=status.title(), inline=True)
    main_embed.add_field(name="🚦 Priority", value=priority_display, inline=True)
    main_embed.add_field(name="🧑‍⚖️ Claimed by", value=claimed_by_text, inline=True)
    last_activity = ticket.get("last_activity_at") or ticket.get("created_at")
    main_embed.add_field(
        name="🕒 Last activity",
        value=last_activity[:16].replace("T", " ") + " UTC" if last_activity else "Unknown",
        inline=True,
    )
    
    if ticket["ticket_type"] == TicketType.PARTNERSHIP.value:
        main_embed.add_field(
            name="🤝 Partnership Requirements",
            value=(
                "• You must remain in our server, or the advertisement will be removed.\n"
                "• Post our advertisement in the appropriate channel.\n"
                "• Do not remove our ad without notifying staff first."
            ),
            inline=False,
        )

    reason_embed = core.make_embed(
        title="📝 Initial Reason",
        description=f"```{ticket['reason'][:4000]}```",
        tone=tone,
    )
        
    return [main_embed, reason_embed]


async def update_ticket_message(ticket: Dict[str, Any], channel: discord.TextChannel) -> None:
    """Updates the ticket's main message with the latest embed state."""
    try:
        await channel.send(embeds=ticket_embeds(ticket), view=SupportTicketView(ticket["ticket_number"]))
    except discord.HTTPException:
        core.app.logger.exception("Could not refresh support ticket #%s", ticket["ticket_number"])


async def create_support_ticket(
    interaction: discord.Interaction, 
    ticket_type: str, 
    reason: str, 
    channel_prefix: Optional[str] = None
) -> Tuple[discord.TextChannel, int]:
    """Creates a new support ticket channel, sets permissions, and stores it in the database."""
    guild = interaction.guild
    creator = interaction.user
    
    category_id = {
        TicketType.REPORT.value: core.REPORT_TICKET_CATEGORY_ID,
        TicketType.PARTNERSHIP.value: core.PARTNERSHIP_TICKET_CATEGORY_ID,
    }.get(ticket_type, core.SUPPORT_TICKET_CATEGORY_ID)
    
    category = guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("The configured support-ticket category does not exist.")
        
    bot_member = guild.me or guild.get_member(core.bot.user.id)
    if bot_member is None:
        raise RuntimeError("The bot member could not be resolved.")

    number = core.allocate_support_ticket_number(guild.id)
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        bot_member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True, attach_files=True
        ),
    }
    
    for role_id in core.SUPPORT_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True, manage_channels=True, attach_files=True
            )

    channel_name = (
        f"{safe_channel_name(channel_prefix)}-{number}"[:95] 
        if channel_prefix 
        else ticket_channel_name(ticket_type, creator, number)
    )

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        topic=f"{TICKET_LABELS.get(ticket_type, 'Ticket')} #{number} • Created by {creator} ({creator.id})",
        reason=f"Support ticket #{number} created by {creator}",
    )
    
    now = datetime.now(timezone.utc).isoformat()
    
    try:
        with core.db_session() as connection:
            connection.execute(
                """INSERT INTO support_tickets
                         (ticket_number, guild_id, channel_id, creator_id, ticket_type, reason, created_at, priority, last_activity_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (number, guild.id, channel.id, creator.id, ticket_type, reason, now, TicketPriority.NORMAL.value, now),
            )
    except Exception:
        await channel.delete(reason="Support ticket database write failed")
        raise
        
    ticket = get_support_ticket(ticket_number=number)
    support_role = core.resolve_middleman_support_role(guild)
    initial_content = f"<@{creator.id}>" + (f" {support_role.mention}" if support_role else "")
    
    await channel.send(
        content=initial_content,
        embeds=ticket_embeds(ticket),
        view=SupportTicketView(number),
        allowed_mentions=discord.AllowedMentions(users=[creator], roles=[support_role] if support_role else []),
    )
    
    await core.send_audit_event(
        guild, "support_ticket_created", actor=creator, target=channel,
        details=f"Created {TICKET_LABELS.get(ticket_type, 'Ticket')} #{number}. Reason: {reason}",
    )
    
    return channel, number


async def close_support_ticket(interaction: discord.Interaction, ticket_number: int, actor: discord.Member) -> None:
    """Handles logic for closing an active ticket and requesting user feedback."""
    ticket = get_support_ticket(ticket_number=ticket_number, guild_id=interaction.guild_id)
    if not ticket or ticket["status"] != TicketStatus.OPEN.value:
        await core.send_ephemeral_embed(interaction, "Already closed", "This ticket is not currently open.", "warning")
        return
        
    now = datetime.now(timezone.utc).isoformat()
    
    with core.db_session() as connection:
        connection.execute(
            "UPDATE support_tickets SET status = 'closed', closed_at = ?, closed_by = ?, last_activity_at = ? WHERE ticket_number = ?",
            (now, actor.id, now, ticket_number),
        )
        
    creator = interaction.guild.get_member(ticket["creator_id"])
    if creator:
        await interaction.channel.set_permissions(creator, send_messages=False)
        try:
            feedback_embed = core.make_embed(
                "Ticket Closed",
                f"Your support ticket **#{ticket_number}** in {interaction.guild.name} has been closed.\n\n"
                "We are always looking to improve. How would you rate the support you received?",
                "info"
            )
            await creator.send(embed=feedback_embed, view=SupportFeedbackView(ticket_number, interaction.guild.id))
        except discord.Forbidden:
            pass
            
    await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:95], reason=f"Ticket #{ticket_number} closed")
    
    updated_ticket = get_support_ticket(ticket_number=ticket_number)
    await core.send_audit_event(
        interaction.guild, "support_ticket_closed", actor, interaction.channel, f"Closed support ticket #{ticket_number}."
    )
    await interaction.response.edit_message(embeds=ticket_embeds(updated_ticket), view=SupportTicketView(ticket_number))


async def reopen_support_ticket(interaction: discord.Interaction, ticket: Dict[str, Any]) -> None:
    """Restores access and reopens a closed support ticket."""
    member = interaction.guild.get_member(ticket["creator_id"])
    if member:
        await interaction.channel.set_permissions(member, send_messages=True)
        
    new_name = re.sub(r"^closed-", "", interaction.channel.name)
    await interaction.channel.edit(name=new_name[:95], reason=f"Support ticket #{ticket['ticket_number']} reopened")
    
    with core.db_session() as connection:
        connection.execute(
            "UPDATE support_tickets SET status = 'open', closed_at = NULL, closed_by = NULL, last_activity_at = ? WHERE ticket_number = ?",
            (datetime.now(timezone.utc).isoformat(), ticket["ticket_number"]),
        )
        
    updated_ticket = get_support_ticket(ticket_number=ticket["ticket_number"])
    await core.send_audit_event(
        interaction.guild, "support_ticket_reopened", interaction.user, interaction.channel, f"Reopened support ticket #{ticket['ticket_number']}."
    )
    await interaction.response.edit_message(embeds=ticket_embeds(updated_ticket), view=SupportTicketView(ticket["ticket_number"]))


# --- UX / Feedback Modals & Views ---

class SupportFeedbackModal(discord.ui.Modal):
    def __init__(self, ticket_number: int, guild_id: int, rating: int):
        super().__init__(title="Support Feedback")
        self.ticket_number = ticket_number
        self.guild_id = guild_id
        self.rating = rating
        
        self.comments = discord.ui.TextInput(
            label="Any additional comments? (Optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Let us know how the staff member did...",
            required=False,
            max_length=1000
        )
        self.add_item(self.comments)

    async def on_submit(self, interaction: discord.Interaction):
        guild = core.bot.get_guild(self.guild_id)
        if guild:
            await core.send_audit_event(
                guild, "support_feedback_received", interaction.user, None,
                f"Ticket #{self.ticket_number} | Rating: {self.rating}/5 | Comments: {self.comments.value or 'None'}"
            )
        await core.send_ephemeral_embed(interaction, "Thank you!", "Your feedback has been submitted to the administration team.", "success")


class SupportFeedbackView(discord.ui.View):
    def __init__(self, ticket_number: int, guild_id: int):
        super().__init__(timeout=86400) # 24 hour timeout
        self.ticket_number = ticket_number
        self.guild_id = guild_id

    async def handle_rating(self, interaction: discord.Interaction, rating: int):
        modal = SupportFeedbackModal(self.ticket_number, self.guild_id, rating)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⭐ 1", style=discord.ButtonStyle.secondary)
    async def rate_1(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_rating(interaction, 1)

    @discord.ui.button(label="⭐⭐ 2", style=discord.ButtonStyle.secondary)
    async def rate_2(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_rating(interaction, 2)

    @discord.ui.button(label="⭐⭐⭐ 3", style=discord.ButtonStyle.secondary)
    async def rate_3(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_rating(interaction, 3)

    @discord.ui.button(label="⭐⭐⭐⭐ 4", style=discord.ButtonStyle.secondary)
    async def rate_4(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_rating(interaction, 4)

    @discord.ui.button(label="⭐⭐⭐⭐⭐ 5", style=discord.ButtonStyle.success)
    async def rate_5(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_rating(interaction, 5)


# --- Core Interaction Views ---

class SupportTicketView(discord.ui.View):
    def __init__(self, ticket_number: int):
        super().__init__(timeout=None)
        self.ticket_number = ticket_number
        self.claim_button.custom_id = f"support-ticket:{ticket_number}:claim"
        self.close_button.custom_id = f"support-ticket:{ticket_number}:close"
        self.escalate_button.custom_id = f"support-ticket:{ticket_number}:escalate"
        self.reopen_button.custom_id = f"support-ticket:{ticket_number}:reopen"
        self.delete_button.custom_id = f"support-ticket:{ticket_number}:delete"

    async def _is_authorized(self, interaction: discord.Interaction, ticket: Dict[str, Any], staff_only: bool = True) -> bool:
        is_staff = core.is_designated_staff(interaction.user)
        is_creator = interaction.user.id == ticket["creator_id"]
        
        if staff_only and not is_staff:
            await core.send_ephemeral_embed(interaction, "Staff Access Required", "Only staff can use this action.", "error")
            return False
        if not staff_only and not (is_staff or is_creator):
            await core.send_ephemeral_embed(interaction, "Access Denied", "You do not have permission to interact with this.", "error")
            return False
        return True

    @discord.ui.button(label="🫱 Claim", style=discord.ButtonStyle.primary, row=0)
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = get_support_ticket(ticket_number=self.ticket_number, guild_id=interaction.guild_id)
        if not ticket or not await self._is_authorized(interaction, ticket, staff_only=True): return
            
        if ticket["status"] != TicketStatus.OPEN.value:
            await core.send_ephemeral_embed(interaction, "Ticket Unavailable", "This ticket is not open.", "warning")
            return
            
        with core.db_session() as connection:
            connection.execute(
            "UPDATE support_tickets SET claimed_by = ?, last_activity_at = ? WHERE ticket_number = ? AND claimed_by IS NULL",
            (interaction.user.id, datetime.now(timezone.utc).isoformat(), self.ticket_number),
            )
            
        updated_ticket = get_support_ticket(ticket_number=self.ticket_number)
        await core.send_audit_event(interaction.guild, "support_ticket_claimed", interaction.user, interaction.channel, f"Claimed ticket #{self.ticket_number}.")
        await interaction.response.edit_message(embeds=ticket_embeds(updated_ticket), view=self)

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.secondary, row=0)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = get_support_ticket(ticket_number=self.ticket_number, guild_id=interaction.guild_id)
        if not ticket or not await self._is_authorized(interaction, ticket, staff_only=False): return
        
        if interaction.user.id == ticket["creator_id"] and not core.is_designated_staff(interaction.user):
            view = UserCloseConfirmView(self.ticket_number, interaction.user)
            await interaction.response.send_message(
                embed=core.make_embed("Confirm Closure", "Are you sure you want to mark your ticket as resolved and close it?", "warning"),
                view=view, ephemeral=True
            )
        else:
            await close_support_ticket(interaction, self.ticket_number, interaction.user)

    @discord.ui.button(label="⭐ Escalate", style=discord.ButtonStyle.danger, row=0)
    async def escalate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = get_support_ticket(ticket_number=self.ticket_number, guild_id=interaction.guild_id)
        if not ticket or not await self._is_authorized(interaction, ticket, staff_only=True): return
        
        with core.db_session() as connection:
            connection.execute(
                "UPDATE support_tickets SET priority = ?, last_activity_at = ? WHERE ticket_number = ?",
                (TicketPriority.URGENT.value, datetime.now(timezone.utc).isoformat(), self.ticket_number),
            )
        
        updated_ticket = get_support_ticket(ticket_number=self.ticket_number)
        await core.send_audit_event(interaction.guild, "support_ticket_escalated", interaction.user, interaction.channel, f"Escalated ticket #{self.ticket_number} to URGENT.")
        await interaction.response.edit_message(embeds=ticket_embeds(updated_ticket), view=self)
        await interaction.followup.send("🚨 **Ticket Escalated** - Senior staff have been notified.", allowed_mentions=discord.AllowedMentions.all())

    @discord.ui.button(label="↩ Reopen", style=discord.ButtonStyle.success, row=1)
    async def reopen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = get_support_ticket(ticket_number=self.ticket_number, guild_id=interaction.guild_id)
        if not ticket or not await self._is_authorized(interaction, ticket, staff_only=True): return
            
        if ticket["status"] != TicketStatus.CLOSED.value:
            await core.send_ephemeral_embed(interaction, "Cannot reopen", "Only closed tickets can be reopened.", "warning")
            return
        await reopen_support_ticket(interaction, ticket)

    @discord.ui.button(label="🗄️ Archive", style=discord.ButtonStyle.secondary, row=1)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = get_support_ticket(ticket_number=self.ticket_number, guild_id=interaction.guild_id)
        if not ticket or not await self._is_authorized(interaction, ticket, staff_only=True): return
        await close_support_ticket(interaction, self.ticket_number, interaction.user)


class UserCloseConfirmView(discord.ui.View):
    def __init__(self, ticket_number: int, user: discord.Member):
        super().__init__(timeout=60)
        self.ticket_number = ticket_number
        self.user = user

    @discord.ui.button(label="✅ Yes, close it", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_support_ticket(interaction, self.ticket_number, self.user)
        await interaction.delete_original_response()


class SupportDeleteConfirmView(discord.ui.View):
    def __init__(self, ticket_number: int, actor_id: int, channel: discord.TextChannel):
        super().__init__(timeout=60)
        self.ticket_number = ticket_number
        self.actor_id = actor_id
        self.channel = channel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await core.send_ephemeral_embed(interaction, "Private", "Only the initiator can confirm.", "error")
            return False
        return True

    @discord.ui.button(label="✅ Confirm deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        countdown = SupportDeleteCountdownView(self.ticket_number, self.actor_id, self.channel)
        await interaction.response.edit_message(
            embed=core.make_embed("Deletion Triggered", "Generating transcript and deleting in **10 seconds**.", "warning"),
            view=countdown,
        )
        countdown.task = asyncio.create_task(countdown.delete_after_delay())


class SupportDeleteCountdownView(discord.ui.View):
    def __init__(self, ticket_number: int, actor_id: int, channel: discord.TextChannel):
        super().__init__(timeout=10)
        self.ticket_number = ticket_number
        self.actor_id = actor_id
        self.channel = channel
        self.task: Optional[asyncio.Task] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id: return False
        return True

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.success)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.task and not self.task.done():
            self.task.cancel()
        await interaction.response.edit_message(
            embed=core.make_embed("Cancelled", "Deletion aborted.", "success"), view=None
        )

    async def delete_after_delay(self) -> None:
        try:
            transcript_file = await generate_transcript(self.channel)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return
            
        ticket = get_support_ticket(ticket_number=self.ticket_number)
        if not ticket: return
            
        with core.db_session() as connection:
            connection.execute(
                "UPDATE support_tickets SET status = 'deleted', deleted_at = ?, deleted_by = ? WHERE ticket_number = ?",
                (datetime.now(timezone.utc).isoformat(), self.actor_id, self.ticket_number),
            )
            
        try:
            audit_channel = await core.resolve_channel(core.AUDIT_LOG_CHANNEL_ID)
            if audit_channel:
                await audit_channel.send(
                    content=f"🗑️ Transcript for deleted ticket **#{self.ticket_number}** (Initiated by <@{self.actor_id}>)",
                    file=transcript_file
                )
        except Exception:
            pass 
            
        await self.channel.delete(reason=f"Ticket #{self.ticket_number} deleted")


class TicketReasonModal(discord.ui.Modal, title="Support ticket details"):
    reason = discord.ui.TextInput(
        label="Explain what you need help with",
        placeholder="Include the relevant details, usernames, and evidence.",
        style=discord.TextStyle.paragraph,
        min_length=10, max_length=1500,
    )
    def __init__(self, ticket_type: str):
        super().__init__()
        self.ticket_type = ticket_type

    async def on_submit(self, interaction: discord.Interaction):
        reason = str(self.reason.value).strip()
        remaining = ticket_creation_cooldown_remaining(interaction.user.id)
        if remaining:
            await core.send_ephemeral_embed(interaction, "Ticket creation cooldown", f"Please wait **{remaining} seconds** before opening another ticket.", "warning")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel, _ = await create_support_ticket(interaction, self.ticket_type, reason)
        except discord.Forbidden:
            await core.send_ephemeral_embed(interaction, "Ticket creation denied", "Discord denied channel creation. Confirm the bot has Manage Channels, View Channel, Send Messages, Embed Links, and Read Message History in the configured support category.", "error")
            return
        except discord.HTTPException as error:
            core.app.logger.exception("Could not create support ticket")
            await core.send_ephemeral_embed(interaction, "Ticket creation failed", f"Discord returned HTTP {error.status}: {error.text[:500]}", "error")
            return
        except Exception as error:
            core.app.logger.exception("Could not create support ticket")
            await core.send_ephemeral_embed(interaction, "Ticket configuration error", f"{type(error).__name__}: {error}", "error")
            return
        await core.send_ephemeral_embed(interaction, "Success", f"Ticket created: {channel.mention}", "success")


class ReportTicketModal(discord.ui.Modal, title="Report a player"):
    reported_player = discord.ui.TextInput(
        label="Player being reported",
        placeholder="Minecraft or Discord username",
        min_length=1, max_length=40,
    )
    evidence = discord.ui.TextInput(
        label="Do you have evidence? Type Yes or No",
        placeholder="Yes or No",
        min_length=2, max_length=3,
    )
    reason = discord.ui.TextInput(
        label="Explain the report",
        placeholder="Describe what happened and include relevant details.",
        style=discord.TextStyle.paragraph,
        min_length=10, max_length=1500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        player = str(self.reported_player.value).strip()
        evidence = str(self.evidence.value).strip().casefold()
        
        if evidence not in {"yes", "no"}:
            await core.send_ephemeral_embed(
                interaction, "Answer required", "Evidence must be answered exactly with **Yes** or **No**.", "warning"
            )
            return
            
        reason = f"Player reported: {player}\nEvidence available: {'Yes' if evidence == 'yes' else 'No'}\n{str(self.reason.value).strip()}"
        remaining = ticket_creation_cooldown_remaining(interaction.user.id)
        if remaining:
            await core.send_ephemeral_embed(interaction, "Ticket creation cooldown", f"Please wait **{remaining} seconds** before opening another ticket.", "warning")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            channel, number = await create_support_ticket(interaction, TicketType.REPORT.value, reason)
        except discord.Forbidden:
            await core.send_ephemeral_embed(interaction, "Ticket creation denied", "Discord denied channel creation. Confirm the bot has Manage Channels, View Channel, Send Messages, Embed Links, and Read Message History in the configured report category.", "error")
            return
        except discord.HTTPException as error:
            core.app.logger.exception("Could not create report ticket")
            await core.send_ephemeral_embed(interaction, "Ticket creation failed", f"Discord returned HTTP {error.status}: {error.text[:500]}", "error")
            return
        except Exception as error:
            core.app.logger.exception("Could not create report ticket")
            await core.send_ephemeral_embed(interaction, "Ticket configuration error", f"{type(error).__name__}: {error}", "error")
            return
            
        await core.send_ephemeral_embed(interaction, "Success", f"Report created in {channel.mention}.", "success")


class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose the type of support you need", custom_id="support-ticket:panel:type",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="General Problems Ticket", value=TicketType.GENERAL.value, emoji="🛠️", description="Server, account, or general assistance"),
                discord.SelectOption(label="Report a Player Ticket", value=TicketType.REPORT.value, emoji="🚩", description="Report harmful or suspicious player behavior"),
                discord.SelectOption(label="Giveaways", value=TicketType.GIVEAWAY.value, emoji="🎉", description="Questions about a giveaway or prize"),
                discord.SelectOption(label="Partnership Request", value=TicketType.PARTNERSHIP.value, emoji="🤝", description="Request or manage a server partnership"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        ticket_type = self.values[0]
        modal = ReportTicketModal() if ticket_type == TicketType.REPORT.value else TicketReasonModal(ticket_type)
        await interaction.response.send_modal(modal)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())


class Ticket(commands.GroupCog, group_name="ticket", group_description="Create and manage private support tickets"):
    
    @app_commands.command(name="create", description="Post the private support ticket panel.")
    @app_commands.default_permissions(administrator=True)
    async def create(self, interaction: discord.Interaction):
        channel = await core.resolve_channel(core.SUPPORT_TICKET_PANEL_CHANNEL_ID)
        embed = core.make_embed(
            "DonutSMP Essentials Support Center",
            "Choose the category that best describes your issue. You will be asked for a clear explanation before a private ticket is opened.",
            "info",
        )
        await channel.send(embed=embed, view=TicketPanelView())
        await core.send_ephemeral_embed(interaction, "Success", f"Panel live in {channel.mention}.", "success")

    @app_commands.command(name="archive", description="Generate a text transcript of this ticket immediately.")
    async def archive(self, interaction: discord.Interaction):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff Access Required", "Only support staff can use this command.", "error")
            return
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if ticket:
            core.touch_support_ticket(ticket["ticket_number"])
        await interaction.response.defer(ephemeral=False)
        transcript = await generate_transcript(interaction.channel)
        await interaction.followup.send(content="📄 **Transcript Generated**", file=transcript)

    @app_commands.command(name="transfer", description="Transfer a claimed ticket to another staff member.")
    async def transfer(self, interaction: discord.Interaction, staff_member: discord.Member):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff Access Required", "Only staff can transfer tickets.", "error")
            return
            
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Not Found", "Use this command inside a ticket.", "warning")
            return
            
        with core.db_session() as connection:
            connection.execute("UPDATE support_tickets SET claimed_by = ?, last_activity_at = ? WHERE ticket_number = ?", (staff_member.id, datetime.now(timezone.utc).isoformat(), ticket["ticket_number"]))
            
        updated = get_support_ticket(ticket_number=ticket["ticket_number"])
        await interaction.channel.set_permissions(staff_member, view_channel=True, send_messages=True)
        await interaction.response.send_message(f"🔄 Ticket transferred to {staff_member.mention}.")
        await update_ticket_message(updated, interaction.channel)

    @app_commands.command(name="claim", description="Claim the support ticket in this channel.")
    async def claim(self, interaction: discord.Interaction):
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Ticket not found", "Use this command inside a support ticket.", "warning")
            return
            
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only configured support roles and administrators can claim tickets.", "error")
            return
            
        with core.db_session() as connection:
            connection.execute("UPDATE support_tickets SET claimed_by = ?, last_activity_at = ? WHERE ticket_number = ?", (interaction.user.id, datetime.now(timezone.utc).isoformat(), ticket["ticket_number"]))
            
        await core.send_audit_event(interaction.guild, "support_ticket_claimed", interaction.user, interaction.channel, f"Claimed support ticket #{ticket['ticket_number']}.")
        await core.send_ephemeral_embed(interaction, "Ticket claimed", "You are now assigned to this support ticket.", "success")
        updated = get_support_ticket(ticket_number=ticket["ticket_number"])
        await update_ticket_message(updated, interaction.channel)

    @app_commands.command(name="close", description="Close the support ticket in this channel.")
    async def close(self, interaction: discord.Interaction):
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket or not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only support staff can close support tickets.", "error")
            return
        await close_support_ticket(interaction, ticket["ticket_number"], interaction.user)

    @app_commands.command(name="reopen", description="Reopen a previously closed support ticket.")
    async def reopen(self, interaction: discord.Interaction):
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket or not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only support staff can reopen support tickets.", "error")
            return
            
        if ticket["status"] != TicketStatus.CLOSED.value:
            await core.send_ephemeral_embed(interaction, "Cannot reopen", "Only closed tickets can be reopened.", "warning")
            return
            
        await reopen_support_ticket(interaction, ticket)

    @app_commands.command(name="delete", description="Permanently delete this support ticket after confirmation (administrator only).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def delete(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await core.send_ephemeral_embed(interaction, "Administrator access required", "Permanent deletion is restricted to administrators.", "error")
            return
            
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Ticket not found", "Use this command inside a support ticket.", "warning")
            return
            
        await interaction.response.send_message(
            embed=core.make_embed("Confirm ticket deletion", "This will permanently delete the ticket channel. Confirm to start a 10-second countdown.", "warning"),
            view=SupportDeleteConfirmView(ticket["ticket_number"], interaction.user.id, interaction.channel),
            ephemeral=True,
        )

    @app_commands.command(name="active", description="Show open support tickets awaiting staff attention.")
    async def active(self, interaction: discord.Interaction):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only support staff can view the ticket queue.", "error")
            return
            
        with core.db_session() as connection:
            rows = connection.execute(
                "SELECT * FROM support_tickets WHERE guild_id = ? AND status = 'open' ORDER BY last_activity_at LIMIT 20", 
                (interaction.guild_id,)
            ).fetchall()
            
        if not rows:
            description = "No open support tickets."
        else:
            description = "\n".join(
                f"**#{row['ticket_number']}** • <#{row['channel_id']}> • {TICKET_LABELS.get(row['ticket_type'], 'Ticket')} • {('Claimed by <@' + str(row['claimed_by']) + '>') if row['claimed_by'] else 'Unclaimed'} • last activity `{(row['last_activity_at'] or row['created_at'])[:16].replace('T', ' ')}` UTC"
                for row in rows
            )
            
        await interaction.response.send_message(embed=core.make_embed("Support queue", description, "info"), ephemeral=True)

    @app_commands.command(name="priority", description="Set the priority of the support ticket in this channel.")
    @app_commands.describe(level="Ticket priority")
    @app_commands.choices(level=[
        app_commands.Choice(name="Low", value=TicketPriority.LOW.value),
        app_commands.Choice(name="Normal", value=TicketPriority.NORMAL.value),
        app_commands.Choice(name="High", value=TicketPriority.HIGH.value),
        app_commands.Choice(name="Urgent", value=TicketPriority.URGENT.value),
    ])
    async def priority(self, interaction: discord.Interaction, level: app_commands.Choice[str]):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only configured support staff can manage ticket priority.", "error")
            return
            
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Ticket not found", "Use this command inside a support ticket.", "warning")
            return
            
        with core.db_session() as connection:
            connection.execute("UPDATE support_tickets SET priority = ?, last_activity_at = ? WHERE ticket_number = ?", (level.value, datetime.now(timezone.utc).isoformat(), ticket["ticket_number"]))
            
        updated = get_support_ticket(ticket_number=ticket["ticket_number"])
        await core.send_audit_event(interaction.guild, "support_ticket_priority_changed", interaction.user, interaction.channel, f"Set ticket #{ticket['ticket_number']} priority to {level.value}.")
        await update_ticket_message(updated, interaction.channel)
        await interaction.response.send_message("✅ Priority updated.", ephemeral=True)

    @app_commands.command(name="add-member", description="Add a member to this support ticket.")
    async def add_member(self, interaction: discord.Interaction, member: discord.Member):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only configured support staff can manage ticket priority and members.", "error")
            return
            
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Ticket not found", "Use this command inside a support ticket.", "warning")
            return
            
        try:
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, reason=f"Added to support ticket #{ticket['ticket_number']}")
        except discord.Forbidden:
            await core.send_ephemeral_embed(interaction, "Could not add member", "The bot cannot update this ticket's permissions.", "error")
            return
            
        await core.send_audit_event(interaction.guild, "support_ticket_member_added", interaction.user, interaction.channel, f"Added {member} to ticket #{ticket['ticket_number']}.")
        core.touch_support_ticket(ticket["ticket_number"])
        await core.send_ephemeral_embed(interaction, "Member added", f"{member.mention} can now access this ticket.", "success")

    @app_commands.command(name="remove-member", description="Remove a member from this support ticket.")
    async def remove_member(self, interaction: discord.Interaction, member: discord.Member):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only configured support staff can manage ticket members.", "error")
            return
            
        ticket = get_support_ticket(channel_id=interaction.channel_id, guild_id=interaction.guild_id)
        if not ticket:
            await core.send_ephemeral_embed(interaction, "Ticket not found", "Use this command inside a support ticket.", "warning")
            return
            
        if member.id == ticket["creator_id"]:
            await core.send_ephemeral_embed(interaction, "Cannot remove creator", "The member who opened the ticket must retain access.", "warning")
            return
            
        try:
            await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed from support ticket #{ticket['ticket_number']}")
        except discord.Forbidden:
            await core.send_ephemeral_embed(interaction, "Could not remove member", "The bot cannot update this ticket's permissions.", "error")
            return
            
        await core.send_audit_event(interaction.guild, "support_ticket_member_removed", interaction.user, interaction.channel, f"Removed {member} from ticket #{ticket['ticket_number']}.")
        core.touch_support_ticket(ticket["ticket_number"])
        await core.send_ephemeral_embed(interaction, "Member removed", f"{member.mention} no longer has explicit access to this ticket.", "success")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ticket())