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
            patch.object(bot, "_ACTIVE_AI_REPORTS", set()),
            patch.object(bot, "_ACTIVE_ARCHIVE_QUESTIONS", set()),
            patch.object(
                bot,
                "get_due_weekly_report_period",
                return_value=(scheduled_at, since_utc, until_utc, "weekly period"),
            ),
            patch.object(
                bot,
                "build_control_center_report_artifact",
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
