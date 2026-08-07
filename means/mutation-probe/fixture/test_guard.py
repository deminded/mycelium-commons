"""Защитный тест: у него есть конкретный способ проиграть — подделанная подпись."""
from guarded import accept, sign


def test_valid_signature_is_accepted():
    text = "привет"
    assert accept(text, sign(text)) is True


def test_forged_signature_is_rejected():
    text = "привет"
    assert accept(text, "deadbeef1234") is False


def test_tampered_text_is_rejected():
    good = sign("привет")
    assert accept("привет!", good) is False
