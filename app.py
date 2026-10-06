import asyncio
import html
import hmac
import io
import json
import re
import secrets
import sys
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, jsonify, request

import config
import database

from config import *

sys.modules.setdefault("main", sys.modules[__name__])
discord_loop = None
bot_started_at = None
last_database_backup_at = None
error_counts = {}
error_alerts = {}
invite_cache = {}

app = Flask(__name__)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


def make_embed(title, description, tone="info"):
    colors = {
        "info": discord.Color.from_rgb(72, 102, 224),
        "success": discord.Color.from_rgb(46, 160, 105),
        "warning": discord.Color.from_rgb(225, 151, 45),
        "error": discord.Color.from_rgb(204, 70, 80),
    }
    icons = {"info": "✨", "success": "✅", "warning": "⚠️", "error": "❌"}
    embed = discord.Embed(
        title=f"{icons[tone]} {title}",
        description=description,
        color=colors[tone],
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{BOT_NAME} • here to help")
    if bot.user:
        embed.set_author(name=BOT_NAME, icon_url=bot.user.display_avatar.url)
    return embed


async def send_ephemeral_embed(interaction, title, description, tone="info", view=None):
    kwargs = {"embed": make_embed(title, description, tone), "ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    return await interaction.response.send_message(**kwargs)


def format_uptime(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


async def resolve_selected_member(guild, user_id):
    member = guild.get_member(user_id)
    if member is None:
        member = await guild.fetch_member(user_id)
    return member


async def refresh_invite_cache(guild):
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        app.logger.warning("Could not read invites for guild %s", guild.id)
        return
    invite_cache[guild.id] = {
        invite.code: invite.uses or 0
        for invite in invites
    }


async def record_invite_use(member):
    try:
        invites = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        app.logger.warning("Could not identify invite used by member %s", member.id)
        return
    previous = invite_cache.get(member.guild.id, {})
    current = {invite.code: invite.uses or 0 for invite in invites}
    increased = [
        invite for invite in invites
        if (invite.uses or 0) > previous.get(invite.code, 0)
    ]
    used_invite = max(increased, key=lambda invite: invite.uses or 0, default=None)
    inviter_id = used_invite.inviter.id if used_invite and used_invite.inviter else None
    invite_code = used_invite.code if used_invite else None
    with db_session() as connection:
        connection.execute(
            """INSERT OR IGNORE INTO invite_uses
               (guild_id, joined_user_id, inviter_id, invite_code, joined_at)
               VALUES (?, ?, ?, ?, ?)""",
            (member.guild.id, member.id, inviter_id, invite_code, datetime.now(timezone.utc).isoformat()),
        )
    invite_cache[member.guild.id] = current


async def resolve_middleman_member(guild, user_id, configured_name):
    try:
        return await resolve_selected_member(guild, user_id)
    except discord.NotFound:
        expected = configured_name.casefold()
        member = discord.utils.find(
            lambda candidate: candidate.name.casefold() == expected
            or (candidate.global_name and candidate.global_name.casefold() == expected),
            guild.members,
        )
        if member is not None:
            app.logger.warning(
                "Configured middleman ID %s resolved by username %s as member %s",
                user_id,
                configured_name,
                member.id,
            )
            return member
        raise


def get_bot_setting(guild_id, setting, default=None):
    if guild_id is None:
        return default
    with db_session() as connection:
        row = connection.execute(
            "SELECT value FROM bot_settings WHERE guild_id = ? AND setting = ?",
            (guild_id, setting),
        ).fetchone()
    if row is None:
        return default
    return row["value"]


def set_bot_setting(guild_id, setting, value):
    if guild_id is None:
        return value
    value = str(value)
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO bot_settings (guild_id, setting, value)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, setting) DO UPDATE SET value = excluded.value
            """,
            (guild_id, setting, value),
        )
    return value


# Keep the public helpers imported by command cogs stable while the database
# implementation lives in its own module.
def connect_db():
    return database.connect_db(DATABASE_PATH)


def db_session():
    return database.db_session(DATABASE_PATH)


def initialize_db():
    database.initialize_db(DATABASE_PATH)


def backup_database():
    database.backup_database(DATABASE_PATH, DATABASE_BACKUP_PATH)


def touch_support_ticket(ticket_number, when=None):
    timestamp = when or datetime.now(timezone.utc).isoformat()
    with db_session() as connection:
        connection.execute(
            "UPDATE support_tickets SET last_activity_at = ? WHERE ticket_number = ?",
            (timestamp, ticket_number),
        )
    return timestamp


def is_designated_staff(member):
    return bool(
        member
        and (
            member.guild_permissions.administrator
            or any(role.id in (SUPPORT_ROLE_IDS or ()) for role in getattr(member, "roles", ()))
        )
    )


def is_marketplace_manager(member):
    return bool(
        member
        and (
            member.guild_permissions.administrator
            or any(role.id == MARKETPLACE_MANAGER_ROLE_ID for role in getattr(member, "roles", ()))
        )
    )


def record_audit_event(guild_id, action, actor_id=None, target_id=None, details="", *, category=None, severity="info", metadata=None, request_id=None):
    created_at = datetime.now(timezone.utc).isoformat()
    with db_session() as connection:
        connection.execute(
                """INSERT INTO audit_events
                    (guild_id, actor_id, action, target_id, details, created_at, category, severity, metadata_json, request_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, actor_id, action, str(target_id) if target_id is not None else None, details[:2000], created_at, category or action.split("_", 1)[0], severity, json.dumps(metadata or {}, sort_keys=True)[:4000], request_id),
        )
    return created_at


async def send_audit_event(guild, action, actor=None, target=None, details=""):
    guild_id = getattr(guild, "id", None)
    actor_id = getattr(actor, "id", actor) if actor is not None else None
    target_id = getattr(target, "id", target) if target is not None else None
    severity = "warning" if action in {
        "member_banned", "member_kicked", "member_timed_out", "channel_deleted",
        "support_ticket_auto_closed", "middleman_payment_mismatch",
    } else "info"
    metadata = {
        "guild_id": guild_id,
        "guild_name": getattr(guild, "name", None),
        "actor_id": actor_id,
        "actor_name": getattr(actor, "display_name", getattr(actor, "name", None)),
        "target_id": target_id,
        "target_type": type(target).__name__ if target is not None else None,
        "target_name": getattr(target, "name", getattr(target, "display_name", None)),
    }
    try:
        record_audit_event(
            guild_id,
            action,
            actor_id,
            target_id,
            details,
            severity=severity,
            metadata=metadata,
        )
    except Exception:
        app.logger.exception("Could not persist audit event %s", action)
    try:
        channel = await resolve_channel(AUDIT_LOG_CHANNEL_ID)
        embed = make_embed(
            f"Audit • {action}",
            details or "No additional details recorded.",
            "warning" if action in {"member_banned", "member_kicked", "member_timed_out", "channel_deleted"} else "info",
        )
        if actor_id:
            embed.add_field(name="Actor", value=f"<@{actor_id}>", inline=True)
        if target_id:
            embed.add_field(name="Target", value=str(target_id), inline=True)
        embed.add_field(name="Category", value=action.split("_", 1)[0].title(), inline=True)
        embed.add_field(name="Severity", value=severity.title(), inline=True)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        app.logger.exception("Could not publish audit event %s", action)


async def send_error_alert(title, details):
    key = f"{title}:{details[:500]}"
    now = time.monotonic()
    if now - error_alerts.get(key, 0) < 300:
        return
    error_alerts[key] = now
    try:
        channel = await resolve_channel(STAFF_DASHBOARD_CHANNEL_ID)
        await channel.send(
            embed=make_embed(title, details[:4000], "error"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        app.logger.exception("Could not publish error alert")


def allocate_support_ticket_number(guild_id):
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO support_ticket_counters (guild_id, last_number) VALUES (?, 0)",
            (guild_id,),
        )
        row = connection.execute(
            "SELECT last_number FROM support_ticket_counters WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        number = row["last_number"] + 1
        connection.execute(
            "UPDATE support_ticket_counters SET last_number = ? WHERE guild_id = ?",
            (number, guild_id),
        )
    return number


def member_reputation_summary(guild_id, member_id):
    with db_session() as connection:
        vouches = connection.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN verdict = 'legit' THEN 1 ELSE 0 END) AS legit,
                      SUM(CASE WHEN verdict = 'scammer' THEN 1 ELSE 0 END) AS scammer,
                      AVG(stars) AS average
               FROM player_vouches WHERE guild_id = ? AND target_id = ?""",
            (guild_id, member_id),
        ).fetchone()
        completed = connection.execute(
            """SELECT COUNT(*) AS total FROM tickets
               WHERE guild_id = ? AND status = 'completed' AND (seller_id = ? OR buyer_id = ?)""",
            (guild_id, member_id, member_id),
        ).fetchone()["total"]
        warnings = connection.execute(
            "SELECT COUNT(*) AS total FROM staff_warnings WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, member_id),
        ).fetchone()["total"]
    return {
        "vouches": vouches["total"] or 0,
        "legit": vouches["legit"] or 0,
        "scammer": vouches["scammer"] or 0,
        "average": float(vouches["average"] or 0),
        "completed_deals": completed,
        "active_warnings": warnings,
    }


def match_transaction_to_tickets(event_id, from_player, to_player, amount, received_at):
    matches = []
    with db_session() as connection:
        tickets = connection.execute(
            """SELECT ticket_number, buyer_ign, seller_ign, amount
               FROM tickets WHERE status = 'open' AND amount IS NOT NULL"""
        ).fetchall()
        for ticket in tickets:
            players = {str(ticket["buyer_ign"] or "").casefold(), str(ticket["seller_ign"] or "").casefold()}
            event_players = {str(from_player or "").casefold(), str(to_player or "").casefold()}
            if not players.intersection(event_players):
                continue
            try:
                same_amount = Decimal(str(ticket["amount"])) == Decimal(str(amount))
            except (InvalidOperation, ValueError):
                same_amount = False
            status = "matched" if same_amount else "mismatch"
            reason = "Player and amount matched an open deal." if same_amount else "Player matched an open deal, but the amount differs."
            connection.execute(
                """INSERT OR IGNORE INTO transaction_matches
                   (event_id, ticket_number, match_status, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, ticket["ticket_number"], status, reason, received_at),
            )
            matches.append((ticket["ticket_number"], status, reason))
    return matches


def get_ticket(ticket_number=None, channel_id=None):
    if ticket_number is None and channel_id is None:
        return None
    column, value = ("ticket_number", ticket_number) if ticket_number is not None else (
        "channel_id",
        channel_id,
    )
    with db_session() as connection:
        row = connection.execute(
            f"SELECT * FROM tickets WHERE {column} = ?", (value,)
        ).fetchone()
    return dict(row) if row else None


def update_ticket(ticket_number, **values):
    allowed = {
        "seller_ign",
        "buyer_ign",
        "amount",
        "pending_amount",
        "price_proposed_by",
        "price_confirmed",
        "payment_reported",
        "payment_verified",
        "middleman_confirmed",
        "money_received",
        "item_delivered",
        "delivery_confirmed",
        "funds_released",
        "close_seller_confirmed",
        "close_buyer_confirmed",
        "status",
    }
    if not values or not set(values).issubset(allowed):
        raise ValueError("Unsupported ticket update")
    values = {**values, "activity_at": datetime.now(timezone.utc).isoformat()}
    assignments = ", ".join(f"{column} = ?" for column in values)
    with db_session() as connection:
        connection.execute(
            f"UPDATE tickets SET {assignments} WHERE ticket_number = ?",
            (*values.values(), ticket_number),
        )
    return get_ticket(ticket_number=ticket_number)


def allocate_ticket_number(existing_numbers=()):
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT last_number FROM ticket_counter WHERE id = 1"
        ).fetchone()
        number = max(row[0], max(existing_numbers, default=0)) + 1
        connection.execute(
            "UPDATE ticket_counter SET last_number = ? WHERE id = 1", (number,)
        )
    return number


