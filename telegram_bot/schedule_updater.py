"""Фоновая проверка кабинета и атомарное обновление расписания."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from cabinet_client import CabinetClient
from import_schedule import build_schedule
from storage import BotStorage, runtime_dir

if TYPE_CHECKING:
    from bot import TelegramBot


BASE_DIR = Path(__file__).resolve().parent


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(
        payload,
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


class ScheduleUpdater:
    def __init__(
        self,
        bot: "TelegramBot",
        storage: BotStorage,
        downloader: Callable[[], bytes],
        interval_minutes: int = 30,
        work_dir: Path | None = None,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.downloader = downloader
        self.interval_seconds = max(interval_minutes, 1) * 60
        work_dir = work_dir or runtime_dir()
        self.workbook_path = work_dir / "latest_schedule.xlsx"
        self.schedule_path = work_dir / "schedule_data.json"
        self._stop = threading.Event()

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
        existing = self.storage.get_state("schedule_payload_sha256")
        if existing:
            return existing
        digest = payload_sha256(self.bot.data)
        self.storage.set_state("schedule_payload_sha256", digest)
        return digest

    def check_once(self) -> bool:
        previous_hash = self._baseline_hash()
        previous_payload_hash = self._baseline_payload_hash()
        workbook = self.downloader()
        current_hash = sha256(workbook)
        if current_hash == previous_hash:
            self.storage.set_state("last_check_at", datetime.now().isoformat(timespec="seconds"))
            self.bot.broadcast_schedule_unchanged()
            return False

        atomic_write(self.workbook_path, workbook)
        payload = build_schedule(self.workbook_path)
        current_payload_hash = payload_sha256(payload)
        self.storage.set_state("schedule_sha256", current_hash)
        self.storage.set_state("last_check_at", datetime.now().isoformat(timespec="seconds"))
        if current_payload_hash == previous_payload_hash:
            self.bot.broadcast_schedule_unchanged()
            logging.info("Файл в кабинете изменился, но расписание выбранных групп осталось прежним.")
            return False

        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write(self.schedule_path, encoded)
        self.bot.reload_schedule(payload)
        self.storage.set_state("schedule_payload_sha256", current_payload_hash)
        self.storage.set_state("last_update_at", datetime.now().isoformat(timespec="seconds"))

        if previous_payload_hash:
            self.bot.broadcast_schedule_update(payload.get("summary", {}))
        logging.info(
            "Расписание обновлено: %s групп, даты %s — %s.",
            payload.get("summary", {}).get("group_count"),
            payload.get("summary", {}).get("date_from"),
            payload.get("summary", {}).get("date_to"),
        )
        return True

    def run_forever(self) -> None:
        logging.info("Автопроверка расписания включена: каждые %s мин.", self.interval_seconds // 60)
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logging.exception("Не удалось проверить или обновить расписание.")
            self._stop.wait(self.interval_seconds)

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
    interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
    updater = ScheduleUpdater(bot, storage, CabinetClient().download_schedule, interval)
    updater.start()
    return updater
