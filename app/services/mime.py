from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_SKIP_PATTERNS = [re.compile(p, re.I) for p in settings.attachment_skip_patterns]

# Типы, которые никогда не являются документом расхода
_SKIP_MIME = {
    "text/plain", "text/html", "text/calendar",
    "application/pgp-signature", "application/pkcs7-signature",
}


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def decode_str(value: str | None) -> str:
    """RFC 2047: =?utf-8?B?...?= в имени файла и теме письма."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def header(msg: Message, name: str) -> str:
    return decode_str(msg.get(name))


def sender_address(msg: Message) -> str:
    """Голый адрес без отображаемого имени, в нижнем регистре.

    Фильтруем именно по адресу, а не по домену: Bolt шлёт с трёх разных
    адресов на bolt.eu — настоящие чеки, маркетинг и верификацию.
    """
    raw = msg.get("From") or ""
    match = re.search(r"<([^>]+)>", raw)
    addr = (match.group(1) if match else raw).strip().lower()
    return addr


def is_ignored_sender(msg: Message) -> bool:
    addr = sender_address(msg)
    return any(addr == s.strip().lower() for s in settings.sender_ignore_list)


def _is_junk_attachment(filename: str, content_type: str, size: int, part: Message) -> str | None:
    """Возвращает причину отбраковки либо None, если вложение годное.

    Наблюдалось живьём в пересланном письме: ~WRD0000.jpg (0 КБ) и
    image001.png (1 КБ) — подпись и иконки из Outlook. Без фильтра они
    уезжают в папку месяца и засоряют реестр.
    """
    if content_type in _SKIP_MIME:
        return f"служебный тип {content_type}"
    if size < settings.attachment_min_bytes:
        return f"меньше {settings.attachment_min_bytes} байт"
    for pattern in _SKIP_PATTERNS:
        if pattern.match(filename):
            return f"имя похоже на артефакт почтового клиента ({filename})"
    disposition = str(part.get("Content-Disposition") or "").lower()
    if part.get("Content-ID") and "attachment" not in disposition:
        return "inline-часть с Content-ID"
    return None


def extract_attachments(msg: Message, _depth: int = 0) -> list[Attachment]:
    """Все годные вложения письма, включая вложенные .eml.

    Одно письмо может дать несколько документов: счёт и квитанция на
    один расход приходят вместе — наблюдалось на пересылке от сотрудницы.
    """
    if _depth > 5:
        logger.warning("Слишком глубокая вложенность .eml, останавливаюсь")
        return []

    found: list[Attachment] = []

    for part in msg.walk():
        if part is msg and msg.is_multipart():
            continue

        content_type = (part.get_content_type() or "").lower()

        # Пересылка от сотрудников приходит вложенным письмом.
        # Явная рекурсия здесь НЕ нужна: msg.walk() уже спускается внутрь
        # message/rfc822, и добавление рекурсии извлекало каждое вложение
        # пересылки дважды. Сам контейнер вложением не считаем.
        if content_type == "message/rfc822":
            continue

        filename = decode_str(part.get_filename())
        disposition = str(part.get("Content-Disposition") or "").lower()
        if not filename and "attachment" not in disposition:
            continue

        try:
            data = part.get_payload(decode=True) or b""
        except Exception as exc:
            logger.warning("Не удалось декодировать вложение %s: %s", filename, exc)
            continue

        filename = filename or f"unnamed.{content_type.split('/')[-1]}"
        reason = _is_junk_attachment(filename, content_type, len(data), part)
        if reason:
            logger.debug("Отбраковано вложение %s: %s", filename, reason)
            continue

        found.append(Attachment(filename=filename, content_type=content_type, data=data))

    return found


def html_body(msg: Message) -> str | None:
    """HTML-тело письма. Когда вложений нет, документ находится здесь:
    квитанция Zak Exhibitions пришла именно так."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            try:
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
    return None


def text_body(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            try:
                raw = part.get_payload(decode=True) or b""
                return raw.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
    return ""


def render_html_to_pdf(html: str, title: str) -> bytes | None:
    """HTML-тело как полноценный документ. Не запасной путь, а обязательный:
    часть вендоров шлёт квитанцию вёрсткой письма, без вложения."""
    try:
        from weasyprint import HTML  # импорт внутри: тянет тяжёлые системные либы
    except ImportError:
        logger.error("WeasyPrint недоступен, HTML-тело письма %r не сохранено", title)
        return None
    try:
        return HTML(string=html).write_pdf()
    except Exception as exc:
        logger.warning("Не удалось отрендерить HTML письма %r в PDF: %s", title, exc)
        return None
