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

                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    selected_group_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);
                """
            )
            # Переносим ранее сохранённых подписчиков после обновления бота.
            connection.execute(
                """
                INSERT OR IGNORE INTO users (
                    chat_id, first_name, active, created_at, updated_at, last_seen_at
                )
                SELECT chat_id, first_name, active, created_at, updated_at, updated_at
                FROM subscribers
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def subscribe(
        self,
        chat_id: int,
        first_name: str = "",
        username: str = "",
        last_name: str = "",
    ) -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    chat_id, username, first_name, last_name, active,
                    created_at, updated_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = CASE WHEN excluded.username <> '' THEN excluded.username ELSE users.username END,
                    first_name = CASE WHEN excluded.first_name <> '' THEN excluded.first_name ELSE users.first_name END,
                    last_name = CASE WHEN excluded.last_name <> '' THEN excluded.last_name ELSE users.last_name END,
                    active = 1,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (chat_id, username, first_name, last_name, now, now, now),
            )
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
            now = self._now()
            connection.execute(
                "UPDATE subscribers SET active = 0, updated_at = ? WHERE chat_id = ?",
                (now, chat_id),
            )
            connection.execute(
                "UPDATE users SET active = 0, updated_at = ?, last_seen_at = ? WHERE chat_id = ?",
                (now, now, chat_id),
            )

    def active_chat_ids(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM users WHERE active = 1 ORDER BY chat_id"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def subscriber_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE active = 1"
            ).fetchone()
        return int(row["count"])

    def user_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def set_selected_group(self, chat_id: int, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET selected_group_id = ?, updated_at = ?, last_seen_at = ? WHERE chat_id = ?",
                (group_id, self._now(), self._now(), chat_id),
            )

    def list_users(self, limit: int = 50) -> list[dict[str, object]]:
        """Вернуть активных пользователей, недавно взаимодействовавших с ботом."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, username, first_name, last_name, selected_group_id,
                       created_at, last_seen_at
                FROM users
                WHERE active = 1
                ORDER BY last_seen_at DESC, chat_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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
