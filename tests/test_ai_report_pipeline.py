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


class SequencedResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class AiReportImageInputTests(unittest.TestCase):
    def test_temperature_error_retries_without_temperature(self):
        response = SimpleNamespace(output_text="{}")
        responses = SequencedResponses([
            RuntimeError("Unsupported parameter: temperature"),
            response,
        ])

        result = bot.create_openai_report_response(
            SimpleNamespace(responses=responses),
            [{"role": "user", "content": "test"}],
        )

        self.assertIs(result, response)
        self.assertEqual(len(responses.calls), 2)
        self.assertIn("temperature", responses.calls[0])
        self.assertNotIn("temperature", responses.calls[1])

    def test_unrelated_unsupported_error_is_not_retried(self):
        responses = SequencedResponses([RuntimeError("unsupported image format")])

        with self.assertRaisesRegex(RuntimeError, "unsupported image format"):
            bot.create_openai_report_response(
                SimpleNamespace(responses=responses),
                [{"role": "user", "content": "test"}],
            )

        self.assertEqual(len(responses.calls), 1)

    def test_text_truncation_respects_exact_budget_and_keeps_both_ends(self):
        source = "BEGIN-" + ("x" * 500) + "-END"

        result = bot.limit_report_input_text("chat_export", source, 120)

        self.assertLessEqual(len(result), 120)
        self.assertTrue(result.startswith("BEGIN-"))
        self.assertTrue(result.endswith("-END"))
        self.assertIn("chat_export truncated", result)

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
    def test_analysis_safety_net_preserves_numbers_and_restores_tasks(self):
        report_data = {
            "summary": ["Краткий отчёт"],
            "tasks": [],
            "important_facts": [],
            "risks_open_questions": [],
        }
        analysis_data = {
            "important_numbers": [{
                "label": "Ставки по предложению",
                "value": "2 100 / 4 200 / 7 300",
                "currency_or_unit": "RUB",
                "source": "image",
                "confidence": "medium",
            }],
            "possible_inconsistencies": [{
                "issue": "Ставки не подтверждены письменно",
                "how_to_handle": "Запросить подтверждение",
            }],
            "action_items": [{
                "actor": "Юлианна",
                "target_party": "страховая компания",
                "action": "Запросить подтверждение ставок",
                "deliverable": "письменный ответ с актуальными ставками",
                "reason": "нужно завершить предложение",
                "source_context": "ставки видны на скриншоте",
                "deadline": "не указан",
                "status": "требует подтверждения",
            }],
        }

        bot.apply_analysis_report_safety_net(report_data, analysis_data)

        self.assertIn(
            "2 100 / 4 200 / 7 300 RUB",
            report_data["important_facts"][0]["fact"],
        )
        self.assertIn("Ставки не подтверждены", report_data["risks_open_questions"][0])
        self.assertEqual(report_data["tasks"][0]["owner"], "Юлианна")
        self.assertIn("страховая компания", report_data["tasks"][0]["task"])
        self.assertIn("письменный ответ", report_data["tasks"][0]["task"])

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

    def test_work_package_waits_while_ai_report_is_running(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with (
            patch.object(bot, "_ACTIVE_AI_REPORTS", {("active",)}),
            patch.object(bot, "export_interval") as export_interval,
        ):
            asyncio.run(bot.send_work_package_for_source_chat(
                context,
                target_chat_id=10,
                source_chat_id=-1001,
                source_chat_title="Рабочий чат",
                mode="shift",
            ))

        export_interval.assert_not_called()
        self.assertIn(
            "Дождитесь его завершения",
            context.bot.send_message.await_args.kwargs["text"],
        )


class ControlCenterReportTests(unittest.TestCase):
    def test_control_center_cache_round_trip_and_telegram_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "latest.json"
            report_data = {
                "tasks": [{
                    "task": "Запросить у партнёра письменный оффер",
                    "owner": "не назначен",
                    "deadline": "не указан",
                    "context": "Чат: Networking",
                    "status": "ожидает ответа",
                }],
                "risks_open_questions": ["Не подтверждены условия оплаты"],
            }
            with patch.object(bot, "CONTROL_CENTER_CACHE_PATH", cache_path):
                bot.save_control_center_report_cache(report_data, "7 дней", 42)
                payload = bot.load_control_center_report_cache()

            text = bot.format_control_center_cache_text(payload)

        self.assertIn("Запросить у партнёра", text)
        self.assertIn("Networking", text)
        self.assertIn("ожидает ответа", text)
        self.assertIn("Не подтверждены условия оплаты", text)

    def test_control_center_aggregates_only_approved_chats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            export_a = root / "a.md"
            export_b = root / "b.md"
            index_a = root / "a-index.md"
            index_b = root / "b-index.md"
            export_a.write_text("Сообщения чата A", encoding="utf-8")
            export_b.write_text("Сообщения чата B", encoding="utf-8")
            index_a.write_text("Вложения чата A", encoding="utf-8")
            index_b.write_text("Вложения чата B", encoding="utf-8")
            output_pdf = root / "control.pdf"

            chats = [
                {"chat_id": -1001, "title": "Чат A", "type": "supergroup"},
                {"chat_id": -1002, "title": "Чат B", "type": "group"},
            ]
            exports = {
                -1001: (export_a, 3),
                -1002: (export_b, 4),
            }
            indexes = {
                -1001: (index_a, 1),
                -1002: (index_b, 1),
            }

            with (
                patch.object(bot, "get_approved_report_chats", return_value=chats),
                patch.object(bot, "prepare_transcripts_for_period") as prepare,
                patch.object(
                    bot,
                    "export_interval",
                    side_effect=lambda *args, **kwargs: exports[kwargs["chat_id"]],
                ),
                patch.object(
                    bot,
                    "create_attachments_index",
                    side_effect=lambda *args, **kwargs: indexes[kwargs["chat_id"]],
                ),
                patch.object(
                    bot,
                    "create_contact_sheets",
                    side_effect=lambda *args, **kwargs: [root / f"{kwargs['chat_id']}.jpg"],
                ),
                patch.object(bot, "collect_important_document_texts_for_period", return_value=""),
                patch.object(bot, "get_chatgpt_work_prompt", return_value="prompt"),
                patch.object(
                    bot,
                    "generate_ai_one_page_report",
                    return_value=("report", {"tasks": []}),
                ) as generate,
                patch.object(bot, "create_ai_pdf_output_path", return_value=output_pdf),
                patch.object(bot, "render_one_page_report_pdf") as render,
            ):
                result = bot.build_control_center_report_artifact(
                    "week",
                    bot.datetime(2026, 10, 1, tzinfo=bot.timezone.utc),
                    bot.datetime(2026, 10, 8, tzinfo=bot.timezone.utc),
                    "7 дней",
                )

        self.assertEqual(result["message_count"], 7)
        self.assertEqual(prepare.call_count, 2)
        chat_export_text = generate.call_args.args[2]
        self.assertIn("Рабочий чат: Чат A", chat_export_text)
        self.assertIn("Рабочий чат: Чат B", chat_export_text)
        self.assertEqual(len(generate.call_args.kwargs["contact_sheet_paths"]), 2)
        render.assert_called_once()

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
