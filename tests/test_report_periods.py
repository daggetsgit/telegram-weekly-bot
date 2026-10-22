import asyncio
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import bot


class ReportPeriodTests(unittest.TestCase):
    def test_previous_monday_period_uses_previous_calendar_week(self):
        now_local = datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo("Asia/Bangkok"))

        since_utc, until_utc, label = bot.get_previous_monday_report_interval(now_local)

        self.assertEqual(
            since_utc,
            datetime(2026, 6, 22, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            until_utc,
            datetime(2026, 7, 1, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            label,
            "22.06.2026 09:00 — 01.07.2026 12:00 Asia/Bangkok",
        )

    def test_current_month_period_starts_at_first_day_midnight(self):
        now_local = datetime(2026, 8, 29, 12, 44, tzinfo=ZoneInfo("Asia/Bangkok"))

        since_utc, until_utc, label = bot.get_current_month_report_interval(now_local)

        self.assertEqual(
            since_utc,
            datetime(2026, 7, 31, 17, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            until_utc,
            datetime(2026, 8, 29, 5, 44, tzinfo=timezone.utc),
        )
        self.assertEqual(
            label,
            "01.08.2026 00:00 — 29.08.2026 12:44 Asia/Bangkok",
        )

    def test_weekly_scheduler_waits_until_friday_evening_on_fresh_deploy(self):
        now_local = datetime(2026, 10, 23, 19, 59, tzinfo=ZoneInfo("Asia/Bangkok"))

        due = bot.get_due_weekly_report_period(now_local=now_local, state={})

        self.assertIsNone(due)

    def test_weekly_scheduler_first_period_starts_previous_friday(self):
        now_local = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))

        scheduled_at, since_utc, until_utc, label = bot.get_due_weekly_report_period(
            now_local=now_local,
            state={},
        )

        self.assertEqual(scheduled_at, now_local)
        self.assertEqual(
            since_utc,
            (now_local - timedelta(days=7)).astimezone(timezone.utc),
        )
        self.assertEqual(until_utc, now_local.astimezone(timezone.utc))
        self.assertIn("16.10.2026 20:00", label)

    def test_weekly_scheduler_does_not_repeat_successful_slot(self):
        now_local = datetime(2026, 10, 23, 21, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        state = {"last_scheduled_at": "2026-10-23T20:00:00+07:00"}

        due = bot.get_due_weekly_report_period(now_local=now_local, state=state)

        self.assertIsNone(due)

    def test_weekly_scheduler_rate_limits_failed_slot_retries(self):
        scheduled_at = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        state = {
            "last_attempt_scheduled_at": scheduled_at.isoformat(),
            "last_attempt_at": "2026-10-23T20:01:00+07:00",
        }

        with patch.object(bot, "WEEKLY_AUTO_REPORT_RETRY_MINUTES", 15):
            self.assertFalse(
                bot.weekly_auto_report_retry_is_due(
                    scheduled_at,
                    now_local=datetime(2026, 10, 23, 20, 10, tzinfo=ZoneInfo("Asia/Bangkok")),
                    state=state,
                )
            )
            self.assertTrue(
                bot.weekly_auto_report_retry_is_due(
                    scheduled_at,
                    now_local=datetime(2026, 10, 23, 20, 16, tzinfo=ZoneInfo("Asia/Bangkok")),
                    state=state,
                )
            )

    def test_weekly_scheduler_does_not_rebuild_during_retry_cooldown(self):
        now_local = datetime(2026, 10, 23, 20, 10, tzinfo=ZoneInfo("Asia/Bangkok"))
        scheduled_at = now_local.replace(hour=20, minute=0)
        since_utc = datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc)
        until_utc = now_local.astimezone(timezone.utc)
        state = {
            "last_attempt_scheduled_at": scheduled_at.isoformat(),
            "last_attempt_at": "2026-10-23T20:01:00+07:00",
        }
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with (
            patch.object(bot, "WEEKLY_AUTO_REPORT_ENABLED", True),
            patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "test-key"),
            patch.object(bot, "ADMIN_USER_IDS", {100}),
            patch.object(bot, "WEEKLY_AUTO_REPORT_RETRY_MINUTES", 15),
            patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
            patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
            patch.object(
                bot,
                "get_due_weekly_report_period",
                return_value=(scheduled_at, since_utc, until_utc, "weekly period"),
            ),
            patch.object(bot, "load_weekly_auto_report_state", return_value=state),
            patch.object(bot, "build_ai_pdf_report_artifact") as build,
            patch.object(bot, "save_weekly_auto_report_state") as save_state,
        ):
            sent = asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=now_local))

        self.assertFalse(sent)
        build.assert_not_called()
        save_state.assert_not_called()

    def test_weekly_scheduler_saves_boundary_only_after_delivery(self):
        scheduled_at = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        since_utc = datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc)
        until_utc = datetime(2026, 10, 23, 13, 0, tzinfo=timezone.utc)
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with (
            patch.object(bot, "WEEKLY_AUTO_REPORT_ENABLED", True),
            patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "test-key"),
            patch.object(bot, "ADMIN_USER_IDS", {100}),
            patch.object(bot, "get_approved_report_chats", return_value=[{"chat_id": -1, "title": "Развитие"}]),
            patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
            patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
            patch.object(
                bot,
                "get_due_weekly_report_period",
                return_value=(scheduled_at, since_utc, until_utc, "weekly period"),
            ),
            patch.object(
                bot,
                "build_ai_pdf_report_artifact",
                return_value={
                    "message_count": 12,
                    "pdf_path": Path("report.pdf"),
                    "report_data": {"tasks": []},
                },
            ),
            patch.object(bot, "send_document_safely_to_chat", AsyncMock(return_value=True)) as send,
            patch.object(bot, "save_weekly_auto_report_state") as save_state,
        ):
            sent = asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=scheduled_at))

        self.assertTrue(sent)
        send.assert_awaited_once()
        saved = save_state.call_args.args[0]
        self.assertEqual(saved["last_scheduled_at"], scheduled_at.isoformat())
        self.assertEqual(saved["last_period_until"], until_utc.isoformat())

    def test_failed_delivery_keeps_previous_boundary_and_records_attempt(self):
        now_local = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        previous_boundary = "2026-10-16T13:00:00+00:00"
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "weekly.json"
            with (
                patch.object(bot, "WEEKLY_AUTO_REPORT_STATE_PATH", state_path),
                patch.object(bot, "WEEKLY_AUTO_REPORT_ENABLED", True),
                patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
                patch.object(bot, "OPENAI_API_KEY", "test-key"),
                patch.object(bot, "ADMIN_USER_IDS", {100}),
                patch.object(bot, "get_approved_report_chats", return_value=[{"chat_id": -1, "title": "Развитие"}]),
                patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
                patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
                patch.object(bot, "build_ai_pdf_report_artifact", return_value={
                    "message_count": 12,
                    "pdf_path": Path("report.pdf"),
                    "report_data": {"tasks": []},
                }) as build,
                patch.object(bot, "send_document_safely_to_chat", AsyncMock(return_value=False)),
            ):
                bot.save_weekly_auto_report_state({
                    "last_scheduled_at": "2026-10-16T20:00:00+07:00",
                    "last_period_until": previous_boundary,
                })
                sent = asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=now_local))
                saved = bot.load_weekly_auto_report_state()
                retry = asyncio.run(bot.run_weekly_auto_report_if_due(
                    app, now_local=now_local + timedelta(minutes=1)
                ))

        self.assertFalse(sent)
        self.assertFalse(retry)
        self.assertEqual(saved["last_period_until"], previous_boundary)
        self.assertEqual(saved["last_scheduled_at"], "2026-10-16T20:00:00+07:00")
        self.assertEqual(saved["last_attempt_at"], now_local.isoformat())
        build.assert_called_once()

    def test_weekly_status_shows_pending_retry_instead_of_only_next_week(self):
        now_local = datetime(2026, 10, 23, 20, 5, tzinfo=ZoneInfo("Asia/Bangkok"))
        state = {
            "last_attempt_scheduled_at": "2026-10-23T20:00:00+07:00",
            "last_attempt_at": "2026-10-23T20:01:00+07:00",
        }
        with (
            patch.object(bot, "WEEKLY_AUTO_REPORT_ENABLED", True),
            patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "test-key"),
            patch.object(bot, "ADMIN_USER_IDS", {100}),
            patch.object(bot, "WEEKLY_AUTO_REPORT_RETRY_MINUTES", 15),
            patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
            patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
        ):
            text = bot.format_weekly_report_status_text(now_local=now_local, state=state)

        self.assertIn("ожидает повторной попытки после ошибки", text)
        self.assertIn("Следующий плановый запуск: 30.10.2026 20:00", text)

    def test_weekly_reports_are_separate_and_retry_only_failed_chat(self):
        now = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        chats = [{"chat_id": -1, "title": "Развитие"}, {"chat_id": -2, "title": "Networking"}]
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = [root / "development.pdf", root / "networking.pdf"]
            for path in paths:
                path.write_bytes(b"test")
            artifacts = [{"message_count": 12, "pdf_path": path, "report_data": {}} for path in paths]
            with (
                patch.object(bot, "WEEKLY_AUTO_REPORT_STATE_PATH", root / "weekly.json"),
                patch.object(bot, "WEEKLY_AUTO_REPORT_ENABLED", True),
                patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
                patch.object(bot, "OPENAI_API_KEY", "test-key"),
                patch.object(bot, "ADMIN_USER_IDS", {100}),
                patch.object(bot, "get_approved_report_chats", return_value=chats),
                patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
                patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
                patch.object(bot, "build_ai_pdf_report_artifact", side_effect=artifacts) as build,
                patch.object(bot, "build_control_center_report_artifact") as aggregate,
                patch.object(bot, "send_document_safely_to_chat", AsyncMock(side_effect=[True, False, True])) as send,
            ):
                self.assertFalse(asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=now)))
                partial = bot.load_weekly_auto_report_state()
                self.assertEqual(partial["chats"]["-1"]["last_period_until"], now.astimezone(timezone.utc).isoformat())
                self.assertNotIn("last_period_until", partial["chats"]["-2"])
                self.assertTrue(asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=now + timedelta(minutes=16))))
                state = bot.load_weekly_auto_report_state()

        aggregate.assert_not_called()
        self.assertEqual(build.call_count, 2)
        self.assertEqual([call.args[0] for call in build.call_args_list], [-1, -2])
        self.assertEqual([call.args[2] for call in send.await_args_list], [paths[0], paths[1], paths[1]])
        self.assertEqual(state["chats"]["-2"]["last_period_until"], now.astimezone(timezone.utc).isoformat())

    def test_legacy_delivery_pause_preserves_progress_for_new_scheduler(self):
        import json

        now = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        previous = {"last_period_until": "2026-10-16T13:00:00+00:00"}
        guard = {
            "last_scheduled_at": "2099-12-31T00:00:00+00:00",
            "resume_state_for_individual_reports": previous,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "weekly.json"
            path.write_text(json.dumps(guard), encoding="utf-8")
            with patch.object(bot, "WEEKLY_AUTO_REPORT_STATE_PATH", path):
                self.assertEqual(bot.load_weekly_auto_report_state(), previous)
                self.assertIsNone(bot.get_due_weekly_report_period(now_local=now, state=guard))
                due = bot.get_due_weekly_report_period(now_local=now)
                self.assertEqual(due[1], datetime(2026, 10, 16, 13, tzinfo=timezone.utc))

    def test_weekly_report_retry_does_not_resend_to_successful_admin(self):
        now = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "report.pdf"
            pdf.write_bytes(b"test")
            with (
                patch.object(bot, "WEEKLY_AUTO_REPORT_STATE_PATH", root / "weekly.json"),
                patch.object(bot, "WEEKLY_AUTO_REPORT_ENABLED", True),
                patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
                patch.object(bot, "OPENAI_API_KEY", "test-key"),
                patch.object(bot, "ADMIN_USER_IDS", {100, 200}),
                patch.object(bot, "get_approved_report_chats", return_value=[{"chat_id": -1, "title": "Развитие"}]),
                patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
                patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
                patch.object(bot, "build_ai_pdf_report_artifact", return_value={"message_count": 12, "pdf_path": pdf, "report_data": {}}) as build,
                patch.object(bot, "send_document_safely_to_chat", AsyncMock(side_effect=[True, False, True])) as send,
            ):
                self.assertFalse(asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=now)))
                self.assertTrue(asyncio.run(bot.run_weekly_auto_report_if_due(app, now_local=now + timedelta(minutes=16))))
        build.assert_called_once()
        self.assertEqual([call.args[1] for call in send.await_args_list], [100, 200, 200])


