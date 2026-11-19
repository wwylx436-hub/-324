import unittest
from cabinet_client import CabinetClient
from schedule_updater import payload_sha256

class SelectionTests(unittest.TestCase):
    def test_only_explicit_full_time_schedule(self):
        for label in ['Расписание ДОТ', 'Расписание ЗАОЧНОЙ формы', 'Расписание очной формы с ДОТ', 'Расписание без указания формы']:
            self.assertFalse(CabinetClient.full_time_news(label), label)
        self.assertTrue(CabinetClient.full_time_news('Расписание занятий для студентов очной формы обучения'))

    def test_filename_does_not_trigger_schedule_update(self):
        self.assertEqual(payload_sha256({'source': 'old.xlsx', 'groups': [], 'schedule': {}}),
                         payload_sha256({'source': 'new.xlsx', 'groups': [], 'schedule': {}}))
