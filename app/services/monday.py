"""Клиент Monday GraphQL: чтение справочников и запись расходов на доску.

Главная опасность этого файла — тихая потеря строки. **Monday отвечает
200 OK с массивом `errors`, а не 429**, когда бюджет сложности исчерпан:

    {"errors": [{"message": "Complexity budget exhausted",
                 "extensions": {"code": "COMPLEXITY_BUDGET_EXHAUSTED",
                                "retry_in_seconds": 60}}]}

Клиент, который смотрит только на `resp.status_code`, пометит запись
успешной. Расход не появится на доске НИКОГДА, и алерт по возрасту очереди
этого не увидит, потому что очередь пуста. Поэтому разбор ответа вынесен в
чистую функцию `analyze` и закреплён тестами.

То же наблюдение уже записано в стеке: `/opt/html-to-mailing/app/services/
monday.py:199-206`. Форма `_gql` взята оттуда же.

Версия API пинуется заголовком: без него Monday применяет версию аккаунта
по умолчанию и меняет её по своему расписанию, а формат этой самой ошибки
уже менялся на версии 2025-07.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

API_URL = "https://api.monday.com/v2"

# Предел на один запрос items_page. Больше — дороже по сложности и ближе
# к потолку в 5M единиц на один запрос.
PAGE_LIMIT = 100


class MondayError(RuntimeError):
    """Monday отказал. Ретраить можно, но не факт, что поможет."""


class MondayRateLimited(MondayError):
    """Бюджет сложности исчерпан либо сработал лимит запросов.

    retry_after приходит от Monday (`retry_in_seconds` в GraphQL-ошибке или
    заголовок Retry-After при 429) — ждать надо ровно столько, а не гадать.
    """

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MondayAuthBroken(MondayError):
    """Токен отозван или потерял права. Ретраить бессмысленно, нужен человек.

    Токен общий с шестью приложениями стека, поэтому самый вероятный сценарий
    здесь — его перевыпустили в другом проекте.
    """


# ── разбор ответа: чистая функция, вся защита от тихой потери здесь ───────

_COMPLEXITY_CODES = {"COMPLEXITY_BUDGET_EXHAUSTED", "ComplexityException"}
_AUTH_CODES = {"UNAUTHENTICATED", "USER_UNAUTHORIZED", "InvalidUserIdException"}
_DEFAULT_RETRY_AFTER = 60


def _int_or(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def analyze(
    status_code: int, headers: Mapping[str, str] | None, payload: Any
) -> MondayError | None:
    """Что не так с ответом Monday. None означает «всё в порядке».

    Возвращает исключение, а не бросает его, чтобы поведение можно было
    проверить тестом без сети и без мока клиента.
    """
    headers = headers or {}

    if status_code == 429:
        return MondayRateLimited(
            "Monday: HTTP 429",
            _int_or(headers.get("Retry-After") or headers.get("retry-after"), _DEFAULT_RETRY_AFTER),
        )
    if status_code in (401, 403):
        return MondayAuthBroken(f"Monday: HTTP {status_code}, токен отклонён")
    if status_code >= 500:
        return MondayError(f"Monday: HTTP {status_code}")

    if not isinstance(payload, dict):
        return MondayError(f"Monday вернул не JSON-объект: {type(payload).__name__}")

    # Ответ вне GraphQL-конверта: Monday так отдаёт часть лимитов и отказов.
    if "error_message" in payload and "data" not in payload:
        code = str(payload.get("error_code") or "")
        message = str(payload.get("error_message"))
        if code in _COMPLEXITY_CODES or "omplexity" in message:
            return MondayRateLimited(
                f"Monday: {message}",
                _int_or(payload.get("retry_in_seconds"), _DEFAULT_RETRY_AFTER),
            )
        if code in _AUTH_CODES:
            return MondayAuthBroken(f"Monday: {message}")
        return MondayError(f"Monday: {message}")

    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        extensions = first.get("extensions") if isinstance(first, dict) else {}
        extensions = extensions if isinstance(extensions, dict) else {}
        code = str(extensions.get("code") or "")
        message = str(first.get("message") if isinstance(first, dict) else first)

        if code in _COMPLEXITY_CODES or "omplexity budget" in message:
            return MondayRateLimited(
                f"Monday: {message}",
                _int_or(extensions.get("retry_in_seconds"), _DEFAULT_RETRY_AFTER),
            )
        if code in _AUTH_CODES:
            return MondayAuthBroken(f"Monday: {message}")
        return MondayError(f"Monday GraphQL: {errors}")

    if payload.get("data") is None:
        return MondayError("Monday вернул пустой data без ошибок")

    return None


# ── транспорт ─────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Один пулленный клиент на процесс. retries=3 переподключается на
    сбоях уровня соединения; всё, что дальше рукопожатия, разгребает очередь
    monday_pushes, а не транспорт."""
    global _client
    if _client is None or _client.is_closed:
        if not settings.monday_api_token:
            raise MondayAuthBroken("MONDAY_API_TOKEN не задан")
        _client = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": settings.monday_api_token.strip(),
                "Content-Type": "application/json",
                "API-Version": settings.monday_api_version,
            },
            timeout=60,
            transport=httpx.AsyncHTTPTransport(retries=3),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def gql(query: str, variables: dict | None = None) -> dict:
    resp = await _get_client().post("", json={"query": query, "variables": variables or {}})
    try:
        payload = resp.json()
    except ValueError:
        payload = None

    problem = analyze(resp.status_code, resp.headers, payload)
    if problem is not None:
        raise problem
    return payload["data"]


