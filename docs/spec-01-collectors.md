# Спек №1: Коллекторы (Telegram + IMAP → Dropbox `_inbox`)

Источник: design doc `~/.gstack/projects/alex1/alex1-nogit-design-20260908-expense-collector.md` (APPROVED)
Репозиторий: `Desktop/Orlanda/expense-collector`, база `ce5287e`
Спек 1 из 3. Дальше: №2 разборщик, №3 реестр и реконсилер.

## Context

Документы по расходам приходят из трёх мест: фото чеков в телеграм-чате, инвойсы на ящик `invoices@orlanda.info`, и личные кабинеты вендоров, откуда их достают руками. В Dropbox всё это складывает фин директор вручную, 100+ операций в месяц, закрытие месяца стоит несколько дней.

Этот спек делает половину работы: всё, что дошло до чата или ящика, автоматически оказывается в Dropbox с записью в БД. **Коллекторы не понимают содержимого.** Они не определяют компанию, сумму и дату, не дедуплицируют, не раскладывают по месяцам. Это спеки №2 и №3.

Разрез именно здесь потому, что признак компании находится внутри документа, разбор будет ошибаться регулярно, и цена его ошибки должна быть перемещением файла, а не потерянным документом. Сбор не имеет права зависеть от понимания.

## Current State

Проверено 09-10.09.2026.

| Что | Состояние |
|---|---|
| IMAP `invoices@orlanda.info` | Работает. Пароль приложения, логин 0.6с с ноутбука и с Hetzner. Context-Aware Access не режет |
| Содержимое ящика | 29 писем за 30 дней, 15 с вложениями, 18 PDF |
| Папка «Все письма» | Есть, флаг `\All`, имя в modified UTF-7 |
| `X-GM-MSGID` | Доступен, годится как ключ дедупа |
| Dropbox | Командный аккаунт Orlanda, `root_namespace_id=2550080707`, подключённый участник `karashn@orlanda.info` |
| Соглашение папок месяца | `August_2026` (`Месяц_Год` по-английски) |
| Телеграм-бот | Не создан. Чат существует |
| Репозиторий | `ce5287e`: `app/core/config.py`, `app/db/models.py`, пустые пакеты `collectors/`, `services/`, `tasks/` |

### Что уже есть в стеке и переиспользуется

| Файл | Что берём |
|---|---|
| `orlandabot/app/services/dropbox_client.py:44-53` | Официальный SDK + автоопределение namespace через `root_info.root_namespace_id`, `PathRoot.root()` |
| `orlandabot/app/tasks/scheduler.py:65-77` | Паттерн бесконечного цикла на `asyncio.sleep` |
| `expense-collector/app/core/config.py` | Все настройки уже заданы |
| `expense-collector/app/db/models.py` | Схема уже задана, требуется два дополнения (ниже) |

### Наблюдения на реальных данных, которые формируют требования

1. Письмо от `elenav@orlanda.info` содержит `Invoice-MJG2ZWNE-0003.pdf` **и** `Receipt-2503-9324-8402.pdf` — счёт и квитанция на один расход. Модель «письмо → N документов».
2. То же письмо несёт `~WRD0000.jpg` (0 КБ) и `image001.png` (1 КБ) — артефакты Outlook. Без фильтра уедут в папку.
3. Tonystam шлёт «Customer Invoice» и «Credit Card Payment Confirmation» с байт-в-байт одинаковым `Invoice-2962.pdf`.
4. «Fwd: Your Zak Exhibitions receipt» пришло **без вложений** — документ в HTML-теле.
5. Пересылки от сотрудников (`elenav@`, `janam@`, Wizz Air) — живой канал.
6. Шум: Google security alerts, Gmail Team, рассылка Tele2, маркетинг Bolt Food.

## Proposed Change

Один контейнер, один процесс, три `asyncio`-задачи в `asyncio.gather`. Redis и RQ не используются.

```
main.py
 ├── collector_tg_loop()      непрерывно, aiogram long polling
 ├── collector_mail_loop()    каждые IMAP_POLL_INTERVAL_S
 └── uploader_loop()          каждые 15с, разгребает буфер в Dropbox
```

**Периодичность источников не симметрична.** `collector-mail` можно ставить хоть на раз в месяц: IMAP хранит всё. `collector-tg` обязан работать непрерывно: Telegram Bot API отдаёт непрочитанные апдейты 24 часа, историю чата не отдаёт вообще. Простой телеграм-коллектора дольше суток означает безвозвратно потерянные фото.

