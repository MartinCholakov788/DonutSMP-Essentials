import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(TEST_ROOT.name) / "bot.sqlite3")
os.environ["DATABASE_BACKUP_PATH"] = str(Path(TEST_ROOT.name) / "bot.sqlite3.backup")
os.environ["TRANSACTION_WEBHOOK_SECRET"] = "test-secret"

import database
import main
from commands.giveaway import parse_duration
from commands.ticket import ticket_creation_cooldown_remaining


class DatabaseTests(unittest.TestCase):
    def test_migrates_legacy_support_ticket_constraint(self):
        path = Path(TEST_ROOT.name) / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        database._migration_1(connection)
        connection.execute("ALTER TABLE support_tickets RENAME TO support_tickets_legacy")
        connection.execute(
            """CREATE TABLE support_tickets (
                ticket_number INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                creator_id INTEGER NOT NULL,
                ticket_type TEXT NOT NULL CHECK (ticket_type IN ('general', 'report', 'giveaway')),
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
        connection.execute("INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00+00:00')")
        connection.commit()
        connection.close()

        database.initialize_db(str(path))
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO support_tickets (ticket_number, guild_id, channel_id, creator_id, ticket_type, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2, 10, 21, 31, "partnership", "partner", "2026-01-01T00:00:00+00:00"),
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(support_tickets)")}
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_version")}
        connection.commit()
        connection.close()
        self.assertIn("last_activity_at", columns)
        self.assertEqual(max(versions), database.SCHEMA_VERSION)

    def test_backup_is_integrity_checked(self):
        source = Path(TEST_ROOT.name) / "source.sqlite3"
        backup = Path(TEST_ROOT.name) / "backup.sqlite3"
        connection = sqlite3.connect(source)
        connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_table VALUES ('ok')")
        connection.commit()
        connection.close()

        database.backup_database(str(source), str(backup))
        connection = sqlite3.connect(backup)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("SELECT value FROM values_table").fetchone()[0], "ok")
        connection.close()


class WorkflowTests(unittest.TestCase):
    def test_duration_parser(self):
        self.assertEqual(parse_duration("1d 6h"), 30 * 60 * 60)
        with self.assertRaises(ValueError):
            parse_duration("1h 1h")

    def test_ticket_creation_has_an_independent_cooldown(self):
        user_id = 987654321
        self.assertEqual(ticket_creation_cooldown_remaining(user_id), 0)
        self.assertGreater(ticket_creation_cooldown_remaining(user_id), 0)

    def test_ticket_activity_updates(self):
        main.initialize_db()
        timestamp = "2026-10-01T12:00:00+00:00"
        with main.db_session() as connection:
            connection.execute(
                "INSERT INTO support_tickets (ticket_number, guild_id, channel_id, creator_id, ticket_type, reason, created_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (900, 1, 901, 902, "general", "test", "2026-10-01T10:00:00+00:00", "2026-10-01T10:00:00+00:00"),
            )
        main.touch_support_ticket(900, timestamp)
        with main.db_session() as connection:
            value = connection.execute("SELECT last_activity_at FROM support_tickets WHERE ticket_number = 900").fetchone()[0]
        self.assertEqual(value, timestamp)

    def test_webhook_retries_are_idempotent(self):
        main.initialize_db()
        client = main.app.test_client()
        payload = {"event_id": "test-event-1", "to_player": "PlayerOne", "amount": 100}
        headers = {"X-Webhook-Secret": "test-secret"}
        first = client.post("/transaction", json=payload, headers=headers)
        second = client.post("/transaction", json=payload, headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        with main.db_session() as connection:
            count = connection.execute("SELECT COUNT(*) FROM transaction_events WHERE event_id = ?", ("test-event-1",)).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
