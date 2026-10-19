"""Электронные чеки по ссылке.

Кассы присылают не файл, а ссылку: 14.10.2026 в чат пришло
`https://receipts.weezmo.com/api/receipts/signed/.../download` рядом с
«Fuel, paid by company card». Разборщик счёл ссылку обсуждением, и
заправка в реестр не попала.

Ссылка принимается, только если по ней отдаётся сам документ: PDF или
картинка. Страница, приложение, что угодно ещё — обычное сообщение.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

from app.services import extract

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[^\s<>\"'()\[\]]+")
MAX_BYTES = 20 * 1024 * 1024
ACCEPTED = {"application/pdf"} | extract.IMAGE_TYPES
EXTENSIONS = {
    "application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/heic": ".heic",
}


@dataclass
class Fetched:
    url: str
    data: bytes
    mime: str
    name: str


def find_urls(text: str | None) -> list[str]:
    return [u.rstrip(".,;:!?") for u in URL_RE.findall(text or "")]


def _filename(disposition: str | None, url: str, mime: str) -> str:
    """Имя из Content-Disposition, иначе последний значимый кусок пути.
    weezmo отдаёт `inline; filename=<uuid>.pdf`, путь кончается на /download."""
    name = None
    if disposition:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
        if m:
            name = unquote(m.group(1)).strip()
    if not name:
        parts = [p for p in urlparse(url).path.split("/") if p and p.lower() != "download"]
        name = parts[-1] if parts else "receipt"
    ext = EXTENSIONS[mime]
    if not name.lower().endswith(ext):
        name += ext
    host = (urlparse(url).hostname or "").split(".")
    prefix = host[-2] if len(host) >= 2 else "link"
    return f"{prefix}_{name}"


class LinkUnavailable(Exception):
    """Сеть или сервер кассы. Сообщение нельзя списывать в обсуждение:
    разборщик повторит попытку на следующем цикле."""


async def fetch_receipt(url: str) -> Fetched | None:
    """Документ по ссылке или None, если по ней не документ."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 500:
                    raise LinkUnavailable(f"{url}: HTTP {resp.status_code}")
                if resp.status_code != 200:
                    logger.info("Ссылка %s: HTTP %s", url, resp.status_code)
                    return None
                mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if mime not in ACCEPTED:
                    logger.info("Ссылка %s: не документ (%s)", url, mime or "без типа")
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        logger.warning("Ссылка %s: больше %d байт, не качаю", url, MAX_BYTES)
                        return None
                    chunks.append(chunk)
                disposition = resp.headers.get("content-disposition")
    except httpx.HTTPError as exc:
        raise LinkUnavailable(f"{url}: {exc}") from exc
    return Fetched(url=url, data=b"".join(chunks), mime=mime,
                   name=_filename(disposition, url, mime))
