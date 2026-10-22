import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


if __name__ == "__main__":
    unittest.main()
