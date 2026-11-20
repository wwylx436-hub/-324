"""Telegram-бот с меню расписания колледжа."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from storage import BotStorage, runtime_dir


BASE_DIR = Path(__file__).resolve().parent
SCHEDULE_PATH = BASE_DIR / "schedule_data.json"
OPEN_SCHEDULE = "Открыть расписание"
CHECK_SCHEDULE = "Проверить расписание"
WEEKDAY_NAMES = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)
WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MAIN_MENU = {
    "keyboard": [[{"text": OPEN_SCHEDULE}], [{"text": CHECK_SCHEDULE}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


def load_env(path: Path = BASE_DIR / ".env") -> None:
    """Загрузить простые пары KEY=VALUE из .env, не перезаписывая окружение."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_schedule(path: Path = SCHEDULE_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден файл расписания {path.name}. Запустите import_schedule.py."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def admin_chat_ids() -> set[int]:
    """Прочитать список Telegram ID администраторов из переменной окружения."""
    result: set[int] = set()
    for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(","):
        try:
            result.add(int(value.strip()))
        except ValueError:
            continue
    return result


class TelegramBot:
    def __init__(
        self,
        token: str,
        schedule_path: Path = SCHEDULE_PATH,
        storage: BotStorage | None = None,
    ) -> None:
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.data = load_schedule(schedule_path)
        self.groups = {group["id"]: group for group in self.data["groups"]}
        self.storage = storage or BotStorage()
        self.schedule_updater: Any | None = None

    def set_schedule_updater(self, updater: Any) -> None:
        self.schedule_updater = updater

    def reload_schedule(self, payload: dict[str, Any]) -> None:
        """Заменить расписание без перезапуска процесса бота."""
        self.data = payload
        self.groups = {group["id"]: group for group in payload["groups"]}

    def request(self, method: str, **params: Any) -> dict[str, Any]:
        body = urlencode(params).encode("utf-8")
        request = Request(f"{self.api_url}/{method}", data=body, method="POST")
        try:
            with urlopen(request, timeout=40) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API вернул HTTP {error.code}: {details}") from error
        except URLError as error:
            raise ConnectionError(f"Не удалось подключиться к Telegram: {error.reason}") from error
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Неизвестная ошибка Telegram API"))
        return payload

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        self.request("sendMessage", **params)

    def answer_callback_query(self, callback_query_id: str) -> None:
        self.request("answerCallbackQuery", callback_query_id=callback_query_id)

    def run(self) -> None:
        offset = 0
        logging.info("Бот запущен. Для остановки нажмите Ctrl+C.")
        while True:
            try:
                payload = self.request(
                    "getUpdates",
                    offset=offset,
                    timeout=30,
                    allowed_updates=json.dumps(["message", "callback_query"]),
                )
                for update in payload["result"]:
                    offset = update["update_id"] + 1
                    self.handle_update(update)
            except (ConnectionError, RuntimeError) as error:
                logging.warning("%s Повтор через 5 секунд.", error)
                time.sleep(5)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        if chat_id is None or not isinstance(text, str):
            return
        user = message.get("from", {})
        first_name = user.get("first_name", "")
        username = user.get("username", "")
        last_name = user.get("last_name", "")
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        self.storage.subscribe(chat_id, first_name, username, last_name)
        if command == "/stop":
            self.storage.unsubscribe(chat_id)
            self.send_message(
                chat_id,
                "Уведомления отключены. Чтобы включить их снова, отправь /start.",
                MAIN_MENU,
            )
            return
        if command == "/start":
            first_name = first_name or "друг"
            self.send_message(
                chat_id,
                f"Привет, {first_name}! 👋\n"
                "Теперь я сообщу, когда расписание изменится.\n"
                "Нажми кнопку ниже, чтобы посмотреть расписание.",
                MAIN_MENU,
            )
        elif command == "/help":
            self.send_message(
                chat_id,
                "Нажми «Открыть расписание», затем выбери курс, группу и день.\n"
                "Команда /stop отключает уведомления.",
                MAIN_MENU,
            )
        elif command == "/myid":
            self.send_message(
                chat_id,
                f"Твой Telegram ID: {chat_id}\n"
                "Передай его администратору, чтобы он выдал доступ к списку пользователей.",
                MAIN_MENU,
            )
        elif command == "/users":
            self.show_users(chat_id)
        elif text.casefold() == OPEN_SCHEDULE.casefold():
            self.show_courses(chat_id)
        elif text.casefold() == CHECK_SCHEDULE.casefold():
            self.start_manual_schedule_check(chat_id)
        else:
            self.send_message(
                chat_id,
                "Я показываю расписание колледжа. Нажми кнопку ниже 👇",
                MAIN_MENU,
            )

    def start_manual_schedule_check(self, chat_id: int) -> None:
        if self.storage.selected_group_id(chat_id) is None:
            self.send_message(
                chat_id,
                "Сначала открой расписание и выбери свою группу, затем нажми «Проверить расписание».",
                MAIN_MENU,
            )
            return
        if self.schedule_updater is None:
            self.send_message(chat_id, "Автоматическая проверка сейчас недоступна.", MAIN_MENU)
            return
        self.send_message(chat_id, "⏳ Проверяю расписание для твоей группы…", MAIN_MENU)
        thread = threading.Thread(
            target=self._run_manual_schedule_check,
            args=(chat_id,),
            name=f"manual-schedule-check-{chat_id}",
            daemon=True,
        )
        thread.start()

    def _run_manual_schedule_check(self, chat_id: int) -> None:
        try:
            self.schedule_updater.check_once(requester_chat_id=chat_id)
        except Exception:
            logging.exception("Не удалось выполнить ручную проверку расписания.")
            self.send_message(chat_id, "Не удалось проверить расписание. Попробуй ещё раз позже.", MAIN_MENU)

    def send_manual_check_result(
        self,
        chat_id: int,
        changed: bool,
        next_check: str | None = None,
    ) -> None:
        group_id = self.storage.selected_group_id(chat_id)
        group_name = self.groups.get(group_id or "", {}).get("name", "выбранной группы")
        next_check_text = f"\n\nСледующая проверка: {next_check}." if next_check else ""
        if changed:
            text = f"🔔 Расписание для группы {group_name} обновилось."
        else:
            text = (
                f"ℹ️ Проверка завершена. Расписание для группы {group_name} "
                "не обновилось — всё осталось без изменений."
            )
        self.send_message(chat_id, f"{text}{next_check_text}", MAIN_MENU)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        data = callback.get("data", "")
        if callback_id:
            self.answer_callback_query(callback_id)
        if chat_id is None:
            return
        user = callback.get("from", {})
        first_name = user.get("first_name", "")
        self.storage.subscribe(
            chat_id,
            first_name,
            user.get("username", ""),
            user.get("last_name", ""),
        )
        parts = data.split(":")
        action = parts[0]
        if action == "course" and len(parts) == 2 and parts[1].isdigit():
            self.show_groups(chat_id, int(parts[1]))
        elif action == "group" and len(parts) == 2:
            self.storage.set_selected_group(chat_id, parts[1])
            self.show_days(chat_id, parts[1])
        elif action == "day" and len(parts) == 3:
            self.show_day(chat_id, parts[1], parts[2])
        elif action == "week" and len(parts) == 3:
            self.show_week(chat_id, parts[1], parts[2])
        else:
            self.send_message(chat_id, "Это меню уже устарело. Открой расписание заново.", MAIN_MENU)

    def show_users(self, chat_id: int) -> None:
        """Показать список активных пользователей только владельцу бота."""
        if chat_id not in admin_chat_ids():
            self.send_message(
                chat_id,
                "Список пользователей доступен только администратору.\n"
                "Отправь /myid и добавь этот ID в ADMIN_TELEGRAM_IDS в Railway.",
                MAIN_MENU,
            )
            return

        users = self.storage.list_users(limit=50)
        active_count = self.storage.subscriber_count()
        known_count = self.storage.user_count()
        lines = [f"👥 Пользователи бота: активных {active_count}, всего в базе {known_count}."]
        if not users:
            lines.append("\nАктивных пользователей пока нет.")
        for index, user in enumerate(users, start=1):
            name = " ".join(filter(None, [str(user["first_name"]), str(user["last_name"])]))
            display_name = name or "Без имени"
            username = str(user["username"])
            username_text = f" (@{username})" if username else ""
            group_id = user.get("selected_group_id")
            group = self.groups.get(str(group_id), {}) if group_id else {}
            group_name = group.get("name", "не выбрана")
            lines.append(
                f"\n{index}. {display_name}{username_text}\n"
                f"ID: {user['chat_id']} · группа: {group_name}"
            )
        if active_count > len(users):
            lines.append("\nПоказаны первые 50 активных пользователей.")
        self.send_message(chat_id, "\n".join(lines), MAIN_MENU)

    def show_courses(self, chat_id: int) -> None:
        courses = sorted({int(group["course"]) for group in self.groups.values()})
        keyboard = [
            [{"text": f"{course} курс", "callback_data": f"course:{course}"}]
            for course in courses
        ]
        self.send_message(chat_id, "Выбери курс:", {"inline_keyboard": keyboard})

    def show_groups(self, chat_id: int, course: int) -> None:
        groups = sorted(
            (group for group in self.groups.values() if int(group["course"]) == course),
            key=lambda group: group["name"],
        )
        if not groups:
            self.send_message(chat_id, "Для этого курса группы не найдены.", MAIN_MENU)
            return
        buttons = [
            {"text": group["name"], "callback_data": f"group:{group['id']}"}
            for group in groups
        ]
        keyboard = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        self.send_message(
            chat_id,
            f"{course} курс. Выбери группу:",
            {"inline_keyboard": keyboard},
        )

    def closest_schedule_week(self, group_id: str, today: date | None = None) -> date | None:
        days = self.data.get("schedule", {}).get(group_id, {})
        if not days:
            return None
        today = today or date.today()
        weeks = sorted({monday_of(date.fromisoformat(day)) for day in days})
        current_week = monday_of(today)
        if current_week in weeks:
            return current_week
        future = [week for week in weeks if week > current_week]
        return future[0] if future else weeks[-1]

    def available_schedule_days(self, group_id: str) -> list[date]:
        """Вернуть только дни, которые реально есть в загруженном расписании."""
        return sorted(
            date.fromisoformat(iso_day)
            for iso_day in self.data.get("schedule", {}).get(group_id, {})
        )

    def show_days(self, chat_id: int, group_id: str) -> None:
        group = self.groups.get(group_id)
        if group is None:
            self.send_message(chat_id, "Группа не найдена. Открой расписание заново.", MAIN_MENU)
            return
        available_days = self.available_schedule_days(group_id)
        if not available_days:
            self.send_message(chat_id, f"Для группы {group['name']} расписание пока не заполнено.", MAIN_MENU)
            return

        keyboard = []
        for lesson_day in available_days:
            keyboard.append([{
                "text": f"{WEEKDAY_SHORT[lesson_day.weekday()]}, {lesson_day:%d.%m}",
                "callback_data": f"day:{group_id}:{lesson_day.isoformat()}",
            }])
        keyboard.append([
            {"text": "◀️ К группам", "callback_data": f"course:{group['course']}"}
        ])
        first_day, last_day = available_days[0], available_days[-1]
        self.send_message(
            chat_id,
            f"Группа {group['name']}\n"
            f"Доступные даты: {first_day:%d.%m}–{last_day:%d.%m.%Y}\n"
            "Выбери день:",
            {"inline_keyboard": keyboard},
        )

    def format_day(self, group_id: str, lesson_day: date) -> str:
        group = self.groups[group_id]
        lessons = self.data.get("schedule", {}).get(group_id, {}).get(lesson_day.isoformat(), [])
        title = (
            f"📅 {WEEKDAY_NAMES[lesson_day.weekday()]}, {lesson_day:%d.%m.%Y}\n"
            f"Группа {group['name']}"
        )
        if not lessons:
            return f"{title}\n\nЗанятий нет."
        rows = [title]
        for lesson in lessons:
            rows.append(f"{lesson['time']}\n{lesson['lesson']}")
        return "\n\n".join(rows)

    def show_day(self, chat_id: int, group_id: str, iso_day: str) -> None:
        if group_id not in self.groups:
            self.send_message(chat_id, "Группа не найдена.", MAIN_MENU)
            return
        try:
            lesson_day = date.fromisoformat(iso_day)
        except ValueError:
            self.send_message(chat_id, "Неверная дата.", MAIN_MENU)
            return
        group = self.groups[group_id]
        self.send_message(
            chat_id,
            self.format_day(group_id, lesson_day),
            {"inline_keyboard": [
                [{"text": "◀️ К дням", "callback_data": f"group:{group_id}"}],
                [{"text": "◀️ К группам", "callback_data": f"course:{group['course']}"}],
            ]},
        )

    def show_week(self, chat_id: int, group_id: str, iso_monday: str) -> None:
        if group_id not in self.groups:
            self.send_message(chat_id, "Группа не найдена.", MAIN_MENU)
            return
        try:
            week_start = date.fromisoformat(iso_monday)
        except ValueError:
            self.send_message(chat_id, "Неверная дата.", MAIN_MENU)
            return
        available_days = [
            lesson_day
            for lesson_day in self.available_schedule_days(group_id)
            if week_start <= lesson_day <= week_start + timedelta(days=5)
        ]
        if not available_days:
            self.send_message(chat_id, "За эту неделю занятий в расписании нет.", MAIN_MENU)
            return
        for lesson_day in available_days:
            self.send_message(chat_id, self.format_day(group_id, lesson_day))
        self.send_message(
            chat_id,
            "Выбери другой день или группу:",
            {"inline_keyboard": [
                [{"text": "◀️ К дням", "callback_data": f"group:{group_id}"}],
                [{
                    "text": "◀️ К группам",
                    "callback_data": f"course:{self.groups[group_id]['course']}",
                }],
            ]},
        )

    def broadcast_schedule_update(
        self,
        summary: dict[str, Any] | None = None,
        next_check: str | None = None,
        group_ids: set[str] | None = None,
    ) -> tuple[int, int]:
        """Уведомить пользователей только групп с изменившимся расписанием."""
        summary = summary or self.data.get("summary", {})
        changed_groups = group_ids if group_ids is not None else set(self.groups)
        group_names = ", ".join(sorted(
            self.groups[group_id]["name"]
            for group_id in changed_groups
            if group_id in self.groups
        )) or "выбранные"
        date_from = summary.get("date_from")
        date_to = summary.get("date_to")
        period = f"\nПериод: {date_from} — {date_to}" if date_from and date_to else ""
        next_check_text = f"\n\nСледующая проверка: {next_check}." if next_check else ""
        text = (
            "🔔 Расписание изменилось!\n"
            f"Обновлены группы: {group_names}.{period}\n\n"
            "Нажми «Открыть расписание», чтобы посмотреть новую версию."
            f"{next_check_text}"
        )
        sent = 0
        failed = 0
        recipients = (
            self.storage.active_chat_ids()
            if group_ids is None
            else self.storage.active_chat_ids_for_groups(changed_groups)
        )
        for chat_id in recipients:
            try:
                self.send_message(chat_id, text, MAIN_MENU)
                sent += 1
                time.sleep(0.05)
            except (ConnectionError, RuntimeError) as error:
                failed += 1
                if "HTTP 403" in str(error):
                    self.storage.unsubscribe(chat_id)
                logging.warning("Не удалось отправить уведомление в чат %s: %s", chat_id, error)
        logging.info("Рассылка обновлений групп %s: отправлено %s, ошибок %s.", group_names, sent, failed)
        return sent, failed

    def broadcast_schedule_unchanged(self, next_check: str | None = None) -> tuple[int, int]:
        """Сообщить подписчикам, что проверка прошла без изменений."""
        next_check_text = f"\n\nСледующая проверка: {next_check}." if next_check else ""
        text = (
            "ℹ️ Проверка расписания выполнена.\n"
            "Новое расписание не появилось — всё осталось без изменений."
            f"{next_check_text}"
        )
        sent = 0
        failed = 0
        for chat_id in self.storage.active_chat_ids():
            try:
                self.send_message(chat_id, text, MAIN_MENU)
                sent += 1
                time.sleep(0.05)
            except (ConnectionError, RuntimeError) as error:
                failed += 1
                if "HTTP 403" in str(error):
                    self.storage.unsubscribe(chat_id)
                logging.warning("Не удалось отправить статус в чат %s: %s", chat_id, error)
        logging.info("Рассылка без изменений завершена: отправлено %s, ошибок %s.", sent, failed)
        return sent, failed