def allocate_deal_code():
    alphabet = string.ascii_uppercase
    with db_session() as connection:
        for _ in range(100):
            code = f"{''.join(secrets.choice(alphabet) for _ in range(3))}-{secrets.randbelow(1000):03d}"
            if connection.execute("SELECT 1 FROM tickets WHERE deal_code = ?", (code,)).fetchone() is None:
                return code
    raise RuntimeError("Could not allocate a unique deal code")


def parse_coin_amount(value):
    number_pattern = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?"
    match = re.fullmatch(rf"\s*({number_pattern})\s*([kmbt]?)\s*", value, re.IGNORECASE)
    if not match:
        raise ValueError("Enter a number optionally suffixed with k, m, b, or t")
    number = Decimal(match.group(1).replace(",", ""))
    multipliers = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}
    amount = number * multipliers[match.group(2).lower()]
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Price must be finite and greater than zero")
    return format(amount, "f")


def format_coin_amount(value):
    amount = Decimal(str(value))
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def middleman_next_action(ticket):
    if ticket["status"] != "open":
        return "No action required", "The deal is closed."
    if ticket["pending_amount"]:
        actor = ticket["buyer_id"] if ticket["price_proposed_by"] == ticket["seller_id"] else ticket["seller_id"]
        return f"<@{actor}> must approve or decline the proposed price", "The other trader proposed a price change."
    if not ticket["seller_ign"]:
        return f"<@{ticket['seller_id']}> must confirm their Minecraft IGN", "The seller's IGN is missing."
    if not ticket["buyer_ign"]:
        return f"<@{ticket['buyer_id']}> must confirm their Minecraft IGN", "The buyer's IGN is missing."
    if not ticket["price_confirmed"]:
        actor = ticket["buyer_id"] if ticket["price_proposed_by"] == ticket["seller_id"] else ticket["seller_id"]
        return f"<@{actor}> must confirm the agreed price", "Both traders must agree before payment can be reported."
    if not ticket["middleman_confirmed"]:
        return f"<@{ticket['middleman_id']}> must confirm the deal", "The agreed price is ready; the middleman must accept the deal."
    if not ticket["money_received"]:
        return f"<@{ticket['buyer_id']}> must send money to <@{ticket['middleman_id']}>", "The middleman accepted the deal."
    if not ticket["payment_reported"]:
        return f"<@{ticket['buyer_id']}> must report payment", "The agreed price is confirmed."
    if not ticket["payment_verified"] and not ticket["money_received"]:
        return "Support must verify payment in-game", "The buyer reported payment, but support has not verified it."
    if not ticket["item_delivered"]:
        return f"<@{ticket['seller_id']}> must mark the item delivered", "Payment is verified by support."
    if not ticket["delivery_confirmed"]:
        return f"<@{ticket['buyer_id']}> must confirm item receipt", "The seller marked the item delivered."
    if not ticket["funds_released"]:
        return f"<@{ticket['middleman_id']}> must pay the seller", "The buyer confirmed delivery; the middleman must transfer the agreed price in-game."
    if not ticket["close_seller_confirmed"]:
        return f"<@{ticket['seller_id']}> must confirm closure", "The manual release is recorded."
    if not ticket["close_buyer_confirmed"]:
        return f"<@{ticket['buyer_id']}> must confirm closure", "The seller has confirmed closure."
    return "Support must archive and close the deal", "Both traders confirmed closure."


