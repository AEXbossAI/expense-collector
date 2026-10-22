from __future__ import annotations

import base64
import logging
from functools import lru_cache
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

PROMPT_VERSION = "extract_v3"

CompanyKey = Literal["orlanda_engineering", "orlanda_israel", "unknown"]

# Род документа. Решает, кто победит в паре «счёт + квитанция на один
# расход»: группа Out на доске фиксирует оплату, поэтому квитанция сильнее.
DocKindKey = Literal["invoice", "receipt", "credit_note", "proforma", "other"]

# Категории нарезаны по боевому корпусу за сентябрь 2026. Список закрытый:
# свободная категоризация даёт двадцать вариантов написания одного и того же
# и превращает доску в мусор. Значения английские, потому что доска целиком
# английская; лейблы заводятся руками по этому же списку, синк их не создаёт.
CategoryKey = Literal[
    "Software & subscriptions",
    "Telecom",
    "Taxi & local transport",
    "Flights & travel",
    "Car rental & leasing",
    "Fuel",
    "Accounting",
    "Legal",
    "Marketing & advertising",
    "Office & equipment",
    "Training & events",
    "Insurance",
    "Subcontracting",
    # Школа детей и национальное страхование — расходы компании (решение
    # владельца), но к объекту не относятся никогда. Без своих категорий
    # они разъезжались: четыре одинаковых школьных счёта попали в Other,
    # пятый в Training & events.
    "Education",
    "Payroll & social",
    "Other",
]


class ExtractedDoc(BaseModel):
    """Структурированный результат разбора документа.

    Все поля опциональны кроме классификации: документ, который модель
    не смогла прочитать, должен уйти человеку с пустыми полями, а не
    с выдуманными.
    """

    is_expense_document: bool = Field(
        description="True только если это счёт, инвойс, чек или квитанция об оплате. "
        "Рассылки, маркетинг, уведомления сервисов, переписка — False."
    )
    company: CompanyKey = Field(
        description="Какой из двух компаний выставлен документ. "
        "unknown, если определить нельзя или найдены признаки обеих."
    )
    doc_date: str | None = Field(
        default=None, description="Дата документа в формате YYYY-MM-DD. Не дата оплаты и не период услуги."
    )
    amount: float | None = Field(
        default=None, description="Итоговая сумма к оплате, включая налог. Число без разделителей разрядов."
    )
    currency: str | None = Field(default=None, description="Код валюты: EUR, ILS, USD, GEL.")
    vendor: str | None = Field(
        default=None, description="Кто выставил документ (поставщик), НЕ покупатель."
    )
    purpose: str | None = Field(
        default=None, description="За что платим, одной короткой фразой на русском."
    )
    vat_rate: float | None = Field(default=None, description="Ставка НДС в процентах, если указана.")
    service_period: str | None = Field(
        default=None, description="Период оказания услуги, если отличается от даты документа."
    )
    doc_kind: DocKindKey = Field(
        default="other",
        description="Что это за документ. receipt — квитанция, чек, подтверждение ОПЛАТЫ "
        "(есть отметка об оплате, слова paid/received/שולם). invoice — счёт к оплате. "
        "credit_note — кредит-нота, возврат. proforma — проформа. "
        "Если документ и счёт, и подтверждение оплаты одновременно — receipt.",
    )
    doc_number: str | None = Field(
        default=None,
        description="Номер документа у поставщика: Invoice No, חשבון עסקה, Receipt #. "
        "Только цифры и буквы самого номера, без слова «номер» и без префикса вида №.",
    )
    category: CategoryKey = Field(
        default="Other",
        description="Категория расхода из закрытого списка. Выбирай по тому, ЗА ЧТО платим, "
        "а не кому: чек Bolt — Taxi & local transport, счёт Google Workspace — "
        "Software & subscriptions, счёт подрядчика за расчёт конструкции — Subcontracting, "
        "счёт школы или курсов для сотрудника и его семьи — Education, взносы в "
        "национальное страхование и зарплатные платежи — Payroll & social, "
        "авиабилет, отель, услуги аэропорта и визы — Flights & travel.",
    )
    project_hint: str | None = Field(
        default=None,
        description="Название объекта или проекта, если оно прямо названо в документе "
        "(например «Aqua sence Netanya», «Orhot Tora»). Дословно, как написано. "
        "Не выдумывай и не обобщай: нет упоминания — null.",
    )
    notes: str | None = Field(
        default=None, description="Что помешало разобрать документ, если что-то помешало."
    )


