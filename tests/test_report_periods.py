import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

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