def main() -> None:
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Токен не найден. Скопируйте .env.example в .env и вставьте токен от BotFather."
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    storage = BotStorage()
    persistent_schedule = runtime_dir() / "schedule_data.json"
    schedule_path = persistent_schedule if persistent_schedule.exists() else SCHEDULE_PATH
    bot = TelegramBot(token, schedule_path=schedule_path, storage=storage)
    broadcast_id = os.getenv("BROADCAST_ON_START_ID", "").strip()
    if broadcast_id and storage.get_state("last_broadcast_on_start_id") != broadcast_id:
        sent, failed = bot.broadcast_schedule_update()
        storage.set_state("last_broadcast_on_start_id", broadcast_id)
        logging.info(
            "Одноразовая рассылка %s завершена: отправлено %s, ошибок %s.",
            broadcast_id,
            sent,
            failed,
        )
    try:
        from schedule_updater import restore_missing_future_schedule, start_updater_from_env

        updater = start_updater_from_env(bot, storage)
        if updater is not None:
            bot.set_schedule_updater(updater)
            restored_groups = restore_missing_future_schedule(bot, persistent_schedule)
            if restored_groups:
                bot.broadcast_schedule_update(
                    bot.data.get("summary", {}),
                    updater.next_check_label(),
                    group_ids=restored_groups,
                )
    except Exception:
        logging.exception("Не удалось запустить автоматическую проверку расписания.")
    bot.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
