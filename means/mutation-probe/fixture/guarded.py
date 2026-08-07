"""Минимальный предмет для проверки самого инструмента подсадки.

Защита здесь одна и настоящая: приём сообщения только с верной подписью.
Рядом стоит НЕ покрытый тестом параметр — он нужен, чтобы показать состояние HOLE.
"""
import hashlib

SECRET = "s3cret"
MAX_LEN = 1000  # ограничение длины — НАМЕРЕННО не покрыто тестом


def sign(text: str) -> str:
    return hashlib.sha256((SECRET + text).encode("utf-8")).hexdigest()[:12]


def accept(text: str, signature: str) -> bool:
    """Принимает сообщение, только если подпись верна и длина в пределах."""
    if len(text) > MAX_LEN:
        return False
    if signature != sign(text):
        return False
    return True