### Порядок операций (инвариант «ноль потерь»)

```
скачали байты → записали в локальный буфер → строка в БД (status=new)
              → подтвердили источник (offset / метка Gmail)
              → [позже] uploader забирает из буфера в Dropbox _inbox
```

Падение на любом шаге даёт повторную обработку, а не потерю. Источник подтверждается только после того, как байты легли на диск и строка появилась в БД.

Для почты работает вторая линия обороны: метка Gmail не ставится до успеха, так что даже потеря диска не теряет письма. Для телеграма второй линии нет — отсюда алерт на простой.

## Implementation Details

### Дополнения к `app/db/models.py`

```python
class Kind(str, enum.Enum):
    file = "file"            # сообщение или письмо с документом
    text_only = "text_only"  # сообщение без файла: расход без документа ЛИБО обсуждение


# в Document добавить:
kind: Mapped[Kind] = mapped_column(Enum(Kind, name="kind_enum"), default=Kind.file)
local_path: Mapped[str | None] = mapped_column(Text)   # путь в буфере, NULL после выгрузки
uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Новая таблица:

```python
class PendingUpload(Base):
    __tablename__ = "pending_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), unique=True, index=True)
    local_path: Mapped[str] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text)      # /_inbox/YYYY-MM/<имя>
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`Document.status` для `text_only` — тот же `new`: решение «расход или обсуждение» принимает разборщик из спека №2.

Таблицы создаются через `Base.metadata.create_all()` при старте. Alembic не заводится. Цена: первое изменение схемы в проде делается руками через `ALTER`.

### `app/services/dropbox_client.py`

Официальный SDK `dropbox`, синхронный, вызовы через `loop.run_in_executor`. SDK сам экранирует `Dropbox-API-Arg` (иначе кириллица в путях ломает ASCII-only заголовок) и сам ставит `PathRoot`.

```python
class DropboxClient:
    def __init__(self) -> None:
        self._dbx = dropbox.Dropbox(
            oauth2_refresh_token=settings.dropbox_refresh_token,
            app_key=settings.dropbox_app_key,
            app_secret=settings.dropbox_app_secret,
        )
        ns = settings.dropbox_namespace_id or self._dbx.users_get_current_account().root_info.root_namespace_id
        self._dbx = self._dbx.with_path_root(dropbox.common.PathRoot.root(ns))

    async def upload(self, local_path: str, target_path: str) -> str:
        """Возвращает content_hash. Идемпотентно: mode=overwrite."""
```

- `mode=WriteMode.overwrite` — повторная выгрузка того же файла не плодит `имя (1).pdf`.
- Файлы больше 150 МБ идут через `upload_session` (в нашем потоке не встречаются, но обработать).
- Возвращаем `content_hash` из ответа: он понадобится спеку №2 для точного дедупа, и его считает сам Dropbox.
- Ошибки `AuthError` (протухший refresh token) отличаем от сетевых: первая не лечится ретраем и должна поднимать алерт немедленно.

### `app/services/buffer.py`

- Корень буфера — именованный docker volume, смонтированный в `/data/buffer`.
- Раскладка: `/data/buffer/<source>/<YYYY-MM-DD>/<uuid4>_<safe_name>`.
- `safe_name`: схлопывание пробелов в `-`, удаление `/ \ : * ? " < > |`, обрезка до 100 байт UTF-8.
- Файл удаляется только после успешного `upload` и проставления `Document.uploaded_at`.

### `app/tasks/uploader.py`

Цикл каждые 15 секунд: берёт из `pending_uploads` строки с `next_attempt_at <= now()`, выгружает, при успехе удаляет строку, файл и проставляет `dropbox_path` + `uploaded_at`.

Бэкофф: `min(2 ** attempts, 300)` секунд, попытки не ограничены. Dropbox лежит редко, но долго, и сдаваться тут нечего.

**Алерт по возрасту, а не по объёму.** Если самый старый элемент `pending_uploads` старше `HEARTBEAT_ALERT_AFTER_S` (по умолчанию час) — шлём алерт в телеграм. Час непрерывных ретраев означает протухший токен или смену прав, это чинится руками. Объём не ограничиваем: на сервере 1.5 ТБ свободно.

### `app/collectors/telegram.py`

aiogram 3.x, long polling.

**Операционное предусловие:** privacy mode у бота должен быть **отключён** (`@BotFather → /setprivacy → Disable`), иначе бот в группе не видит обычные сообщения. После смены настройки бота надо переподключить к чату. Альтернатива — сделать администратором группы.

