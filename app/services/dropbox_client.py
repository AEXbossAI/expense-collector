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


    # ── операции разборщика ───────────────────────────────────────────────

    def _download_sync(self, path: str) -> bytes:
        _, resp = self._dbx.files_download(path)
        return resp.content

    async def download(self, path: str) -> bytes:
        """Читает файл обратно из Dropbox. Локальный буфер удаляется сразу
        после выгрузки, поэтому разборщик берёт документ отсюда."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, path)

    def _ensure_folder_sync(self, path: str) -> None:
        try:
            self._dbx.files_create_folder_v2(path)
            logger.info("Создана папка %s", path)
        except ApiError as exc:
            # conflict/folder — папка уже есть, это нормальный исход гонки
            if "conflict" in str(exc):
                return
            raise

    async def ensure_folder(self, path: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_folder_sync, path)

    def _move_sync(self, src: str, dst: str) -> str:
        try:
            meta = self._dbx.files_move_v2(src, dst, autorename=False)
            return meta.metadata.path_display
        except ApiError as exc:
            text = str(exc)
            # Идемпотентность: файл уже на месте после частичного сбоя.
            # from_lookup/not_found + существующая цель = перемещение
            # прошло, а подтверждение в БД не записалось.
            if "not_found" in text or "conflict" in text:
                try:
                    meta = self._dbx.files_get_metadata(dst)
                    logger.info("Файл уже в цели, перемещение не требуется: %s", dst)
                    return meta.path_display
                except Exception:
                    pass
            raise

    async def move(self, src: str, dst: str) -> str:
        """Перемещение, в том числе между namespace разных компаний —
        проверено на живом аккаунте, работает."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._move_sync, src, dst)
        except AuthError as exc:
            raise DropboxAuthBroken(str(exc)) from exc

    def _delete_sync(self, path: str) -> None:
        self._dbx.files_delete_v2(path)

    async def delete(self, path: str) -> None:
        """Используется только для status=ignored: рассылки, отрендеренные
        из HTML-тела, не должны копиться в инбоксе мегабайтами."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._delete_sync, path)
        except ApiError as exc:
            if "not_found" in str(exc):
                return
            raise


@lru_cache
def get_dropbox() -> DropboxClient:
    return DropboxClient()
