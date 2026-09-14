from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    timezone: str = Field(default="Asia/Jerusalem")

    # ── Telegram ──────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="changeme")
    telegram_chat_id: int = Field(default=0)
    # Расходы из ТГ-чата относятся ровно к одной компании, разбирать её из
    # документа не нужно. Ключ должен совпадать с ключом в companies.
    telegram_company: str = Field(default="companyA")
    # Кому уходит очередь needs_review. 0 = дайджест никому не шлётся.
    telegram_reviewer_id: int = Field(default=0)
    # getFile не отдаёт файлы больше 20 МБ
    telegram_max_file_bytes: int = Field(default=20 * 1024 * 1024)
    # Альбом приходит несколькими апдейтами, подпись только у первого
    telegram_media_group_window_s: float = Field(default=2.0)

    # ── Почта (IMAP, пароль приложения Google) ────────────────────────────
    imap_host: str = Field(default="imap.gmail.com")
    imap_user: str = Field(default="invoices@orlanda.info")
    imap_password: str = Field(default="changeme")
    imap_timeout_s: int = Field(default=25)
    imap_poll_interval_s: int = Field(default=300)
    # Папку выбираем по special-use флагу \All, а не по имени: имя приходит
    # в modified UTF-7 и зависит от локали ящика.
    imap_use_all_mail: bool = Field(default=True)
    imap_backfill_days: int = Field(default=30)

    # Вложения-артефакты почтовых клиентов: подписи, иконки, inline-картинки.
    # Наблюдались живьём (~WRD0000.jpg 0 КБ, image001.png 1 КБ).
    attachment_min_bytes: int = Field(default=5 * 1024)
    attachment_skip_patterns: List[str] = Field(
        default_factory=lambda: [
            r"^image\d{3}\.(png|jpg|jpeg|gif)$",
            r"^~WRD\d+\.(jpg|jpeg|png)$",
            r"^oledata\.mso$",
            r"^ATT\d+\.(txt|htm|html)$",
        ]
    )
    # Только то, что НИКОГДА не бывает документом расхода.
    #
    # Bolt шлёт с трёх разных адресов на bolt.eu: чеки, маркетинг и
    # верификацию — поэтому фильтруем по адресу, а не по домену.
    #
    # feedback@slack.com и info@tele2.ee здесь были и это была ошибка:
    # Slack присылает с него реальные счета ("your plan has renewed" с
    # двумя PDF), Tele2 — оператор связи. Игнор по отправителю там, где
    # вендор шлёт с одного адреса и рассылку, и счёт, тихо теряет расход.
    # Сомнительное пропускаем: разборщик пометит ignored сам.
    sender_ignore_list: List[str] = Field(
        default_factory=lambda: [
            "no-reply@accounts.google.com",
            "noreply@google.com",
            "mail-noreply@google.com",
            "cyprus@delivery-marketing.bolt.eu",
            "verify@bolt.eu",
        ]
    )

    # ── Dropbox (командный аккаунт Orlanda) ───────────────────────────────
    dropbox_app_key: str = Field(default="changeme")
    dropbox_app_secret: str = Field(default="changeme")
    dropbox_refresh_token: str = Field(default="changeme")
    # Работа от имени участника команды и в командном пространстве, иначе
    # запись уйдёт в личное пространство участника.
    dropbox_team_member_id: str = Field(default="")
    dropbox_namespace_id: str = Field(default="")
    # Ключ компании -> корневая папка. _inbox обязан лежать в том же
    # namespace, что и корни: move_v2 через границу namespace запрещён.
    dropbox_company_roots: dict[str, str] = Field(default_factory=dict)
    dropbox_inbox_path: str = Field(default="/_inbox")
    # Существующее соглашение фин директора: August_2026
    dropbox_month_format: str = Field(default="%B_%Y")
    registry_filename: str = Field(default="_реестр.csv")
    # Полная перегенерация реестра батчем, а не по каждому файлу: иначе
    # десктопный клиент Dropbox плодит conflicted copy.
    registry_debounce_s: int = Field(default=180)

    # ── Разбор документов ─────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="changeme")
    parser_model: str = Field(default="claude-opus-5")
    parser_interval_s: int = Field(default=600)
    parser_max_attempts: int = Field(default=3)
    # Сухой прогон: разбирает и пишет результат в БД, но НЕ перекладывает
    # файлы в папки компаний и ничего не удаляет. Нужен, чтобы посмотреть
    # раскладку на реальных документах до того, как она случится.
    parser_dry_run: bool = Field(default=True)
    # Компания определяется точным вхождением названия юрлица в текст, а не
    # самооценкой модели: юрлиц ровно два и их названия известны заранее.
    company_legal_names: dict[str, List[str]] = Field(default_factory=dict)
    # Допуск для бизнес-ключа дедупа: вендор + сумма + валюта + дата ± N дней
    dedup_date_window_days: int = Field(default=3)

    # Компания по умолчанию, когда в документе нет НИКАКИХ признаков ни
    # одной из компаний. Решение владельца 14.09.2026: чеки без покупателя
    # (такси, аренда авто, авиабилеты, подписки) всегда эстонские,
    # "если прямо в чеке не написано обратного".
    #
    # Применяется ТОЛЬКО при полном отсутствии признаков. Любое указание
    # на израильскую компанию — идентификатор, название, иврит — отменяет
    # умолчание и документ уходит по найденному признаку.
    #
    # Пустая строка выключает умолчание: тогда такие документы идут в очередь.
    default_company: str = Field(default="")

    # ── Локальный буфер ───────────────────────────────────────────────────
    # Файл ложится сюда ДО подтверждения источника: падение на любом шаге
    # означает повторную обработку, а не потерю документа.
    buffer_root: str = Field(default="/data/buffer")
    uploader_interval_s: int = Field(default=15)
    uploader_backoff_cap_s: int = Field(default=300)

    # ── Инфраструктура ────────────────────────────────────────────────────
    database_url: str = Field(default="postgresql+asyncpg://user:pass@db-router:6432/expenses")
    redis_url: str = Field(default="redis://redis:6379/0")
    # Молча умерший коллектор нарушает главный критерий "ноль потерь":
    # Telegram хранит непрочитанные апдейты только 24 часа.
    heartbeat_alert_after_s: int = Field(default=3600)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
