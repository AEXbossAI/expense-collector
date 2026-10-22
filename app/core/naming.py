from __future__ import annotations

from datetime import date, datetime

from app.core.config import get_settings

settings = get_settings()

# strftime("%B") зависит от локали процесса: в контейнере с ru_RU он вернёт
# "августа" вместо "August" и папки разъедутся с существующим соглашением
# фин директора. Поэтому таблица явная, локаль ни на что не влияет.
_MONTHS_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def month_folder(when: datetime) -> str:
    """Имя папки месяца: September_2026. Год в имени дублируется — так уже
    заведено у фин директора в обеих компаниях, не ломаем."""
    return f"{_MONTHS_EN[when.month - 1]}_{when.year}"


def is_month_folder(name: str) -> bool:
    """`September_2026` — да, `+INVOICES WISE` и `2026` — нет."""
    parts = name.split("_")
    return len(parts) == 2 and parts[0] in _MONTHS_EN and parts[1].isdigit()


def month_of_path(path: str, root: str) -> str | None:
    """Папка месяца, в которой лежит файл: `<корень>/<год>/<Месяц_Год>/...`.

    Подпапки внутри месяца (`+INVOICES WISE`) не мешают: месяц — второй
    уровень под корнем компании. Путь вне корня или вне папки месяца — None.
    """
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):].split("/")
    if len(rel) >= 3 and rel[0].isdigit() and is_month_folder(rel[1]):
        return rel[1]
    return None


# Поездки относятся к месяцу самой поездки, а не оплаты (решение фин
# директора 22.10.2026): билет на октябрь, купленный в сентябре, — расход
# октября. Значение совпадает с категорией из закрытого списка в llm.py.
TRAVEL_CATEGORY = "Flights & travel"


def travel_date(doc) -> date | None:
    """Дата вылета или заезда, если документ — поездка и дата известна."""
    if getattr(doc, "category", None) != TRAVEL_CATEGORY:
        return None
    return getattr(doc, "travel_date", None)


def expense_month(doc) -> str | None:
    """К какому месяцу относится расход. Одно правило для папки, реестра
    и колонки Billing month на доске.

    1. Поездка с известной датой — месяц вылета или заезда. Даже если файл
       фин директор положила в папку другого месяца: файл остаётся в её
       подпапке банка, а строка уходит в месяц поездки. Кроме поездки в
       уже закрытый месяц — сданный реестр не дописывается.
    2. Файл лежит в папке месяца компании — этот месяц. Иначе ручной файл
       без распознанной даты не попадал ни в один реестр, а счёт за август,
       положенный в сентябрь, уезжал строкой в август.
    3. Месяц документа (у квитанции это месяц оплаты).
    """
    travel = travel_date(doc)
    if travel and not getattr(doc, "belongs_to_closed_month", None):
        return month_folder(datetime(travel.year, travel.month, 1))
    root = settings.dropbox_company_roots.get(getattr(doc, "company", None) or "")
    path = getattr(doc, "dropbox_path", None) or ""
    if root and path and not path.startswith(settings.dropbox_inbox_path.rstrip("/") + "/"):
        month = month_of_path(path, root)
        if month:
            return month
    doc_date = getattr(doc, "doc_date", None)
    if doc_date:
        return month_folder(datetime(doc_date.year, doc_date.month, 1))
    return None


def folder_in_month(path: str, root: str) -> str | None:
    """Папка файла от папки месяца: `September_2026/+ invoices REVOLUT`."""
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):].split("/")
    if len(rel) >= 3 and rel[0].isdigit() and is_month_folder(rel[1]):
        return "/".join(rel[1:-1])
    return None


def company_path(company: str, when: datetime) -> str:
    """Целевая папка документа: <корень компании>/<год>/<Месяц_Год>.

    Реальная структура на 11.09.2026:
      /Finance/AMS Group_accouting/2026/September_2026
      /Finance/Orlanda Israel/Invoices/2026/September_2026

    Папка года существует, папку месяца создаём при необходимости.
    """
    root = settings.dropbox_company_roots.get(company)
    if not root:
        raise KeyError(f"Нет корневой папки Dropbox для компании {company!r}")
    return f"{root.rstrip('/')}/{when.year}/{month_folder(when)}"


def inbox_path(when: datetime, filename: str) -> str:
    """Путь в _inbox. Месяц здесь — месяц ПОЛУЧЕНИЯ документа, а не месяц
    расхода: коллектор содержимого не знает. Разборщик из спека №2
    переложит файл в правильный месяц правильной компании."""
    root = settings.dropbox_inbox_path.rstrip("/")
    return f"{root}/{month_folder(when)}/{filename}"
