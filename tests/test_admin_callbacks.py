import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


def make_callback_update(data: str, user_id: int = 1, chat_type: str = "private"):
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        chat_id=100,
        reply_text=AsyncMock(),
        edit_text=AsyncMock(),
    )
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
    )
    return update, query


class AdminCallbackSafetyTests(unittest.TestCase):
    def test_start_stays_quiet_in_groups(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="supergroup"),
            effective_user=SimpleNamespace(id=1),
            message=message,
        )

        asyncio.run(bot.start(update, SimpleNamespace()))

        message.reply_text.assert_not_awaited()

    def test_work_package_callback_stays_quiet_in_groups(self):
        update, query = make_callback_update("workpkg:week:-1001", chat_type="supergroup")

        with patch.object(bot, "ADMIN_USER_IDS", {1}):
            asyncio.run(bot.work_package_callback(update, SimpleNamespace()))

        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query.message.reply_text.assert_not_awaited()

    def test_participant_callback_rejects_non_admin(self):
        update, query = make_callback_update("person:list:-1001", user_id=2)

        with patch.object(bot, "ADMIN_USER_IDS", {1}):
            asyncio.run(bot.participant_report_callback(update, SimpleNamespace()))

        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query.message.edit_text.assert_not_awaited()

    def test_participant_callback_stays_quiet_in_groups(self):
        update, query = make_callback_update("person:list:-1001", chat_type="group")

        with patch.object(bot, "ADMIN_USER_IDS", {1}):
            asyncio.run(bot.participant_report_callback(update, SimpleNamespace()))

        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query.message.edit_text.assert_not_awaited()

    def test_ai_pdf_report_is_scheduled_as_background_task(self):
        scheduled = []

        class FakeApplication:
            def create_task(self, coroutine, update=None, name=None):
                scheduled.append((update, name))
                coroutine.close()
                return SimpleNamespace()

        context = SimpleNamespace(application=FakeApplication())
        update = SimpleNamespace(update_id=123)

        bot.schedule_ai_pdf_report(
            context,
            update,
            100,
            -1001,
            "Рабочий чат",
            "week",
        )

        self.assertEqual(scheduled, [(update, "ai-pdf--1001-week")])

    def test_duplicate_ai_pdf_report_is_rejected_before_build(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        key = (100, -1001, "week", "")

        with (
            patch.object(bot, "OPENAI_REPORTS_ENABLED", True),
            patch.object(bot, "OPENAI_API_KEY", "test-key"),
            patch.object(bot, "_ACTIVE_AI_REPORTS", {key}),
            patch.object(bot, "get_ai_report_interval") as get_interval,
        ):
            asyncio.run(
                bot.send_ai_pdf_report_for_source_chat(
                    context,
                    100,
                    -1001,
                    "Рабочий чат",
                    "week",
                )
            )

        get_interval.assert_not_called()
        context.bot.send_message.assert_awaited_once_with(
            chat_id=100,
            text="Другой AI PDF уже готовится. Дождитесь его завершения.",
        )

    def test_control_center_button_is_shown_when_ai_reports_are_enabled(self):
        with patch.object(bot, "OPENAI_REPORTS_ENABLED", True):
            keyboard = bot.get_private_admin_inline_keyboard()

        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("admin:control", callback_data)
        self.assertIn("admin:control_latest", callback_data)
        self.assertIn("admin:ask", callback_data)

    def test_control_center_callback_schedules_private_admin_report(self):
        update, query = make_callback_update("admin:control:week")
        context = SimpleNamespace()

        with (
            patch.object(bot, "ADMIN_USER_IDS", {1}),
            patch.object(bot, "schedule_control_center_report") as schedule,
        ):
            asyncio.run(bot.admin_menu_callback(update, context))

        schedule.assert_called_once_with(context, update, 100, "week")
        query.message.edit_text.assert_awaited_once()

    def test_archive_question_input_schedules_background_answer(self):
        scheduled = []

        class FakeApplication:
            def create_task(self, coroutine, update=None, name=None):
                scheduled.append((update, name))
                coroutine.close()

        message = SimpleNamespace(
            text="Что ещё ждём от партнёра?",
            chat_id=100,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
            message=message,
        )
        context = SimpleNamespace(
            user_data={"archive_question": {
                "chat_id": -1001,
                "chat_title": "Рабочий чат",
                "mode": "week",
            }},
            application=FakeApplication(),
        )

        with patch.object(bot, "ADMIN_USER_IDS", {1}):
            handled = asyncio.run(bot.handle_archive_question_input(update, context))

        self.assertTrue(handled)
        self.assertNotIn("archive_question", context.user_data)
        self.assertEqual(scheduled[0][1], "archive-question--1001")
        message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
