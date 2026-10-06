import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone


SCHEMA_VERSION = 7


def connect_db(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def db_session(path):
    connection = connect_db(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _columns(connection, table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_columns(connection, table, definitions):
    columns = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_1(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ticket_counter (id INTEGER PRIMARY KEY CHECK (id = 1), last_number INTEGER NOT NULL);
        INSERT OR IGNORE INTO ticket_counter VALUES (1, 0);
        CREATE TABLE IF NOT EXISTS support_ticket_counters (guild_id INTEGER PRIMARY KEY, last_number INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tickets (ticket_number INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL UNIQUE, status_message_id INTEGER, creator_id INTEGER NOT NULL, seller_id INTEGER NOT NULL DEFAULT 0, buyer_id INTEGER NOT NULL, seller_ign TEXT, buyer_ign TEXT, amount TEXT, pending_amount TEXT, price_proposed_by INTEGER NOT NULL DEFAULT 0, price_confirmed INTEGER NOT NULL DEFAULT 0, payment_reported INTEGER NOT NULL DEFAULT 0, payment_verified INTEGER NOT NULL DEFAULT 0, item_delivered INTEGER NOT NULL DEFAULT 0, delivery_confirmed INTEGER NOT NULL DEFAULT 0, funds_released INTEGER NOT NULL DEFAULT 0, close_seller_confirmed INTEGER NOT NULL DEFAULT 0, close_buyer_confirmed INTEGER NOT NULL DEFAULT 0, activity_at TEXT NOT NULL DEFAULT '', last_reminded_at TEXT, flagged_at TEXT, transcript_sent_at TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS transaction_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, player TEXT NOT NULL COLLATE NOCASE, from_player TEXT COLLATE NOCASE, to_player TEXT COLLATE NOCASE, amount TEXT NOT NULL, received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS webhook_outbox (event_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, last_error TEXT, delivered_at TEXT);
        CREATE TABLE IF NOT EXISTS player_vouches (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, player TEXT NOT NULL COLLATE NOCASE, target_id INTEGER NOT NULL DEFAULT 0, author_id INTEGER NOT NULL, verdict TEXT NOT NULL, stars INTEGER NOT NULL, reason TEXT NOT NULL, evidence_url TEXT, evidence_filename TEXT, created_at TEXT NOT NULL, UNIQUE (guild_id, player, author_id));
        CREATE TABLE IF NOT EXISTS marketplace_listings (listing_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, owner_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, closed_at TEXT);
        CREATE TABLE IF NOT EXISTS marketplace_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, category TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, price TEXT NOT NULL, stock INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', created_by INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS giveaways (giveaway_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER UNIQUE, host_id INTEGER NOT NULL, prize TEXT NOT NULL, conditions TEXT, required_role_id INTEGER, winner_count INTEGER NOT NULL DEFAULT 1, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', winner_ids TEXT, created_at TEXT NOT NULL, ended_at TEXT, ended_by INTEGER);
        CREATE TABLE IF NOT EXISTS giveaway_entries (giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL, entered_at TEXT NOT NULL, PRIMARY KEY (giveaway_id, user_id));
        CREATE TABLE IF NOT EXISTS support_tickets (ticket_number INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL UNIQUE, creator_id INTEGER NOT NULL, ticket_type TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', claimed_by INTEGER, created_at TEXT NOT NULL, closed_at TEXT, closed_by INTEGER, deleted_at TEXT, deleted_by INTEGER);
        CREATE TABLE IF NOT EXISTS staff_applications (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, answers TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', decision_reason TEXT, decided_by INTEGER, review_message_id INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, actor_id INTEGER, action TEXT NOT NULL, target_id TEXT, details TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS staff_warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, staff_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS moderation_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, target_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, duration_seconds INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_attempts (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, locked_until TEXT, PRIMARY KEY (guild_id, user_id));
        CREATE TABLE IF NOT EXISTS verified_accounts (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, minecraft_name TEXT NOT NULL COLLATE NOCASE, verified_at TEXT NOT NULL, PRIMARY KEY (guild_id, user_id), UNIQUE (guild_id, minecraft_name));
        CREATE TABLE IF NOT EXISTS bot_settings (guild_id INTEGER NOT NULL, setting TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (guild_id, setting));
        CREATE TABLE IF NOT EXISTS transaction_matches (event_id TEXT NOT NULL, ticket_number INTEGER NOT NULL, match_status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (event_id, ticket_number));
        """
    )


def _migration_2(connection):
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'support_tickets'"
    ).fetchone()
    if table_sql and "ticket_type IN ('general', 'report', 'giveaway')" in table_sql[0]:
        connection.execute("ALTER TABLE support_tickets RENAME TO support_tickets_legacy")
        connection.execute(
            """CREATE TABLE support_tickets (
                ticket_number INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                creator_id INTEGER NOT NULL,
                ticket_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                claimed_by INTEGER,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                closed_by INTEGER,
                deleted_at TEXT,
                deleted_by INTEGER
            )"""
        )
        connection.execute(
            """INSERT INTO support_tickets
               SELECT ticket_number, guild_id, channel_id, creator_id, ticket_type,
                      reason, status, claimed_by, created_at, closed_at, closed_by,
                      deleted_at, deleted_by
               FROM support_tickets_legacy"""
        )
        connection.execute("DROP TABLE support_tickets_legacy")
    _add_columns(connection, "support_tickets", {"priority": "TEXT NOT NULL DEFAULT 'normal'", "last_reminded_at": "TEXT", "auto_closed_at": "TEXT", "last_activity_at": "TEXT"})
    connection.execute("UPDATE support_tickets SET last_activity_at = COALESCE(last_activity_at, created_at)")
    _add_columns(connection, "giveaways", {"outcome_status": "TEXT", "outcome_attempts": "INTEGER NOT NULL DEFAULT 0", "outcome_error": "TEXT", "outcome_published_at": "TEXT", "recovery_at": "TEXT"})
    _add_columns(connection, "audit_events", {"event_id": "TEXT", "category": "TEXT", "severity": "TEXT NOT NULL DEFAULT 'info'", "metadata_json": "TEXT", "request_id": "TEXT"})


def _migration_3(connection):
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_transaction_event_id ON transaction_events(event_id) WHERE event_id IS NOT NULL")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_vouch_per_member_target ON player_vouches(guild_id, target_id, author_id) WHERE target_id > 0")
    connection.execute("CREATE INDEX IF NOT EXISTS support_ticket_activity_idx ON support_tickets(guild_id, status, last_activity_at)")


def _migration_4(connection):
    _add_columns(connection, "tickets", {"last_activity_at": "TEXT"})
    connection.execute("UPDATE tickets SET last_activity_at = COALESCE(last_activity_at, activity_at, created_at)")


def _migration_5(connection):
    _add_columns(connection, "tickets", {
        "deal_code": "TEXT",
        "middleman_id": "INTEGER",
        "creator_role": "TEXT",
    })
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS tickets_deal_code_idx ON tickets(deal_code) WHERE deal_code IS NOT NULL")


def _migration_6(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS invite_uses (
            guild_id INTEGER NOT NULL,
            joined_user_id INTEGER NOT NULL,
            inviter_id INTEGER,
            invite_code TEXT,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, joined_user_id)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS invite_uses_inviter_idx ON invite_uses(guild_id, inviter_id)")


def _migration_7(connection):
    _add_columns(
        connection,
        "tickets",
        {
            "middleman_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "money_received": "INTEGER NOT NULL DEFAULT 0",
        },
    )


MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
    7: _migration_7,
}


def initialize_db(path):
    with db_session(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_version")}
        for version in range(1, SCHEMA_VERSION + 1):
            if version not in applied:
                MIGRATIONS[version](connection)
                connection.execute("INSERT INTO schema_version VALUES (?, ?)", (version, datetime.now(timezone.utc).isoformat()))


def backup_database(path, backup_path, rotations=3):
    directory = os.path.dirname(os.path.abspath(backup_path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".middleman-", suffix=".sqlite3", dir=directory, delete=False) as temporary:
            temporary_path = temporary.name
        source = connect_db(path)
        destination = connect_db(temporary_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        verified = connect_db(temporary_path)
        try:
            if verified.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("backup integrity check failed")
        finally:
            verified.close()
        for index in range(rotations - 1, 0, -1):
            older = f"{backup_path}.{index}"
            if os.path.exists(older):
                os.replace(older, f"{backup_path}.{index + 1}")
        if os.path.exists(backup_path):
            os.replace(backup_path, f"{backup_path}.1")
        os.replace(temporary_path, backup_path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)