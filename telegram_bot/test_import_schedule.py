import unittest

from openpyxl import Workbook

from import_schedule import worksheet_is_full_time


class StudyFormFilterTests(unittest.TestCase):
    @staticmethod
    def worksheet(title: str, heading: str = ""):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        sheet["A1"] = heading
        return sheet

    def test_accepts_explicit_full_time_form(self) -> None:
        sheet = self.worksheet("2 курс", "Очная форма обучения")
        self.assertTrue(worksheet_is_full_time(sheet, 3))

    def test_accepts_current_workbook_without_form_label(self) -> None:
        sheet = self.worksheet("1 курс-11, 2 курс-9")
        self.assertTrue(worksheet_is_full_time(sheet, 3))

    def test_rejects_part_time_form(self) -> None:
        sheet = self.worksheet("Заочная форма")
        self.assertFalse(worksheet_is_full_time(sheet, 3))

    def test_rejects_dot_and_distance_forms(self) -> None:
        dot = self.worksheet("2 курс", "ДОТ")
        distance = self.worksheet("Дистанционная форма")
        self.assertFalse(worksheet_is_full_time(dot, 3))
        self.assertFalse(worksheet_is_full_time(distance, 3))


if __name__ == "__main__":
    unittest.main()
