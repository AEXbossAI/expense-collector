from __future__ import annotations

import logging
import time

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Дедуп алертов: один и тот же ключ не шлётся чаще раза в час.
# Без этого цикл ретраев превращает алерт в спам, и его перестают читать.
_SENT: dict[str, float] = {}
_COOLDOWN_S = 3600


async def alert(key: str, text: str) -> None:
    """Шлёт алерт в телеграм. key определяет дедупликацию."""
    now = time.time()
    if now - _SENT.get(key, 0) < _COOLDOWN_S:
        return
    _SENT[key] = now

    logger.warning("АЛЕРТ [%s] %s", key, text)

    if not settings.telegram_reviewer_id or settings.telegram_bot_token == "changeme":
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                url,
                json={
                    "chat_id": settings.telegram_reviewer_id,
                    "text": f"expense-collector: {text}",
                },
            )
    except Exception as exc:  # алерт не имеет права ронять вызывающий код
        logger.error("Не удалось отправить алерт: %s", exc)


def reset(key: str) -> None:
    """Снимает кулдаун, когда проблема починилась: следующий сбой
    того же рода будет виден сразу, а не через час."""
    _SENT.pop(key, None)