def ticket_embed(ticket):
    status = ticket["status"]
    color = "success" if status == "completed" else "error" if status in {"cancelled", "closed"} else "info"
    deal_label = ticket.get("deal_code") or f"#{ticket['ticket_number']}"
    embed = make_embed(
        f"Middleman deal {deal_label}",
        "Both traders: use **🪪 Confirm IGN** to record your Minecraft Java username. "
        "The other trader must confirm the setup price before payment can be reported. "
        "Price changes and closing this deal require both participants' consent.",
        color,
    )
    embed.add_field(
        name="👥 Participants",
        value=f"🛍️ Seller: <@{ticket['seller_id']}>\n🛒 Buyer: <@{ticket['buyer_id']}>",
        inline=False,
    )
    if ticket.get("middleman_id"):
        embed.add_field(name="🧑‍⚖️ Assigned middleman", value=f"<@{ticket['middleman_id']}>", inline=False)
    next_action, blocked_by = middleman_next_action(ticket)
    embed.add_field(
        name="🚦 Next required action",
        value=f"**{next_action}**\nBlocked by: {blocked_by}",
        inline=False,
    )
    embed.add_field(name="🪪 Seller IGN", value=ticket["seller_ign"] or "⏳ Awaiting confirmation")
    embed.add_field(name="🪪 Buyer IGN", value=ticket["buyer_ign"] or "⏳ Awaiting confirmation")
    agreed_price = f"{format_coin_amount(ticket['amount'])} coins" if ticket["amount"] else "Not set"
    if ticket["pending_amount"]:
        agreed_price += f"\n🟠 Proposed: {format_coin_amount(ticket['pending_amount'])} coins (awaiting the other trader)"
    if ticket["price_confirmed"]:
        price_status = "✅ Both traders agreed"
    else:
        price_status = "⏳ Waiting for the other trader to confirm"
    embed.add_field(name="💰 Agreed price", value=f"{agreed_price}\n{price_status}")
    middleman_id = ticket.get("middleman_id")
    embed.add_field(
        name="🧑‍⚖️ Middleman",
        value=f"<@{middleman_id}>" if middleman_id else "Not assigned",
        inline=False,
    )

    if ticket["payment_verified"]:
        payment = "✅ Verified in-game by support"
    elif ticket["money_received"]:
        payment = "✅ Middleman confirmed the money is received"
    elif ticket["payment_reported"]:
        payment = "🟠 Buyer reports sent; support verification required"
    else:
        payment = "⏳ Waiting for buyer payment report"
    embed.add_field(name="💸 Payment to middleman", value=payment, inline=False)

    if ticket["delivery_confirmed"]:
        delivery = "✅ Buyer confirmed receipt"
    elif ticket["item_delivered"]:
        delivery = "📦 Seller marked delivered; waiting for buyer"
    else:
        delivery = "⏳ Waiting for seller to mark delivery"
    embed.add_field(name="📦 Item delivery", value=delivery, inline=False)
    embed.add_field(
        name="🤝 Close deal",
        value=f"Seller: {'✅' if ticket['close_seller_confirmed'] else '⏳'}  •  Buyer: {'✅' if ticket['close_buyer_confirmed'] else '⏳'}",
        inline=False,
    )
    embed.add_field(
        name="🔐 Manual release",
        value=(
            "✅ Middleman recorded payment to the seller"
            if ticket["funds_released"]
            else "Not recorded • the middleman must pay the seller in-game"
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"{status.replace('_', ' ').title()} • Support verifies and transfers coins in-game; the bot never holds funds."
    )
    return embed


async def refresh_ticket_message(ticket_number):
    ticket = get_ticket(ticket_number=ticket_number)
    if not ticket:
        return
    channel = bot.get_channel(ticket["channel_id"])
    if channel is None:
        channel = await bot.fetch_channel(ticket["channel_id"])
    if ticket["status_message_id"]:
        message = await channel.fetch_message(ticket["status_message_id"])
        await message.edit(embed=ticket_embed(ticket), view=TicketControls(ticket_number))


def is_support(member):
    return is_designated_staff(member) or any(role.id == SUPPORT_ROLE_ID for role in member.roles)


def resolve_middleman_support_role(guild):
    role = guild.get_role(SUPPORT_ROLE_ID)
    if role is not None:
        return role
    for role_id in SUPPORT_ROLE_IDS or ():
        role = guild.get_role(role_id)
        if role is not None:
            app.logger.warning(
                "Configured middleman support role %s is missing; using support role %s instead",
                SUPPORT_ROLE_ID,
                role.id,
            )
            return role
    return None


def is_party(ticket, user_id):
    return user_id in (ticket["seller_id"], ticket["buyer_id"])


async def close_ticket_channel(ticket, channel=None):
    ticket_number = ticket["ticket_number"]
    if channel is None:
        channel = await resolve_channel(ticket["channel_id"])
    if ticket["status"] != "closed":
        await channel.send(
            embed=make_embed(
                "Deal closed",
                f"Ticket **#{ticket_number}** was closed. This channel is now read-only.",
                "warning",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        update_ticket(ticket_number, status="closed")
    guild = channel.guild
    for user_id in {ticket["seller_id"], ticket["buyer_id"]}:
        if not user_id or guild is None:
            continue
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                continue
        await channel.set_permissions(member, send_messages=False)
    await channel.edit(
        name=f"closed-deal-{ticket_number}",
        topic=f"Closed Deal ({ticket_number})",
    )
    current_ticket = get_ticket(ticket_number=ticket_number)
    if not current_ticket["transcript_sent_at"]:
        try:
            await send_ticket_transcript(current_ticket, channel)
            with db_session() as connection:
                connection.execute(
                    "UPDATE tickets SET transcript_sent_at = ? WHERE ticket_number = ?",
                    (datetime.now(timezone.utc).isoformat(), ticket_number),
                )
        except Exception:
            app.logger.exception("Could not archive transcript for ticket #%s", ticket_number)
            try:
                await channel.send(
                    embed=make_embed(
                        "Transcript archive pending",
                        "The ticket is closed, but its transcript could not be delivered. Support can retry from the staff controls.",
                        "error",
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                app.logger.exception("Could not report transcript archive failure in ticket #%s", ticket_number)
    return get_ticket(ticket_number=ticket_number)


async def send_order_summary(ticket):
    channel = await resolve_channel(MIDDLEMAN_ORDERS_CHANNEL_ID)
    deal_label = ticket.get("deal_code") or f"#{ticket['ticket_number']}"
    embed = make_embed(
        f"Order finished • {deal_label}",
        "All required confirmations were completed and the middleman recorded payment to the seller.",
        "success",
    )
    embed.add_field(name="Order ID", value=f"`{ticket['ticket_number']}`", inline=True)
    embed.add_field(name="Agreed price", value=f"{format_coin_amount(ticket['amount'])} coins", inline=True)
    embed.add_field(name="Seller", value=f"<@{ticket['seller_id']}>", inline=True)
    embed.add_field(name="Buyer", value=f"<@{ticket['buyer_id']}>", inline=True)
    embed.add_field(name="Middleman", value=f"<@{ticket['middleman_id']}>", inline=True)
    embed.add_field(name="Minecraft IGNs", value=f"Seller: `{ticket['seller_ign']}`\nBuyer: `{ticket['buyer_ign']}`", inline=False)
    embed.add_field(
        name="Completed checks",
        value="✅ Price agreed\n✅ Middleman confirmed\n✅ Money received\n✅ Delivery confirmed\n✅ Seller paid\n✅ Buyer and seller finished",
        inline=False,
    )
    embed.set_footer(text=f"DonutSMP Essentials • Deal #{ticket['ticket_number']}")
    await channel.send(
        content=f"<@{ticket['seller_id']}> <@{ticket['buyer_id']}> <@{ticket['middleman_id']}>",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            users=[
                discord.Object(id=ticket["seller_id"]),
                discord.Object(id=ticket["buyer_id"]),
                discord.Object(id=ticket["middleman_id"]),
            ]
        ),
    )


async def send_ticket_transcript(ticket, channel):
    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        author = html.escape(getattr(message.author, "display_name", str(message.author)))
        timestamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts = [f"<header><strong>{author}</strong> <time>{timestamp}</time></header>"]
        if message.clean_content:
            content = html.escape(message.clean_content).replace("\n", "<br>")
            parts.append(f"<p>{content}</p>")
        for embed in message.embeds:
            if embed.title:
                parts.append(f"<h3>{html.escape(embed.title)}</h3>")
            if embed.description:
                description = html.escape(embed.description).replace("\n", "<br>")
                parts.append(f"<p>{description}</p>")
            for field in embed.fields:
                parts.append(
                    f"<p><strong>{html.escape(field.name)}</strong><br>{html.escape(field.value).replace(chr(10), '<br>')}</p>"
                )
        for attachment in message.attachments:
            url = html.escape(attachment.url, quote=True)
            filename = html.escape(attachment.filename)
            parts.append(f'<p>Attachment: <a href="{url}">{filename}</a></p>')
        messages.append(f"<article>{''.join(parts)}</article>")

    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Middleman ticket transcript</title><style>
body{font:15px system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 20px;color:#20252b;background:#f4f6f8}
h1{font-size:24px}article{background:white;border:1px solid #d9dee5;border-radius:6px;margin:12px 0;padding:14px;overflow-wrap:anywhere}
header{color:#506070}time{margin-left:8px;font-size:12px}p{white-space:normal}
</style></head><body>
""" + f"<h1>Middleman deal #{ticket['ticket_number']} transcript</h1>" + "".join(messages) + "</body></html>"
    transcript_channel = await resolve_channel(TRANSCRIPT_CHANNEL_ID)
    filename = f"middleman-deal-{ticket['ticket_number']}.html"
    await transcript_channel.send(
        embed=make_embed(
            f"Transcript • Deal #{ticket['ticket_number']}",
            f"Archived {len(messages)} messages from <#{ticket['channel_id']}>.",
            "success",
        ),
        file=discord.File(io.BytesIO(document.encode("utf-8")), filename=filename),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def log_ticket_event(channel, ticket_number, title, description, tone="info"):
    await channel.send(
        embed=make_embed(f"Deal #{ticket_number} • {title}", description, tone),
        allowed_mentions=discord.AllowedMentions.none(),
    )


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction, error, item):
        app.logger.exception(
            "Middleman panel interaction failed for %s",
            getattr(item, "custom_id", type(item).__name__),
            exc_info=error,
        )
        message = (
            "The bot could not open the deal form. Please try again in a moment. "
            "If it keeps happening, contact staff."
        )
        try:
            await send_ephemeral_embed(interaction, "Could not start deal", message, "error")
        except discord.HTTPException:
            app.logger.exception("Could not report middleman panel interaction failure")

    @discord.ui.button(
        label="🤝 Start a deal",
        style=discord.ButtonStyle.primary,
        custom_id="middleman:panel:create",
    )
    async def create_ticket(self, interaction, button):
        await interaction.response.send_modal(DealSetupModal(interaction.guild))

    @discord.ui.button(
        label="📖 How it works",
        style=discord.ButtonStyle.secondary,
        custom_id="middleman:panel:how_it_works",
    )
    async def how_it_works(self, interaction, button):
        await send_ephemeral_embed(
            interaction,
            "How it works",
            "Choose a middleman and your buyer/seller role. A private ticket is created in the middleman category, and the selected middleman is notified. Wait for them to accept and handle the trade.",
            "info",
        )

class DealSetupModal(discord.ui.Modal, title="Start a deal"):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        self.amount = discord.ui.TextInput(
            label="Agreed price in DonutSMP coins",
            placeholder="Examples: 7000, 7k, 7m, 7b",
            min_length=1,
            max_length=32,
        )
        self.trader_select = discord.ui.UserSelect(
            custom_id="middleman:setup:trader",
            placeholder="Pick a trading partner",
            min_values=1,
            max_values=1,
            required=True,
        )
        middlemen = [
            member for member_id in MIDDLEMAN_IDS
            if (member := guild.get_member(member_id)) is not None and not member.bot
        ]
        self.middleman_select = discord.ui.Select(
            custom_id="middleman:setup:middleman",
            placeholder="Pick a middleman",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                )
                for member in middlemen[:25]
            ],
        )
        self.role_select = discord.ui.Select(
            custom_id="middleman:setup:role",
            placeholder="Choose your role",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="I am the buyer", value="buyer", emoji="🛒"),
                discord.SelectOption(label="I am the seller", value="seller", emoji="🛍️"),
            ],
        )
        self.add_item(discord.ui.Label(text="Agreed price", component=self.amount))
        self.add_item(discord.ui.Label(text="Trading partner", component=self.trader_select))
        self.add_item(discord.ui.Label(text="Middleman", component=self.middleman_select))
        self.add_item(discord.ui.Label(text="Your role", component=self.role_select))

    async def on_submit(self, interaction):
        if interaction.guild is None:
            await send_ephemeral_embed(interaction, "Cannot start deal", "This form must be submitted inside a Discord server, not a direct message.", "error")
            return
        try:
            amount = parse_coin_amount(str(self.amount.value))
        except (ValueError, InvalidOperation):
            await send_ephemeral_embed(
                interaction,
                "Invalid amount",
                "Enter a positive price such as `7000`, `7k`, `7m`, or `7b`.",
                "warning",
            )
            return
        trader = self.trader_select.values[0]
        middleman_id = int(self.middleman_select.values[0])
        creator_role = self.role_select.values[0]
        if trader.id == interaction.user.id:
            await send_ephemeral_embed(interaction, "Invalid trading partner", "You cannot trade with yourself.", "warning")
            return
        if trader.bot:
            await send_ephemeral_embed(interaction, "Invalid member", "Bots cannot participate as a trading partner.", "warning")
            return
        middleman = interaction.guild.get_member(middleman_id)
        if middleman is None or middleman.id not in MIDDLEMAN_IDS or middleman.bot:
            await send_ephemeral_embed(
                interaction,
                "Invalid middleman",
                "Choose a configured middleman from the list.",
                "warning",
            )
            return
        if middleman.id == interaction.user.id:
            await send_ephemeral_embed(interaction, "Invalid middleman", "You cannot select yourself as the middleman for your own deal.", "warning")
            return
        if middleman.id == trader.id:
            await send_ephemeral_embed(interaction, "Invalid selection", "The trading partner and middleman must be different members.", "warning")
            return
        if trader.bot or middleman.bot:
            await send_ephemeral_embed(interaction, "Invalid member", "Bots cannot participate as a trading partner or middleman.", "warning")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            trader = await resolve_selected_member(interaction.guild, trader.id)
            middleman = await resolve_selected_member(interaction.guild, middleman.id)
            ticket_number = await create_deal_ticket(
                interaction,
                trader,
                middleman,
                None,
                amount,
                creator_role,
            )
        except discord.Forbidden:
            await send_ephemeral_embed(
                interaction,
                "Ticket creation permission denied",
                f"Discord denied channel creation or message sending in the configured middleman category. Required permissions: View Channel, Send Messages, Embed Links, Read Message History, Manage Channels, and Manage Permissions. Category: `{TICKET_CATEGORY_ID}`.",
                "error",
            )
            return
        except discord.HTTPException as error:
            await send_ephemeral_embed(
                interaction,
                "Discord ticket creation failed",
                f"Discord returned HTTP {error.status} while creating the ticket. No ticket was completed. Try again shortly.",
                "error",
            )
            return
        except RuntimeError as error:
            await send_ephemeral_embed(
                interaction,
                "Middleman configuration error",
                f"{error}. Contact an administrator; this is a bot configuration issue, not a user permission issue.",
                "error",
            )
            return
        except Exception:
            app.logger.exception("Could not create middleman ticket")
            await send_ephemeral_embed(
                interaction,
                "Unexpected ticket creation error",
                "The bot encountered an unexpected internal error while creating the ticket. No completed ticket was confirmed. An administrator should check the bot logs.",
                "error",
            )
            return
        ticket = get_ticket(ticket_number=ticket_number)
        await send_ephemeral_embed(
            interaction,
            "Deal started",
            f"Deal **{ticket['deal_code']}** is ready in <#{ticket['channel_id']}>. The selected middleman has been notified.",
            "success",
        )


class DealInviteView(discord.ui.View):
    def __init__(self, ticket_number):
        super().__init__(timeout=None)
        self.ticket_number = ticket_number

    async def respond(self, interaction, accepted):
        ticket = get_ticket(ticket_number=self.ticket_number)
        other_trader_id = None
        if ticket:
            other_trader_id = ticket["buyer_id"] if ticket["creator_id"] == ticket["seller_id"] else ticket["seller_id"]
        if not ticket or interaction.user.id != other_trader_id:
            await send_ephemeral_embed(interaction, "Trading partner only", "Only the selected trading partner can respond to this deal invite.", "error")
            return
        title = "Deal accepted" if accepted else "Deal declined"
        description = (
            f"<@{interaction.user.id}> accepted deal **{ticket.get('deal_code') or ticket['ticket_number']}**. The middleman can now begin the trade."
            if accepted
            else f"<@{interaction.user.id}> declined deal **{ticket.get('deal_code') or ticket['ticket_number']}**."
        )
        await interaction.response.edit_message(embed=make_embed(title, description, "success" if accepted else "warning"), view=None)
        if accepted:
            if ticket["amount"]:
                update_ticket(self.ticket_number, price_confirmed=1)
                ticket = get_ticket(ticket_number=self.ticket_number)
            status_message = await send_deal_status_message(ticket, interaction.channel)
            with db_session() as connection:
                connection.execute(
                    "UPDATE tickets SET status_message_id = ? WHERE ticket_number = ?",
                    (status_message.id, self.ticket_number),
                )
        await log_ticket_event(interaction.channel, self.ticket_number, title, description, "success" if accepted else "warning")

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="middleman:invite:accept")
    async def accept(self, interaction, button):
        await self.respond(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, custom_id="middleman:invite:decline")
    async def decline(self, interaction, button):
        await self.respond(interaction, False)


async def send_deal_status_message(ticket, channel):
    seller = channel.guild.get_member(ticket["seller_id"])
    buyer = channel.guild.get_member(ticket["buyer_id"])
    support_role = resolve_middleman_support_role(channel.guild)
    mentions = [member for member in (seller, buyer) if member is not None]
    content_parts = [member.mention for member in mentions]
    if support_role is not None:
        content_parts.append(support_role.mention)
    middleman = channel.guild.get_member(ticket["middleman_id"])
    if middleman is not None:
        content_parts.append(middleman.mention)
    return await channel.send(
        content=" ".join(content_parts),
        embed=ticket_embed(ticket),
        view=TicketControls(ticket["ticket_number"]),
        allowed_mentions=discord.AllowedMentions(
            users=mentions + ([middleman] if middleman is not None else []),
            roles=[support_role] if support_role is not None else [],
        ),
    )


async def create_deal_ticket(interaction, other_trader, middleman, creator_ign, amount, creator_role):
    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Configured middleman category is missing")
    creator = interaction.user
    if creator_role == "seller":
        seller, buyer = creator, other_trader
        seller_ign, buyer_ign = creator_ign, None
    else:
        seller, buyer = other_trader, creator
        seller_ign, buyer_ign = None, creator_ign
    bot_member = guild.get_member(bot.user.id)
    support_role = resolve_middleman_support_role(guild)
    if support_role is None:
        raise RuntimeError("Configured middleman support role is missing")
    if bot_member is None:
        raise RuntimeError("Could not resolve the bot's server member")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        seller: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        buyer: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        middleman: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    overwrites[bot_member] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        manage_channels=True,
        manage_messages=True,
    )
    overwrites[support_role] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        manage_messages=True,
    )

    existing_numbers = [
        int(match.group(1))
        for existing_channel in category.text_channels
        if (match := re.fullmatch(r"middleman-deal-(\d+)", existing_channel.name))
    ]
    ticket_number = allocate_ticket_number(existing_numbers)
    deal_code = allocate_deal_code()
    created_at = datetime.now(timezone.utc).isoformat()
    channel = await category.create_text_channel(
        name=f"middleman-deal-{ticket_number}",
        overwrites=overwrites,
        topic=f"Private middleman deal #{ticket_number}",
    )
    try:
        with db_session() as connection:
            connection.execute(
                """INSERT INTO tickets
                (ticket_number, guild_id, channel_id, creator_id, seller_id, buyer_id,
                 seller_ign, buyer_ign, amount, price_proposed_by, price_confirmed, activity_at, created_at, deal_code, middleman_id, creator_role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (
                    ticket_number,
                    guild.id,
                    channel.id,
                    creator.id,
                    seller.id,
                    buyer.id,
                    seller_ign,
                    buyer_ign,
                    amount,
                    creator.id,
                    created_at,
                    created_at,
                    deal_code,
                    middleman.id,
                    creator_role,
                ),
            )
    except Exception:
        await channel.delete(reason="Middleman deal database write failed")
        raise
    invite = await channel.send(
        content=f"{creator.mention} {other_trader.mention} {middleman.mention}",
        embed=make_embed(
            f"Deal {deal_code}",
            f"{creator.mention} opened a deal with you.\n\nAfter the middleman confirms, the buyer pays the selected middleman in-game. The seller is paid after delivery is confirmed.\n\nSelected middleman: {middleman.mention}\n\nPress **Accept** to agree, or **Decline** if you cannot take this deal.",
            "info",
        ),
        view=DealInviteView(ticket_number),
        allowed_mentions=discord.AllowedMentions(users=[creator, other_trader, middleman]),
    )
    update_ticket(ticket_number, status="open")
    return ticket_number


class IGNModal(discord.ui.Modal, title="🪪 Confirm Minecraft IGN"):
    ign = discord.ui.TextInput(
        label="Your Minecraft Java username",
        placeholder="Example: zlorbie788",
        min_length=1,
        max_length=16,
    )

    def __init__(self, ticket_number):
        super().__init__()
        self.ticket_number = ticket_number

    async def on_submit(self, interaction):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not is_party(ticket, interaction.user.id):
            await send_ephemeral_embed(interaction, "Access denied", "You are not a participant in this deal.", "error")
            return
        if ticket["status"] != "open":
            await send_ephemeral_embed(interaction, "Ticket closed", "This deal is no longer accepting changes.", "warning")
            return
        username = str(self.ign.value).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", username):
            await send_ephemeral_embed(
                interaction,
                "Invalid Minecraft IGN",
                "Enter 1-16 letters, numbers, or underscores.",
                "warning",
            )
            return
        field = "seller_ign" if interaction.user.id == ticket["seller_id"] else "buyer_ign"
        update_ticket(self.ticket_number, **{field: username})
        await send_ephemeral_embed(
            interaction, "IGN confirmed", f"Your Minecraft IGN **{username}** has been recorded.", "success"
        )
        role = "seller" if field == "seller_ign" else "buyer"
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Minecraft IGN confirmed",
            f"<@{interaction.user.id}> confirmed the {role} IGN **{username}**.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)


class PriceModal(discord.ui.Modal, title="💰 Set agreed price"):
    amount = discord.ui.TextInput(
        label="Proposed amount in DonutSMP coins",
        placeholder="Examples: 7000, 7k, 7m, 7b",
        min_length=1,
        max_length=32,
    )

    def __init__(self, ticket_number):
        super().__init__()
        self.ticket_number = ticket_number

    async def on_submit(self, interaction):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not is_party(ticket, interaction.user.id):
            await send_ephemeral_embed(interaction, "Access denied", "Only deal participants can propose a price.", "error")
            return
        try:
            amount = parse_coin_amount(str(self.amount.value))
        except (ValueError, InvalidOperation):
            await send_ephemeral_embed(
                interaction,
                "Invalid amount",
                "Enter a positive price such as `7000`, `7k`, `7m`, or `7b`.",
                "warning",
            )
            return
        if ticket["status"] != "open" or ticket["payment_reported"]:
            await send_ephemeral_embed(
                interaction, "Price locked", "Price changes are unavailable after payment is reported.", "warning"
            )
            return
        if ticket["pending_amount"]:
            await send_ephemeral_embed(
                interaction, "Price proposal pending", "Resolve the current proposal before creating another.", "warning"
            )
            return
        if not ticket["seller_ign"] or not ticket["buyer_ign"]:
            await send_ephemeral_embed(
                interaction, "Both IGNs required", "Both traders must confirm their Minecraft IGNs first.", "warning"
            )
            return
        if not ticket["price_confirmed"]:
            await send_ephemeral_embed(
                interaction, "Initial price not confirmed", "The other trader must confirm the initial price first.", "warning"
            )
            return
        other_id = ticket["buyer_id"] if interaction.user.id == ticket["seller_id"] else ticket["seller_id"]
        update_ticket(
            self.ticket_number,
            pending_amount=amount,
            price_proposed_by=interaction.user.id,
        )
        proposal = make_embed(
            "Price change proposed",
            f"<@{interaction.user.id}> proposes **{format_coin_amount(amount)}** coins (current agreed price: **{format_coin_amount(ticket['amount'])}**). The other trader must accept or reject this change.",
            "warning",
        )
        await interaction.response.send_message(
            content=f"<@{other_id}>",
            embed=proposal,
            view=PriceProposalView(self.ticket_number),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=other_id)]),
        )
        await refresh_ticket_message(self.ticket_number)


class PriceProposalView(discord.ui.View):
    def __init__(self, ticket_number):
        super().__init__(timeout=None)
        self.ticket_number = ticket_number
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                action = child.custom_id.rsplit(":", 1)[-1]
                child.custom_id = f"middleman:price:{ticket_number}:{action}"

    async def resolve(self, interaction, accept):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not ticket["pending_amount"] or ticket["status"] != "open":
            await send_ephemeral_embed(interaction, "Proposal unavailable", "This price proposal is no longer active.", "warning")
            return
        if not is_party(ticket, interaction.user.id) or interaction.user.id == ticket["price_proposed_by"]:
            await send_ephemeral_embed(
                interaction, "Other trader only", "Only the other deal participant can respond to this proposal.", "error"
            )
            return
        proposed_amount = ticket["pending_amount"]
        if accept:
            update_ticket(
                self.ticket_number,
                amount=proposed_amount,
                pending_amount=None,
                price_confirmed=1,
            )
            title, description, tone = (
                "Price change accepted",
                f"The agreed price is now **{format_coin_amount(proposed_amount)}** coins.",
                "success",
            )
        else:
            update_ticket(self.ticket_number, pending_amount=None)
            title, description, tone = (
                "Price change declined",
                f"The existing agreed price remains **{format_coin_amount(ticket['amount'])}** coins.",
                "warning",
            )
        await interaction.response.edit_message(
            embed=make_embed(title, description, tone),
            view=None,
        )
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            title,
            f"<@{interaction.user.id}> responded to the proposed price. {description}",
            tone,
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(label="✅ Accept price", style=discord.ButtonStyle.success, custom_id="middleman:price:ticket:accept")
    async def accept(self, interaction, button):
        await self.resolve(interaction, True)

    @discord.ui.button(label="✖ Decline", style=discord.ButtonStyle.danger, custom_id="middleman:price:ticket:decline")
    async def decline(self, interaction, button):
        await self.resolve(interaction, False)


class DeliveryConfirmationView(discord.ui.View):
    def __init__(self, ticket_number):
        super().__init__(timeout=None)
        self.ticket_number = ticket_number
        self.children[0].custom_id = f"middleman:{ticket_number}:public_confirm_delivery"

    @discord.ui.button(
        label="✅ Confirm delivery",
        style=discord.ButtonStyle.success,
        custom_id="middleman:public_confirm_delivery",
    )
    async def confirm(self, interaction, button):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or interaction.user.id != ticket["buyer_id"]:
            await send_ephemeral_embed(interaction, "Buyer only", "Only the buyer can confirm delivery.", "error")
            return
        if ticket["status"] != "open" or not ticket["money_received"] or not ticket["item_delivered"]:
            await send_ephemeral_embed(
                interaction,
                "Delivery confirmation unavailable",
                "The middleman must receive the money and the seller must mark the item delivered first.",
                "warning",
            )
            return
        await confirm_delivery(interaction, ticket)


async def confirm_delivery(interaction, ticket):
    if ticket["delivery_confirmed"]:
        await send_ephemeral_embed(interaction, "Already confirmed", "Delivery was already confirmed.", "info")
        return
    update_ticket(ticket["ticket_number"], delivery_confirmed=1)
    seller = interaction.guild.get_member(ticket["seller_id"])
    middleman = interaction.guild.get_member(ticket["middleman_id"])
    mentions = [member for member in (seller, middleman) if member is not None]
    await interaction.response.send_message(
        content=" ".join(member.mention for member in mentions),
        embed=make_embed(
            f"Deal #{ticket['ticket_number']} • Delivery confirmed",
            f"The buyer confirmed receiving the item. <@{ticket['middleman_id']}> may now pay **{format_coin_amount(ticket['amount'])} coins** to <@{ticket['seller_id']}> in-game, then press **Pay seller**.",
            "success",
        ),
        allowed_mentions=discord.AllowedMentions(users=mentions),
    )
    await log_ticket_event(
        interaction.channel,
        ticket["ticket_number"],
        "Delivery confirmed",
        f"Buyer <@{interaction.user.id}> confirmed receipt. The middleman must now pay the seller.",
        "success",
    )
    await refresh_ticket_message(ticket["ticket_number"])


class TicketControls(discord.ui.View):
    def __init__(self, ticket_number):
        super().__init__(timeout=None)
        self.ticket_number = ticket_number
        ticket = get_ticket(ticket_number=ticket_number)
        if ticket and ticket["status"] != "open":
            for child in self.children:
                child.disabled = True
        elif ticket:
            for child in self.children:
                if child.custom_id == "middleman:ticket:agree_price" and ticket["price_confirmed"]:
                    child.disabled = True
                if child.custom_id == "middleman:ticket:agree_price" and ticket["pending_amount"]:
                    child.disabled = True
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                action = child.custom_id.rsplit(":", 1)[-1]
                child.custom_id = f"middleman:{ticket_number}:{action}"

    async def get_participant_ticket(self, interaction):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not is_party(ticket, interaction.user.id):
            await send_ephemeral_embed(
                interaction, "Access denied", "Only the two deal participants can use this control.", "error"
            )
            return None
        if ticket["status"] != "open":
            await send_ephemeral_embed(interaction, "Ticket closed", "This deal is no longer open.", "warning")
            return None
        return ticket

    @discord.ui.button(
        label="🪪 Confirm IGN",
        style=discord.ButtonStyle.primary,
        custom_id="middleman:ticket:ign",
        row=0,
    )
    async def confirm_ign(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if ticket:
            await interaction.response.send_modal(IGNModal(self.ticket_number))

    @discord.ui.button(
        label="💰 Propose price change",
        style=discord.ButtonStyle.secondary,
        custom_id="middleman:ticket:price",
        row=0,
    )
    async def set_price(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if not ticket:
            return
        if not ticket["seller_ign"] or not ticket["buyer_ign"]:
            await send_ephemeral_embed(
                interaction, "Both IGNs required", "Both traders must confirm their Minecraft IGNs first.", "warning"
            )
            return
        if not ticket["price_confirmed"]:
            await send_ephemeral_embed(
                interaction, "Price not confirmed", "The other trader must approve the current price first.", "warning"
            )
            return
        if ticket["pending_amount"]:
            await send_ephemeral_embed(
                interaction, "Proposal pending", "Wait for the other trader to accept or decline the current price proposal.", "warning"
            )
            return
        await interaction.response.send_modal(PriceModal(self.ticket_number))

    @discord.ui.button(
        label="✅ Confirm agreed price",
        style=discord.ButtonStyle.success,
        custom_id="middleman:ticket:agree_price",
        row=0,
    )
    async def agree_price(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if not ticket:
            return
        if ticket["pending_amount"]:
            await send_ephemeral_embed(
                interaction, "Price proposal pending", "Respond to the latest price proposal instead.", "warning"
            )
            return
        if ticket["price_confirmed"]:
            await send_ephemeral_embed(interaction, "Price already confirmed", "The current price is agreed.", "info")
            return
        if interaction.user.id == ticket["price_proposed_by"]:
            await send_ephemeral_embed(
                interaction,
                "Other trader must confirm",
                "You proposed this price. The other trader must confirm it.",
                "warning",
            )
            return
        if not ticket["seller_ign"] or not ticket["buyer_ign"]:
            await send_ephemeral_embed(
                interaction, "Both IGNs required", "Both traders must confirm their Minecraft IGNs first.", "warning"
            )
            return
        update_ticket(self.ticket_number, price_confirmed=1)
        await send_ephemeral_embed(
            interaction,
            "Agreed price confirmed",
            f"Both traders have accepted **{format_coin_amount(ticket['amount'])}** coins.",
            "success",
        )
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Agreed price confirmed",
            f"<@{interaction.user.id}> confirmed the setup price of {format_coin_amount(ticket['amount'])} coins.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="🤝 Middleman confirms deal",
        style=discord.ButtonStyle.success,
        custom_id="middleman:ticket:middleman_confirm",
        row=1,
    )
    async def confirm_middleman(self, interaction, button):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or interaction.user.id != ticket["middleman_id"]:
            await send_ephemeral_embed(interaction, "Middleman only", "Only the selected middleman can confirm this deal.", "error")
            return
        if ticket["status"] != "open":
            await send_ephemeral_embed(interaction, "Ticket closed", "This deal is no longer open.", "warning")
            return
        if not ticket["price_confirmed"]:
            await send_ephemeral_embed(interaction, "Price not confirmed", "Both traders must confirm the agreed price first.", "warning")
            return
        if ticket["middleman_confirmed"]:
            await send_ephemeral_embed(interaction, "Already confirmed", "You already confirmed this deal.", "info")
            return
        update_ticket(self.ticket_number, middleman_confirmed=1)
        await interaction.response.send_message(
            content=f"<@{ticket['buyer_id']}>",
            embed=make_embed(
                f"Deal #{self.ticket_number} • Money is due",
                f"The middleman accepted the deal. Buyer, please send **{format_coin_amount(ticket['amount'])} coins** to <@{ticket['middleman_id']}> in-game. After receiving it, the middleman must run `/middleman money-received`.",
                "success",
            ),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=ticket["buyer_id"])]),
        )
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Middleman confirmed the deal",
            f"Middleman <@{interaction.user.id}> accepted. Buyer <@{ticket['buyer_id']}> must now send the agreed price.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="💸 Report payment (buyer)",
        style=discord.ButtonStyle.primary,
        custom_id="middleman:ticket:paid",
        row=1,
    )
    async def report_payment(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if not ticket:
            return
        if not ticket["middleman_confirmed"]:
            await send_ephemeral_embed(
                interaction,
                "Middleman confirmation required",
                "Wait for the middleman to confirm the deal, then have them run `/middleman money-received` after the buyer pays.",
                "warning",
            )
            return
        if interaction.user.id != ticket["buyer_id"]:
            await send_ephemeral_embed(interaction, "Buyer only", "Only the buyer can report payment.", "warning")
            return
        if not ticket["amount"]:
            await send_ephemeral_embed(interaction, "Price not set", "The seller must record the agreed price first.", "warning")
            return
        if not ticket["price_confirmed"] or ticket["pending_amount"]:
            await send_ephemeral_embed(
                interaction,
                "Price needs agreement",
                "Both traders must confirm the current price before payment can be reported.",
                "warning",
            )
            return
        if not ticket["seller_ign"] or not ticket["buyer_ign"]:
            await send_ephemeral_embed(
                interaction,
                "IGNs required",
                "Both traders must confirm their Minecraft IGN before payment is reported.",
                "warning",
            )
            return
        if ticket["payment_reported"]:
            await send_ephemeral_embed(interaction, "Already reported", "Payment has already been reported.", "info")
            return
        update_ticket(self.ticket_number, payment_reported=1)
        await send_ephemeral_embed(
            interaction,
            "Payment reported",
            "Your report is not proof of payment. Support must verify the balance in-game before the deal proceeds.",
            "warning",
        )
        await ping_support(
            interaction.guild,
            interaction.channel,
            f"Buyer reports payment for deal #{self.ticket_number}; verify it in-game before proceeding.",
        )
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Payment reported",
            f"<@{interaction.user.id}> reported sending {format_coin_amount(ticket['amount'])} coins. Support verification is still required.",
            "warning",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="📦 Mark delivered (seller)",
        style=discord.ButtonStyle.primary,
        custom_id="middleman:ticket:delivered",
        row=1,
    )
    async def mark_delivered(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if not ticket:
            return
        if interaction.user.id != ticket["seller_id"]:
            await send_ephemeral_embed(interaction, "Seller only", "Only the seller can mark the item delivered.", "warning")
            return
        if not ticket["money_received"]:
            await send_ephemeral_embed(
                interaction,
                "Money not received",
                "The middleman must run `/middleman money-received` before the seller delivers the item.",
                "warning",
            )
            return
        update_ticket(self.ticket_number, item_delivered=1)
        await send_ephemeral_embed(
            interaction,
            "Delivery marked",
            "The buyer now has a **Confirm delivery** button. The seller should tpa/tpahere to the buyer and hand over the item first.",
            "success",
        )
        buyer = interaction.guild.get_member(ticket["buyer_id"])
        middleman = interaction.guild.get_member(ticket["middleman_id"])
        mentions = [member for member in (buyer, middleman) if member is not None]
        await interaction.channel.send(
            content=" ".join(member.mention for member in mentions),
            embed=make_embed(
                f"Deal #{self.ticket_number} • Item ready for confirmation",
                f"The seller marked the item delivered. Seller, please tpa/tpahere to the buyer and give the item. Only the buyer can confirm delivery.",
                "info",
            ),
            view=DeliveryConfirmationView(self.ticket_number),
            allowed_mentions=discord.AllowedMentions(users=mentions),
        )
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Item marked delivered",
            f"Seller <@{interaction.user.id}> marked the item delivered; the buyer must confirm receipt.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="✅ Confirm delivery (buyer)",
        style=discord.ButtonStyle.success,
        custom_id="middleman:ticket:confirm_delivery",
        row=1,
    )
    async def confirm_delivery(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if not ticket:
            return
        if interaction.user.id != ticket["buyer_id"]:
            await send_ephemeral_embed(interaction, "Buyer only", "Only the buyer can confirm delivery.", "warning")
            return
        if not ticket["money_received"] or not ticket["item_delivered"]:
            await send_ephemeral_embed(interaction, "Delivery not ready", "The middleman must receive the money and the seller must mark the item delivered first.", "warning")
            return
        await confirm_delivery(interaction, ticket)

    @discord.ui.button(
        label="💸 Pay seller",
        style=discord.ButtonStyle.success,
        custom_id="middleman:ticket:pay_seller",
        row=2,
    )
    async def pay_seller(self, interaction, button):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or interaction.user.id != ticket["middleman_id"]:
            await send_ephemeral_embed(interaction, "Middleman only", "Only the selected middleman can record payment to the seller.", "error")
            return
        if ticket["status"] != "open":
            await send_ephemeral_embed(interaction, "Ticket closed", "This deal is no longer open.", "warning")
            return
        if not ticket["delivery_confirmed"]:
            await send_ephemeral_embed(interaction, "Delivery not confirmed", "The buyer must confirm delivery before you pay the seller.", "warning")
            return
        if ticket["funds_released"]:
            await send_ephemeral_embed(interaction, "Already recorded", "Payment to the seller is already recorded.", "info")
            return
        update_ticket(self.ticket_number, funds_released=1)
        seller = interaction.guild.get_member(ticket["seller_id"])
        await interaction.response.send_message(
            content=seller.mention if seller else None,
            embed=make_embed(
                f"Deal #{self.ticket_number} • Seller paid",
                f"The middleman recorded paying **{format_coin_amount(ticket['amount'])} coins** to the seller. Both buyer and seller must now press **Finish Order**.",
                "success",
            ),
            allowed_mentions=discord.AllowedMentions(users=[seller] if seller else []),
        )
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Seller paid",
            f"Middleman <@{interaction.user.id}> recorded payment of {format_coin_amount(ticket['amount'])} coins to the seller.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="🆘 Problem",
        style=discord.ButtonStyle.danger,
        custom_id="middleman:ticket:problem",
        row=2,
    )
    async def problem(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if ticket:
            await send_ephemeral_embed(
                interaction,
                "What needs attention?",
                "Choose the issue that best describes the problem.",
                "warning",
                ProblemView(self.ticket_number),
            )

    @discord.ui.button(
        label="✅ Finish Order",
        style=discord.ButtonStyle.secondary,
        custom_id="middleman:ticket:close_deal",
        row=2,
    )
    async def close_deal(self, interaction, button):
        ticket = await self.get_participant_ticket(interaction)
        if not ticket:
            return
        if not ticket["funds_released"]:
            await send_ephemeral_embed(
                interaction,
                "Seller payment required",
                "The middleman must press **Pay seller** before either trader can finish the order.",
                "warning",
            )
            return
        field = "close_seller_confirmed" if interaction.user.id == ticket["seller_id"] else "close_buyer_confirmed"
        if ticket[field]:
            await send_ephemeral_embed(
                interaction, "Confirmation already recorded", "Your close-deal vote is already recorded.", "info"
            )
            return
        ticket = update_ticket(self.ticket_number, **{field: 1})
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Close vote recorded",
            f"<@{interaction.user.id}> pressed **Finish Order**. Both buyer and seller must confirm the order.",
            "warning",
        )
        if ticket["close_seller_confirmed"] and ticket["close_buyer_confirmed"]:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await send_order_summary(ticket)
            await close_ticket_channel(ticket, interaction.channel)
            await send_ephemeral_embed(
                interaction,
                "Deal closed by both traders",
                f"Both members agreed. This channel is now locked and renamed `closed-deal-{self.ticket_number}`.",
                "success",
            )
        else:
            await send_ephemeral_embed(
                interaction,
                "Close vote recorded",
                "Your confirmation is recorded. The other trader must also press **Finish Order** to publish the order summary and close this channel.",
                "warning",
            )
        await refresh_ticket_message(self.ticket_number)

    async def get_staff_ticket(self, interaction):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not is_support(interaction.user):
            await send_ephemeral_embed(
                interaction,
                "Support access required",
                "Only configured middleman support staff can use this control.",
                "error",
            )
            return None
        if ticket["status"] != "open":
            await send_ephemeral_embed(interaction, "Ticket closed", "This deal is no longer open.", "warning")
            return None
        return ticket

    @discord.ui.button(
        label="🛡️ Verify payment",
        style=discord.ButtonStyle.primary,
        custom_id="middleman:ticket:verify",
        row=3,
    )
    async def verify_payment(self, interaction, button):
        ticket = await self.get_staff_ticket(interaction)
        if not ticket:
            return
        if not ticket["payment_reported"]:
            await send_ephemeral_embed(interaction, "No payment report", "The buyer must report payment before support verifies it.", "warning")
            return
        if ticket["payment_verified"]:
            await send_ephemeral_embed(interaction, "Already verified", "Payment is already marked as verified.", "info")
            return
        update_ticket(self.ticket_number, payment_verified=1)
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Payment manually verified",
            f"Support member <@{interaction.user.id}> recorded an in-game payment check.",
            "success",
        )
        await send_audit_event(
            interaction.guild,
            "middleman_payment_verified",
            interaction.user,
            interaction.channel,
            f"Verified payment for middleman deal #{self.ticket_number}.",
        )
        await send_ephemeral_embed(
            interaction,
            "Payment verification recorded",
            "This records the staff member's in-game check. The bot does not inspect DonutSMP or move coins.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="💸 Record release",
        style=discord.ButtonStyle.success,
        custom_id="middleman:ticket:release",
        row=3,
    )
    async def record_release(self, interaction, button):
        ticket = await self.get_staff_ticket(interaction)
        if not ticket:
            return
        if not ticket["delivery_confirmed"]:
            await send_ephemeral_embed(interaction, "Delivery confirmation required", "The buyer must confirm receipt before support records a release.", "warning")
            return
        if ticket["funds_released"]:
            await send_ephemeral_embed(interaction, "Already released", "The manual release is already recorded.", "info")
            return
        update_ticket(self.ticket_number, funds_released=1, status="completed")
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Manual release recorded",
            f"Support member <@{interaction.user.id}> recorded the manual in-game transfer.",
            "success",
        )
        await send_audit_event(
            interaction.guild,
            "middleman_release_recorded",
            interaction.user,
            interaction.channel,
            f"Recorded manual release for middleman deal #{self.ticket_number}.",
        )
        await send_ephemeral_embed(
            interaction,
            "Manual release recorded",
            "This records a transfer staff already completed in-game. The bot did not move any coins.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="🔒 Staff close",
        style=discord.ButtonStyle.secondary,
        custom_id="middleman:ticket:staff_close",
        row=4,
    )
    async def staff_close(self, interaction, button):
        ticket = await self.get_staff_ticket(interaction)
        if not ticket:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await log_ticket_event(
            interaction.channel,
            self.ticket_number,
            "Closed by support",
            f"Support member <@{interaction.user.id}> closed this deal after review.",
            "warning",
        )
        await close_ticket_channel(ticket, interaction.channel)
        await send_audit_event(
            interaction.guild,
            "middleman_closed_by_staff",
            interaction.user,
            interaction.channel,
            f"Closed middleman deal #{self.ticket_number} after staff review.",
        )
        await send_ephemeral_embed(
            interaction,
            "Deal closed by support",
            f"The channel was locked and renamed `closed-deal-{self.ticket_number}`.",
            "success",
        )
        await refresh_ticket_message(self.ticket_number)

    @discord.ui.button(
        label="🗑️ Delete ticket",
        style=discord.ButtonStyle.danger,
        custom_id="middleman:ticket:delete",
        row=4,
    )
    async def delete_ticket(self, interaction, button):
        if not is_support(interaction.user):
            await send_ephemeral_embed(
                interaction,
                "Support access required",
                "Only administrators and configured support staff can delete tickets.",
                "error",
            )
            return
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket:
            await send_ephemeral_embed(interaction, "Ticket not found", "This ticket record no longer exists.", "warning")
            return
        await interaction.response.send_message(
            embed=make_embed(
                "Confirm ticket deletion",
                "This will permanently delete the ticket channel. Confirm to start a 10-second countdown.",
                "warning",
            ),
            view=MiddlemanDeleteConfirmView(self.ticket_number, interaction.user.id, interaction.channel),
            ephemeral=True,
        )


