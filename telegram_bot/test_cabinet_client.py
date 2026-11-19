import unittest
from cabinet_client import CabinetClient
from schedule_updater import payload_sha256


class FakeResponse:
    class Headers:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    headers = Headers()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    @staticmethod
    def read():
        return b"already authorised"


class FakeOpener:
    @staticmethod
    def open(request, timeout):
        return FakeResponse()


class SelectionTests(unittest.TestCase):
    def test_only_explicit_full_time_schedule(self):
        for label in ['Расписание ДОТ', 'Расписание ЗАОЧНОЙ формы', 'Расписание очной формы с ДОТ', 'Расписание без указания формы']:
            self.assertFalse(CabinetClient.full_time_news(label), label)
        self.assertTrue(CabinetClient.full_time_news('Расписание занятий для студентов очной формы обучения'))

    def test_filename_does_not_trigger_schedule_update(self):
        self.assertEqual(payload_sha256({'source': 'old.xlsx', 'groups': [], 'schedule': {}}),
                         payload_sha256({'source': 'new.xlsx', 'groups': [], 'schedule': {}}))

    def test_reuses_existing_authorised_session(self):
        client = object.__new__(CabinetClient)
        client.login_url = "https://example.test/login"
        client.opener = FakeOpener()
        client._request = lambda url, data=None: url
        client._find_login_form = lambda html: (_ for _ in ()).throw(RuntimeError("no form"))
        client._discover_schedule = lambda: b"PK-authorised-workbook"

        self.assertEqual(client.download_schedule(), b"PK-authorised-workbook")
