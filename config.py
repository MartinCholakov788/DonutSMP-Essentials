import os

from dotenv import load_dotenv

load_dotenv()


def env_int(name, default=None):
    value = os.getenv(name)
    if value in (None, ""):
        if default is None:
            return None
        value = str(default)
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number") from error


def env_int_list(name):
    value = os.getenv(name)
    if value in (None, ""):
        return None
    values = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            raise RuntimeError(f"{name} must contain only numbers")
        values.append(env_int_value(name, item))
    return tuple(values)


def env_int_value(name, value):
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number") from error


TOKEN = os.getenv("DISCORD_TOKEN")
BOT_NAME = "DonutSMP Essentials Assistant"
BOT_ACTIVITY = "Keeping trades safe"
WEBHOOK_SECRET = os.getenv("TRANSACTION_WEBHOOK_SECRET")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
GUILD_ID = env_int("DISCORD_GUILD_ID")
MIDDLEMAN_BANNER_URL = os.getenv("MIDDLEMAN_BANNER_URL", "")
VOUCH_OVER_50_ROLE_ID = env_int("VOUCH_OVER_50_ROLE_ID")
VOUCH_OVER_75_ROLE_ID = env_int("VOUCH_OVER_75_ROLE_ID")
MODERATION_ROLE_ID = env_int("MODERATION_ROLE_ID")
APPLICATION_CHANNEL_ID = env_int("APPLICATION_CHANNEL_ID")
ACCEPTANCE_CHANNEL_ID = env_int("ACCEPTANCE_CHANNEL_ID")
APPLICATION_PANEL_CHANNEL_ID = env_int("APPLICATION_PANEL_CHANNEL_ID")
DATABASE_PATH = os.getenv("DATABASE_PATH", "middleman.sqlite3")
DATABASE_BACKUP_PATH = os.getenv("DATABASE_BACKUP_PATH", "middleman.sqlite3.backup")
DISCORD_BACKUP_DIRECTORY = os.getenv("DISCORD_BACKUP_DIRECTORY", "backups")

PANEL_CHANNEL_ID = env_int("MIDDLEMEN_PANEL_CHANNEL_ID")
MIDDLEMAN_ORDERS_CHANNEL_ID = env_int("MIDDLEMAN_ORDERS_CHANNEL_ID", 1556000162114183218)
TICKET_CATEGORY_ID = env_int("MIDDLEMEN_CATEGORY_ID")
SUPPORT_ROLE_ID = env_int("MIDDLEMEN_SUPPORT_ROLE_ID")
VOUCH_CHANNEL_ID = env_int("VOUCH_CHANNEL_ID")
MARKET_SELLING_CHANNEL_ID = env_int("MARKET_SELLING_CHANNEL_ID")
MARKET_BUYING_CHANNEL_ID = env_int("MARKET_BUYING_CHANNEL_ID")
MARKETPLACE_CHANNEL_ID = env_int("MARKETPLACE_CHANNEL_ID")
MARKETPLACE_MANAGER_ROLE_ID = env_int("MARKETPLACE_MANAGER_ROLE_ID")
WELCOME_CHANNEL_ID = env_int("WELCOME_CHANNEL_ID")
TRANSCRIPT_CHANNEL_ID = env_int("TICKET_TRANSCRIPT_CHANNEL_ID", 1555693761747615756)
SUPPORT_TICKET_PANEL_CHANNEL_ID = env_int("SUPPORT_TICKET_PANEL_CHANNEL_ID")
SUPPORT_TICKET_CATEGORY_ID = env_int("SUPPORT_TICKET_CATEGORY_ID")
REPORT_TICKET_CATEGORY_ID = env_int("REPORT_TICKET_CATEGORY_ID")
PARTNERSHIP_TICKET_CATEGORY_ID = env_int("PARTNERSHIP_TICKET_CATEGORY_ID")
STAFF_DASHBOARD_CHANNEL_ID = env_int("STAFF_DASHBOARD_CHANNEL_ID")
AUDIT_LOG_CHANNEL_ID = env_int("AUDIT_LOG_CHANNEL_ID")
VERIFICATION_CHANNEL_ID = env_int("VERIFICATION_CHANNEL_ID")
VERIFIED_ROLE_IDS = env_int_list("VERIFIED_ROLE_IDS")
PENDING_VERIFICATION_ROLE_ID = env_int("PENDING_VERIFICATION_ROLE_ID")
SUPPORT_ROLE_IDS = env_int_list("SUPPORT_ROLE_IDS") or ((SUPPORT_ROLE_ID,) if SUPPORT_ROLE_ID else None)
MIDDLEMAN_IDS = env_int_list("MIDDLEMAN_IDS") or env_int_list("MIDDLEMEN_IDS") or ()
DONUTSMP_API_URL = os.getenv("DONUTSMP_API_URL")
TICKET_REMINDER_HOURS = max(1, env_int("TICKET_REMINDER_HOURS", 24))
TICKET_REVIEW_HOURS = max(TICKET_REMINDER_HOURS, env_int("TICKET_REVIEW_HOURS", 72))
TICKET_AUTO_CLOSE_HOURS = max(1, env_int("TICKET_AUTO_CLOSE_HOURS", 72))
DATABASE_BACKUP_INTERVAL_HOURS = max(1, env_int("DATABASE_BACKUP_INTERVAL_HOURS", 6))
WEBHOOK_OUTBOX_POLL_SECONDS = max(5, env_int("WEBHOOK_OUTBOX_POLL_SECONDS", 15))

