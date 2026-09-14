"""Очистка текста, имена файлов и папок.

Здесь закреплены два бага, каждый из которых ронял систему целиком:
NUL-байты из PDF и зависимость имени месяца от локали контейнера.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core import naming
from app.core.text import clean
from app.services import buffer


# ── NUL-байты ─────────────────────────────────────────────────────────────


def test_nul_вырезается():
    """asyncpg падает с CharacterNotInRepertoireError и уносит весь батч,
    а не один документ. Извлечение PDF отдаёт \\x00 регулярно."""
    assert clean("до\x00после") == "допосле"


def test_управляющие_символы_вырезаются():
    assert clean("a\x01b\x1fc\x7fd") == "abcd"


def test_переводы_строк_сохраняются():
    """Они несут структуру документа, и терять их нельзя."""
    assert clean("строка1\nстрока2\tтаб\r\n") == "строка1\nстрока2\tтаб\r\n"


def test_none_остаётся_none():
    assert clean(None) is None


# ── имена папок месяцев ───────────────────────────────────────────────────


def test_месяц_по_английски_независимо_от_локали():
    """strftime('%B') в контейнере с ru_RU вернёт «августа», и папки
    разъедутся с соглашением фин директора. Таблица месяцев явная."""
    assert naming.month_folder(datetime(2026, 8, 1)) == "August_2026"
    assert naming.month_folder(datetime(2026, 9, 30)) == "September_2026"
    assert naming.month_folder(datetime(2026, 1, 15)) == "January_2026"


def test_путь_компании_двухуровневый():
    """Реальная структура: <корень>/<год>/<Месяц_Год>."""
    path = naming.company_path("orlanda_engineering", datetime(2026, 9, 1))
    assert path == "/Finance/AMS Group_accouting/2026/September_2026"


def test_путь_израильской_компании():
    path = naming.company_path("orlanda_israel", datetime(2026, 9, 1))
    assert path == "/Finance/Orlanda Israel/Invoices/2026/September_2026"


def test_неизвестная_компания_это_ошибка():
    with pytest.raises(KeyError):
        naming.company_path("нет такой", datetime(2026, 9, 1))


# ── имена файлов ──────────────────────────────────────────────────────────


def test_опасные_символы_убираются():
    assert "/" not in buffer.safe_name("a/b:c*d?.pdf")
    assert buffer.safe_name("счёт 2026.pdf").endswith(".pdf")


def test_пробелы_становятся_дефисами():
    assert buffer.safe_name("Tonystam LLC invoice.pdf") == "Tonystam-LLC-invoice.pdf"


def test_длина_считается_в_БАЙТАХ_а_не_символах():
    """Лимит Dropbox — 255 байт. Кириллица в UTF-8 съедает его вдвое
    быстрее, поэтому считать символы недостаточно."""
    name = "щ" * 200 + ".pdf"
    out = buffer.safe_name(name)
    assert len(out.encode("utf-8")) <= buffer.MAX_NAME_BYTES
    assert out.endswith(".pdf")


def test_обрыв_utf8_не_ломает_строку():
    out = buffer.safe_name("я" * 120 + ".pdf")
    out.encode("utf-8").decode("utf-8")   # не должно падать


def test_пустое_имя_заменяется():
    assert buffer.safe_name("", fallback="letter") == "letter"
    assert buffer.safe_name(None, fallback="letter") == "letter"


def test_имя_без_расширения():
    assert buffer.safe_name("просто имя") == "просто-имя"
