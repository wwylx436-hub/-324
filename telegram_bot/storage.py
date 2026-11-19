"""Постоянное хранилище подписчиков и состояния проверки расписания."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def runtime_dir() -> Path:
    """Использовать Railway Volume, если он подключён, иначе локальную папку."""
    configured = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    path = Path(configured) if configured else BASE_DIR / "runtime_data"
    path.mkdir(parents=True, exist_ok=True)
    return path


class BotStorage:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or runtime_dir() / "bot.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def subscribe(self, chat_id: int, first_name: str = "") -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscribers (chat_id, first_name, active, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (chat_id, first_name, now, now),
            )

    def unsubscribe(self, chat_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE subscribers SET active = 0, updated_at = ? WHERE chat_id = ?",
                (self._now(), chat_id),
            )

    def active_chat_ids(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM subscribers WHERE active = 1 ORDER BY chat_id"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def subscriber_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM subscribers WHERE active = 1"
            ).fetchone()
        return int(row["count"])

    def get_state(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
