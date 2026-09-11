from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

from sqlalchemy import select

from app.core import health, naming
from app.core.config import get_settings
from app.core.text import clean
from app.db.models import Document, Kind, PendingUpload, Source, Status
from app.db.session import session_scope
from app.services import alerts, buffer, mime

logger = logging.getLogger(__name__)
settings = get_settings()

# Плоская метка: слэш в имени создаёт вложенный ярлык и усложняет поиск
DONE_LABEL = "expense-collector-done"

# Gmail отдаёт длинные строки на FETCH больших писем
imaplib._MAXLINE = 10_000_000


# ── низкоуровневая работа с IMAP (синхронная, живёт в executor) ───────────


def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(settings.imap_host, timeout=settings.imap_timeout_s)
    conn.login(settings.imap_user, settings.imap_password)
    return conn


def _all_mail_folder(conn: imaplib.IMAP4_SSL) -> str:
    """Папка «Все письма» по special-use флагу \\All.

    По имени искать нельзя: оно приходит в modified UTF-7 и зависит от
    локали ящика ([Gmail]/&BBIEQQRP- &BD8EPgRHBEIEMA-). Читаем именно её,
    а не INBOX: инвойсы регулярно уходят фильтрами в Промоакции и Спам.
    """
    typ, folders = conn.list()
    if typ == "OK":
        for raw in folders or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line and '"/"' in line:
                return line.split('"/"', 1)[1].strip().strip('"')
    logger.warning("Папка с флагом \\All не найдена, читаю INBOX")
    return "INBOX"


def _search_criteria() -> tuple[str, str]:
    """X-GM-RAW: исключаем обработанное меткой, а не своим состоянием.

    Метка живёт в Gmail и переживает пересоздание базы, потерю курсора
    и переезд сервера. IMAP UID для этого не годится: он меняется при
    смене UIDVALIDITY.
    """
    query = f"-label:{DONE_LABEL}"
    if settings.imap_backfill_days > 0:
        query += f" newer_than:{settings.imap_backfill_days}d"
    return "X-GM-RAW", f'"{query}"'


def _fetch_batch(conn: imaplib.IMAP4_SSL, limit: int) -> list[tuple[bytes, str, bytes]]:
    """[(UID, X-GM-MSGID, сырое письмо)] для необработанных писем.

    Работаем через UID, а не через порядковые номера: последние сдвигаются,
    если во время сессии в папке что-то меняется, и метка уедет не на то
    письмо.
    """
    key, value = _search_criteria()
    typ, ids = conn.uid("SEARCH", None, key, value)
    if typ != "OK" or not ids or not ids[0]:
        return []

    out: list[tuple[bytes, str, bytes]] = []
    for uid in ids[0].split()[:limit]:
        typ, data = conn.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
        if typ != "OK" or not data:
            continue
        gm_msgid, raw = None, None
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                head = item[0].decode("utf-8", "replace") if isinstance(item[0], bytes) else str(item[0])
                match = re.search(r"X-GM-MSGID\s+(\d+)", head)
                if match:
                    gm_msgid = match.group(1)
                raw = item[1]
        if gm_msgid and raw:
            out.append((uid, gm_msgid, raw))
        else:
            logger.warning("Пропуск письма uid=%s: не разобран ответ FETCH", uid)
    return out


