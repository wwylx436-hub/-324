# Бот «Расписание колледжа»

Бот показывает расписание из Excel через удобное меню:

1. постоянная нижняя кнопка **«Открыть расписание»**;
2. выбор курса;
3. выбор группы;
4. выбор дня или кнопка **«Вся неделя»**.

В проект уже импортированы 56 групп и расписание на даты с 19.10.2026 по 26.12.2026.

## Запуск в Visual Studio Code

Откройте эту папку в VS Code, затем выберите **Terminal → New Terminal** и выполните:

```powershell
& 'C:\Users\avoka\AppData\Local\Python\bin\python.exe' bot.py
```

Токен уже хранится локально в `.env`. Не отправляйте этот файл другим людям и не добавляйте его в Git. Для остановки бота нажмите `Ctrl+C`.

## Проверка

Тесты работают без Telegram и без токена:

```powershell
& 'C:\Users\avoka\AppData\Local\Python\bin\python.exe' -m unittest -v
```

## Обновление расписания

Замените файл `data/schedule.xlsx` новой таблицей такого же формата. Затем установите библиотеку для чтения Excel и повторно создайте данные:

```powershell
& 'C:\Users\avoka\AppData\Local\Python\bin\python.exe' -m pip install -r requirements-import.txt
& 'C:\Users\avoka\AppData\Local\Python\bin\python.exe' import_schedule.py
```

После этого перезапустите `bot.py`. Для обычной работы бота сторонние библиотеки не нужны.