Обработчики:

| Событие | Поведение |
|---|---|
| `message` с `photo` | Берём **максимальный** `PhotoSize`. `source_key = file_unique_id` |
| `message` с `document` | Любой тип. `source_key = file_unique_id` |
| `message` только текст | `kind=text_only`, файла нет, в буфер ничего не кладём |
| `edited_message` | Обновляем `raw_caption` у существующей записи по `file_unique_id` или `message_id` |
| Альбом (`media_group_id`) | Буферизуем в памяти, ждём `TELEGRAM_MEDIA_GROUP_WINDOW_S` (2с), затем применяем подпись первого сообщения ко всем элементам группы |

**Ключ источника — `file_unique_id`, не `message_id`.** Повторно отправленное то же фото получает новый `message_id`, но тот же `file_unique_id`.

**Лимит `getFile` 20 МБ.** aiogram бросает `TelegramBadRequest`. Ловим, пишем `Document` со `status=needs_review`, `status_reason="файл больше 20 МБ, Bot API не отдаёт"`, файла нет. Документ не теряется, человек достаёт его руками.

**Обработчик не имеет права бросать исключение.** aiogram двигает offset после обработки батча; исключение означает потерянный апдейт. Каждый обработчик обёрнут в `try/except`, при любом сбое пишется строка со `status=failed` и текстом ошибки.

Фильтр по чату: обрабатываем только `TELEGRAM_CHAT_ID`.

### `app/collectors/mail.py`

`imaplib` (стандартная библиотека) в executor. Асинхронный клиент не берём: `imaplib` уже проверен на этом ящике, и лишняя зависимость не окупается при опросе раз в 5 минут.

**Выбор папки.** Из ответа `LIST` берём строку с флагом `\All` и передаём её имя в `SELECT` **сырым**. Имя приходит в modified UTF-7 (`[Gmail]/&BBIEQQRP- &BD8EPgRHBEIEMA-`), декодировать нужно только для логов. Читаем «Все письма», а не INBOX: инвойсы уходят фильтрами в Промоакции и Спам.

**Что уже обработано.** Метка Gmail `expense-collector/done` через `STORE ... +X-GM-LABELS`. Плюс последний `X-GM-MSGID` в `sync_cursors`. IMAP UID не используется: он переезжает при смене `UIDVALIDITY`.

**Backfill.** `IMAP_BACKFILL_DAYS=0` означает «взять всё, что есть в ящике». Это режим первого запуска: нужен тестовый набор реальных документов для спека №2. После первого прогона выставляется в 30.

**Разбор MIME, по порядку:**

1. Если письмо от отправителя из `SENDER_IGNORE_LIST` — ставим метку, `Document` не создаём. Список по **точному адресу**, не по домену: Bolt шлёт с `receipts-cyprus@bolt.eu` (чеки), `cyprus@delivery-marketing.bolt.eu` (маркетинг) и `verify@bolt.eu` (верификация).
2. `msg.walk()`, для каждой части:
   - `message/rfc822` → рекурсивно разбираем вложенный `.eml` (пересылки от сотрудников).
   - Есть `filename` или `Content-Disposition: attachment` → кандидат во вложения.
3. Отбраковка кандидата, если выполняется любое:
   - размер меньше `ATTACHMENT_MIN_BYTES` (5 КБ);
   - имя матчится на `ATTACHMENT_SKIP_PATTERNS` (`image00N.png`, `~WRD*.jpg`, `oledata.mso`, `ATT*.txt`);
   - часть inline (есть `Content-ID` и `Content-Disposition: inline`).
4. Если после отбраковки вложений **ноль**, а тело письма содержит HTML — рендерим HTML-тело в PDF через WeasyPrint и заводим его как документ с именем `<тема>_body.pdf`. Это не запасной путь, а обязательный: квитанция Zak Exhibitions пришла именно так.
5. Каждое уцелевшее вложение → отдельный `Document`, все с общим `email_id = X-GM-MSGID`.

**Имена вложений** декодируем через `email.header.decode_header` (RFC 2047) и `email.utils.collapse_rfc2231_value` (RFC 2231). Коллизии имён в разных письмах не проблема: в буфере имя префиксуется `uuid4`.

**PDF под паролем** в текущем ящике не встречался, но обрабатываем: коллектор его не открывает, просто кладёт как есть. Проблема всплывёт в спеке №2.

### `app/core/health.py`

