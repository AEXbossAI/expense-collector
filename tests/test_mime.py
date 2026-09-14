"""Разбор письма.

Фикстуры повторяют структуры, наблюдавшиеся в живом ящике
invoices@orlanda.info, но с синтетическим содержимым: настоящие
документы в репозиторий не кладём.
"""

from __future__ import annotations

import email
from email.message import EmailMessage

from app.services import mime


def _letter(subject: str = "Customer Invoice", sender: str = "info@tonystam.us") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Tonystam <{sender}>"
    msg.set_content("Текст письма")
    return msg


def test_вложение_забирается():
    msg = _letter()
    msg.add_attachment(b"x" * 20000, maintype="application", subtype="pdf",
                       filename="Invoice-2945.pdf")
    atts = mime.extract_attachments(email.message_from_bytes(bytes(msg)))
    assert [a.filename for a in atts] == ["Invoice-2945.pdf"]


def test_два_документа_из_одного_письма():
    """Наблюдалось на пересылке: счёт и квитанция на один расход
    приходят вместе. Модель обязана быть «письмо → N документов»."""
    msg = _letter("FW: Your receipt from Anthropic")
    msg.add_attachment(b"a" * 20000, maintype="application", subtype="pdf",
                       filename="Invoice-MJG2ZWNE-0003.pdf")
    msg.add_attachment(b"b" * 20000, maintype="application", subtype="pdf",
                       filename="Receipt-2503-9324-8402.pdf")
    atts = mime.extract_attachments(email.message_from_bytes(bytes(msg)))
    assert len(atts) == 2


def test_артефакты_outlook_отбраковываются():
    """~WRD0000.jpg (0 КБ) и image001.png (1 КБ) — подпись и иконки.
    Без фильтра уезжают в папку месяца и засоряют реестр."""
    msg = _letter("FW: receipt")
    msg.add_attachment(b"", maintype="image", subtype="jpeg", filename="~WRD0000.jpg")
    msg.add_attachment(b"x" * 900, maintype="image", subtype="png", filename="image001.png")
    msg.add_attachment(b"p" * 30000, maintype="application", subtype="pdf", filename="real.pdf")
    atts = mime.extract_attachments(email.message_from_bytes(bytes(msg)))
    assert [a.filename for a in atts] == ["real.pdf"]


def test_мелкое_вложение_отбраковывается_по_размеру():
    msg = _letter()
    msg.add_attachment(b"x" * 100, maintype="application", subtype="pdf", filename="tiny.pdf")
    assert mime.extract_attachments(email.message_from_bytes(bytes(msg))) == []


def test_вложенное_письмо_распаковывается():
    """Пересылка от сотрудников приходит вложенным .eml."""
    inner = _letter("Original invoice", "vendor@example.com")
    inner.add_attachment(b"z" * 25000, maintype="application", subtype="pdf",
                         filename="inner-invoice.pdf")

    outer = EmailMessage()
    outer["Subject"] = "Fwd: Original invoice"
    outer["From"] = "Roman <vralumglass@gmail.com>"
    outer.set_content("Пересылаю")
    # EmailMessage сам проставит message/rfc822, если передать письмо
    outer.add_attachment(inner)

    atts = mime.extract_attachments(email.message_from_bytes(bytes(outer)))
    assert [a.filename for a in atts] == ["inner-invoice.pdf"]


def test_имя_в_кодировке_rfc2047():
    msg = _letter()
    msg.add_attachment(b"x" * 20000, maintype="application", subtype="pdf",
                       filename="счёт за сентябрь.pdf")
    atts = mime.extract_attachments(email.message_from_bytes(bytes(msg)))
    assert atts[0].filename == "счёт за сентябрь.pdf"


def test_html_тело_когда_вложений_нет():
    """Квитанция Zak Exhibitions пришла именно так: документ в вёрстке
    письма, вложений ноль."""
    msg = EmailMessage()
    msg["Subject"] = "Your Bolt ride on Monday"
    msg["From"] = "Bolt <receipts-cyprus@bolt.eu>"
    msg.set_content("plain")
    msg.add_alternative("<html><body><h1>Receipt</h1><p>23.00 EUR</p></body></html>",
                        subtype="html")
    parsed = email.message_from_bytes(bytes(msg))
    assert mime.extract_attachments(parsed) == []
    assert "Receipt" in (mime.html_body(parsed) or "")


def test_адрес_отправителя_без_имени():
    msg = _letter(sender="RECEIPTS-Cyprus@Bolt.eu")
    parsed = email.message_from_bytes(bytes(msg))
    assert mime.sender_address(parsed) == "receipts-cyprus@bolt.eu"


def test_игнор_по_адресу_а_не_по_домену():
    """Bolt шлёт с трёх адресов на bolt.eu: чеки, маркетинг, верификацию.
    Фильтр по домену затащил бы мусор и выбросил бы настоящие чеки."""
    junk = email.message_from_bytes(bytes(_letter(sender="verify@bolt.eu")))
    real = email.message_from_bytes(bytes(_letter(sender="receipts-cyprus@bolt.eu")))
    assert mime.is_ignored_sender(junk) is True
    assert mime.is_ignored_sender(real) is False


def test_slack_не_в_игноре():
    """feedback@slack.com был в списке игнора, и это теряло реальные
    счета: «your plan has renewed» приходит с двумя PDF."""
    msg = email.message_from_bytes(bytes(_letter(sender="feedback@slack.com")))
    assert mime.is_ignored_sender(msg) is False
