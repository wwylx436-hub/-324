"""Фоновая проверка кабинета и атомарное обновление расписания."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from copy import deepcopy
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from cabinet_client import CabinetClient
from import_schedule import build_schedule
from storage import BotStorage, runtime_dir

if TYPE_CHECKING:
    from bot import TelegramBot


BASE_DIR = Path(__file__).resolve().parent
# Москва использует постоянное время UTC+3, поэтому не зависим от базы часовых
# поясов операционной системы (она часто отсутствует в Windows Python).
MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")
DEFAULT_CHECK_TIMES = ("07:00", "11:00", "15:00", "18:00", "21:00", "23:00")


def parse_check_times(raw_value: str | None) -> tuple[clock_time, ...]:
    values = raw_value.split(",") if raw_value else list(DEFAULT_CHECK_TIMES)
    parsed: list[clock_time] = []
    for value in values:
        cleaned = value.strip()
        try:
            parsed.append(datetime.strptime(cleaned, "%H:%M").time())
        except ValueError as error:
            raise ValueError(
                f"Некорректное время проверки {cleaned!r}. Используйте формат ЧЧ:ММ."
            ) from error
    if not parsed:
        raise ValueError("Нужно указать хотя бы одно время проверки.")
    return tuple(sorted(set(parsed)))


def next_check_at(check_times: tuple[clock_time, ...], now: datetime | None = None) -> datetime:
    local_now = (now or datetime.now(MOSCOW_TZ)).astimezone(MOSCOW_TZ)
    for check_time in check_times:
        candidate = datetime.combine(local_now.date(), check_time, tzinfo=MOSCOW_TZ)
        if candidate >= local_now:
            return candidate
    return datetime.combine(local_now.date() + timedelta(days=1), check_times[0], tzinfo=MOSCOW_TZ)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(
        {key: payload[key] for key in ('groups', 'schedule')},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def minimum_visible_date() -> str:
    """Вернуть самую раннюю дату, которую разрешено показывать в расписании."""
    today_iso = datetime.now(MOSCOW_TZ).date().isoformat()
    configured = os.getenv("SCHEDULE_VISIBLE_FROM", "").strip()
    if not configured:
        return today_iso
    try:
        configured_iso = datetime.strptime(configured, "%Y-%m-%d").date().isoformat()
    except ValueError:
        logging.warning("Игнорируется некорректный SCHEDULE_VISIBLE_FROM=%r.", configured)
        return today_iso
    return max(today_iso, configured_iso)


def filter_schedule_from(payload: dict, minimum_date_iso: str) -> dict:
    """Убрать из выдачи дни, которые раньше заданной даты."""
    filtered = deepcopy(payload)
    schedule = filtered.setdefault("schedule", {})
    filtered["schedule"] = {
        group_id: {
            iso_day: deepcopy(lessons)
            for iso_day, lessons in days.items()
            if iso_day >= minimum_date_iso
        }
        for group_id, days in schedule.items()
    }
    all_days = sorted(
        iso_day for days in filtered["schedule"].values() for iso_day in days
    )
    summary = filtered.setdefault("summary", {})
    summary["date_from"] = all_days[0] if all_days else None
    summary["date_to"] = all_days[-1] if all_days else None
    return filtered


def merge_missing_future_dates(
    incoming: dict,
    preservation_sources: list[dict],
    today_iso: str | None = None,
) -> dict:
    """Сохранить будущие даты, которых нет в свежей частичной выгрузке."""
    merged = deepcopy(incoming)
    merged_schedule = merged.setdefault("schedule", {})
    today_iso = today_iso or minimum_visible_date()
    for source in preservation_sources:
        for group_id, days in source.get("schedule", {}).items():
            target_days = merged_schedule.setdefault(group_id, {})
            for iso_day, lessons in days.items():
                if iso_day >= today_iso and iso_day not in target_days:
                    target_days[iso_day] = deepcopy(lessons)

    all_days = sorted(
        iso_day for days in merged_schedule.values() for iso_day in days
    )
    summary = merged.setdefault("summary", {})
    if all_days:
        summary["date_from"] = all_days[0]
        summary["date_to"] = all_days[-1]
    return merged


def bundled_schedule() -> dict | None:
    path = BASE_DIR / "schedule_data.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def restore_missing_future_schedule(bot: "TelegramBot", schedule_path: Path) -> set[str]:
    """Восстановить будущие дни из встроенной резервной версии после обновления."""
    fallback = bundled_schedule()
    minimum_date_iso = minimum_visible_date()
    restored = filter_schedule_from(bot.data, minimum_date_iso)
    if fallback is not None:
        restored = merge_missing_future_dates(restored, [fallback], minimum_date_iso)
    if payload_sha256(restored) == payload_sha256(bot.data):
        return set()
    affected = changed_group_ids(bot.data, restored)
    atomic_write(schedule_path, json.dumps(restored, ensure_ascii=False, indent=2).encode("utf-8"))
    bot.reload_schedule(restored)
    logging.info("Восстановлены отсутствующие будущие даты для групп: %s.", ", ".join(sorted(affected)))
    return affected


def changed_group_ids(previous: dict, current: dict) -> set[str]:
    """Найти группы, у которых реально изменились занятия."""
    before = previous.get("schedule", {})
    after = current.get("schedule", {})
    return {
        group_id
        for group_id in set(before) | set(after)
        if before.get(group_id, {}) != after.get(group_id, {})
    }


class ScheduleUpdater:
    def __init__(
        self,
        bot: "TelegramBot",
        storage: BotStorage,
        downloader: Callable[[], bytes],
        interval_minutes: int = 30,
        work_dir: Path | None = None,
        check_times: tuple[clock_time, ...] | None = None,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.downloader = downloader
        # interval_minutes оставлен для совместимости со старыми вызовами.
        self.check_times = check_times or parse_check_times(None)
        work_dir = work_dir or runtime_dir()
        self.workbook_path = work_dir / "latest_schedule.xlsx"
        self.schedule_path = work_dir / "schedule_data.json"
        self._stop = threading.Event()
        self._check_lock = threading.Lock()

    def _baseline_hash(self) -> str | None:
        existing = self.storage.get_state("schedule_sha256")
        if existing:
            return existing
        bundled = BASE_DIR / "data" / "schedule.xlsx"
        if bundled.exists():
            digest = sha256(bundled.read_bytes())
            self.storage.set_state("schedule_sha256", digest)
            return digest
        return None

    def _baseline_payload_hash(self) -> str:
        digest = payload_sha256(self.bot.data)
        self.storage.set_state("schedule_payload_sha256", digest)
        return digest

    def check_once(self, requester_chat_id: int | None = None) -> bool:
        if not self._check_lock.acquire(blocking=False):
            if requester_chat_id is not None:
                self.bot.send_message(
                    requester_chat_id,
                    "Проверка уже выполняется. Подожди немного и попробуй снова.",
                )
            return False
        try:
            return self._check_once(requester_chat_id)
        finally:
            self._check_lock.release()

    def _check_once(self, requester_chat_id: int | None = None) -> bool:
        previous_hash = self._baseline_hash()
        previous_payload_hash = self._baseline_payload_hash()
        workbook = self.downloader()
        current_hash = sha256(workbook)
        if current_hash == previous_hash:
            self.storage.set_state("last_check_at", datetime.now().isoformat(timespec="seconds"))
            self.notify_unchanged(self.next_check_label(), requester_chat_id)
            return False

        atomic_write(self.workbook_path, workbook)
        minimum_date_iso = minimum_visible_date()
        payload = filter_schedule_from(build_schedule(self.workbook_path), minimum_date_iso)
        fallback = bundled_schedule()
        payload = merge_missing_future_dates(
            payload,
            [source for source in (self.bot.data, fallback) if source is not None],
            minimum_date_iso,
        )
        current_payload_hash = payload_sha256(payload)
        self.storage.set_state("last_check_at", datetime.now().isoformat(timespec="seconds"))
        if current_payload_hash == previous_payload_hash:
            self.storage.set_state("schedule_sha256", current_hash)
            self.notify_unchanged(self.next_check_label(), requester_chat_id)
            logging.info("Файл в кабинете изменился, но расписание выбранных групп осталось прежним.")
            return False

        affected_groups = changed_group_ids(self.bot.data, payload)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write(self.schedule_path, encoded)
        self.bot.reload_schedule(payload)
        self.storage.set_state("schedule_sha256", current_hash)
        self.storage.set_state("schedule_payload_sha256", current_payload_hash)
        self.storage.set_state("last_update_at", datetime.now().isoformat(timespec="seconds"))

        if previous_payload_hash:
            self.bot.broadcast_schedule_update(
                payload.get("summary", {}),
                self.next_check_label(),
                group_ids=affected_groups,
            )
        if requester_chat_id is not None:
            selected_group_id = self.storage.selected_group_id(requester_chat_id)
            if selected_group_id not in affected_groups:
                self.bot.send_manual_check_result(
                    requester_chat_id,
                    changed=False,
                    next_check=self.next_check_label(),
                )
        logging.info(
            "Расписание обновлено: %s групп, даты %s — %s.",
            payload.get("summary", {}).get("group_count"),
            payload.get("summary", {}).get("date_from"),
            payload.get("summary", {}).get("date_to"),
        )
        return True

    def next_check_label(self) -> str:
        return next_check_at(self.check_times).strftime("%d.%m в %H:%M МСК")

    def notify_unchanged(self, next_check: str, requester_chat_id: int | None = None) -> None:
        """Отправить статус всем при плановой проверке или одному при ручной."""
        if requester_chat_id is None:
            self.bot.broadcast_schedule_unchanged(next_check)
        else:
            self.bot.send_manual_check_result(requester_chat_id, changed=False, next_check=next_check)
        logging.info('Проверка завершена: расписание выбранных очных групп без изменений.')

    def run_forever(self) -> None:
        schedule = ", ".join(value.strftime("%H:%M") for value in self.check_times)
        logging.info("Автопроверка расписания включена по Москве: %s.", schedule)
        while not self._stop.is_set():
            run_at = next_check_at(self.check_times)
            delay = max((run_at - datetime.now(MOSCOW_TZ)).total_seconds(), 0)
            logging.info("Следующая проверка расписания: %s.", run_at.strftime("%d.%m %H:%M МСК"))
            if self._stop.wait(delay):
                break
            try:
                self.check_once()
            except Exception:
                logging.exception("Не удалось проверить или обновить расписание.")

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="schedule-updater", daemon=True)
        thread.start()
        return thread


def start_updater_from_env(bot: "TelegramBot", storage: BotStorage) -> ScheduleUpdater | None:
    required = (
        "CABINET_LOGIN_URL",
        "CABINET_USERNAME",
        "CABINET_PASSWORD",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        logging.warning(
            "Автопроверка выключена: не заданы переменные %s.", ", ".join(missing)
        )
        return None
    check_times = parse_check_times(os.getenv("CHECK_TIMES_MSK"))
    updater = ScheduleUpdater(
        bot,
        storage,
        CabinetClient().download_schedule,
        check_times=check_times,
    )
    updater.start()
    return updater
