"""Клиент простого личного кабинета с HTML-формой входа."""

from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlparse
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


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


class CabinetClient:
    """Авторизация и скачивание Excel из кабинета.

    Подходит для обычной серверной HTML-формы. Кабинеты с CAPTCHA, 2FA или
    JavaScript-авторизацией потребуют отдельного адаптера после осмотра сайта.
    """

    def __init__(self) -> None:
        self.login_url = os.environ["CABINET_LOGIN_URL"]
        self.schedule_url = os.getenv("CABINET_SCHEDULE_URL", "").strip()
        self.dashboard_url = os.getenv("CABINET_DASHBOARD_URL", "https://lk.mibiu.ru/dashboard")
        self.username = os.environ["CABINET_USERNAME"]
        self.password = os.environ["CABINET_PASSWORD"]
        self.username_field = os.getenv("CABINET_USERNAME_FIELD", "")
        self.password_field = os.getenv("CABINET_PASSWORD_FIELD", "")
        self.extra_fields = json.loads(os.getenv("CABINET_EXTRA_FORM_JSON", "{}"))
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    @staticmethod
    def _is_excel(content: bytes) -> bool:
        return content.startswith(b"PK")

    def _same_site(self, url: str) -> bool:
        return urlparse(url).netloc == urlparse(self.login_url).netloc

    @staticmethod
    def _looks_relevant(url: str, text: str) -> bool:
        value = f"{url} {text}".casefold()
        include = (
            "распис", "schedule", "заняти", "учеб", "study", "education",
            "скач", "download", "excel", "xlsx",
        )
        exclude = ("logout", "delete", "remove", "отчис", "удал")
        return any(word in value for word in include) and not any(word in value for word in exclude)

    def _discover_schedule(self) -> bytes:
        queue: list[tuple[str, int]] = [(self.dashboard_url, 0)]
        visited: set[str] = set()
        candidates: list[str] = []
        while queue and len(visited) < 15:
            url, depth = queue.pop(0)
            if url in visited or not self._same_site(url):
                continue
            visited.add(url)
            with self.opener.open(self._request(url), timeout=60) as response:
                content = response.read()
                final_url = response.geturl()
                content_type = response.headers.get_content_type()
            if self._is_excel(content):
                return content
            if depth >= 2 or content_type not in {"text/html", "application/xhtml+xml"}:
                continue
            parser = LinkParser()
            parser.feed(content.decode("utf-8", errors="replace"))
            for href, text in parser.links:
                candidate = urljoin(final_url, href)
                if self._same_site(candidate) and self._looks_relevant(candidate, text):
                    candidates.append(candidate)
                    queue.append((candidate, depth + 1))
        sample = ", ".join(dict.fromkeys(candidates[:8])) or "кандидаты не найдены"
        raise RuntimeError(
            "Не удалось автоматически найти Excel с расписанием после входа. "
            f"Проверенные ссылки: {sample}"
        )

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
            login_result_url = response.geturl()
            login_result = response.read().decode(
                response.headers.get_content_charset() or "utf-8",
                errors="replace",
            )
        result_parser = LoginFormParser()
        result_parser.feed(login_result)
        password_form_remains = any(
            any((item.get("type") or "").lower() == "password" for item in form["inputs"])
            for form in result_parser.forms
        )
        if password_form_remains or login_result_url.rstrip("/") == self.login_url.rstrip("/"):
            raise RuntimeError("Вход в личный кабинет не выполнен. Проверьте логин и пароль.")

        if not self.schedule_url:
            return self._discover_schedule()

        with self.opener.open(self._request(self.schedule_url), timeout=60) as response:
            content = response.read()
        if not self._is_excel(content):
            raise RuntimeError(
                "Кабинет вернул не Excel-файл. Возможно, вход не выполнен или нужна отдельная ссылка скачивания."
            )
        return content
