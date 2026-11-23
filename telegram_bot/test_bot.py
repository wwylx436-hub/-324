import json
import unittest
from datetime import date
from typing import Any

from bot import MAIN_MENU, TelegramBot


class FakeStorage:
    def __init__(self) -> None:
        self.active: dict[int, str] = {}
        self.selected_groups: dict[int, str] = {}

    def subscribe(
        self,
        chat_id: int,
        first_name: str = "",
        username: str = "",
        last_name: str = "",
    ) -> None:
        self.active[chat_id] = first_name

    def unsubscribe(self, chat_id: int) -> None:
        self.active.pop(chat_id, None)

    def active_chat_ids(self) -> list[int]:
        return sorted(self.active)

    def active_chat_ids_for_groups(self, group_ids: set[str]) -> list[int]:
        return sorted(
            chat_id
            for chat_id in self.active
            if self.selected_groups.get(chat_id) in group_ids
        )

    def set_selected_group(self, chat_id: int, group_id: str) -> None:
        self.selected_groups[chat_id] = group_id

    def selected_group_id(self, chat_id: int) -> str | None:
        return self.selected_groups.get(chat_id)

    def subscriber_count(self) -> int:
        return len(self.active)

    def user_count(self) -> int:
        return len(self.active)

    def list_users(self, limit: int = 50) -> list[dict[str, object]]:
        return [
            {
                "chat_id": chat_id,
                "first_name": first_name,
                "last_name": "",
                "username": "",
                "selected_group_id": self.selected_groups.get(chat_id),
            }
            for chat_id, first_name in self.active.items()
        ][:limit]


class FakeTelegramBot(TelegramBot):
    def __init__(self) -> None:
        self.fake_storage = FakeStorage()
        super().__init__("test-token", storage=self.fake_storage)
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
            "from": {"first_name": "Аня"},
            "message": {"chat": {"id": 42}},
            "data": data,
        }}

    def test_start_shows_bottom_button(self) -> None:
        self.bot.handle_update(self.update("/start"))
        self.assertIn("Привет, Аня", self.bot.sent_messages[0][1])
        self.assertEqual(self.bot.sent_messages[0][2], MAIN_MENU)
        self.assertEqual(self.bot.fake_storage.active, {42: "Аня"})

    def test_stop_disables_notifications(self) -> None:
        self.bot.handle_update(self.update("/start"))
        self.bot.handle_update(self.update("/stop"))
        self.assertEqual(self.bot.fake_storage.active, {})
        self.assertIn("Уведомления отключены", self.bot.sent_messages[-1][1])

    def test_group_selection_is_saved(self) -> None:
        group_id = next(iter(self.bot.groups))
        self.bot.handle_update(self.callback(f"group:{group_id}"))
        self.assertEqual(self.bot.fake_storage.selected_groups[42], group_id)

    def test_manual_check_requires_selected_group(self) -> None:
        self.bot.handle_update(self.update("Проверить расписание"))
        self.assertIn("сначала открой расписание", self.bot.sent_messages[-1][1].casefold())

    def test_broadcast_notifies_all_active_chats(self) -> None:
        self.bot.fake_storage.subscribe(11, "Иван")
        self.bot.fake_storage.subscribe(22, "Оля")
        sent, failed = self.bot.broadcast_schedule_update()
        self.assertEqual((sent, failed), (2, 0))
        self.assertEqual([message[0] for message in self.bot.sent_messages], [11, 22])
        self.assertTrue(all("Расписание изменилось" in message[1] for message in self.bot.sent_messages))

    def test_group_update_notifies_only_users_of_changed_group(self) -> None:
        group_ids = list(self.bot.groups)
        self.bot.fake_storage.subscribe(11, "Иван")
        self.bot.fake_storage.subscribe(22, "Оля")
        self.bot.fake_storage.set_selected_group(11, group_ids[0])
        self.bot.fake_storage.set_selected_group(22, group_ids[1])

        self.bot.broadcast_schedule_update(group_ids={group_ids[0]})

        self.assertEqual([message[0] for message in self.bot.sent_messages], [11])

    def test_unchanged_broadcast_notifies_all_active_chats(self) -> None:
        self.bot.fake_storage.subscribe(11, "Иван")
        self.bot.fake_storage.subscribe(22, "Оля")
        sent, failed = self.bot.broadcast_schedule_unchanged()
        self.assertEqual((sent, failed), (2, 0))
        self.assertTrue(all("осталось без изменений" in message[1] for message in self.bot.sent_messages))

    def test_check_time_change_notifies_all_active_chats(self) -> None:
        self.bot.fake_storage.subscribe(11, "Иван")
        self.bot.fake_storage.subscribe(22, "Оля")

        sent, failed = self.bot.broadcast_check_time_change()

        self.assertEqual((sent, failed), (2, 0))
        self.assertTrue(all("07:00, 18:00 и 23:00" in message[1] for message in self.bot.sent_messages))

    def test_open_schedule_shows_courses(self) -> None:
        self.bot.handle_update(self.update("Открыть расписание"))
        markup = self.bot.sent_messages[0][2]
        self.assertEqual(self.bot.sent_messages[0][1], "Выбери курс:")
        self.assertEqual(markup["inline_keyboard"], [[{"text": "2 курс", "callback_data": "course:2"}]])

    def test_course_callback_shows_groups(self) -> None:
        self.bot.handle_update(self.callback("course:2"))
        self.assertEqual(self.bot.answered_callbacks, ["callback-1"])
        self.assertIn("2 курс", self.bot.sent_messages[0][1])
        buttons = sum(self.bot.sent_messages[0][2]["inline_keyboard"], [])
        self.assertEqual({button["text"] for button in buttons}, {"П9/25-4", "Э9/25-3"})

    def test_group_callback_shows_days(self) -> None:
        group_id = next(iter(self.bot.groups))
        saturday = self.bot.data["schedule"][group_id]["2026-11-21"]
        self.bot.data["schedule"][group_id] = {
            "2026-11-21": saturday,
            "2026-11-24": [{"time": "09:00", "lesson": "Следующая неделя"}],
        }
        self.bot.handle_update(self.callback(f"group:{group_id}"))
        self.assertIn("Группа", self.bot.sent_messages[0][1])
        buttons = sum(self.bot.sent_messages[0][2]["inline_keyboard"], [])
        day_buttons = [button for button in buttons if button["callback_data"].startswith("day:")]
        self.assertEqual([button["text"] for button in day_buttons], ["Сб, 21.11", "Вт, 24.11"])

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