class UpcomingEventsTests(unittest.TestCase):
    def test_only_future_dated_events_are_kept(self):
        now_local = datetime(2026, 8, 29, 12, 44, tzinfo=ZoneInfo("Asia/Bangkok"))
        items = [
            {
                "date": "2026-08-30",
                "time": "15:00",
                "timezone": "Asia/Bangkok",
                "title": "Созвон с партнёром",
                "participants_or_party": "Партнёр",
                "purpose": "Обсудить условия",
                "status": "подтверждено",
            },
            {
                "date": "2026-08-31",
                "time": "не указано",
                "timezone": "Asia/Bangkok",
                "title": "Встреча с клиникой",
                "participants_or_party": "Клиника",
                "status": "предварительно",
            },
            {
                "date": "2026-08-28",
                "time": "10:00",
                "timezone": "Asia/Bangkok",
                "title": "Прошедшая встреча",
            },
            {
                "title": "Надо созвониться",
                "time": "не указано",
            },
        ]

        events = bot.normalize_upcoming_events(items, now_local=now_local)

        self.assertEqual(
            [event["title"] for event in events],
            ["Созвон с партнёром", "Встреча с клиникой"],
        )

    def test_same_day_event_must_still_be_in_the_future(self):
        now_local = datetime(2026, 8, 29, 12, 44, tzinfo=ZoneInfo("Asia/Bangkok"))
        items = [
            {"date": "2026-08-29", "time": "12:30", "title": "Уже прошло"},
            {"date": "2026-08-29", "time": "13:00", "title": "Ещё впереди"},
        ]

        events = bot.normalize_upcoming_events(items, now_local=now_local)

        self.assertEqual([event["title"] for event in events], ["Ещё впереди"])


class BestServicePdfTests(unittest.TestCase):
    def test_branded_pdf_renders_without_logo(self):
        report_data = {
            "summary": ["Проверка фирменного PDF с русским текстом."],
            "tasks": [{
                "task": "Запросить у партнёра подтверждение условий",
                "owner": "не назначен",
                "deadline": "не указан",
                "context": "Нужен письменный ответ.",
                "status": "новая",
            }],
            "upcoming_events": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "report.pdf"
            with patch.object(bot, "AI_REPORT_LOGO_PATH", Path("missing-logo.png")):
                bot.render_one_page_report_pdf(
                    "",
                    "Тестовый чат",
                    "последние 7 дней",
                    output_path,
                    report_data=report_data,
                )

            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 1000)
            self.assertEqual(output_path.read_bytes()[:4], b"%PDF")


if __name__ == "__main__":
    unittest.main()