- Каждая задача обновляет свою строку в `heartbeats` раз в 60с.
- Отдельная проверка раз в 5 минут: если `beat_at` любого компонента старше `HEARTBEAT_ALERT_AFTER_S` — алерт в телеграм.
- Dead-man-switch для телеграма: тот же механизм, но это единственный компонент, чей простой означает **безвозвратную** потерю данных.
- Алерт «ноль писем за N часов» в спек №1 не входит: пока не набрана статистика нормального потока, порог не из чего вывести.

### Docker

- Базовый образ `python:3.12-slim`.
- WeasyPrint требует системных библиотек: `libpango-1.0-0`, `libpangoft2-1.0-0`, `libharfbuzz0b`, `libffi-dev`. Ставятся в `apt-get install`.
- Один процесс: `CMD ["python", "-m", "app.main"]`. Supervisord не нужен.
- Volume `expense_buffer:/data/buffer`.
- Подключение к сети db-router, база `db_expenses` на `:6432`.

## Acceptance Criteria

1. Фото, отправленное в чат, за 60 секунд появляется в Dropbox `_inbox/<текущий месяц>/` и имеет ровно одну строку в `documents` со `status=new`, `kind=file`, `source=telegram`.
2. Альбом из 3 фото с подписью только у первого даёт **3** строки в `documents`, у всех трёх заполнен `raw_caption` подписью первого.
3. Повторная отправка того же файла в чат не создаёт вторую строку в `documents` (совпадение по `file_unique_id`).
4. Текстовое сообщение без файла создаёт строку с `kind=text_only`, `local_path IS NULL`, и **не** создаёт запись в `pending_uploads`.
5. Файл больше 20 МБ создаёт строку со `status=needs_review` и непустым `status_reason`; коллектор продолжает работать.
6. Письмо от `elenav@orlanda.info` с четырьмя вложениями даёт **2** строки в `documents` (два PDF), а `~WRD0000.jpg` и `image001.png` отбракованы.
7. Письмо «Fwd: Your Zak Exhibitions receipt» без вложений даёт **1** строку с отрендеренным из HTML-тела PDF.
8. Пересланное письмо с вложенным `.eml` даёт документы из вложенного письма, а не сам `.eml` файлом.
9. Письмо от `no-reply@accounts.google.com` не создаёт строк в `documents` и получает метку `expense-collector/done`.
10. Повторный запуск `collector-mail` после полного прохода не создаёт дублей: письма с меткой пропускаются.
11. При недоступном Dropbox (подставить неверный `refresh_token`) файлы копятся в `/data/buffer`, строки висят в `pending_uploads` с растущим `attempts`, ни один документ не потерян. После починки токена всё выгружается без ручного вмешательства.
12. Остановка контейнера на 5 минут и запуск: все сообщения и письма, пришедшие за это время, обработаны (telegram — за счёт 24-часового окна апдейтов, mail — за счёт метки).
13. Самый старый элемент `pending_uploads` старше часа порождает алерт в телеграм ровно один раз, а не каждый цикл.
14. Тесты написаны и проходят.
15. Существующая функциональность не деградирует (в спеке №1 её нет — критерий вступает в силу со спека №2).

## Testing Plan

| Слой | Что | Кол-во |
|---|---|---|
| Unit | Разбор MIME на `.eml`-фикстурах: отбраковка мусора, рекурсия во вложенный `.eml`, HTML-тело без вложений, RFC 2047/2231 имена | +8 |
| Unit | `safe_name`: кириллица, слэши, длина больше 100 байт, пустое имя | +4 |
| Unit | Бэкофф `pending_uploads`: рост интервала, потолок 300с | +2 |
| Unit | Буферизация media_group: подпись первого применяется ко всем, таймаут срабатывает | +3 |
| Integration | Живой ящик `invoices@orlanda.info`: полный проход, повторный проход не создаёт дублей | +2 |
| Integration | Живой бот в тестовом чате: фото, документ, альбом, текст, файл больше 20 МБ | +5 |
| Integration | Dropbox: выгрузка в тестовую папку, идемпотентность `overwrite`, кириллица в пути | +3 |
| E2E | Фото в чат → файл в Dropbox `_inbox` + строка в БД, за один прогон | +1 |
| E2E | Отключённый Dropbox → буфер растёт → включённый Dropbox → буфер пуст, все файлы на месте | +1 |