# ── чтение ────────────────────────────────────────────────────────────────

_BOARD_COLUMNS = """
query ($board: [ID!]) {
  boards(ids: $board) { id name columns { id title type } }
}
"""


async def board_columns(board_id: int) -> dict[str, str]:
    """id колонки -> её тип. Нужен на старте синка: опечатка в MONDAY_COLUMNS
    иначе вылезет только на доске, пустой ячейкой без объяснений."""
    data = await gql(_BOARD_COLUMNS, {"board": [str(board_id)]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayError(f"Доска {board_id} не найдена или недоступна токену")
    return {c["id"]: c["type"] for c in boards[0].get("columns") or []}


_BOARD_GROUPS = """
query ($board: [ID!]) {
  boards(ids: $board) { groups { id title } }
}
"""


async def board_groups(board_id: int) -> dict[str, str]:
    """title -> id. Группа адресуется по id, а заводится по названию."""
    data = await gql(_BOARD_GROUPS, {"board": [str(board_id)]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayError(f"Доска {board_id} не найдена или недоступна токену")
    return {g["title"]: g["id"] for g in boards[0].get("groups") or []}


_TASK_BY_CODE = """
query ($board: ID!, $column: String!, $value: String!) {
  items_page_by_column_values(
    board_id: $board, limit: 5,
    columns: [{column_id: $column, column_values: [$value]}]
  ) {
    items {
      id
      name
      column_values(ids: ["board_relation2__1", "board_relation3__1"]) {
        id
        ... on BoardRelationValue { linked_item_ids linked_items { id name } }
      }
    }
  }
}
"""


async def task_by_code(code: str) -> dict | None:
    """Задача по коду Drawings Num. Один запрос, никакого угадывания.

    Возвращает {task_id, task_name, project_id, project_name, service_id}
    либо None, если задачи с таким кодом на доске нет.
    """
    data = await gql(
        _TASK_BY_CODE,
        {
            "board": str(settings.monday_todo_board_id),
            "column": settings.monday_task_code_column,
            "value": code,
        },
    )
    items = ((data.get("items_page_by_column_values") or {}).get("items")) or []
    if not items:
        return None

    item = items[0]
    linked: dict[str, list[dict]] = {}
    for cv in item.get("column_values") or []:
        linked[cv.get("id")] = cv.get("linked_items") or []

    projects = linked.get("board_relation2__1") or []
    services = linked.get("board_relation3__1") or []
    return {
        "task_id": int(item["id"]),
        "task_name": item.get("name"),
        "project_id": int(projects[0]["id"]) if projects else None,
        "project_name": projects[0]["name"] if projects else None,
        "service_id": int(services[0]["id"]) if services else None,
    }


_PROJECTS_PAGE = """
query ($board: [ID!], $limit: Int!, $cursor: String) {
  boards(ids: $board) {
    items_page(limit: $limit, cursor: $cursor) {
      cursor
      items {
        id
        name
        group { title }
        column_values(ids: ["text_mkvczb03"]) { id text }
      }
    }
  }
}
"""


async def all_projects() -> list[dict]:
    """Весь справочник проектов, постранично. Вызывается раз в сутки на
    прогреве, а не внутри разбора документа."""
    out: list[dict] = []
    cursor: str | None = None
    while True:
        data = await gql(
            _PROJECTS_PAGE,
            {"board": [str(settings.monday_projects_board_id)], "limit": PAGE_LIMIT,
             "cursor": cursor},
        )
        boards = data.get("boards") or []
        if not boards:
            break
        page = boards[0].get("items_page") or {}
        for item in page.get("items") or []:
            shortcut = ""
            for cv in item.get("column_values") or []:
                if cv.get("id") == "text_mkvczb03":
                    shortcut = (cv.get("text") or "").strip()
            out.append({
                "monday_id": int(item["id"]),
                "name": item.get("name") or "",
                "group_title": (item.get("group") or {}).get("title"),
                "shortcut": shortcut or None,
            })
        cursor = page.get("cursor")
        if not cursor:
            break
    return out


_ITEM_VALUES = """
query ($items: [ID!]) {
  items(ids: $items) {
    id
    column_values { id text }
  }
}
"""


async def item_values(item_id: int) -> dict[str, str]:
    """Текущее содержимое айтема. Нужно, чтобы не затереть правку человека."""
    data = await gql(_ITEM_VALUES, {"items": [str(item_id)]})
    items = data.get("items") or []
    if not items:
        return {}
    return {cv["id"]: (cv.get("text") or "") for cv in items[0].get("column_values") or []}


# ── запись ────────────────────────────────────────────────────────────────

_CREATE_GROUP = """
mutation ($board: ID!, $name: String!) {
  create_group(board_id: $board, group_name: $name) { id }
}
"""


async def create_group(board_id: int, title: str) -> str:
    data = await gql(_CREATE_GROUP, {"board": str(board_id), "name": title})
    return (data.get("create_group") or {})["id"]


_CREATE_ITEM = """
mutation ($board: ID!, $group: String!, $name: String!, $values: JSON!) {
  create_item(
    board_id: $board, group_id: $group, item_name: $name,
    column_values: $values, create_labels_if_missing: false
  ) { id }
}
"""


async def create_item(board_id: int, group_id: str, name: str, column_values: str) -> int:
    """create_labels_if_missing намеренно false: опечатка в разборе не должна
    плодить мусорные лейблы на доске, которой пользуется вся команда."""
    data = await gql(
        _CREATE_ITEM,
        {"board": str(board_id), "group": group_id, "name": name[:255], "values": column_values},
    )
    return int((data.get("create_item") or {})["id"])


_UPDATE_ITEM = """
mutation ($board: ID!, $item: ID!, $values: JSON!) {
  change_multiple_column_values(
    board_id: $board, item_id: $item, column_values: $values,
    create_labels_if_missing: false
  ) { id }
}
"""


async def update_item(board_id: int, item_id: int, column_values: str) -> None:
    await gql(
        _UPDATE_ITEM,
        {"board": str(board_id), "item": str(item_id), "values": column_values},
    )


_POST_UPDATE = """
mutation ($item: ID!, $body: String!) {
  create_update(item_id: $item, body: $body) { id }
}
"""


async def post_update(item_id: int, body: str) -> None:
    """Текстовая запись в ленту айтема. Туда уходит то, чему не нашлось
    колонки: остальные проекты многопроектного счёта и список дублей."""
    await gql(_POST_UPDATE, {"item": str(item_id), "body": body})


_FIND_BY_DOC_ID = """
query ($board: ID!, $column: String!, $value: String!) {
  items_page_by_column_values(
    board_id: $board, limit: 5,
    columns: [{column_id: $column, column_values: [$value]}]
  ) { items { id } }
}
"""


async def find_by_doc_id(board_id: int, column_id: str, doc_id: int) -> int | None:
    """Проба восстановления после падения между create_item и записью id.

    Точечный лукап по колонке Doc ID, а не скан доски: айтемов на ней уже
    368 и будет больше.
    """
    data = await gql(
        _FIND_BY_DOC_ID, {"board": str(board_id), "column": column_id, "value": str(doc_id)}
    )
    items = ((data.get("items_page_by_column_values") or {}).get("items")) or []
    return int(items[0]["id"]) if items else None
