from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Официальный SDK синхронный, поэтому все вызовы уходят в executor.
# Взамен он сам экранирует Dropbox-API-Arg (заголовок обязан быть ASCII,
# а пути у нас кириллические) и сам разбирается с PathRoot.
# Референс: orlandabot/app/services/dropbox_client.py:44-53
CHUNK_THRESHOLD = 140 * 1024 * 1024


class DropboxAuthBroken(Exception):
    """Протухший refresh token или отозванные права. Ретраить бесполезно,
    нужен человек — поэтому отделено от сетевых сбоев."""


class DropboxClient:
    def __init__(self) -> None:
        # Токен командный (team-scoped), тот же, что у OrlandaBot. Такой токен
        # не адресует конкретный аккаунт: без Dropbox-API-Select-User Dropbox
        # отвечает BadInputError "token is for an entire Dropbox Business team".
        # В SDK этот заголовок ставит DropboxTeam.as_user(); у обычного
        # Dropbox метода as_user нет вовсе.
        if not settings.dropbox_team_member_id:
            raise RuntimeError(
                "DROPBOX_TEAM_MEMBER_ID обязателен: токен командный, "
                "без выбора участника Dropbox отклонит любой файловый вызов"
            )

        team = dropbox.DropboxTeam(
            oauth2_refresh_token=settings.dropbox_refresh_token,
            app_key=settings.dropbox_app_key,
            app_secret=settings.dropbox_app_secret,
            timeout=120,
        )
        dbx = team.as_user(settings.dropbox_team_member_id)

        namespace = settings.dropbox_namespace_id
        if not namespace:
            # Без явного namespace запись уходит в личное пространство
            # участника вместо командного, и замечается это не сразу.
            namespace = dbx.users_get_current_account().root_info.root_namespace_id
            logger.info("Namespace определён автоматически: %s", namespace)

        self._dbx = dbx.with_path_root(dropbox.common.PathRoot.root(str(namespace)))

    # ── синхронная часть, живёт в executor ────────────────────────────────

    def _upload_sync(self, local_path: str, target_path: str) -> str:
        size = Path(local_path).stat().st_size
        with open(local_path, "rb") as fh:
            if size <= CHUNK_THRESHOLD:
                meta = self._dbx.files_upload(
                    fh.read(), target_path, mode=WriteMode.overwrite
                )
            else:
                meta = self._upload_session(fh, target_path, size)
        return meta.content_hash

    def _upload_session(self, fh, target_path: str, size: int):
        chunk = 8 * 1024 * 1024
        start = self._dbx.files_upload_session_start(fh.read(chunk))
        cursor = dropbox.files.UploadSessionCursor(start.session_id, fh.tell())
        commit = dropbox.files.CommitInfo(target_path, mode=WriteMode.overwrite)
        while fh.tell() < size:
            if size - fh.tell() <= chunk:
                return self._dbx.files_upload_session_finish(fh.read(chunk), cursor, commit)
            self._dbx.files_upload_session_append_v2(fh.read(chunk), cursor)
            cursor.offset = fh.tell()
        return self._dbx.files_upload_session_finish(b"", cursor, commit)

    # ── асинхронная обёртка ───────────────────────────────────────────────

    async def upload(self, local_path: str | Path, target_path: str) -> str:
        """Кладёт файл в Dropbox, возвращает content_hash.

        Идемпотентно: mode=overwrite, поэтому повторная выгрузка того же
        файла не плодит "имя (1).pdf". content_hash считает сам Dropbox
        (блочный SHA-256), спеку №2 он нужен для точного дедупа.
        """
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, self._upload_sync, str(local_path), target_path
            )
        except AuthError as exc:
            raise DropboxAuthBroken(str(exc)) from exc
        except ApiError as exc:
            # insufficient_space и подобное ретраем не лечится, но и
            # человеку сообщать надо иначе — пока просто пробрасываем.
            logger.error("Dropbox API отказал на %s: %s", target_path, exc)
            raise


@lru_cache
def get_dropbox() -> DropboxClient:
    return DropboxClient()
