from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Конфиг читается при импорте модулей, поэтому переменные ставим ДО них.
# Значения повторяют боевые по форме, но не по содержанию: тесты не должны
# зависеть от .env и не должны ходить в реальный Dropbox.
os.environ.setdefault("BUFFER_ROOT", tempfile.gettempdir())
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/test")
os.environ.setdefault("TELEGRAM_COMPANY", "orlanda_engineering")
os.environ.setdefault("DEFAULT_COMPANY", "orlanda_engineering")
os.environ.setdefault("RECONCILE_FROM", "2026-09")
os.environ.setdefault("DROPBOX_TEAM_MEMBER_ID", "dbmid:test")
os.environ.setdefault(
    "DROPBOX_COMPANY_ROOTS",
    json.dumps({
        "orlanda_engineering": "/Finance/AMS Group_accouting",
        "orlanda_israel": "/Finance/Orlanda Israel/Invoices",
    }),
)
os.environ.setdefault(
    "COMPANY_LEGAL_NAMES",
    json.dumps({
        "orlanda_engineering": ["16503223", "EE102602283", "Orlanda Engineering"],
        "orlanda_israel": ["512354424", "Orlanda Israel", "אורלנדה ישראל"],
    }, ensure_ascii=False),
)


@pytest.fixture
def tmp_buffer(tmp_path, monkeypatch):
    monkeypatch.setenv("BUFFER_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