class MiddlemanDeleteConfirmView(discord.ui.View):
    def __init__(self, ticket_number, actor_id, channel):
        super().__init__(timeout=60)
        self.ticket_number = ticket_number
        self.actor_id = actor_id
        self.channel = channel

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await send_ephemeral_embed(interaction, "Confirmation is private", "Only the staff member who started this deletion can confirm it.", "error")
            return False
        return True

    @discord.ui.button(label="✅ Confirm deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        countdown = MiddlemanDeleteCountdownView(self.ticket_number, self.actor_id, self.channel)
        await interaction.response.edit_message(
            embed=make_embed("Deletion countdown started", "The ticket will be deleted in **10 seconds**.", "warning"),
            view=countdown,
        )
        countdown.task = asyncio.create_task(countdown.delete_after_delay())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(embed=make_embed("Deletion cancelled", "The ticket channel was not deleted.", "success"), view=None)


class MiddlemanDeleteCountdownView(discord.ui.View):
    def __init__(self, ticket_number, actor_id, channel):
        super().__init__(timeout=10)
        self.ticket_number = ticket_number
        self.actor_id = actor_id
        self.channel = channel
        self.task = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await send_ephemeral_embed(interaction, "Confirmation is private", "Only the staff member who started this deletion can cancel it.", "error")
            return False
        return True

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.success)
    async def cancel(self, interaction, button):
        if self.task:
            self.task.cancel()
        await interaction.response.edit_message(embed=make_embed("Deletion cancelled", "The ticket channel was not deleted.", "success"), view=None)

    async def delete_after_delay(self):
        await asyncio.sleep(10)
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket:
            return
        update_ticket(self.ticket_number, status="closed")
        await send_audit_event(
            self.channel.guild,
            "middleman_ticket_deleted",
            actor=self.actor_id,
            target=self.channel,
            details=f"Permanently deleted middleman ticket #{self.ticket_number}.",
        )
        await self.channel.delete(reason=f"Middleman ticket #{self.ticket_number} deleted after confirmation")