SYSTEM = """Ты разбираешь документы по расходам двух компаний одной группы.

КОМПАНИИ:
- orlanda_engineering — Orlanda Engineering OÜ, Эстония.
  Рег. номер 16503223, VAT EE102602283. Ставка НДС обычно 24%.
- orlanda_israel — אורלנדה ישראל שיווק ומכירות בע"מ (Orlanda Israel marketing & sales LTD), Израиль.
  Рег. номер 512354424. Ставка НДС обычно 18%.

КАК ОПРЕДЕЛЯТЬ КОМПАНИЮ:
Компания — это ПОКУПАТЕЛЬ (кому выставлен документ), а не поставщик.
В документе почти всегда два юрлица, не перепутай их.
Самый надёжный признак — регистрационный номер или VAT из списка выше.
Если признаков нет или найдены признаки обеих компаний — верни unknown.
Не угадывай по косвенным признакам вроде валюты или языка документа.

ВАЖНО ПРО ТЕКСТ НА ИВРИТЕ:
Извлечённый из PDF текст на иврите часто имеет сломанный порядок символов
(суффикс בע"מ может оказаться в начале строки). Это артефакт извлечения,
а не ошибка документа. Ориентируйся на узнаваемые фрагменты и на цифры.

СУММА:
Бери итог к оплате с налогом. Если итогов несколько (в разных валютах,
до и после налога) — бери финальный к оплате.
Если сумму прочитать не удалось, верни null. НЕ придумывай её.

РОД ДОКУМЕНТА:
Счёт к оплате и квитанция об оплате — разные документы, и различать их
обязательно: на один расход они приходят парой, а учитывается один.
Квитанция несёт отметку об оплате: paid, payment received, оплачено, שולם,
номер транзакции, последние цифры карты. Счёт несёт срок оплаты и реквизиты.

НОМЕР ДОКУМЕНТА:
В израильских документах он подписан «חשבון עסקה 40378» — нужно 40378.
В европейских — Invoice No / Arve nr. Номер поставщика, НЕ номер заказа
и не номер клиента.

ПРОЕКТ:
Если в документе прямо назван объект или проект — перенеси название
дословно в project_hint. Это подсказка для сопоставления со справочником,
поэтому важна точность написания, а не красота. Ничего не додумывай.

Если документ не является счётом, инвойсом, чеком или квитанцией —
поставь is_expense_document=false и не заполняй остальные поля."""


@lru_cache
def get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _doc_block(data: bytes, mime_type: str) -> dict:
    """Блок контента под тип файла. PDF Claude принимает напрямую —
    растеризация в картинки не нужна и только теряет качество."""
    b64 = base64.standard_b64encode(data).decode("ascii")
    if mime_type == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": b64},
    }


async def extract_fields(
    *,
    text: str | None = None,
    file_data: bytes | None = None,
    mime_type: str | None = None,
    hint: str | None = None,
) -> ExtractedDoc:
    """Разбор документа. Передаётся либо извлечённый текст, либо сам файл.

    Текст дешевле: PDF с текстовым слоем обходится без vision вообще.
    """
    content: list[dict] = []

    if file_data and mime_type:
        content.append(_doc_block(file_data, mime_type))

    parts = []
    if text:
        parts.append(f"Текст документа:\n\n{text[:60000]}")
    if hint:
        # 8000, а не 2000: даты перелёта в письме авиакомпании стоят ниже
        # шапки и рекламы, первые две тысячи символов до них не доходили.
        parts.append(f"Контекст из письма или подписи к фото:\n{hint[:8000]}")
    parts.append("Разбери документ и заполни поля.")
    content.append({"type": "text", "text": "\n\n".join(parts)})

    response = await get_client().messages.parse(
        model=settings.parser_model,
        max_tokens=4000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": content}],
        output_format=ExtractedDoc,
    )
    return response.parsed_output