REQUIRED_CONFIG = {
    "DISCORD_TOKEN": TOKEN,
    "TRANSACTION_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "DISCORD_GUILD_ID": GUILD_ID,
    "VOUCH_OVER_50_ROLE_ID": VOUCH_OVER_50_ROLE_ID,
    "VOUCH_OVER_75_ROLE_ID": VOUCH_OVER_75_ROLE_ID,
    "MODERATION_ROLE_ID": MODERATION_ROLE_ID,
    "APPLICATION_CHANNEL_ID": APPLICATION_CHANNEL_ID,
    "ACCEPTANCE_CHANNEL_ID": ACCEPTANCE_CHANNEL_ID,
    "APPLICATION_PANEL_CHANNEL_ID": APPLICATION_PANEL_CHANNEL_ID,
    "MIDDLEMEN_PANEL_CHANNEL_ID": PANEL_CHANNEL_ID,
    "MIDDLEMEN_CATEGORY_ID": TICKET_CATEGORY_ID,
    "MIDDLEMEN_SUPPORT_ROLE_ID": SUPPORT_ROLE_ID,
    "MIDDLEMAN_IDS": MIDDLEMAN_IDS,
    "VOUCH_CHANNEL_ID": VOUCH_CHANNEL_ID,
    "MARKET_SELLING_CHANNEL_ID": MARKET_SELLING_CHANNEL_ID,
    "MARKET_BUYING_CHANNEL_ID": MARKET_BUYING_CHANNEL_ID,
    "MARKETPLACE_CHANNEL_ID": MARKETPLACE_CHANNEL_ID,
    "MARKETPLACE_MANAGER_ROLE_ID": MARKETPLACE_MANAGER_ROLE_ID,
    "WELCOME_CHANNEL_ID": WELCOME_CHANNEL_ID,
    "TICKET_TRANSCRIPT_CHANNEL_ID": TRANSCRIPT_CHANNEL_ID,
    "SUPPORT_TICKET_PANEL_CHANNEL_ID": SUPPORT_TICKET_PANEL_CHANNEL_ID,
    "SUPPORT_TICKET_CATEGORY_ID": SUPPORT_TICKET_CATEGORY_ID,
    "REPORT_TICKET_CATEGORY_ID": REPORT_TICKET_CATEGORY_ID,
    "PARTNERSHIP_TICKET_CATEGORY_ID": PARTNERSHIP_TICKET_CATEGORY_ID,
    "STAFF_DASHBOARD_CHANNEL_ID": STAFF_DASHBOARD_CHANNEL_ID,
    "AUDIT_LOG_CHANNEL_ID": AUDIT_LOG_CHANNEL_ID,
    "VERIFICATION_CHANNEL_ID": VERIFICATION_CHANNEL_ID,
    "VERIFIED_ROLE_IDS": VERIFIED_ROLE_IDS,
    "PENDING_VERIFICATION_ROLE_ID": PENDING_VERIFICATION_ROLE_ID,
    "SUPPORT_ROLE_IDS": SUPPORT_ROLE_IDS,
}


def validate_config():
    missing = []
    invalid = []
    for name, value in REQUIRED_CONFIG.items():
        if value in (None, "") or (isinstance(value, (tuple, frozenset)) and not value):
            missing.append(name)
        elif isinstance(value, (tuple, frozenset)):
            if any(not isinstance(item, int) or item <= 0 for item in value):
                invalid.append(name)
        elif name not in {"DISCORD_TOKEN", "TRANSACTION_WEBHOOK_SECRET"} and (
            not isinstance(value, int) or value <= 0
        ):
            invalid.append(name)
    if missing or invalid:
        problems = []
        if missing:
            problems.append("Missing required configuration: " + ", ".join(missing))
        if invalid:
            problems.append("Configuration values must be positive integers: " + ", ".join(invalid))
        raise RuntimeError("; ".join(problems))
    return True