async def ping_support(guild, channel, text):
    role = guild.get_role(SUPPORT_ROLE_ID)
    content = f"{role.mention} {text}" if role else text
    allowed_mentions = discord.AllowedMentions(roles=[role]) if role else discord.AllowedMentions.none()
    await channel.send(
        content,
        embed=make_embed("Support attention requested", text, "warning"),
        allowed_mentions=allowed_mentions,
    )


class ProblemView(discord.ui.View):
    def __init__(self, ticket_number):
        super().__init__(timeout=300)
        self.ticket_number = ticket_number
        self.add_item(ProblemSelect(ticket_number))


class ProblemSelect(discord.ui.Select):
    def __init__(self, ticket_number):
        self.ticket_number = ticket_number
        super().__init__(
            placeholder="🆘 Select an issue",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Agreed price is wrong", value="price", emoji="💰"),
                discord.SelectOption(label="Payment is not showing", value="payment", emoji="💸"),
                discord.SelectOption(label="Item was not delivered", value="delivery", emoji="📦"),
                discord.SelectOption(label="Something else", value="other", emoji="📝"),
            ],
        )

    async def callback(self, interaction):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not is_party(ticket, interaction.user.id):
            await send_ephemeral_embed(
                interaction, "Access denied", "Only deal participants can report an issue.", "error"
            )
            return
        issue = self.values[0]
        help_text = {
            "price": "The seller can use **Set / update price** to correct the agreed amount before payment is reported.",
            "payment": "Check the amount and recipient in-game. The bot cannot inspect DonutSMP balances; staff must verify the payment manually.",
            "delivery": "Ask the seller to confirm the in-game handoff. Confirm delivery only after you have received the item.",
            "other": "Describe the issue to staff using the escalation button below.",
        }[issue]
        await send_ephemeral_embed(
            interaction,
            "Issue help",
            help_text,
            "warning",
            ProblemFollowupView(self.ticket_number, issue),
        )


