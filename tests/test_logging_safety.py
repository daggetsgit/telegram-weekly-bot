import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot


class SecretLoggingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.formatter = bot.SecretRedactingFormatter(bot.LOG_FORMAT)

    def test_redacts_telegram_and_openai_secrets_from_message(self):
        telegram_token = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
        openai_key = "sk-proj-abcdefghijklmnopqrstuvwxyz012345"
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "Telegram=%s OpenAI=%s",
            (telegram_token, openai_key),
            None,
        )

        rendered = self.formatter.format(record)

        self.assertNotIn(telegram_token, rendered)
        self.assertNotIn(openai_key, rendered)
        self.assertIn("[REDACTED_TELEGRAM_TOKEN]", rendered)
        self.assertIn("[REDACTED_OPENAI_KEY]", rendered)

    def test_redacts_token_embedded_in_exception_traceback(self):
        token = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
        try:
            raise RuntimeError(f"The token `{token}` was rejected by the server.")
        except RuntimeError:
            record = logging.LogRecord(
                "test",
                logging.ERROR,
                __file__,
                1,
                "Telegram initialization failed",
                (),
                __import__("sys").exc_info(),
            )

        rendered = self.formatter.format(record)

        self.assertNotIn(token, rendered)
        self.assertIn("[REDACTED_TELEGRAM_TOKEN]", rendered)

    def test_application_error_handler_logs_only_update_id(self):
        update = SimpleNamespace(update_id=321, message=SimpleNamespace(text="sensitive message"))
        error = RuntimeError("safe failure")
        context = SimpleNamespace(error=error)

        with patch.object(bot.logging, "error") as log_error:
            asyncio.run(bot.application_error_handler(update, context))

        log_error.assert_called_once()
        args, kwargs = log_error.call_args
        self.assertEqual(args[1], 321)
        self.assertNotIn("sensitive message", str(log_error.call_args))
        self.assertEqual(kwargs["exc_info"][1], error)


if __name__ == "__main__":
    unittest.main()
