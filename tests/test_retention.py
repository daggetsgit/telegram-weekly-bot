import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.files_dir = self.data_dir / "files"
        self.data_dir.mkdir()
        self.files_dir.mkdir()

        self.originals = {
            "BASE_DIR": bot.BASE_DIR,
            "DATA_DIR": bot.DATA_DIR,
            "FILES_DIR": bot.FILES_DIR,
            "DB_PATH": bot.DB_PATH,
        }
        bot.BASE_DIR = self.root
        bot.DATA_DIR = self.data_dir
        bot.FILES_DIR = self.files_dir
        bot.DB_PATH = self.data_dir / "messages.db"
        bot.init_db()

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(bot, name, value)
        self.temp_dir.cleanup()

    def add_message(self, message_id, age_days, file_path=None):
        date_utc = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute(
            """
            INSERT INTO messages (
                chat_id, message_id, date_utc, message_type, text,
                file_path, file_skipped, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (1, message_id, date_utc, "text", f"message {message_id}", file_path, date_utc),
        )
        conn.commit()
        conn.close()

    def test_attachment_cleanup_keeps_message_history(self):
        old_file = self.files_dir / "old.txt"
        old_file.write_text("old", encoding="utf-8")
        self.add_message(1, age_days=20, file_path=str(old_file.relative_to(self.root)))
        self.add_message(2, age_days=20)
        self.add_message(3, age_days=1)

        deleted_messages, deleted_files, expired_records = bot.cleanup_old_data(
            file_retention_days=14,
            message_retention_days=0,
        )

        self.assertEqual(deleted_messages, 0)
        self.assertEqual(deleted_files, 1)
        self.assertEqual(expired_records, 1)
        self.assertFalse(old_file.exists())

        conn = sqlite3.connect(bot.DB_PATH)
        rows = conn.execute(
            "SELECT message_id, file_path, file_skipped, file_skip_reason "
            "FROM messages ORDER BY message_id"
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0][1])
        self.assertEqual(rows[0][2], 1)
        self.assertIn("message text and metadata retained", rows[0][3])

    def test_message_retention_can_delete_old_rows(self):
        old_file = self.files_dir / "old.txt"
        old_file.write_text("old", encoding="utf-8")
        self.add_message(1, age_days=40, file_path=str(old_file.relative_to(self.root)))
        self.add_message(2, age_days=1)

        deleted_messages, deleted_files, expired_records = bot.cleanup_old_data(
            file_retention_days=0,
            message_retention_days=30,
        )

        self.assertEqual(deleted_messages, 1)
        self.assertEqual(deleted_files, 1)
        self.assertEqual(expired_records, 0)

        conn = sqlite3.connect(bot.DB_PATH)
        message_ids = [row[0] for row in conn.execute("SELECT message_id FROM messages")]
        conn.close()
        self.assertEqual(message_ids, [2])

    def test_nonnegative_env_parser_rejects_negative_and_invalid_values(self):
        with patch.dict(os.environ, {"TEST_RETENTION": "-5"}):
            self.assertEqual(bot.get_nonnegative_int_env("TEST_RETENTION", 14), 14)
        with patch.dict(os.environ, {"TEST_RETENTION": "bad"}):
            self.assertEqual(bot.get_nonnegative_int_env("TEST_RETENTION", 14), 14)
        with patch.dict(os.environ, {"TEST_RETENTION": "0"}):
            self.assertEqual(bot.get_nonnegative_int_env("TEST_RETENTION", 14), 0)


if __name__ == "__main__":
    unittest.main()
