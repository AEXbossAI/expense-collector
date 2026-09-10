from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Символы, запрещённые в путях Dropbox и Windows
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_SPACES = re.compile(r"\s+")

# Лимит имени в Dropbox — 255 байт. Кириллица в UTF-8 съедает его вдвое
# быстрее, поэтому режем с запасом и считаем БАЙТЫ, а не символы.
MAX_NAME_BYTES = 100


def safe_name(name: str | None, fallback: str = "file") -> str:
    """Имя, пригодное и для диска, и для пути Dropbox."""
    name = (name or "").strip()
    if not name:
        return fallback

    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 10:
        stem, ext = name, ""

    stem = _UNSAFE.sub("", stem)
    stem = _SPACES.sub("-", stem).strip("-.")
    if not stem:
        stem = fallback

    ext = _UNSAFE.sub("", ext).strip(".")
    suffix = f".{ext}" if ext else ""

    budget = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8")
    if len(encoded) > budget:
        # Режем по байтам и чиним возможный обрыв посреди UTF-8 символа
        stem = encoded[:budget].decode("utf-8", "ignore")

    return f"{stem}{suffix}"


def buffer_path(source: str, original_name: str | None) -> Path:
    """Уникальный путь в буфере. uuid4 в префиксе снимает проблему
    одинаковых имён вложений в разных письмах."""
    root = Path(settings.buffer_root) / source / date.today().isoformat()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{uuid.uuid4().hex}_{safe_name(original_name)}"


def write(source: str, original_name: str | None, data: bytes) -> Path:
    path = buffer_path(source, original_name)
    path.write_bytes(data)
    logger.debug("В буфер: %s (%d байт)", path, len(data))
    return path


def discard(path: str | Path | None) -> None:
    """Удаляет файл из буфера. Вызывается ТОЛЬКО после успешной выгрузки
    и проставленного uploaded_at."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Не удалось удалить из буфера %s: %s", path, exc)