class ProblemFollowupView(discord.ui.View):
    def __init__(self, ticket_number, issue):
        super().__init__(timeout=300)
        self.ticket_number = ticket_number
        self.issue = issue

    @discord.ui.button(label="🆘 Still need staff", style=discord.ButtonStyle.danger, row=1)
    async def escalate(self, interaction, button):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if not ticket or not is_party(ticket, interaction.user.id):
            await send_ephemeral_embed(
                interaction, "Access denied", "Only deal participants can escalate this ticket.", "error"
            )
            return
        await send_ephemeral_embed(
            interaction, "Support notified", "A support member has been pinged in this private ticket.", "success"
        )
        await ping_support(
            interaction.guild,
            interaction.channel,
            f"Support requested by {interaction.user.mention} for deal #{self.ticket_number} ({self.issue}).",
        )

    @discord.ui.button(label="↩ Back to issue options", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await send_ephemeral_embed(
            interaction,
            "Choose an issue",
            "Select the issue that best describes the problem.",
            view=ProblemView(self.ticket_number),
        )

    @discord.ui.button(label="💰 Update price", style=discord.ButtonStyle.primary, row=0)
    async def update_price(self, interaction, button):
        ticket = get_ticket(ticket_number=self.ticket_number)
        if self.issue != "price" or not ticket or not is_party(ticket, interaction.user.id):
            await send_ephemeral_embed(
                interaction, "Access denied", "Only deal participants can propose a price change.", "warning"
            )
            return
        await interaction.response.send_modal(PriceModal(self.ticket_number))


async def resolve_channel(channel_id):
    channel = bot.get_channel(channel_id)
    return channel if channel else await bot.fetch_channel(channel_id)


def mark_missing_ticket_channel(ticket_number, *, support=False):
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as connection:
        if support:
            connection.execute(
                """UPDATE support_tickets
                   SET status = 'deleted', deleted_at = ?, last_activity_at = ?
                   WHERE ticket_number = ? AND status = 'open'""",
                (now, now, ticket_number),
            )
        else:
            connection.execute(
                """UPDATE tickets
                   SET status = 'closed', activity_at = ?, last_activity_at = ?
                   WHERE ticket_number = ? AND status = 'open'""",
                (now, now, ticket_number),
            )


def ticket_reminder_recipient(ticket):
    if ticket["pending_amount"]:
        target_id = ticket["buyer_id"] if ticket["price_proposed_by"] == ticket["seller_id"] else ticket["seller_id"]
        return "participant", target_id, "A price change is waiting for your response."
    if not ticket["seller_ign"]:
        return "participant", ticket["seller_id"], "Please confirm your Minecraft IGN."
    if not ticket["buyer_ign"]:
        return "participant", ticket["buyer_id"], "Please confirm your Minecraft IGN."
    if not ticket["price_confirmed"]:
        target_id = ticket["buyer_id"] if ticket["price_proposed_by"] == ticket["seller_id"] else ticket["seller_id"]
        return "participant", target_id, "Please confirm the agreed price."
    if not ticket["middleman_confirmed"]:
        return "participant", ticket["middleman_id"], "Please confirm the deal before payment begins."
    if not ticket["money_received"]:
        return "participant", ticket["buyer_id"], "Please send the agreed price to the middleman; they must run `/middleman money-received`."
    if not ticket["item_delivered"]:
        return "participant", ticket["seller_id"], "Please update the item-delivery status."
    if not ticket["delivery_confirmed"]:
        return "participant", ticket["buyer_id"], "Please confirm whether you received the item."
    if not ticket["funds_released"]:
        return "participant", ticket["middleman_id"], "Delivery is confirmed; pay the seller and press **Pay seller**."
    if ticket["close_seller_confirmed"] and not ticket["close_buyer_confirmed"]:
        return "participant", ticket["buyer_id"], "The seller requested to close this deal."
    if ticket["close_buyer_confirmed"] and not ticket["close_seller_confirmed"]:
        return "participant", ticket["seller_id"], "The buyer requested to close this deal."
    return None, None, None


async def process_stalled_tickets():
    now = datetime.now(timezone.utc)
    with db_session() as connection:
        tickets = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM tickets WHERE status = 'open' AND activity_at != ''"
            )
        ]
    for ticket in tickets:
        try:
            activity_at = datetime.fromisoformat(ticket["activity_at"])
            if activity_at.tzinfo is None:
                activity_at = activity_at.replace(tzinfo=timezone.utc)
            inactive_hours = (now - activity_at).total_seconds() / 3600
            try:
                channel = await resolve_channel(ticket["channel_id"])
            except discord.NotFound:
                mark_missing_ticket_channel(ticket["ticket_number"])
                app.logger.warning(
                    "Marked deal #%s closed because channel %s no longer exists",
                    ticket["ticket_number"],
                    ticket["channel_id"],
                )
                continue
            recipient_type, recipient_id, reminder = ticket_reminder_recipient(ticket)
            if inactive_hours >= TICKET_REMINDER_HOURS and recipient_type:
                last_reminded_at = ticket["last_reminded_at"]
                reminder_due = True
                if last_reminded_at:
                    previous = datetime.fromisoformat(last_reminded_at)
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=timezone.utc)
                    reminder_due = (now - previous).total_seconds() >= TICKET_REMINDER_HOURS * 3600
                if reminder_due:
                    if recipient_type == "support":
                        role = channel.guild.get_role(SUPPORT_ROLE_ID)
                        content = role.mention if role else ""
                        allowed_mentions = discord.AllowedMentions(roles=[role]) if role else discord.AllowedMentions.none()
                        description = reminder
                    else:
                        content = f"<@{recipient_id}>"
                        allowed_mentions = discord.AllowedMentions(users=[discord.Object(id=recipient_id)])
                        description = reminder
                    await channel.send(
                        content=content,
                        embed=make_embed(
                            f"Deal #{ticket['ticket_number']} • Reminder",
                            f"{description}\n\nThis ticket has been inactive for {inactive_hours:.0f} hours.",
                            "warning",
                        ),
                        allowed_mentions=allowed_mentions,
                    )
                    with db_session() as connection:
                        connection.execute(
                            "UPDATE tickets SET last_reminded_at = ? WHERE ticket_number = ?",
                            (now.isoformat(), ticket["ticket_number"]),
                        )

            if inactive_hours >= TICKET_REVIEW_HOURS and not ticket["flagged_at"]:
                role = channel.guild.get_role(SUPPORT_ROLE_ID)
                content = role.mention if role else ""
                allowed_mentions = discord.AllowedMentions(roles=[role]) if role else discord.AllowedMentions.none()
                await channel.send(
                    content=content,
                    embed=make_embed(
                        f"Deal #{ticket['ticket_number']} • Support review",
                        f"This ticket has been inactive for {inactive_hours:.0f} hours and needs staff review.",
                        "error",
                    ),
                    allowed_mentions=allowed_mentions,
                )
                with db_session() as connection:
                    connection.execute(
                        "UPDATE tickets SET flagged_at = ? WHERE ticket_number = ?",
                        (now.isoformat(), ticket["ticket_number"]),
                    )
        except Exception:
            app.logger.exception("Ticket reminder processing failed for #%s", ticket["ticket_number"])


