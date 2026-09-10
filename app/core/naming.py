from __future__ import annotations

from datetime import datetime

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
    """Имя папки месяца в соглашении фин директора: August_2026.

    Формат задаётся DROPBOX_MONTH_FORMAT. Поддерживается %B (английское
    название месяца), %m, %Y — этого хватает и для August_2026,
    и для 2026-08_August, если решат перейти ради сортировки.
    """
    return (
        settings.dropbox_month_format
        .replace("%B", _MONTHS_EN[when.month - 1])
        .replace("%m", f"{when.month:02d}")
        .replace("%Y", str(when.year))
    )


def inbox_path(when: datetime, filename: str) -> str:
    """Путь в _inbox. Месяц здесь — месяц ПОЛУЧЕНИЯ документа, а не месяц
    расхода: коллектор содержимого не знает. Разборщик из спека №2
    переложит файл в правильный месяц правильной компании."""
    root = settings.dropbox_inbox_path.rstrip("/")
    return f"{root}/{month_folder(when)}/{filename}"
