from __future__ import annotations

import asyncio
import io
import logging

from app.core.config import get_settings
from app.core.text import clean

logger = logging.getLogger(__name__)
settings = get_settings()

# Короче этого текстовый слой считаем непригодным и идём к модели.
# Порог уточняется на корпусе из папок компаний.
MIN_TEXT_CHARS = 100

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}


def _pdf_text_sync(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber недоступен")
        return ""
    try:
        chunks: list[str] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:30]:
                chunks.append(page.extract_text() or "")
        # Чистим на источнике: извлечение PDF регулярно отдаёт NUL-байты,
        # а Postgres их в текстовых полях не принимает и роняет весь цикл.
        return (clean("\n".join(chunks)) or "").strip()
    except Exception as exc:
        logger.warning("pdfplumber не смог прочитать PDF: %s", exc)
        return ""


async def pdf_text(data: bytes) -> str:
    """Текстовый слой PDF. Бесплатно и без обращения к модели.

    На реальных инвойсах (AccRes Nola OÜ) отсюда извлекаются покупатель,
    рег. номер, VAT, позиции, суммы и ставка НДС — всё, что нужно.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _pdf_text_sync, data)


async def prepare(data: bytes, mime_type: str | None) -> tuple[str | None, bytes | None, str | None]:
    """Решает, что отправлять модели: текст, файл или и то и другое.

    Возвращает (текст, файл, mime). Лестница:
      1. PDF с текстовым слоем  -> только текст, vision не нужен
      2. PDF без текстового слоя -> сам PDF (Claude принимает напрямую)
      3. Изображение             -> сам файл
      4. Прочее                  -> ничего, документ уйдёт человеку
    """
    mime_type = (mime_type or "").lower()

    if mime_type == "application/pdf":
        text = await pdf_text(data)
        if len(text) >= MIN_TEXT_CHARS:
            logger.debug("PDF разобран текстовым слоем: %d символов", len(text))
            return text, None, None
        logger.debug("Текстовый слой пуст (%d симв.), отправляю PDF в модель", len(text))
        return (text or None), data, mime_type

    if mime_type in IMAGE_TYPES:
        return None, data, mime_type

    logger.info("Тип %r не поддержан для автоматического разбора", mime_type)
    return None, None, None