async def process_stalled_support_tickets():
    now = datetime.now(timezone.utc)
    with db_session() as connection:
        tickets = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM support_tickets WHERE status = 'open' ORDER BY created_at"
            )
        ]
    for ticket in tickets:
        try:
            last_activity = ticket.get("last_activity_at") or ticket["created_at"]
            last_activity_at = datetime.fromisoformat(last_activity)
            if last_activity_at.tzinfo is None:
                last_activity_at = last_activity_at.replace(tzinfo=timezone.utc)
            inactive_hours = (now - last_activity_at).total_seconds() / 3600
            try:
                channel = await resolve_channel(ticket["channel_id"])
            except discord.NotFound:
                mark_missing_ticket_channel(ticket["ticket_number"], support=True)
                app.logger.warning(
                    "Marked support ticket #%s deleted because channel %s no longer exists",
                    ticket["ticket_number"],
                    ticket["channel_id"],
                )
                continue
            reminder_hours = int(get_bot_setting(ticket["guild_id"], "ticket_reminder_hours", str(TICKET_REMINDER_HOURS)))
            auto_close_hours = int(get_bot_setting(ticket["guild_id"], "ticket_auto_close_hours", str(TICKET_AUTO_CLOSE_HOURS)))
            if inactive_hours >= reminder_hours and not ticket["last_reminded_at"]:
                role = channel.guild.get_role(SUPPORT_ROLE_ID)
                content = f"<@{ticket['creator_id']}>" + (f" {role.mention}" if role else "")
                allowed_mentions = discord.AllowedMentions(
                    users=[discord.Object(id=ticket["creator_id"])],
                    roles=[role] if role else [],
                )
                await channel.send(
                    content=content,
                    embed=make_embed(
                        f"Support ticket #{ticket['ticket_number']} • Reminder",
                        f"This ticket has been inactive for {inactive_hours:.0f} hours. Please reply if you still need assistance.",
                        "warning",
                    ),
                    allowed_mentions=allowed_mentions,
                )
                with db_session() as connection:
                    connection.execute(
                        "UPDATE support_tickets SET last_reminded_at = ? WHERE ticket_number = ?",
                        (now.isoformat(), ticket["ticket_number"]),
                    )
            if inactive_hours >= auto_close_hours:
                member = channel.guild.get_member(ticket["creator_id"])
                if member:
                    await channel.set_permissions(member, send_messages=False)
                with db_session() as connection:
                    connection.execute(
                        "UPDATE support_tickets SET status = 'closed', closed_at = ?, auto_closed_at = ?, last_activity_at = ? WHERE ticket_number = ? AND status = 'open'",
                        (now.isoformat(), now.isoformat(), now.isoformat(), ticket["ticket_number"]),
                    )
                await channel.send(
                    embed=make_embed(
                        f"Support ticket #{ticket['ticket_number']} auto-closed",
                        f"This ticket was automatically closed after {auto_close_hours} hours without activity. Reply or contact staff if it should be reopened.",
                        "warning",
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await send_audit_event(
                    channel.guild,
                    "support_ticket_auto_closed",
                    target=channel,
                    details=f"Automatically closed inactive support ticket #{ticket['ticket_number']}.",
                )
        except Exception:
            app.logger.exception("Support ticket reminder processing failed for #%s", ticket["ticket_number"])


async def process_webhook_outbox():
    now = datetime.now(timezone.utc)
    with db_session() as connection:
        events = [
            dict(row)
            for row in connection.execute(
                """SELECT outbox.event_id, outbox.attempts, event.from_player,
                          event.to_player, event.amount
                   FROM webhook_outbox AS outbox
                   JOIN transaction_events AS event ON event.event_id = outbox.event_id
                   WHERE outbox.delivered_at IS NULL AND outbox.next_attempt_at <= ?
                   ORDER BY outbox.next_attempt_at LIMIT 10""",
                (now.isoformat(),),
            )
        ]
    for event in events:
        try:
            embed = make_embed(
                "Transaction received",
                f"Webhook event `{event['event_id']}` was accepted for delivery.",
            )
            embed.add_field(name="📤 From", value=event["from_player"] or "Unknown", inline=True)
            embed.add_field(name="📥 To", value=event["to_player"] or "Unknown", inline=True)
            embed.add_field(name="💰 Amount", value=f"{Decimal(event['amount']):,}", inline=True)
            with db_session() as connection:
                matches = connection.execute(
                    "SELECT ticket_number, match_status, reason FROM transaction_matches WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchall()
            if matches:
                match_text = "\n".join(
                    f"Deal #{row['ticket_number']}: {'✅' if row['match_status'] == 'matched' else '⚠️'} {row['reason']}"
                    for row in matches
                )
                embed.add_field(name="🔎 Ticket matching", value=match_text[:1024], inline=False)
            else:
                embed.add_field(name="🔎 Ticket matching", value="No matching open middleman deal found.", inline=False)
            await send_to_designated_channel(embed, STAFF_DASHBOARD_CHANNEL_ID)
            with db_session() as connection:
                connection.execute(
                    "UPDATE webhook_outbox SET delivered_at = ?, last_error = NULL WHERE event_id = ?",
                    (datetime.now(timezone.utc).isoformat(), event["event_id"]),
                )
        except Exception as error:
            attempts = event["attempts"] + 1
            delay_seconds = min(3600, 5 * (2 ** min(attempts, 9)))
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
            with db_session() as connection:
                connection.execute(
                    """UPDATE webhook_outbox SET attempts = ?, next_attempt_at = ?, last_error = ?
                       WHERE event_id = ?""",
                    (attempts, next_attempt, str(error)[:500], event["event_id"]),
                )
            app.logger.warning(
                "Transaction alert %s failed (attempt %s); retry scheduled in %s seconds",
                event["event_id"], attempts, delay_seconds,
            )


async def process_database_backup():
    global last_database_backup_at
    now = datetime.now(timezone.utc)
    if last_database_backup_at and (now - last_database_backup_at).total_seconds() < DATABASE_BACKUP_INTERVAL_HOURS * 3600:
        return
    try:
        backup_database()
        last_database_backup_at = now
    except Exception as error:
        app.logger.exception("Database backup failed")
        await send_error_alert("Database backup failed", str(error))


@tasks.loop(minutes=30)
async def ticket_reminder_worker():
    await process_stalled_tickets()
    await process_stalled_support_tickets()
    await process_database_backup()


@ticket_reminder_worker.before_loop
async def before_ticket_reminder_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=WEBHOOK_OUTBOX_POLL_SECONDS)
async def webhook_outbox_worker():
    await process_webhook_outbox()


@webhook_outbox_worker.before_loop
async def before_webhook_outbox_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def giveaway_worker():
    from commands.giveaway import process_expired_giveaways

    await process_expired_giveaways()


@giveaway_worker.before_loop
async def before_giveaway_worker():
    await bot.wait_until_ready()


class MiddlemanBot(commands.Bot):
    async def setup_hook(self):
        global bot_started_at, discord_loop
        bot_started_at = time.monotonic()
        discord_loop = asyncio.get_running_loop()
        config.validate_config()
        initialize_db()
        for extension in (
            "commands.middleman",
            "commands.ticket",
            "commands.giveaway",
            "commands.embed_builder",
            "commands.apply",
            "commands.verify",
            "commands.profile",
            "commands.operations",
            "commands.transactionhistory",
            "commands.vouch",
            "commands.vouchcount",
            "commands.invites",
            "commands.help",
        ):
            await self.load_extension(extension)
        self.add_view(PanelView())
        with db_session() as connection:
            ticket_numbers = [
                row[0] for row in connection.execute("SELECT ticket_number FROM tickets")
            ]
            pending_invites = [
                row[0]
                for row in connection.execute(
                    "SELECT ticket_number FROM tickets WHERE status_message_id IS NULL"
                )
            ]
        for ticket_number in ticket_numbers:
            self.add_view(TicketControls(ticket_number))
        with db_session() as connection:
            delivered_ticket_numbers = [
                row[0]
                for row in connection.execute(
                    "SELECT ticket_number FROM tickets WHERE status = 'open' AND money_received = 1 AND item_delivered = 1 AND delivery_confirmed = 0"
                )
            ]
        for ticket_number in delivered_ticket_numbers:
            self.add_view(DeliveryConfirmationView(ticket_number))
        for ticket_number in pending_invites:
            self.add_view(DealInviteView(ticket_number))
        with db_session() as connection:
            pending_tickets = [
                row[0]
                for row in connection.execute(
                    "SELECT ticket_number FROM tickets WHERE pending_amount IS NOT NULL"
                )
            ]
        for ticket_number in pending_tickets:
            self.add_view(PriceProposalView(ticket_number))
        from commands.ticket import SupportTicketView, TicketPanelView
        from commands.apply import ApplicationReviewView, StartApplicationView
        from commands.verify import VerifyView

        self.add_view(TicketPanelView())
        self.add_view(StartApplicationView())
        self.add_view(VerifyView())
        with db_session() as connection:
            support_ticket_numbers = [
                row[0]
                for row in connection.execute(
                    "SELECT ticket_number FROM support_tickets WHERE status IN ('open', 'closed')"
                )
            ]
        for ticket_number in support_ticket_numbers:
            self.add_view(SupportTicketView(ticket_number))
        with db_session() as connection:
            application_ids = [
                row[0] for row in connection.execute(
                    "SELECT id FROM staff_applications WHERE status = 'pending' AND review_message_id IS NOT NULL"
                )
            ]
        for application_id in application_ids:
            self.add_view(ApplicationReviewView(application_id))
        from commands.giveaway import GiveawayView

        with db_session() as connection:
            giveaway_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT giveaway_id FROM giveaways WHERE status = 'active' AND message_id IS NOT NULL"
                )
            ]
        for giveaway_id in giveaway_ids:
            self.add_view(GiveawayView(giveaway_id))
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Keep commands guild-local for immediate updates and remove global duplicates.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()


