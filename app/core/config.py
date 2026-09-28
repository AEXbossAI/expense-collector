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

    # Час отправки дайджеста фин директору, в TIMEZONE.
    # Одно сообщение в день: уведомление на каждый чек
    # перестают читать через неделю.
    digest_hour: int = Field(default=9)

    # С какого месяца реконсилер сверяет папки, включительно. Всё раньше
    # не трогается вовсе: решение владельца 14.09.2026 — "август не
    # трогаем". Полный проход по августу завёл бы 79 документов
    # эстонской компании, потратил около доллара на разбор и вскрыл бы
    # то, что фин директор считает закрытым.
    reconcile_from: str = Field(default="2026-09")

    # ── Monday: доска расходов ────────────────────────────────────────────
    # Токен общий с шестью другими приложениями стека, и это не лечится:
    # бюджет сложности Monday выделяется НА АККАУНТ, а не на токен (замер
    # 28.09.2026: complexity.before = 20_000_000, сброс раз в 60с), а второй
    # персональный токен Monday пользователю не выдаёт — перевыпуск заменяет
    # старый и уронил бы все шесть приложений разом.
    monday_api_token: str = Field(default="")
    # Версию API пиновать обязательно: без заголовка Monday применяет версию
    # аккаунта по умолчанию и меняет её по своему расписанию, а формат ошибки
    # "Complexity budget exhausted" уже менялся на версии 2025-07.
    monday_api_version: str = Field(default="2026-04")
    monday_board_id: int = Field(default=1550620081)
    monday_todo_board_id: int = Field(default=1542673123)
    monday_projects_board_id: int = Field(default=1542627875)
    # Колонка Drawings Num на доске TO DO LIST. В ней лежит код вида
    # AMR-SAN-AQU-SC-2026-07-1677, который инвойс подрядчика несёт в тексте.
    monday_task_code_column: str = Field(default="text9__1")
    # Наши поля -> id колонок доски. В конфиге, а не в коде: правка колонки
    # на доске не должна требовать релиза.
    monday_columns: dict[str, str] = Field(default_factory=dict)
    # Все расходы идут в одну группу Out, всегда. Групп-месяцев нет и не
    # должно быть: месяц и так стоит в колонке Billing month у каждой
    # строки, а разрезание доски по месяцам ломает привычный вид.
    monday_group_id: str = Field(default="group_mm5x742")
    # Выключено до ручной приёмки, как PARSER_DRY_RUN в спеке №2.
    monday_sync_enabled: bool = Field(default=False)
    # Разбирает и печатает, какие items завёл бы, но ничего не пишет.
    monday_dry_run: bool = Field(default=True)
    monday_sync_interval_s: int = Field(default=120)
    monday_backoff_cap_s: int = Field(default=600)
    # Документов за проход. Лимиты Monday с запасом (20M единиц сложности
    # в минуту на аккаунт), так что это вежливость, а не защита.
    monday_batch_size: int = Field(default=5)
    # Справочник проектов с доски Projects, кэш в БД.
    monday_projects_cache_ttl_s: int = Field(default=86400)

    # ── Пересчёт валют ────────────────────────────────────────────────────
    # ECB через frankfurter: отдаёт "единиц валюты за 1 EUR", ключа не надо.
    fx_ecb_url: str = Field(default="https://api.frankfurter.dev/v1")
    # Нацбанк Грузии — универсальный запасной источник для всего, чего нет
    # в наборе ECB. Публикует курсы В ЛАРИ примерно для сорока валют, то есть
    # "лари за 1 единицу валюты" — направление ПРОТИВОПОЛОЖНОЕ ECB.
    # Проверено 28.09.2026: GEL и AZN в ECB отсутствуют оба, в НБГ есть.
    fx_nbg_url: str = Field(
        default="https://nbg.gov.ge/gw/api/ct/monetarypolicy/currencies/en/json/"
    )
    fx_timeout_s: int = Field(default=20)

    # ── Привязка расхода к проекту ────────────────────────────────────────
    # Порог нечёткого совпадения названия проекта. Ниже порога совпадение не
    # засчитывается вовсе: «orhot tota» против «Orhot Tora» даёт 0.9, а
    # случайное созвучие двух разных объектов — заметно меньше.
    project_match_min_ratio: float = Field(default=0.85)
    # Категории, которые никогда не относятся к проекту: по ним вопрос
    # человеку не задаётся вовсе, иначе фин директор получит три десятка
    # бессмысленных вопросов в месяц и перестанет читать дайджест.
    overhead_categories: List[str] = Field(
        default_factory=lambda: [
            "Software & subscriptions", "Telecom", "Accounting", "Legal", "Insurance",
            "Education", "Payroll & social",
        ]
    )

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
