import asyncio
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

import bot


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.files_dir = self.data_dir / "files"
        self.exports_dir = self.data_dir / "exports"
        self.transcripts_dir = self.data_dir / "transcripts"
        self.data_dir.mkdir()
        self.files_dir.mkdir()
        self.exports_dir.mkdir()
        self.transcripts_dir.mkdir()

        self.originals = {
            "BASE_DIR": bot.BASE_DIR,
            "DATA_DIR": bot.DATA_DIR,
            "FILES_DIR": bot.FILES_DIR,
            "EXPORT_DIR": bot.EXPORT_DIR,
            "TRANSCRIPTS_DIR": bot.TRANSCRIPTS_DIR,
            "WORK_ANALYSIS_PROMPT_PATH": bot.WORK_ANALYSIS_PROMPT_PATH,
            "DB_PATH": bot.DB_PATH,
        }
        bot.BASE_DIR = self.root
        bot.DATA_DIR = self.data_dir
        bot.FILES_DIR = self.files_dir
        bot.EXPORT_DIR = self.exports_dir
        bot.TRANSCRIPTS_DIR = self.transcripts_dir
        bot.WORK_ANALYSIS_PROMPT_PATH = Path("data/config/work_analysis_prompt.txt")
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

    def test_positive_and_bounded_env_parsers_use_safe_defaults(self):
        with patch.dict(os.environ, {"TEST_VALUE": "0"}):
            self.assertEqual(bot.get_positive_int_env("TEST_VALUE", 10), 10)
        with patch.dict(os.environ, {"TEST_VALUE": "24"}):
            self.assertEqual(bot.get_bounded_int_env("TEST_VALUE", 9, 0, 23), 9)
        with patch.dict(os.environ, {"TEST_VALUE": "23"}):
            self.assertEqual(bot.get_bounded_int_env("TEST_VALUE", 9, 0, 23), 23)

    def test_metadata_backup_contains_database_prompt_and_transcripts(self):
        self.add_message(1, age_days=1)
        prompt_path = self.data_dir / "config" / "work_analysis_prompt.txt"
        prompt_path.parent.mkdir()
        prompt_path.write_text("Prompt {chat_title} {period_label}", encoding="utf-8")
        transcript_path = self.transcripts_dir / "2026-10-22" / "voice.transcript.txt"
        transcript_path.parent.mkdir()
        transcript_path.write_text("Тестовая расшифровка", encoding="utf-8")

        backup_path, transcript_count = bot.create_metadata_backup()

        self.assertEqual(transcript_count, 1)
        self.assertTrue(backup_path.exists())
        with zipfile.ZipFile(backup_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "messages.db",
                    "config/work_analysis_prompt.txt",
                    "transcripts/2026-10-22/voice.transcript.txt",
                    "BACKUP_INFO.txt",
                },
            )
            extracted_db = self.root / "restored.db"
            extracted_db.write_bytes(archive.read("messages.db"))

        conn = sqlite3.connect(extracted_db)
        row_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        self.assertEqual(row_count, 1)

    def test_storage_status_reports_history_range_and_expired_attachments(self):
        old_file = self.files_dir / "old.txt"
        old_file.write_text("old", encoding="utf-8")
        self.add_message(1, age_days=20, file_path=str(old_file.relative_to(self.root)))
        self.add_message(2, age_days=1)
        bot.cleanup_old_data(file_retention_days=14, message_retention_days=0)

        status = bot.get_storage_status_data()

        self.assertEqual(status["message_count"], 2)
        self.assertEqual(status["expired_attachment_records"], 1)
        self.assertIsNotNone(status["oldest_message_at"])
        self.assertIsNotNone(status["newest_message_at"])
        self.assertLess(status["oldest_message_at"], status["newest_message_at"])

    def test_export_cleanup_only_deletes_old_export_files(self):
        old_file = self.exports_dir / "old-report.pdf"
        nested_old_file = self.exports_dir / "nested" / "old-package.zip"
        recent_file = self.exports_dir / "recent-report.pdf"
        nested_old_file.parent.mkdir()
        old_file.write_bytes(b"old")
        nested_old_file.write_bytes(b"nested-old")
        recent_file.write_bytes(b"recent")

        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(old_file, (old_timestamp, old_timestamp))
        os.utime(nested_old_file, (old_timestamp, old_timestamp))

        candidates = bot.get_old_export_files(days=7)
        self.assertEqual(
            {path for path, _size in candidates},
            {old_file, nested_old_file},
        )

        deleted_files, freed_bytes = bot.cleanup_old_exports(days=7)

        self.assertEqual(deleted_files, 2)
        self.assertEqual(freed_bytes, len(b"old") + len(b"nested-old"))
        self.assertFalse(old_file.exists())
        self.assertFalse(nested_old_file.exists())
        self.assertTrue(recent_file.exists())

    def test_cleanup_is_throttled_while_processing_messages(self):
        original_last_cleanup = bot._LAST_RETENTION_CLEANUP_MONOTONIC
        original_interval = bot.RETENTION_CLEANUP_INTERVAL_SECONDS
        bot._LAST_RETENTION_CLEANUP_MONOTONIC = None
        bot.RETENTION_CLEANUP_INTERVAL_SECONDS = 3600
        try:
            with patch.object(bot.time, "monotonic", side_effect=[100.0, 200.0, 3800.0]):
                with patch.object(bot, "cleanup_old_data", return_value=(0, 0, 0)) as cleanup:
                    self.assertEqual(bot.maybe_cleanup_old_data(), (0, 0, 0))
                    self.assertIsNone(bot.maybe_cleanup_old_data())
                    self.assertEqual(bot.maybe_cleanup_old_data(), (0, 0, 0))
                    self.assertEqual(cleanup.call_count, 2)
        finally:
            bot._LAST_RETENTION_CLEANUP_MONOTONIC = original_last_cleanup
            bot.RETENTION_CLEANUP_INTERVAL_SECONDS = original_interval

    def test_failed_attachment_download_returns_skipped_metadata(self):
        document = SimpleNamespace(
            file_id="file-id",
            file_unique_id="unique-id",
            file_name="document.pdf",
            mime_type="application/pdf",
            file_size=1024,
        )
        message = SimpleNamespace(
            document=document,
            photo=None,
            video=None,
            voice=None,
            audio=None,
            animation=None,
            text=None,
            sticker=None,
            chat=SimpleNamespace(id=-1001),
            message_id=42,
            date=datetime.now(timezone.utc),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_file=AsyncMock(side_effect=TimeoutError("test timeout")))
        )

        file_info = asyncio.run(bot.download_attachment(message, context))

        self.assertEqual(file_info["file_id"], "file-id")
        self.assertEqual(file_info["file_path"], None)
        self.assertEqual(file_info["file_skipped"], 1)
        self.assertEqual(
            file_info["file_skip_reason"],
            "Telegram attachment download failed: TimeoutError",
        )

    def test_all_history_interval_starts_with_earliest_saved_message(self):
        earliest = datetime(2026, 8, 29, 6, 15, tzinfo=timezone.utc)
        latest = datetime(2026, 10, 20, 14, 2, tzinfo=timezone.utc)
        conn = sqlite3.connect(bot.DB_PATH)
        for message_id, message_date in ((1, earliest), (2, latest)):
            conn.execute(
                """
                INSERT INTO messages (
                    chat_id, message_id, date_utc, message_type, text, created_at
                ) VALUES (?, ?, ?, 'text', ?, ?)
                """,
                (77, message_id, message_date.isoformat(), f"message {message_id}", message_date.isoformat()),
            )
        conn.commit()
        conn.close()
        until = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)

        since_dt, until_dt, label = bot.get_chat_history_interval(77, until_dt=until)

        self.assertEqual(since_dt, earliest)
        self.assertEqual(until_dt, until)
        self.assertEqual(
            label,
            "29.08.2026 13:15 — 22.10.2026 22:00 Asia/Bangkok",
        )

    def test_chat_participants_are_built_from_saved_messages(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(bot.DB_PATH)
        conn.executemany(
            """
            INSERT INTO messages (
                chat_id, message_id, date_utc, user_id, username,
                full_name, message_type, text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'text', ?, ?)
            """,
            [
                (77, 1, now, 1001, "juliana", "Юлианна", "Первая реплика", now),
                (77, 2, now, 1001, "juliana", "Юлианна", "Вторая реплика", now),
                (77, 3, now, 1002, "ivan", "Иван", "Сообщение", now),
            ],
        )
        conn.commit()
        conn.close()

        participants = bot.get_chat_participants(77)
        juliana = bot.get_chat_participant(77, 1001)

        self.assertEqual({item["name"] for item in participants}, {"Юлианна", "Иван"})
        self.assertEqual(juliana["message_count"], 2)
        self.assertEqual(juliana["username"], "juliana")
        self.assertEqual(bot.get_participant_report_label(juliana), "Юлианна (@juliana)")

    def test_person_report_focus_only_targets_assigned_tasks(self):
        focus = bot.build_person_tasks_report_focus("Юлианна")

        self.assertIn("поставлены этому участнику", focus)
        self.assertIn("Не включай задачи, которые этот участник поставил другим", focus)
        self.assertIn("Юлианна", focus)


if __name__ == "__main__":
    unittest.main()
