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


if __name__ == "__main__":
    unittest.main()
