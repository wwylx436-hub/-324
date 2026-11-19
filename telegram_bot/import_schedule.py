"""Извлечение расписания из исходного Excel-файла в JSON для бота."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel


BASE_DIR = Path(__file__).resolve().parent
ALLOWED_GROUP_NAMES = frozenset({"П9/25-4", "Э9/25-3"})
GROUP_RE = re.compile(r"^[^\n]{1,50}/(\d{2})-\d+$")
TIME_RE = re.compile(
    r"(?P<h1>\d{1,2})[.:](?P<m1>\d{2})\s*[–—-]\s*"
    r"(?P<h2>\d{1,2})[.:](?P<m2>\d{2})"
)
WEEKDAYS = {
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def normalize_time(value: Any) -> str | None:
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    return (
        f"{int(match.group('h1')):02d}:{match.group('m1')}–"
        f"{int(match.group('h2')):02d}:{match.group('m2')}"
    )


def excel_date(value: Any, epoch: datetime) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 35000 < value < 60000:
        return from_excel(value, epoch).date()
    return None


def expanded_values(worksheet: Any) -> dict[tuple[int, int], Any]:
    """Вернуть значения объединённых ячеек для каждой координаты диапазона."""
    values: dict[tuple[int, int], Any] = {}
    for merged_range in worksheet.merged_cells.ranges:
        value = worksheet.cell(merged_range.min_row, merged_range.min_col).value
        for row in range(merged_range.min_row, merged_range.max_row + 1):
            for column in range(merged_range.min_col, merged_range.max_col + 1):
                values[(row, column)] = value
    return values


def cell_value(worksheet: Any, merged: dict[tuple[int, int], Any], row: int, column: int) -> Any:
    return merged.get((row, column), worksheet.cell(row, column).value)


def find_group_columns(worksheet: Any) -> tuple[int, dict[int, str]]:
    candidates: dict[int, dict[int, str]] = defaultdict(dict)
    for row in worksheet.iter_rows():
        for cell in row:
            value = clean_text(cell.value)
            if GROUP_RE.fullmatch(value):
                candidates[cell.row][cell.column] = value

    if not candidates:
        return 0, {}
    header_row = max(candidates, key=lambda row: len(candidates[row]))
    return header_row, candidates[header_row]


def build_schedule(source: Path) -> dict[str, Any]:
    workbook = load_workbook(source, data_only=True, read_only=False)
    sheet_groups: dict[str, tuple[int, dict[int, str]]] = {}
    all_group_names: set[str] = set()
    entry_years: list[int] = []

    for worksheet in workbook.worksheets:
        header_row, columns = find_group_columns(worksheet)
        if not columns:
            continue
        sheet_groups[worksheet.title] = (header_row, columns)
        all_group_names.update(columns.values())
        entry_years.extend(int(GROUP_RE.fullmatch(name).group(1)) for name in columns.values())

    if not all_group_names:
        raise ValueError("В книге не найдены названия групп вида …/26-3.")

    missing_groups = ALLOWED_GROUP_NAMES - all_group_names
    if missing_groups:
        missing = ", ".join(sorted(missing_groups))
        raise ValueError(f"В книге не найдены выбранные группы: {missing}.")

    for sheet_name, (header_row, columns) in tuple(sheet_groups.items()):
        selected_columns = {
            column: name for column, name in columns.items() if name in ALLOWED_GROUP_NAMES
        }
        sheet_groups[sheet_name] = (header_row, selected_columns)

    newest_entry_year = max(entry_years)
    sorted_groups = sorted(
        ALLOWED_GROUP_NAMES,
        key=lambda name: (newest_entry_year - int(GROUP_RE.fullmatch(name).group(1)) + 1, name),
    )
    group_ids = {name: f"g{index:03d}" for index, name in enumerate(sorted_groups, start=1)}
    groups = [
        {
            "id": group_ids[name],
            "name": name,
            "course": newest_entry_year - int(GROUP_RE.fullmatch(name).group(1)) + 1,
        }
        for name in sorted_groups
    ]

    raw_schedule: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))

    for worksheet in workbook.worksheets:
        if worksheet.title not in sheet_groups:
            continue
        header_row, group_columns = sheet_groups[worksheet.title]
        if not group_columns:
            continue
        merged = expanded_values(worksheet)
        current_date: date | None = None
        current_weekday = ""

        for row in range(header_row + 1, worksheet.max_row + 1):
            time_column = 0
            lesson_time: str | None = None
            for column in range(1, min(group_columns) + 1):
                normalized = normalize_time(cell_value(worksheet, merged, row, column))
                if normalized:
                    time_column = column
                    lesson_time = normalized
                    break
            if not lesson_time:
                continue

            for column in range(1, time_column):
                value = cell_value(worksheet, merged, row, column)
                possible_date = excel_date(value, workbook.epoch)
                if possible_date:
                    current_date = possible_date
                possible_weekday = clean_text(value).lower()
                if possible_weekday in WEEKDAYS:
                    current_weekday = possible_weekday

            if current_date is None:
                continue

            for column, group_name in group_columns.items():
                lesson = clean_text(cell_value(worksheet, merged, row, column))
                if not lesson:
                    continue
                raw_schedule[group_ids[group_name]][current_date.isoformat()].append(
                    {
                        "time": lesson_time,
                        "lesson": lesson,
                        "weekday": current_weekday,
                    }
                )

    schedule: dict[str, dict[str, list[dict[str, str]]]] = {}
    for group_id, days in raw_schedule.items():
        schedule[group_id] = {}
        for day, lessons in sorted(days.items()):
            unique: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for lesson in lessons:
                key = (lesson["time"], lesson["lesson"])
                if key not in seen:
                    seen.add(key)
                    unique.append(lesson)
            unique.sort(key=lambda item: item["time"])
            schedule[group_id][day] = unique

    date_counter = Counter(day for days in schedule.values() for day in days)
    return {
        "source": source.name,
        "groups": groups,
        "schedule": schedule,
        "summary": {
            "group_count": len(groups),
            "date_from": min(date_counter) if date_counter else None,
            "date_to": max(date_counter) if date_counter else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=BASE_DIR / "data" / "schedule.xlsx")
    parser.add_argument("output", nargs="?", type=Path, default=BASE_DIR / "schedule_data.json")
    args = parser.parse_args()

    payload = build_schedule(args.source)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = payload["summary"]
    print(
        f"Готово: {summary['group_count']} групп, "
        f"даты {summary['date_from']} — {summary['date_to']}"
    )


if __name__ == "__main__":
    main()
