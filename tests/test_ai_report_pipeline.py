import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class FakeResponses:
    def __init__(self, result_text):
        self.result_text = result_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("image input failed")
        return SimpleNamespace(output_text=self.result_text)


class AiReportImageInputTests(unittest.TestCase):
    def test_contact_sheet_image_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = []
            for index in range(3):
                path = Path(temp_dir) / f"sheet-{index}.jpg"
                path.write_bytes(f"image-{index}".encode("ascii"))
                paths.append(path)

            with patch.object(bot, "OPENAI_REPORT_INCLUDE_IMAGES", True), patch.object(
                bot, "OPENAI_REPORT_MAX_IMAGES", 2
            ):
                image_inputs = bot.build_openai_report_image_inputs(paths)

        self.assertEqual(len(image_inputs), 2)
        self.assertTrue(image_inputs[0]["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(image_inputs[0]["type"], "input_image")

    def test_images_can_be_disabled(self):
        with patch.object(bot, "OPENAI_REPORT_INCLUDE_IMAGES", False):
            self.assertEqual(bot.build_openai_report_image_inputs([Path("unused.jpg")]), [])

    def test_analysis_retries_without_images(self):
        analysis_data = {
            "source_facts": [{"fact": "Факт", "source_type": "chat"}],
            "action_items": [],
            "upcoming_events": [],
        }
        responses = FakeResponses(json.dumps(analysis_data, ensure_ascii=False))
        client = SimpleNamespace(responses=responses)

        with patch.object(bot.logging, "exception") as log_exception:
            result = bot.run_ai_report_analysis_pass(
                client,
                "Тестовый чат",
                "последние 7 дней",
                "Правила анализа",
                "Экспорт чата",
                "Индекс вложений",
                "",
                [{"type": "input_image", "image_url": "data:image/jpeg;base64,ZmFrZQ=="}],
                "2026-10-22T23:00+07:00",
            )

        self.assertEqual(result, analysis_data)
        self.assertEqual(len(responses.calls), 2)
        self.assertIsInstance(responses.calls[0]["input"][1]["content"], list)
        self.assertIsInstance(responses.calls[1]["input"][1]["content"], str)
        self.assertIn(
            "недоверенными рабочими источниками",
            responses.calls[0]["input"][0]["content"],
        )
        log_exception.assert_called_once()


class AiReportTaskRenderingTests(unittest.TestCase):
    def test_task_status_is_not_rewritten_as_missing(self):
        self.assertEqual(
            bot.compact_pdf_short_value("требуется подтверждение", "status"),
            "требуется подтверждение",
        )

    def test_missing_owner_and_deadline_are_shortened_by_column(self):
        self.assertEqual(
            bot.compact_pdf_short_value("не указан / требуется назначить", "owner"),
            "не назначен",
        )
        self.assertEqual(
            bot.compact_pdf_short_value("не указан / требуется подтверждение", "deadline"),
            "не указан",
        )


class WorkPackageEmptyPeriodTests(unittest.TestCase):
    def test_shift_package_stops_before_building_empty_files(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with patch.object(bot, "prepare_transcripts_for_period"), patch.object(
            bot, "export_interval", return_value=(Path("empty.md"), 0)
        ), patch.object(bot, "create_attachments_index") as create_index:
            asyncio.run(bot.send_work_package_for_source_chat(
                context,
                target_chat_id=10,
                source_chat_id=-1001,
                source_chat_title="Рабочий чат",
                mode="shift",
            ))

        create_index.assert_not_called()
        self.assertIn(
            "сообщений не найдено",
            context.bot.send_message.await_args_list[-1].kwargs["text"],
        )

    def test_week_package_stops_before_building_empty_files(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with patch.object(bot, "prepare_transcripts_for_period"), patch.object(
            bot, "export_last_days", return_value=(Path("empty.md"), 0)
        ), patch.object(bot, "create_attachments_index") as create_index:
            asyncio.run(bot.send_work_package_for_source_chat(
                context,
                target_chat_id=10,
                source_chat_id=-1001,
                source_chat_title="Рабочий чат",
                mode="week",
            ))

        create_index.assert_not_called()
        self.assertIn(
            "сообщений не найдено",
            context.bot.send_message.await_args_list[-1].kwargs["text"],
        )


if __name__ == "__main__":
    unittest.main()
