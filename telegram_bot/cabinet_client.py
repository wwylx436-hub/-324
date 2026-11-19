"""Клиент простого личного кабинета с HTML-формой входа."""

from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._current = {
                "action": attributes.get("action") or "",
                "method": (attributes.get("method") or "post").lower(),
                "inputs": [],
            }
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None:
            inputs = self._current["inputs"]
            assert isinstance(inputs, list)
            inputs.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


class CabinetClient:
    """Авторизация и скачивание Excel из кабинета.

    Подходит для обычной серверной HTML-формы. Кабинеты с CAPTCHA, 2FA или
    JavaScript-авторизацией потребуют отдельного адаптера после осмотра сайта.
    """

    def __init__(self) -> None:
        self.login_url = os.environ["CABINET_LOGIN_URL"]
        self.schedule_url = os.environ["CABINET_SCHEDULE_URL"]
        self.username = os.environ["CABINET_USERNAME"]
        self.password = os.environ["CABINET_PASSWORD"]
        self.username_field = os.getenv("CABINET_USERNAME_FIELD", "")
        self.password_field = os.getenv("CABINET_PASSWORD_FIELD", "")
        self.extra_fields = json.loads(os.getenv("CABINET_EXTRA_FORM_JSON", "{}"))
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    @staticmethod
    def _request(url: str, data: bytes | None = None) -> Request:
        return Request(
            url,
            data=data,
            headers={
                "User-Agent": "CollegeScheduleBot/1.0",
                "Accept": "text/html,application/xhtml+xml,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
            },
        )

    def _find_login_form(self, html: str) -> tuple[str, dict[str, str], str, str]:
        parser = LoginFormParser()
        parser.feed(html)
        candidates = []
        for form in parser.forms:
            inputs = form["inputs"]
            assert isinstance(inputs, list)
            if any((item.get("type") or "").lower() == "password" for item in inputs):
                candidates.append(form)
        if not candidates:
            raise RuntimeError("На странице кабинета не найдена форма с полем пароля.")

        form = candidates[0]
        fields: dict[str, str] = {}
        detected_username = ""
        detected_password = ""
        for item in form["inputs"]:
            name = item.get("name")
            if not name:
                continue
            input_type = (item.get("type") or "text").lower()
            if input_type in {"hidden", "submit"}:
                fields[name] = item.get("value") or ""
            if input_type in {"text", "email"} and not detected_username:
                detected_username = name
            if input_type == "password" and not detected_password:
                detected_password = name

        username_field = self.username_field or detected_username
        password_field = self.password_field or detected_password
        if not username_field or not password_field:
            raise RuntimeError("Не удалось определить поля логина и пароля.")
        action = urljoin(self.login_url, str(form["action"]))
        return action, fields, username_field, password_field

    def download_schedule(self) -> bytes:
        with self.opener.open(self._request(self.login_url), timeout=45) as response:
            html = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")

        action, fields, username_field, password_field = self._find_login_form(html)
        fields.update(self.extra_fields)
        fields[username_field] = self.username
        fields[password_field] = self.password
        with self.opener.open(
            self._request(action, urlencode(fields).encode("utf-8")), timeout=45
        ) as response:
            response.read()

        with self.opener.open(self._request(self.schedule_url), timeout=60) as response:
            content = response.read()
        if not content.startswith(b"PK"):
            raise RuntimeError(
                "Кабинет вернул не Excel-файл. Возможно, вход не выполнен или нужна отдельная ссылка скачивания."
            )
        return content