def _mark_done(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    """Метка ставится ТОЛЬКО после того, как байты легли в буфер и строка
    появилась в БД. Сбой раньше означает переобработку, а не потерю."""
    typ, _ = conn.uid("STORE", uid, "+X-GM-LABELS", f"({DONE_LABEL})")
    if typ != "OK":
        logger.warning("Не удалось поставить метку на uid=%s", uid)


# ── обработка письма ──────────────────────────────────────────────────────


def _received_at(msg: Message) -> datetime:
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


async def _already_seen(session, source_key: str, original_name: str) -> bool:
    stmt = select(Document.id).where(
        Document.source == Source.mail,
        Document.source_key == source_key,
        Document.original_name == original_name,
    )
    return (await session.execute(stmt)).first() is not None


async def _store_email(gm_msgid: str, raw: bytes) -> int:
    """Возвращает число созданных документов."""
    msg = email.message_from_bytes(raw)
    subject = mime.header(msg, "Subject")
    sender = mime.sender_address(msg)
    received = _received_at(msg)

    if mime.is_ignored_sender(msg):
        logger.info("Отправитель в списке игнора: %s (%s)", sender, subject[:50])
        return 0

    attachments = mime.extract_attachments(msg)

    # Вложений не осталось — документом может быть само тело письма.
    # Подтверждено на реальном письме Zak Exhibitions без вложений.
    if not attachments:
        html = mime.html_body(msg)
        pdf = mime.render_html_to_pdf(html, subject) if html else None
        if pdf:
            safe_subject = buffer.safe_name(subject or "letter", fallback="letter")
            stem = safe_subject.rsplit(".", 1)[0]
            attachments = [mime.Attachment(f"{stem}_body.pdf", "application/pdf", pdf)]
        else:
            logger.info("Письмо без вложений и без пригодного тела: %s", subject[:60])
            return 0

    body_text = (clean(mime.text_body(msg)) or "")[:20000]
    created = 0

    for att in attachments:
        async with session_scope() as session:
            if await _already_seen(session, gm_msgid, buffer.safe_name(att.filename)):
                continue

            local = buffer.write("mail", att.filename, att.data)
            doc = Document(
                source=Source.mail,
                source_key=gm_msgid,        # стабилен, в отличие от IMAP UID
                email_id=gm_msgid,          # связывает счёт и квитанцию из одного письма
                kind=Kind.file,
                original_name=buffer.safe_name(att.filename),
                local_path=str(local),
                size_bytes=att.size,
                mime_type=att.content_type,
                raw_email_body=body_text,
                raw_caption=clean(subject),
                sender=sender,
                received_at=received,
                status=Status.new,          # компанию определит разборщик
            )
            session.add(doc)
            await session.flush()
            session.add(
                PendingUpload(
                    document_id=doc.id,
                    local_path=str(local),
                    target_path=naming.inbox_path(received, f"{doc.id}_{doc.original_name}"),
                )
            )
            created += 1
            logger.info("Почта: документ %s (%s, %d байт) из %r", doc.id, att.filename, att.size, subject[:40])

    return created


# ── цикл ──────────────────────────────────────────────────────────────────


async def _poll_once(batch: int = 25) -> None:
    loop = asyncio.get_running_loop()
    conn = await loop.run_in_executor(None, _connect)
    try:
        folder = await loop.run_in_executor(None, _all_mail_folder, conn)
        await loop.run_in_executor(None, lambda: conn.select(f'"{folder}"', readonly=False))

        messages = await loop.run_in_executor(None, _fetch_batch, conn, batch)
        if not messages:
            logger.debug("Почта: новых писем нет")
            return

        logger.info("Почта: к обработке %d писем", len(messages))
        for uid, gm_msgid, raw in messages:
            try:
                await _store_email(gm_msgid, raw)
            except Exception:
                # Метку НЕ ставим: письмо переобработается на следующем цикле.
                # IMAP хранит его сам, терять нечего — это вторая линия
                # обороны, которой нет у телеграма.
                logger.exception("Сбой на письме %s, метка не поставлена", gm_msgid)
                continue
            await loop.run_in_executor(None, _mark_done, conn, uid)

        alerts.reset("imap-auth")
    finally:
        try:
            await loop.run_in_executor(None, conn.logout)
        except Exception:
            pass


async def run() -> None:
    if settings.imap_password == "changeme":
        logger.error("IMAP_PASSWORD не задан, почтовый коллектор не запущен")
        return

    asyncio.create_task(health.beat_loop("collector-mail"))
    logger.info(
        "Почтовый коллектор запущен: %s, интервал %dс, backfill %s",
        settings.imap_user,
        settings.imap_poll_interval_s,
        "весь ящик" if settings.imap_backfill_days == 0 else f"{settings.imap_backfill_days} дн.",
    )
    while True:
        try:
            await _poll_once()
        except imaplib.IMAP4.error as exc:
            # Один и тот же AUTHENTICATIONFAILED приходит и на неверный
            # пароль, и на запрет паролей приложений политикой домена.
            await alerts.alert(
                "imap-auth",
                f"IMAP отклоняет доступ к {settings.imap_user}: {exc}. "
                f"Проверьте пароль приложения и политику домена в админке Workspace.",
            )
        except Exception:
            logger.exception("Почтовый коллектор упал на цикле")
        await asyncio.sleep(settings.imap_poll_interval_s)
