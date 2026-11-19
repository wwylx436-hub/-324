import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot import load_schedule
from schedule_updater import ScheduleUpdater, next_check_at, parse_check_times, sha256


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_state(self, key: str) -> str | None:
        return self.values.get(key)

    def set_state(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeBot:
    def __init__(self, payload: dict) -> None:
        self.data = payload
        self.reloaded: list[dict] = []
        self.broadcasts: list[dict] = []
        self.unchanged_checks = 0

    def reload_schedule(self, payload: dict) -> None:
        self.data = payload
        self.reloaded.append(payload)

    def broadcast_schedule_update(self, summary: dict) -> None:
        self.broadcasts.append(summary)

    def broadcast_schedule_unchanged(self) -> None:
        self.unchanged_checks += 1


class ScheduleUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {'NOTIFY_UNCHANGED_CHECK_ID': 'test-check'})
        env.start()
        self.addCleanup(env.stop)
        self.payload = load_schedule()
        self.storage = FakeStorage()
        self.bot = FakeBot(copy.deepcopy(self.payload))

    def make_updater(self, work_dir: Path, workbook: bytes) -> ScheduleUpdater:
        return ScheduleUpdater(
            self.bot,
            self.storage,
            lambda: workbook,
            interval_minutes=30,
            work_dir=work_dir,
        )

    def test_does_not_broadcast_when_selected_groups_are_unchanged(self) -> None:
        workbook = b"changed-workbook"
        with tempfile.TemporaryDirectory() as temporary:
            updater = self.make_updater(Path(temporary), workbook)
            with patch("schedule_updater.build_schedule", return_value=copy.deepcopy(self.payload)):
                changed = updater.check_once()

        self.assertFalse(changed)
        self.assertEqual(self.bot.broadcasts, [])
        self.assertEqual(self.bot.unchanged_checks, 1)
        self.assertEqual(self.storage.get_state("schedule_sha256"), sha256(workbook))

    def test_reloads_and_broadcasts_when_selected_groups_change(self) -> None:
        workbook = b"new-selected-group-schedule"
        updated = copy.deepcopy(self.payload)
        first_group = next(iter(updated["schedule"]))
        first_day = next(iter(updated["schedule"][first_group]))
        updated["schedule"][first_group][first_day][0]["lesson"] += " (изменено)"

        with tempfile.TemporaryDirectory() as temporary:
            work_dir = Path(temporary)
            updater = self.make_updater(work_dir, workbook)
            with patch("schedule_updater.build_schedule", return_value=updated):
                changed = updater.check_once()
            self.assertTrue((work_dir / "schedule_data.json").exists())

        self.assertTrue(changed)
        self.assertEqual(self.bot.reloaded, [updated])
        self.assertEqual(self.bot.broadcasts, [updated["summary"]])

    def test_unchanged_notification_is_sent_after_each_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            updater = self.make_updater(Path(temporary), b'unchanged')
            with patch('schedule_updater.build_schedule', return_value=self.payload):
                updater.check_once()
                updater.check_once()
        self.assertEqual(self.bot.unchanged_checks, 2)

    def test_next_check_uses_fixed_moscow_times(self):
        times = parse_check_times("07:00,11:00,15:00")
        now = datetime(2026, 11, 19, 10, 15, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(next_check_at(times, now).strftime("%Y-%m-%d %H:%M"), "2026-11-19 11:00")

    def test_next_check_moves_to_tomorrow_after_last_time(self):
        times = parse_check_times("07:00,23:00")
        now = datetime(2026, 11, 19, 23, 1, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(next_check_at(times, now).strftime("%Y-%m-%d %H:%M"), "2026-11-20 07:00")


if __name__ == "__main__":
    unittest.main()
