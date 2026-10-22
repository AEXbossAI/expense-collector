"""Привязка расхода к проекту.

Каскад, сверху вниз. Слабый уровень не имеет права переигрывать сильный:

1. **Код задачи в документе** — детерминированно, без модели. Инвойсы
   подрядчиков несут `Drawings Num` вида `AMR-SAN-AQU-SC-2026-07-1677`;
   это колонка `text9__1` на доске TO DO LIST, у задачи есть relation на
   проект. Проверено на четырёх боевых документах, 4 из 4.
2. **Название проекта в имени файла или тексте** — нечётко, и только при
   ЕДИНСТВЕННОМ кандидате. Имя файла человек пишет от руки: «Orhot Tota»
   вместо «Orhot Tora», «aqua_sence», «eilat».
3. Правило по вендору — заводится только человеком.
4. Вопрос в дайджест.
5. Пусто.

Слово «eilat» из `orlanda_40375_static eilat.pdf` совпадает и с
`Eilat project Plot 103-104-105-106`, и с `Hotel Eilat`. Это не дефект
матчера, а честный исход: два кандидата означают вопрос человеку, а не
выбор наугад. Одна ошибочная привязка в P&L проекта дороже ста пустых ячеек.

`project_source` хранится в базе, но на доску отдельной колонкой НЕ едет:
источник привязки нужен на отладке и в сухом прогоне, а фин директору на
доске важен сам проект.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import MondayProject, MondayTask, ProjectSource
from app.services import monday

logger = logging.getLogger(__name__)
settings = get_settings()

# Клиент-Исполнитель-Проект-Услуга-Год-Месяц-Номер.
# Токены по 2-4 буквы: встречались SC (static calculation), SD (shop
# drawings), HP. Номер до шести цифр: видели и 1673, и 6119.
TASK_CODE_RE = re.compile(
    r"\b[A-Z]{2,4}-[A-Z]{2,4}-[A-Z]{2,4}-[A-Z]{2,4}-\d{4}-\d{2}-\d{1,6}\b"
)

# Слова, которые есть у десятков проектов и потому ничего не различают.
# Без стоп-листа «Project» из «Amnir Project Hedera» дало бы полсотни
# кандидатов и матч не срабатывал бы никогда.
_GENERIC = {
    "project", "projects", "проект", "building", "buildings", "tower", "towers",
    "center", "centre", "mall", "hotel", "plot", "city", "street", "house",
    "residential", "commercial", "part", "test", "phase",
}

_MIN_CORE_LEN = 5


@dataclass(frozen=True)
class Match:
    """Результат матча. `ambiguous` важнее, чем отсутствие совпадения:
    в очередь человеку уходят оба случая, но причины разные."""

    project_monday_id: int | None = None
    project_name: str | None = None
    source: ProjectSource | None = None
    ambiguous: tuple[str, ...] = ()
    reason: str = ""


# ── чистые функции ────────────────────────────────────────────────────────


def find_task_codes(*texts: str | None) -> list[str]:
    """Все коды задач из любых источников, в порядке появления, без повторов."""
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for code in TASK_CODE_RE.findall(text.upper()):
            if code not in seen:
                seen.append(code)
    return seen


def normalize(text: str | None) -> str:
    """Подчёркивания и пунктуация в пробелы, регистр вниз, пробелы схлопнуть.

    Имя файла приходит как `aqua_sence_40378_static.pdf`, а проект на доске
    называется `Aqua sence Netanya`. Без нормализации не совпадёт ничто.
    """
    if not text:
        return ""
    # Класс [\W_], а не [^\w]: в Python \w включает подчёркивание, и без
    # явного _ имя `aqua_sence_40378_static.pdf` осталось бы одним словом.
    # Поймано тестом: матч по различающему слову переставал работать на
    # всех именах с подчёркиваниями, то есть почти на всех.
    lowered = re.sub(r"[\W_]+", " ", text.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def core_words(name: str) -> set[str]:
    """Различающие слова названия: длиной от пяти букв и не из стоп-листа."""
    return {
        w for w in normalize(name).split()
        if len(w) >= _MIN_CORE_LEN and w not in _GENERIC and not w.isdigit()
    }


def is_distinctive(name: str) -> bool:
    """Можно ли искать это название в тексте документа.

    Название из одного слова искать в тексте нельзя: проекты называются
    «Island», «Factory», «Shferber», и такое слово встречается в общих
    условиях любого инвойса. Двух значимых слов уже достаточно —
    «Aqua sence Netanya» случайно в тексте не появится.

    В имени файла ограничения нет: там название пишет человек и осознанно.
    """
    return len(core_words(name)) >= 2


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Вхождение по границам слов, а не подстрокой.

    Подстрочный поиск даёт «island» внутри «Highlands» и «reem» внутри
    «agreement». Обе строки уже нормализованы, поэтому достаточно
    сравнить последовательности токенов.
    """
    if not phrase or not haystack:
        return False
    needle = phrase.split()
    hay = haystack.split()
    return any(
        hay[i:i + len(needle)] == needle for i in range(len(hay) - len(needle) + 1)
    )