class PrintedTotal(BaseModel):
    total_as_printed: str | None = Field(
        default=None,
        description="Итог к оплате ровно так, как он напечатан: те же цифры и "
        "разделители, без валюты. null, если итога не видно.",
    )


async def read_total(data: bytes, mime_type: str) -> str | None:
    """Второе, независимое чтение итога с фото или скана.

    У картинки нет текстового слоя, и сверить сумму разбора с текстом
    документа нечем: каждое фото чека из чата уходило фин директору
    вопросом. Это чтение не видит первого разбора и только переписывает
    итог как напечатан; сумма принимается, если два чтения совпали.
    """
    response = await get_client().messages.parse(
        model=settings.parser_model,
        max_tokens=1500,
        system=(
            "Перепиши из документа итоговую сумму к оплате — ровно как она "
            "напечатана, цифра в цифру. Не считай и не округляй. Если итога "
            "не видно или он нечитаем, верни null."
        ),
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [
            _doc_block(data, mime_type),
            {"type": "text", "text": "Итог к оплате?"},
        ]}],
        output_format=PrintedTotal,
    )
    return response.parsed_output.total_as_printed


class TravelDate(BaseModel):
    travel_date: str | None = Field(
        default=None,
        description="Дата первого вылета или заезда в отель, YYYY-MM-DD. null, если её нет.",
    )


TRAVEL_SYSTEM = """Документ — расход на поездку: авиабилет, отель, услуга аэропорта.
Нужна дата самой поездки: первого вылета или заезда в отель. Для услуг при
перелёте (fast lane, выбор места, багаж, смена или возврат билета) — дата
этого перелёта. Поездка через границу месяцев — всё равно первая дата.

Это НЕ дата документа и НЕ дата оплаты. Иногда дата есть только в письме:
«Your flight on 14 Oct», «Check-in: 16/09/2026». Год не указан — бери
ближайший после даты документа. Даты поездки нигде нет — верни null,
не угадывай."""


async def read_travel_date(
    *,
    text: str | None = None,
    file_data: bytes | None = None,
    mime_type: str | None = None,
    hint: str | None = None,
) -> str | None:
    """Дата поездки — отдельным вызовом, а не полем ExtractedDoc.

    22.10.2026 поле travel_date в ExtractedDoc уронило разбор целиком:
    API ответил «Schema is too complex» даже на простую строку. Схема
    основного разбора на пределе, поэтому дата поездки спрашивается
    отдельно и только у документов категории Flights & travel.
    """
    content: list[dict] = []
    if file_data and mime_type:
        content.append(_doc_block(file_data, mime_type))
    parts = []
    if text:
        parts.append(f"Текст документа:\n\n{text[:60000]}")
    if hint:
        parts.append(f"Контекст из письма или подписи к фото:\n{hint[:8000]}")
    parts.append("Дата поездки?")
    content.append({"type": "text", "text": "\n\n".join(parts)})
    response = await get_client().messages.parse(
        model=settings.parser_model,
        max_tokens=2000,
        system=TRAVEL_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": content}],
        output_format=TravelDate,
    )
    return response.parsed_output.travel_date


class TextOnlyVerdict(BaseModel):
    """Классификация сообщения из чата без файла."""

    is_expense: bool = Field(description="Это сообщение о расходе, а не обсуждение?")
    amount: float | None = Field(default=None, description="Сумма, если названа.")
    currency: str | None = Field(default=None)
    purpose: str | None = Field(default=None, description="На что потрачено.")
    doc_date: str | None = Field(default=None, description="Дата расхода YYYY-MM-DD, если названа.")


async def classify_text(message: str) -> TextOnlyVerdict:
    response = await get_client().messages.parse(
        model=settings.parser_model,
        max_tokens=1500,
        system=(
            "В рабочем чате директор присылает расходы компании, но там же идёт "
            "обычное обсуждение. Определи, сообщение о конкретном расходе "
            "(«оплатил такси 45», «обед с подрядчиком 120 евро») или просто переписка. "
            "Если это расход, вытащи сумму, валюту, назначение и дату, если они названы. "
            "Не придумывай то, чего в сообщении нет."
        ),
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": message[:4000]}],
        output_format=TextOnlyVerdict,
    )
    return response.parsed_output
