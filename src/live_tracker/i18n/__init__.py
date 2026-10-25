"""Portuguese and English for everything people read.

English is the source language: the English text in the code is the key, and translation_cache.json
maps it to Portuguese, as in {"pt": {"Today": "Hoje"}}. A key without a translation falls back to
English. Only words go through here; data such as names, numbers and file names never do.
tests/test_i18n.py fails when text used in the templates or code has no Portuguese translation.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

LANGUAGES = ("pt", "en")
CATALOG_PATH = Path(__file__).with_name("translation_cache.json")
_CATALOG: dict[str, dict[str, str]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def translate(text: str, language: str, **values: object) -> str:
    if language != "en":
        text = _CATALOG.get(language, {}).get(text, text)
    return text.format(**values) if values else text


def catalog(language: str) -> dict[str, str]:
    return _CATALOG.get(language, {})


class Translator:
    """t("Hello, {name}", name=...) in one language."""

    def __init__(self, language: str) -> None:
        self.language = language if language in LANGUAGES else "en"

    def __call__(self, text: str, **values: object) -> str:
        return translate(text, self.language, **values)

    def long_date(self, day: date) -> str:
        text = self(
            "{weekday}, {month} {day}",
            weekday=self(WEEKDAYS[day.weekday()]),
            month=self(MONTHS[day.month - 1]),
            day=day.day,
        )
        return text[:1].upper() + text[1:]

    def day_month(self, day: date) -> str:
        """23/10 in Portuguese, Oct 23 in English."""
        if self.language == "pt":
            return f"{day.day:02d}/{day.month:02d}"
        return f"{MONTHS[day.month - 1][:3]} {day.day}"

    def short_date(self, day: date) -> str:
        """23/10/2026 in Portuguese, Oct 23, 2026 in English."""
        return f"{self.day_month(day)}/{day.year}" if self.language == "pt" else f"{self.day_month(day)}, {day.year}"

    def number(self, value: float, digits: int = 2) -> str:
        text = f"{value:.{digits}f}"
        return text.replace(".", ",") if self.language == "pt" else text