Тесты идут на живых ящике и боте, как решено. Сверх этого сохраняем 6 реальных писем как `.eml`-фикстуры в `tests/fixtures/`: письмо Елены с четырьмя вложениями, Zak Exhibitions без вложений, два Tonystam с одинаковым PDF, пересылка Wizz Air, письмо Google alerts. Фикстуры не требуют кредов и ловят регрессии разбора, когда живой ящик недоступен.

## Rollback Plan

Коллекторы только читают источники и **добавляют** файлы в `_inbox`. Ничего не удаляют и не перемещают — перемещение появится в спеке №2.

- Откат кода: `git revert`, пересборка контейнера.
- Откат данных: удалить папку `_inbox` в Dropbox и сделать `TRUNCATE documents, pending_uploads, sync_cursors, heartbeats`.
- Единственное необратимое действие — метки Gmail `expense-collector/done`. Снимаются массово из веб-интерфейса Gmail, после чего повторный проход соберёт всё заново.

## Effort Estimate

| Компонент | Оценка |
|---|---|
| `dropbox_client.py` (SDK + executor + upload) | 3ч |
| `buffer.py` + `uploader.py` + бэкофф | 4ч |
| `collectors/telegram.py` (включая media_group и edited) | 6ч |
| `collectors/mail.py` (MIME, `.eml`, HTML→PDF, метки) | 10ч |
| `health.py` + алерты | 2ч |
| `main.py`, `db/session.py`, Docker, compose | 3ч |
| Тесты (фикстуры + живые прогоны) | 8ч |
| Отладка на реальных данных | 6ч |
| **Итого** | **~42ч** |

## Files Reference

| Файл | Изменение |
|---|---|
| `app/db/models.py` | Добавить `Kind`, поля `kind`/`local_path`/`uploaded_at`, таблицу `PendingUpload` |
| `app/db/session.py` | Новый: engine, sessionmaker, `create_all()` |
| `app/services/dropbox_client.py` | Новый: SDK-обёртка, `upload`, `PathRoot`, executor |
| `app/services/buffer.py` | Новый: запись и удаление файлов буфера, `safe_name` |
| `app/services/mime.py` | Новый: разбор MIME, отбраковка вложений, HTML→PDF |
| `app/services/alerts.py` | Новый: отправка алертов в телеграм |
| `app/collectors/telegram.py` | Новый: aiogram, обработчики, media_group |
| `app/collectors/mail.py` | Новый: IMAP, выбор папки по `\All`, метки, backfill |
| `app/tasks/uploader.py` | Новый: разгребание `pending_uploads` |
| `app/core/health.py` | Новый: heartbeat и проверка простоя |
| `app/main.py` | Новый: `asyncio.gather` трёх задач |
| `Dockerfile` | Новый: python:3.12-slim + системные зависимости WeasyPrint |
| `docker-compose.yml` | Новый: сервис, volume `expense_buffer`, сеть db-router |
| `tests/fixtures/*.eml` | 6 реальных писем |

## Out of Scope

- Извлечение текста, определение компании, суммы, даты, назначения — спек №2.
- Дедупликация любого уровня — спек №2. Коллектор проверяет только `source_key` от повторной доставки.
- Перемещение файлов из `_inbox` в папку компании — спек №2.
- Реестр `_реестр.csv`, реконсилер Dropbox, дайджест `needs_review` — спек №3.
- Классификация «расход или обсуждение» для `text_only` — спек №2.
- Скачивание инвойсов по ссылкам из писем — открытый вопрос, требует авторизации в кабинетах вендоров.
- Alembic-миграции.
- Определение личности Dropbox с доступом к целевым папкам — не блокирует спек №1, потому что `_inbox` можно завести в пространстве `karashn@orlanda.info`.

## Related

- Design doc: `~/.gstack/projects/alex1/alex1-nogit-design-20260908-expense-collector.md`
- Референс Dropbox SDK: `orlandabot/app/services/dropbox_client.py:44-53`
- Референс httpx-клиента с team-заголовками: `html-to-mailing/app/services/dropbox_service.py`
- Референс цикла на `asyncio.sleep`: `orlandabot/app/tasks/scheduler.py:65-77`

## Открытые вопросы, не блокирующие реализацию

1. Где физически лежит `_inbox`. На время разработки — в пространстве `karashn@orlanda.info`. Целевое расположение определится, когда фин директор расшарит корневую папку.
2. Периодичность `collector-mail` в проде. Решение за директором. Код параметризован, менять — одна переменная окружения. **Телеграма это решение не касается: он работает непрерывно всегда.**
3. Порог алерта «ноль писем за N часов». Выводится после месяца статистики.
