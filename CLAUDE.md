# expense-collector

Автоматический сбор документов по расходам из телеграм-чата и почтового ящика в Dropbox.
Дизайн: `~/.gstack/projects/alex1/alex1-nogit-design-20260908-expense-collector.md`
Спеки: `docs/spec-01-collectors.md`, `docs/spec-02-parser.md`

## Архитектура в одну строку

Коллекторы кладут документ в `_inbox` и пишут строку в БД, **не понимая содержимого**. Разборщик отдельным процессом читает, определяет компанию и перекладывает. Цена ошибки разбора — перемещение файла, а не потерянный документ.

## Грабли, на которые уже наступили

**`docker compose restart` не перечитывает `.env`.** Переменные фиксируются при создании контейнера. После правки `.env` всегда:
```bash
docker compose up -d --force-recreate
```

**Telegram хранит непрочитанные апдейты 24 часа** и историю чата не отдаёт вообще. `collector-tg` обязан работать непрерывно. Почтовый коллектор можно опрашивать хоть раз в месяц: IMAP хранит письма сам.

**Токен Dropbox командный (team-scoped).** Нужен `DropboxTeam.as_user(member_id)`, а не `Dropbox`. У обычного `Dropbox` метода `as_user` нет вовсе, а без выбора участника API отвечает `BadInputError: token is for an entire Dropbox Business team`.

**Писать в корень `/Finance` нельзя** (`no_write_permission`). Папки компаний расшарены отдельно и лежат в своих namespace. Перемещение между ними работает, проверено.

**Иврит разваливается при извлечении из PDF.** `בע״מ` уезжает в начало строки. Матчить компанию по полному юридическому названию нельзя — только по рег. номеру или короткому токену.

**`strftime("%B")` зависит от локали.** В контейнере с `ru_RU` вернёт «августа» вместо «August» и папки разъедутся с соглашением фин директора. В `app/core/naming.py` таблица месяцев явная.

**Git Bash на Windows корёжит UTF-8 в `curl -d`.** Для POST с кириллицей или ивритом использовать python с `json.dumps(...).encode("utf-8")`.

## Деплой

```bash
tar --exclude='.git' --exclude='__pycache__' --exclude='.env' -czf - app docs Dockerfile docker-compose.yml requirements.txt \
  | ssh hetzner-main 'tar -xzf - -C /opt/expense-collector'
ssh hetzner-main 'cd /opt/expense-collector && docker compose up -d --build --force-recreate'
```

База `db_expenses` на хостовом Postgres через haproxy `db-router` на `127.0.0.1:6432`. Контейнер в `network_mode: host`, как у `waorlanda` и `orlandabot`. Входящих портов нет, туннель и балансировщик не нужны.

## Проверка состояния

```bash
ssh hetzner-main 'docker logs expense-collector 2>&1 | tail -20'
ssh hetzner-main 'cd /opt/expense-collector && docker exec expense-collector python -c "
from app.core.config import get_settings; print(get_settings().model_dump())"'
```

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

- Product ideas/brainstorming → /office-hours
- Architecture → /plan-eng-review
- Bugs/errors → /investigate
- Code review/diff check → /review
- Ship/deploy/PR → /ship
- Author a backlog-ready spec/issue → /spec
