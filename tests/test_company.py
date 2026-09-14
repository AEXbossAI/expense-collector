"""Определение компании.

Каждый тест закрепляет вывод, полученный на боевых данных. Прогон по
28 размеченным документам показал: правило «совпало название» даёт
2 ошибки из 4 срабатываний, правило «решает только идентификатор» —
18 верных и ни одной ошибки.
"""

from __future__ import annotations

from app.services import company


def test_идентификатор_решает():
    text = "Invoice to: Orlanda Engineering OÜ\nReg. No. 16503223\nVAT ID EE102602283"
    key, reason = company.detect(text)
    assert key == "orlanda_engineering"
    assert "16503223" in reason


def test_vat_тоже_считается_идентификатором():
    key, _ = company.detect("...\nVAT ID EE102602283\n...")
    assert key == "orlanda_engineering"


def test_израильский_номер():
    key, _ = company.detect("מספר מזהה: 512354424")
    assert key == "orlanda_israel"


def test_одно_название_без_номера_НЕ_решает():
    """Главный урок корпусного прогона.

    Существуют межфирменные документы, где эстонская компания выставляет
    счёт израильской. Её название в тексте есть, но она поставщик, а не
    покупатель. Решать по названию — подбрасывать монетку.
    """
    key, reason = company.detect("Supplier: Orlanda Engineering OÜ\nSome invoice text")
    assert key == company.UNKNOWN
    assert "только название" in reason


def test_название_отдаётся_модели_подсказкой():
    hints = company.name_hints("Supplier: Orlanda Engineering OÜ")
    assert hints == {"orlanda_engineering": ["Orlanda Engineering"]}


def test_идентификаторы_обеих_компаний_не_решают():
    text = "Buyer reg 512354424, supplier reg 16503223"
    key, reason = company.detect(text)
    assert key == company.UNKNOWN
    assert "обеих" in reason


def test_иврит_после_развала_rtl():
    """Извлечение PDF ломает порядок RTL: суффикс בע\"מ уезжает в начало.

    Полное юридическое название после этого не матчится, а короткий
    токен — матчится. Строка взята из реального invoice_684_lawyer.pdf.
    """
    broken = 'מ"אורלנדה ישראל שיווק ומכירות בע'
    assert 'אורלנדה ישראל שיווק ומכירות בע"מ' not in broken   # полное не найдётся
    key, _ = company.detect(broken + " 512354424")
    assert key == "orlanda_israel"


def test_пробелы_схлопываются():
    """В извлечённом PDF строки рвутся в случайных местах."""
    assert company.name_hints("Orlanda   Engineering") == {
        "orlanda_engineering": ["Orlanda Engineering"]
    }


def test_пустой_текст():
    key, reason = company.detect("")
    assert key == company.UNKNOWN
    assert reason == "пустой текст"


# ── ставка НДС ────────────────────────────────────────────────────────────


def test_ноль_процентов_законен():
    """Трансграничные B2B идут по reverse charge с НДС 0%: Slack,
    Anthropic, Time Doctor. Проверка блокировала 9 документов из 20."""
    assert company.vat_matches("orlanda_engineering", 0.0) is None


def test_домашняя_ставка_совпадает():
    assert company.vat_matches("orlanda_engineering", 24.0) is True
    assert company.vat_matches("orlanda_israel", 18.0) is True


def test_чужая_ставка_не_совпадает():
    assert company.vat_matches("orlanda_engineering", 18.0) is False


def test_неизвестная_ставка_не_проверяется():
    assert company.vat_matches("orlanda_engineering", None) is None
