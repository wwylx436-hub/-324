import json
import unittest
from datetime import date
from typing import Any

from bot import MAIN_MENU, TelegramBot


class FakeTelegramBot(TelegramBot):
    def __init__(self) -> None:
        super().__init__("test-token")
        self.sent_messages: list[tuple[int, str, dict[str, Any] | None]] = []
        self.answered_callbacks: list[str] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.sent_messages.append((chat_id, text, reply_markup))

    def answer_callback_query(self, callback_query_id: str) -> None:
        self.answered_callbacks.append(callback_query_id)


class TelegramBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = FakeTelegramBot()

    @staticmethod
    def update(text: str) -> dict:
        return {"message": {
            "chat": {"id": 42},
            "from": {"first_name": "Аня"},
            "text": text,
        }}

    @staticmethod
    def callback(data: str) -> dict:
        return {"callback_query": {
            "id": "callback-1",
            "message": {"chat": {"id": 42}},
            "data": data,
        }}

    def test_start_shows_bottom_button(self) -> None:
        self.bot.handle_update(self.update("/start"))
        self.assertIn("Привет, Аня", self.bot.sent_messages[0][1])
        self.assertEqual(self.bot.sent_messages[0][2], MAIN_MENU)

    def test_open_schedule_shows_courses(self) -> None:
        self.bot.handle_update(self.update("Открыть расписание"))
        markup = self.bot.sent_messages[0][2]
        self.assertEqual(self.bot.sent_messages[0][1], "Выбери курс:")
        self.assertEqual(len(markup["inline_keyboard"]), 4)

    def test_course_callback_shows_groups(self) -> None:
        self.bot.handle_update(self.callback("course:1"))
        self.assertEqual(self.bot.answered_callbacks, ["callback-1"])
        self.assertIn("1 курс", self.bot.sent_messages[0][1])
        buttons = sum(self.bot.sent_messages[0][2]["inline_keyboard"], [])
        self.assertEqual(len(buttons), 22)

    def test_group_callback_shows_days(self) -> None:
        self.bot.handle_update(self.callback("group:g001"))
        self.assertIn("Группа", self.bot.sent_messages[0][1])
        buttons = sum(self.bot.sent_messages[0][2]["inline_keyboard"], [])
        self.assertTrue(any(button["text"] == "Вся неделя" for button in buttons))

    def test_day_format_uses_real_schedule(self) -> None:
        group_id = next(iter(self.bot.data["schedule"]))
        iso_day = next(iter(self.bot.data["schedule"][group_id]))
        text = self.bot.format_day(group_id, date.fromisoformat(iso_day))
        self.assertIn(self.bot.groups[group_id]["name"], text)
        self.assertIn(self.bot.data["schedule"][group_id][iso_day][0]["time"], text)

    def test_reply_markup_is_serializable_for_telegram(self) -> None:
        json.dumps(MAIN_MENU, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
