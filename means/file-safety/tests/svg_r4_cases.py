#!/usr/bin/env python3
"""#2411, круг 3 ревью: два случая, которых прежние наборы не держали. Написаны ДО правки кода.

Запуск: python3 tests/svg_r4_cases.py <путь к file_safety_check.py>

Откуда случаи (заключение ревью круга 3, ~/reviews/svg-2411/review-r3/REVIEW-astra-2411-r3.md):
1. P1: сбой внутри ZIP-проверки встроенной картинки (OSError, который zipfile превращает в BadZipFile)
   читался как «не ZIP» и кончался приёмом. SPEC §4.2: любая неожиданная ошибка самой проверки —
   отказ svg_parse_error, а не пропуск. Подлинное «не ZIP» по-прежнему принимается — это контроль,
   без него правка «любой BadZipFile = отказ» прошла бы этот набор.
2. Пробел тестов: знак «>» на последнем байте окна 64 КиБ. SPEC §4.1: открывающий тег корня,
   не закончившийся внутри первых 64 КиБ, — «нет». Окно короче на байт оставляло прежние наборы
   зелёными; здесь граница проверяется с обеих сторон.
Обвязка и фикстуры берутся из svg_cases.py, лежащего рядом.
"""
import importlib.util
import os
import sys
import tempfile
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load_base():
    spec = importlib.util.spec_from_file_location("svg_cases_base", os.path.join(HERE, "svg_cases.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_base()
WINDOW = 64 * 1024   # SPEC §4.1, written here from the requirement, not imported from the code under test
PNG_DRAWING = B.svg(B.image(B.PNG_URI))


def run_check(data, patch=None):
    """svg_check on a fresh file, optionally with zipfile._EndRecData swapped; returns (result, intact, calls)."""
    check = B.entry("svg_check")
    calls = []

    def action(fd, path):
        if patch is None:
            ctx = mock.patch.object(zipfile, "_EndRecData", zipfile._EndRecData)
        else:
            def failing(*_args, **_kwargs):
                calls.append(1)
                raise patch("injected by the test: ZIP check failed")
            ctx = mock.patch.object(zipfile, "_EndRecData", failing)
        with ctx:
            try:
                ok, reasons = check(fd)
                return (ok, reasons)
            except BaseException as exc:
                return f"ИСКЛЮЧЕНИЕ {type(exc).__name__}"
    result, intact = B.on_file(data, ".svg", action)
    return result, intact, calls


def zip_failure_group():
    print("== §4.2, круг 3 P1: сбой ZIP-проверки встроенной картинки — отказ, подлинное «не ZIP» — приём ==")
    for exc in (OSError, MemoryError):
        def body(exc=exc):
            result, intact, calls = run_check(PNG_DRAWING, patch=exc)
            # The swap has to be reached: a check that never looks for ZIP inside the PNG proves nothing here.
            good = intact and bool(calls) and not isinstance(result, str) and tuple(result) == (False, [B.PERR])
            return good, f"вызовов подмены {len(calls)}: {result if isinstance(result, str) else tuple(result)}"
        B.attempt(f"П1 р4: PNG-фикстура, zipfile._EndRecData бросает {exc.__name__}",
                  f"(False, ['{B.PERR}']) и подмена вызвана", body)

    def clean():
        result, intact, _ = run_check(PNG_DRAWING)
        return intact and not isinstance(result, str) and tuple(result) == (True, []), \
            (result if isinstance(result, str) else tuple(result))
    B.attempt("П1 р4 контроль: та же PNG-фикстура без подмены (подлинное «не ZIP»)", "(True, [])", clean)

    def real_zip():
        result, intact, _ = run_check(B.svg(B.image(B.png_uri(B.PNG_ZIP))))
        return intact and not isinstance(result, str) and tuple(result) == (False, [B.IMGD]), \
            (result if isinstance(result, str) else tuple(result))
    B.attempt("П1 р4 контроль: PNG с ZIP внутри без подмены", f"(False, ['{B.IMGD}'])", real_zip)


def root_ending_at(length):
    """Bytes whose root start tag ends with its '>' as byte number `length` of the file (1-based)."""
    head = f'<svg xmlns="{B.SVG_NS}" data-pad="'
    tail = '">'
    pad = length - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    data = (head + "a" * pad + tail + B.RECT + "</svg>\n").encode("utf-8")
    assert data.index(b">") + 1 == length, "fixture: '>' is not where the case says"
    return data


def window_group():
    print("== §4.1: тег корня, закончившийся на последнем байте окна 64 КиБ, — «да»; на байт дальше — «нет» ==")
    sniff = None
    for length, want in ((WINDOW - 1, True), (WINDOW, True), (WINDOW + 1, False)):
        def body(length=length, want=want):
            nonlocal sniff
            sniff = sniff or B.entry("sniff_svg_root")
            data = root_ending_at(length)
            result, intact = B.on_file(data, ".svg", lambda fd, path: sniff(fd))
            return intact and result is want, result
        B.attempt(f"§4.1 р4: «>» корня — байт {length} файла (окно {WINDOW})", str(want), body)


def main():
    if len(sys.argv) != 2:
        print("запуск: python3 tests/svg_r4_cases.py <путь к file_safety_check.py>")
        return 2
    with tempfile.TemporaryDirectory(prefix="svg-2411-r4-") as work:
        B.WORK = work
        # The gate journals every check; keep test runs out of the production journal.
        os.environ["FILE_SAFETY_JOURNAL"] = os.path.join(work, "journal.test.log")
        try:
            spec = importlib.util.spec_from_file_location("fsc_2411_r4", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            B.FSC = module
        except Exception as exc:
            B.LOAD_ERROR = f"{type(exc).__name__}: {exc}"
            print("✗ МОДУЛЬ НЕ ЗАГРУЖЕН:", B.LOAD_ERROR, "— все случаи ниже провалятся", flush=True)
        zip_failure_group()
        window_group()
    print(f"ИТОГО: {B.COUNT['total'] - B.COUNT['failed']}/{B.COUNT['total']}")
    return 1 if B.COUNT["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