bot = MiddlemanBot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.tree.error
async def handle_app_command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        title = "Administrator permission required"
        message = "Only server administrators can use this command."
        tone = "warning"
    else:
        error_counts[type(error).__name__] = error_counts.get(type(error).__name__, 0) + 1
        app.logger.error("Application command failed: %s", error)
        await send_error_alert("Application command error", f"`{type(error).__name__}`\n{error}")
        title = "Command failed"
        message = "That command could not be completed. Please contact server staff."
        tone = "error"
    await send_ephemeral_embed(interaction, title, message, tone)


@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.dnd,
        activity=discord.Activity(type=discord.ActivityType.watching, name=BOT_ACTIVITY),
    )
    if not ticket_reminder_worker.is_running():
        ticket_reminder_worker.start()
    if not webhook_outbox_worker.is_running():
        webhook_outbox_worker.start()
    if not giveaway_worker.is_running():
        giveaway_worker.start()
    for guild in bot.guilds:
        await refresh_invite_cache(guild)
    print(f"{BOT_NAME} online as {bot.user} (ID: {bot.user.id}) • DND • Watching {BOT_ACTIVITY}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    with db_session() as connection:
        ticket = connection.execute(
            "SELECT ticket_number FROM support_tickets WHERE channel_id = ? AND status = 'open'",
            (message.channel.id,),
        ).fetchone()
    if ticket:
        touch_support_ticket(ticket["ticket_number"], message.created_at.astimezone(timezone.utc).isoformat())
    await bot.process_commands(message)


@bot.event
async def on_member_join(member):
    if member.bot:
        return
    try:
        await record_invite_use(member)
        inviter_id = None
        inviter_count = 0
        with db_session() as connection:
            row = connection.execute(
                "SELECT inviter_id FROM invite_uses WHERE guild_id = ? AND joined_user_id = ? ORDER BY joined_at DESC LIMIT 1",
                (member.guild.id, member.id),
            ).fetchone()
            if row and row["inviter_id"] is not None:
                inviter_id = row["inviter_id"]
                inviter_count = connection.execute(
                    "SELECT COUNT(*) FROM invite_uses WHERE guild_id = ? AND inviter_id = ?",
                    (member.guild.id, inviter_id),
                ).fetchone()[0]

        from commands.verify import restrict_member

        await restrict_member(member)
        channel = await resolve_channel(WELCOME_CHANNEL_ID)
        inviter_text = "Unknown" if inviter_id is None else f"<@{inviter_id}>"
        embed = make_embed(
            f"Welcome to {member.guild.name}",
            f"We're glad you're here, {member.mention}! Check the server channels and enjoy your time on DonutSMP.",
            "success",
        )
        if MIDDLEMAN_BANNER_URL:
            embed.set_image(url=MIDDLEMAN_BANNER_URL)
        if inviter_id is not None:
            embed.add_field(
                name="👤 Invited by",
                value=f"{inviter_text} • **{inviter_count:,}** invite{'s' if inviter_count != 1 else ''}",
                inline=False,
            )
        else:
            embed.add_field(
                name="👤 Invited by",
                value="Unknown invite source",
                inline=False,
            )
        member_count = member.guild.member_count
        embed.add_field(
            name="👥 Members",
            value=f"{member_count:,}" if member_count is not None else "Updating",
            inline=True,
        )
        await channel.send(
            content=member.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=[member] + ([member.guild.get_member(inviter_id)] if inviter_id and member.guild.get_member(inviter_id) else [])),
        )
    except discord.HTTPException:
        app.logger.exception("Could not send welcome message for member %s", member.id)


@bot.event
async def on_member_ban(guild, user):
    await send_audit_event(guild, "member_banned", target=user, details=f"Member {user} was banned.")


@bot.event
async def on_member_remove(member):
    action = "member_left_or_kicked"
    actor = None
    details = f"Member {member} left or was removed from the server."
    try:
        async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
            if entry.target and entry.target.id == member.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 15:
                action = "member_kicked"
                actor = entry.user
                details = f"Member {member} was kicked by {entry.user}."
                break
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        app.logger.exception("Could not inspect audit log for removed member %s", member.id)
    await send_audit_event(member.guild, action, actor=actor, target=member, details=details)


@bot.event
async def on_member_update(before, after):
    if before.communication_disabled_until != after.communication_disabled_until:
        action = "member_timed_out" if after.communication_disabled_until else "member_timeout_removed"
        details = f"Timeout changed to {after.communication_disabled_until or 'none'}."
        await send_audit_event(after.guild, action, target=after, details=details)
    before_roles = {role.id for role in before.roles}
    after_roles = {role.id for role in after.roles}
    if before_roles != after_roles:
        added = after_roles - before_roles
        removed = before_roles - after_roles
        await send_audit_event(after.guild, "member_roles_updated", target=after, details=f"Added roles: {sorted(added)}; removed roles: {sorted(removed)}.")
    if before.nick != after.nick:
        await send_audit_event(after.guild, "member_nickname_updated", target=after, details=f"Nickname changed from {before.nick or before.name} to {after.nick or after.name}.")


@bot.event
async def on_guild_channel_delete(channel):
    await send_audit_event(channel.guild, "channel_deleted", target=channel, details=f"Channel `{channel.name}` ({channel.id}) was deleted.")


@bot.event
async def on_guild_channel_create(channel):
    await send_audit_event(channel.guild, "channel_created", target=channel, details=f"Channel `{channel.name}` ({channel.id}) was created.")


@bot.event
async def on_guild_channel_update(before, after):
    if before.name != after.name:
        await send_audit_event(after.guild, "channel_renamed", target=after, details=f"Channel renamed from `{before.name}` to `{after.name}`.")
    if before.category_id != after.category_id:
        await send_audit_event(after.guild, "channel_category_updated", target=after, details=f"Category changed from `{before.category_id}` to `{after.category_id}`.")
    if str(before.overwrites) != str(after.overwrites):
        await send_audit_event(after.guild, "channel_permissions_updated", target=after, details=f"Permission overwrites changed for `{after.name}`.")


@bot.event
async def on_guild_role_create(role):
    await send_audit_event(role.guild, "role_created", target=role, details=f"Role `{role.name}` was created.")


@bot.event
async def on_guild_role_delete(role):
    await send_audit_event(role.guild, "role_deleted", target=role, details=f"Role `{role.name}` was deleted.")


@bot.event
async def on_guild_role_update(before, after):
    if before.name != after.name or before.permissions != after.permissions:
        await send_audit_event(after.guild, "role_updated", target=after, details=f"Role changed from `{before.name}` to `{after.name}`; permissions updated: {before.permissions != after.permissions}.")


@app.get("/")
def health_root():
    return jsonify(status="ok", service=BOT_NAME)


@app.post("/transaction")
def transaction():
    if not WEBHOOK_SECRET:
        return jsonify(error="Transaction webhook is disabled until its secret is configured"), 503
    supplied_secret = request.headers.get("X-Webhook-Secret", "")
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        return jsonify(error="Unauthorized"), 401
    if not request.is_json:
        return jsonify(error="Content-Type must be application/json"), 415

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Request body must be a JSON object"), 400
    event_id = payload.get("event_id", request.headers.get("X-Event-ID", ""))
    if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", event_id.strip()):
        return jsonify(error="Provide a unique 'event_id' or X-Event-ID header (1-128 safe characters)"), 400
    event_id = event_id.strip()
    from_player = payload.get("from_player", payload.get("from"))
    to_player = payload.get("to_player", payload.get("to"))
    legacy_player = payload.get("player")
    amount = payload.get("amount")
    if to_player is None and legacy_player is not None:
        to_player = legacy_player
    for field_name, player_name in (("from_player", from_player), ("to_player", to_player)):
        if player_name is not None and (
            not isinstance(player_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player_name.strip())
        ):
            return jsonify(error=f"'{field_name}' must be a valid Minecraft Java username"), 400
    if from_player is None and to_player is None:
        return jsonify(error="Provide 'from_player' and 'to_player' (or legacy 'player')"), 400
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        return jsonify(error="'amount' must be a number or numeric string"), 400
    try:
        parsed_amount = Decimal(str(amount))
        if not parsed_amount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        return jsonify(error="'amount' must be a finite number"), 400
    from_player = from_player.strip() if from_player else None
    to_player = to_player.strip() if to_player else None
    player = to_player or from_player
    received_at = datetime.now(timezone.utc).isoformat()
    normalized_amount = format(parsed_amount, "f")
    is_new_event = False
    with db_session() as connection:
        cursor = connection.execute(
            """INSERT INTO transaction_events
            (event_id, player, from_player, to_player, amount, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) WHERE event_id IS NOT NULL DO NOTHING""",
            (event_id, player, from_player, to_player, normalized_amount, received_at),
        )
        if cursor.rowcount:
            is_new_event = True
            connection.execute(
                "INSERT INTO webhook_outbox (event_id, next_attempt_at) VALUES (?, ?)",
                (event_id, received_at),
            )
        else:
            existing = connection.execute(
                """SELECT from_player, to_player, amount FROM transaction_events
                   WHERE event_id = ?""",
                (event_id,),
            ).fetchone()
            if not existing or (
                existing["from_player"], existing["to_player"], existing["amount"]
            ) != (from_player, to_player, normalized_amount):
                return jsonify(error="event_id was already used for a different transaction"), 409
            return jsonify(status="duplicate", event_id=event_id), 200
    try:
        matches = match_transaction_to_tickets(
            event_id, from_player, to_player, normalized_amount, received_at
        ) if is_new_event else []
    except Exception:
        app.logger.exception("Could not match webhook event %s to open tickets", event_id)
        matches = []
    return jsonify(status="queued", event_id=event_id, matched_tickets=len(matches)), 202


async def send_to_designated_channel(embed, channel_id=None):
    channel = await resolve_channel(channel_id or PANEL_CHANNEL_ID)
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


# Function to start the Discord bot in the background thread
def run_discord_bot():
    try:
        config.validate_config()
    except RuntimeError as error:
        app.logger.error("Config validation failed: %s", error)
        return
    bot.run(TOKEN.strip())


# Automatically start the Discord bot thread when Gunicorn imports app.py or when run locally
import threading
if not any(t.name == "DiscordBotThread" for t in threading.enumerate()):
    bot_thread = threading.Thread(
        target=run_discord_bot,
        name="DiscordBotThread",
        daemon=True,
    )
    bot_thread.start()


if __name__ == "__main__":
    # Local development execution
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)