def _windows(tokens: list[str], size: int) -> list[str]:
    if size <= 0 or size > len(tokens):
        return []
    return [" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)]


def best_ratio(name_normalized: str, haystack_normalized: str) -> float:
    """Лучшее сходство названия с окном той же длины в тексте.

    Сравнивать название с текстом целиком бесполезно: длинный текст утопит
    короткое название. Окно той же словарной длины ловит опечатку:
    «orhot tora» против «orhot tota» даёт 0.9.
    """
    tokens = haystack_normalized.split()
    size = len(name_normalized.split())
    best = 0.0
    for window in _windows(tokens, size):
        ratio = SequenceMatcher(None, name_normalized, window).ratio()
        if ratio > best:
            best = ratio
    return best


def match_project(
    filename: str | None,
    text: str | None,
    projects: list[tuple[int, str]],
    min_ratio: float,
) -> Match:
    """Проект по названию. Два правила по убыванию надёжности, и каждое
    срабатывает ТОЛЬКО при единственном кандидате.

    `projects` — список (monday_id, name), приходит из кэша справочника.
    """
    if not projects:
        return Match(reason="справочник проектов пуст")

    file_norm = normalize(filename)
    text_norm = normalize(text)
    if not file_norm and not text_norm:
        return Match(reason="ни имени файла, ни текста")

    # 1. Полное вхождение названия.
    #
    #    По имени файла — любое название: имя человек пишет руками и
    #    осознанно. По тексту документа — только достаточно различимое,
    #    то есть минимум из двух значимых слов.
    #
    #    Иначе проект «Island» или «Factory» ловится в любом инвойсе,
    #    где это слово встретилось в общих условиях. Границы слов
    #    обязательны: подстрока «island» сидит внутри «Highlands».
    hits = [
        (pid, name, ProjectSource.filename)
        for pid, name in projects
        if _contains_phrase(file_norm, normalize(name))
    ]
    if not hits:
        hits = [
            (pid, name, ProjectSource.text)
            for pid, name in projects
            if is_distinctive(name) and _contains_phrase(text_norm, normalize(name))
        ]
    if len(hits) == 1:
        pid, name, where = hits[0]
        return Match(pid, name, where, reason=f"полное вхождение названия «{name}»")
    if len(hits) > 1:
        return Match(
            ambiguous=tuple(n for _, n, _ in hits),
            reason=f"полное вхождение сразу у {len(hits)} проектов",
        )

    # Дальше — ТОЛЬКО имя файла.
    #
    # Одного слова из названия проекта в тексте документа недостаточно:
    # в названиях сидят обычные английские слова (green, city, market,
    # begin, riding), и в многостраничном инвойсе такое слово найдётся
    # всегда. Прогон 28.09.2026 на боевых данных дал чужой проект почти
    # каждому накладному расходу: Anthropic -> «Port Market Tel Aviv»,
    # Tonystam -> «m.a. green city Ltd», такси Bolt -> «Riding Tel-Aviv».
    #
    # Имя файла человек пишет сам и коротко, поэтому совпадение в нём —
    # намерение, а не совпадение.
    if not file_norm:
        return Match(reason="в тексте полного названия нет, имени файла нет тоже")

    # Правила «одно различающее слово названия в имени файла» больше нет
    # (22.10.2026). За всё время оно сработало дважды, и оба раза мимо:
    # билеты `Flights - AZAL ... (Daniel Cheled).pdf` получили проект
    # «Daniel Tower» по имени пассажира. Фин директор пишет в имени файла
    # имена людей — «(Roman)», «Anthropic Anna», — и одно слово из них
    # совпадает с названием проекта случайно. Счета подрядчиков, ради
    # которых правило заводилось (`orlanda_40376_Amnir.pdf`), находят
    # проект раньше, по коду задачи в тексте.

    # 2. Опечатка в имени файла: «Orhot Tota» вместо «Orhot Tora».
    if file_norm:
        scored = [
            (pid, name, best_ratio(normalize(name), file_norm)) for pid, name in projects
        ]
        close = [(pid, name, r) for pid, name, r in scored if r >= min_ratio]
        if len(close) == 1:
            pid, name, ratio = close[0]
            return Match(
                pid, name, ProjectSource.filename,
                reason=f"имя файла похоже на «{name}» ({ratio:.2f})",
            )
        if len(close) > 1:
            return Match(
                ambiguous=tuple(n for _, n, _ in close),
                reason=f"имя файла похоже сразу на {len(close)} проектов",
            )

    return Match(reason="ни одно название проекта не совпало")


# ── справочник проектов и задач ───────────────────────────────────────────


async def cached_projects(session: AsyncSession) -> list[tuple[int, str]]:
    rows = (await session.execute(select(MondayProject))).scalars().all()
    return [(r.monday_id, r.name) for r in rows]


async def refresh_projects(session: AsyncSession, *, force: bool = False) -> int:
    """Перетягивает справочник с доски, если он протух.

    Вызывается на прогреве при старте задачи, а не лениво внутри разбора
    документа: 380 проектов приезжают четырьмя страницами, и делать это
    посреди обработки значит добавить задержку в цикл на ровном месте.
    """
    newest = (
        await session.execute(select(MondayProject).order_by(MondayProject.refreshed_at.desc()))
    ).scalars().first()

    fresh_until = timedelta(seconds=settings.monday_projects_cache_ttl_s)
    if not force and newest is not None:
        if datetime.now(timezone.utc) - newest.refreshed_at < fresh_until:
            return 0

    items = await monday.all_projects()
    if not items:
        logger.warning("Справочник проектов пуст, кэш не трогаю")
        return 0

    existing = {r.monday_id: r for r in (await session.execute(select(MondayProject))).scalars()}
    now = datetime.now(timezone.utc)
    for item in items:
        row = existing.get(item["monday_id"])
        if row is None:
            session.add(
                MondayProject(
                    monday_id=item["monday_id"],
                    name=item["name"],
                    normalized=normalize(item["name"])[:255],
                    group_title=item.get("group_title"),
                    shortcut=item.get("shortcut"),
                    refreshed_at=now,
                )
            )
        else:
            row.name = item["name"]
            row.normalized = normalize(item["name"])[:255]
            row.group_title = item.get("group_title")
            row.shortcut = item.get("shortcut")
            row.refreshed_at = now

    logger.info("Справочник проектов обновлён: %d", len(items))
    return len(items)


async def resolve_task(session: AsyncSession, code: str) -> MondayTask | None:
    """Задача по коду. Кэш бессрочный: код неизменяем, и задача за ним не
    переезжает на другой проект. Отрицательный результат тоже кэшируется —
    иначе каждый проход будет спрашивать Monday про заведомо отсутствующую
    задачу.
    """
    row = await session.get(MondayTask, code)
    if row is not None:
        return row if row.found else None

    found = await monday.task_by_code(code)
    if found is None:
        session.add(MondayTask(code=code, found=False))
        logger.info("Код задачи %s в Monday не найден", code)
        return None

    row = MondayTask(
        code=code,
        task_monday_id=found["task_id"],
        task_name=found.get("task_name"),
        project_monday_id=found.get("project_id"),
        project_name=found.get("project_name"),
        service_monday_id=found.get("service_id"),
        found=True,
    )
    session.add(row)
    await session.flush()
    return row


@dataclass
class Resolution:
    """Итог каскада для одного документа."""

    project_monday_id: int | None = None
    project_name: str | None = None
    source: ProjectSource | None = None
    task_monday_id: int | None = None
    service_monday_id: int | None = None
    task_codes: list[str] = None            # type: ignore[assignment]
    extra_projects: list[str] = None        # type: ignore[assignment]
    ambiguous: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        self.task_codes = self.task_codes or []
        self.extra_projects = self.extra_projects or []


async def resolve(
    session: AsyncSession,
    *,
    filename: str | None,
    text: str | None,
    caption: str | None = None,
    match_names: bool = True,
) -> Resolution:
    """Уровни 1 и 2 каскада. Правило по вендору и вопрос человеку живут
    выше, в разборщике: им нужен вендор и очередь.

    `match_names=False` — только код задачи, без поиска названия проекта
    в имени файла и тексте. Для поездок: в имени билета стоит имя
    пассажира, а билет — общий расход."""
    codes = find_task_codes(filename, text, caption)

    if codes:
        tasks = []
        for code in codes:
            row = await resolve_task(session, code)
            if row is not None and row.project_monday_id:
                tasks.append(row)

        if tasks:
            # Главным берём задачу с наименьшим номером в коде: он же
            # порядковый, значит это первая задача счёта. Правило
            # произвольное, но детерминированное — важно именно второе.
            tasks.sort(key=lambda r: _code_number(r.code))
            head = tasks[0]
            others = [r.project_name for r in tasks[1:] if r.project_name and
                      r.project_monday_id != head.project_monday_id]
            return Resolution(
                project_monday_id=head.project_monday_id,
                project_name=head.project_name,
                source=ProjectSource.task_code,
                task_monday_id=head.task_monday_id,
                service_monday_id=head.service_monday_id,
                task_codes=codes,
                extra_projects=others,
                reason=f"код задачи {head.code}",
            )

        # Код есть, задачи нет. Не гадаем: похожий код — это чужой проект,
        # а не «примерно тот».
        return Resolution(
            task_codes=codes,
            reason=f"код задачи найден ({', '.join(codes)}), но в Monday его нет",
        )

    if not match_names:
        return Resolution(reason="поиск по названию для этой категории выключен")

    projects = await cached_projects(session)
    hit = match_project(filename, text, projects, settings.project_match_min_ratio)
    return Resolution(
        project_monday_id=hit.project_monday_id,
        project_name=hit.project_name,
        source=hit.source,
        ambiguous=hit.ambiguous,
        reason=hit.reason,
    )


def _code_number(code: str) -> int:
    tail = code.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0
