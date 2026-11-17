#!/usr/bin/env python3
"""#2411, редакция 3 требований: дополнение к tests/svg_cases.py (тот набор остаётся как есть,
кроме одного случая check_file, чьё ожидание редакция 3 меняет).

Запуск: python3 tests/svg_r3_cases.py <путь к file_safety_check.py>

Откуда случаи: независимое ревью круга 1 (границы аргумента url(), маршрут открытия, ошибка до
первого байта, журнал, код возврата 0) и подсадки в код, которые прежний набор не заметил
(UTF-16 без метки порядка байтов, голое http://, байты после первого IEND, лишнее дополнение base64).
Оракул написан по SPEC.md редакции 3 автором требований; код проверки писал другой исполнитель.
Обвязка и фикстуры берутся из svg_cases.py, лежащего рядом. Сверка с независимым разборщиком CSS
требует tinycss2: нет модуля — случай ПРОВАЛЕН, а не пропущен (пропуск прячет неисполнение).
check_file зовёт живой `file` и clamscan: статику проходят пять случаев этого набора.
"""
import hashlib
import importlib.util
import inspect
import json
import os
import random
import subprocess
import sys
import tempfile
import xml.parsers.expat
from unittest import mock
from xml.sax.saxutils import quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))


def load_base():
    spec = importlib.util.spec_from_file_location("svg_cases_base", os.path.join(HERE, "svg_cases.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_base()
NAME = "svg_name_not_svg_content"   # SPEC ed. 3 §4.1, the name rule
GZIP_REASON = "mime_not_whitelisted:application/gzip"
RECT1 = '<rect width="1" height="1" {attr}/>'


def check_case(name, data, want):
    """svg_check on a fresh file; want is an exact reason list or a predicate with its text."""
    def body():
        (ok, reasons), intact = B.on_file(data, ".svg", lambda fd, path: B.entry("svg_check")(fd))
        good = want[0](ok, reasons) if isinstance(want, tuple) else (reasons == want and ok == (not want))
        return good and intact, f"({ok}, {B.tail(reasons)})" + ("" if intact else " ФАЙЛ ИЗМЕНЁН")
    B.attempt(name, want[1] if isinstance(want, tuple) else f"({not want}, {want})", body)


# ================================================================ rule 16, ed. 3
# Whitespace that CSS does not treat as whitespace: inside url() it stays in the address.
ODD_SPACES = [("U+00A0", " "), ("U+2003", " "), ("U+3000", "　"), ("U+0085", "\u0085"),
              ("U+2028", " "), ("U+202F", " "), ("U+1680", " ")]
# Every place of the argument; the attribute is delimited by double quotes, so CSS quotes are single.
ODD_PLACES = [
    ("перед # без кавычек", "url({c}#g)"),
    ("внутри имени", "url(#a{c}b)"),
    ("в конце имени без кавычек", "url(#g{c})"),
    ("перед открывающей кавычкой", "url({c}'#g')"),
    ("после открывающей кавычки", "url('{c}#g')"),
    ("в конце имени в кавычках", "url('#g{c}')"),
    ("после закрывающей кавычки", "url('#g'{c})"),
]
CSS_REJECT = [
    ("скобка в имени без кавычек: url(#aurl(#b))", "url(#aurl(#b))"),
    ("скобка в имени без кавычек: url(#aurl(https://…))", "url(#aurl(https://evil.example/x))"),
    ("скобка в имени без кавычек: url(#a(b)", "url(#a(b)"),
    ("скобка в имени в кавычках: url('#a(b')", "url('#a(b')"),
    ("скобка в имени в кавычках: url('#url(sample')", "url('#url(sample')"),
    ("комментарий CSS перед #: url(/**/#g)", "url(/**/#g)"),
    ("пустой аргумент url()", "url()"),
    ("только пробел: url( )", "url( )"),
    ("относительный адрес url(g)", "url(g)"),
    ("разные кавычки url('#g&quot;)", "url('#g&quot;)"),
    ("пробел CSS после открывающей кавычки url(' #g')", "url(' #g')"),
    ("пробел CSS перед закрывающей кавычкой url('#g ')", "url('#g ')"),
    ("табуляция внутри имени url(#a&#9;b)", "url(#a&#9;b)"),
    ("внутренний, затем наружу после неразрывного пробела", "url(#g) url( #h)"),
]
CSS_PASS = [
    ("пробелы по краям: url( #g )", "url( #g )"),
    ("табуляция по краям: url(&#9;#g&#9;)", "url(&#9;#g&#9;)"),
    ("перевод строки по краям: url(&#10;#g&#10;)", "url(&#10;#g&#10;)"),
    ("возврат каретки по краям: url(&#13;#g&#13;)", "url(&#13;#g&#13;)"),
    ("смесь пробелов CSS вокруг кавычек", "url( &#9;&#10;'#g'&#13; )"),
    ("URL( заглавными с внутренним адресом", "URL( #g )"),
    ("два внутренних адреса подряд", "url(#a) url( '#b' )"),
    ("неразрывный пробел ВНЕ url() — не аргумент", "url(#g) "),
]


def css_group():
    print("== правило 16, редакция 3: границы аргумента url() и состав имени ==")
    for code, char in ODD_SPACES:
        for place, template in ODD_PLACES:
            value = template.format(c=char)
            check_case(f"П16р3 {code} {place}", B.svg(RECT1.format(attr=f'fill="{value}"')), B.W(B.CSSX))
    for code, char in ODD_SPACES[:2]:
        check_case(f"П16р3 style: {code} перед # без кавычек",
                   B.svg(f"<style>rect{{fill:url({char}#g)}}</style>"), B.W(B.CSSX))
        check_case(f"П16р3 style: {code} после закрывающей кавычки",
                   B.svg(f"<style>rect{{fill:url('#g'{char})}}</style>"), B.W(B.CSSX))
    for label, value in CSS_REJECT:
        check_case(f"П16р3 {label}", B.svg(RECT1.format(attr=f'fill="{value}"')), B.W(B.CSSX))
    for label, value in CSS_PASS:
        check_case(f"П16р3 сосед: {label}", B.svg(RECT1.format(attr=f'fill="{value}"')), [])


# ---------------------------------------------------------------- independent CSS tokenizer
# A reading of ed. 3 to the letter accepts 741 of these samples; the floor only guards against an
# oracle that has gone blind (nothing accepted means nothing compared).
DIFF_SEED, DIFF_SAMPLES, DIFF_MIN_ACCEPTED = 2411, 24000, 600
JUNK = ["fill:", ";", ",", " ", "a", "#a", "/", "/*", "*/", "https://e.example/x", "x.png", "'", '"', "(", ")"]
INNER = [" ", "\t", "\n", "\r", " ", " ", "　", "\u0085", "'", '"', "#", "#a", "a", "b", "(",
         "/", "/*", "*/", "//e.example/x", "url("]


def external_urls(tinycss2, value):
    """Addresses a CSS parser would take from the value and that are not pure #fragments."""
    found = []

    def walk(tokens):
        for token in tokens:
            if token.type == "url":
                found.append(token.value)
            elif token.type == "function":
                if token.lower_name == "url":
                    args = [a for a in token.arguments if a.type not in ("whitespace", "comment")]
                    if args and args[0].type == "string":
                        found.append(args[0].value)
                walk(token.arguments)
            elif hasattr(token, "content"):
                walk(token.content)

    walk(tinycss2.parse_component_value_list(value))
    return [u for u in found if not u.startswith("#")]


def css_diff_group():
    print("== правило 16: сверка принятых значений с независимым разборщиком CSS (tinycss2) ==")
    name = (f"П16р3 СВЕРКА: {DIFF_SAMPLES} значений с url( (затравка {DIFF_SEED}) — у принятого гейтом "
            "значения tinycss2 не видит ни одного адреса, кроме #имя")
    want = f"расхождений 0, принято не меньше {DIFF_MIN_ACCEPTED}"

    def body():
        try:
            import tinycss2
        except ImportError as exc:
            return False, f"НЕТ tinycss2 ({exc}) — сверка не выполнена"
        check = B.entry("svg_check")
        rng = random.Random(DIFF_SEED)
        path = os.path.join(B.WORK, "diff.svg")
        accepted, strict, bad = 0, 0, []
        for _ in range(DIFF_SAMPLES):
            pre = "".join(rng.choice(JUNK) for _ in range(rng.randint(0, 2)))
            mid = "".join(rng.choice(INNER) for _ in range(rng.randint(0, 5)))
            post = "".join(rng.choice(JUNK) for _ in range(rng.randint(0, 2)))
            value = pre + rng.choice(["url(", "URL(", "url( "]) + mid + rng.choice([")", " )", ""]) + post
            with open(path, "wb") as stream:
                stream.write(B.svg(f"<rect width=\"1\" height=\"1\" fill={quoteattr(value)}/>"))
            fd = os.open(path, os.O_RDONLY)
            try:
                ok, reasons = check(fd)
            finally:
                os.close(fd)
            outside = external_urls(tinycss2, value)
            if ok:
                accepted += 1
                if outside:
                    bad.append((value, outside))
            elif not outside:
                strict += 1
        os.unlink(path)
        good = not bad and accepted >= DIFF_MIN_ACCEPTED
        return good, (f"расхождений {len(bad)}, принято {accepted}, отвергнуто строже tinycss2 {strict}"
                      + (f"; первое: {bad[0][0]!r} → tinycss2 видит {bad[0][1]!r}" if bad else ""))
    B.attempt(name, want, body)


# ================================================================ rules 4а, 11, 14 — holes the mutants found
def encoding_group():
    print("== правило 4а: UTF-16 без метки порядка байтов ==")
    text = B.svg_str(B.RECT)
    either = (lambda ok, reasons: ok is False and reasons in ([B.MAL], [B.ENC]),
              f"(False, ['{B.MAL}']) или (False, ['{B.ENC}'])")
    check_case("П4а чистый SVG в UTF-16LE без метки и без объявления", text.encode("utf-16-le"), either)
    check_case("П4а чистый SVG в UTF-16BE без метки и без объявления", text.encode("utf-16-be"), either)


LINK_BARE = ['href="http://"', 'href="https://"', 'href="HTTP://"', 'href="  https://  "', 'xlink:href="http://"']


def link_group():
    print("== правило 11: после // обязан быть хотя бы один знак ==")
    for attr in LINK_BARE:
        check_case(f"П11р3 a {attr}", B.svg(f"<a {attr}>{B.RECT}</a>"), B.W(B.LINK))
    check_case("П11р3 сосед: a href=\"http://a\"", B.svg(f'<a href="http://a">{B.RECT}</a>'), [])


PNG_NOPAD = next(p for p in (B.with_chunk(B.PNG, b"tEXt", b"Comment\x00" + b"x" * n) for n in range(3))
                 if len(p) % 3 == 0)
NOPAD64 = B.B64(PNG_NOPAD)
IMG_R3 = [
    ("PNG: второй IEND сразу за первым (конец — ровно IEND)", B.png_uri(B.PNG + B.IEND)),
    ("PNG: блок tEXt и второй IEND после первого IEND",
     B.png_uri(B.PNG + B.png_chunk(b"tEXt", b"Comment\x00hidden") + B.IEND)),
    ("PNG: zip и второй IEND после первого IEND", B.png_uri(B.PNG + B.ZIP + B.IEND)),
    ("PNG: лишний = при длине, кратной четырём", "data:image/png;base64," + NOPAD64 + "="),
    ("PNG: лишние == при длине, кратной четырём", "data:image/png;base64," + NOPAD64 + "=="),
    ("PNG: лишние ===", "data:image/png;base64," + NOPAD64 + "==="),
    ("PNG: лишние ====", "data:image/png;base64," + NOPAD64 + "===="),
    ("PNG: третий = после законных ==", "data:image/png;base64," + B.B64(B.PNG_PAD) + "="),
    ("PNG: = в начале данных", "data:image/png;base64,=" + NOPAD64),
]


def image_group():
    print("== правило 14: первый IEND — последний; строгий base64 ==")
    for label, uri in IMG_R3:
        check_case(f"П14р3 image {label}", B.svg(B.image(uri)), B.W(B.IMGD))
    check_case("П14р3 сосед: PNG без дополнения (длина base64 кратна четырём)", B.svg(B.image(B.png_uri(PNG_NOPAD))), [])


# ================================================================ svg_check never raises (§4.2)
def boom(*_args, **_kwargs):
    raise MemoryError("injected by the test")


def robustness_group():
    print("== §4.2: ошибка самой проверки — отказ svg_parse_error, а не исключение ==")
    allowed = ((True, []), (False, [B.PERR]))

    def body():
        fsc = B.FSC
        if fsc is None:
            raise B.Missing(f"модуль не загружен ({B.LOAD_ERROR})")
        check = B.entry("svg_check")
        # Every class the module defines is swapped, one at a time, for a constructor that fails:
        # whichever of them holds the scan state, its failure must end as a refusal.
        targets = [(fsc, n) for n, o in sorted(vars(fsc).items())
                   if inspect.isclass(o) and getattr(o, "__module__", None) == fsc.__name__]
        targets += [(xml.parsers.expat, "ParserCreate"), (os, "pread")]
        outcomes, good = [], True
        for owner, attr in targets:
            def action(fd, path, owner=owner, attr=attr):
                with mock.patch.object(owner, attr, boom):
                    try:
                        ok, reasons = check(fd)
                        return (ok, reasons)
                    except BaseException as exc:
                        return f"ИСКЛЮЧЕНИЕ {type(exc).__name__}"
            result, intact = B.on_file(B.INKSCAPE, ".svg", action)
            fine = intact and not isinstance(result, str) and tuple(result) in allowed
            good &= fine
            outcomes.append(f"{attr}: {result if isinstance(result, str) else tuple(result)}")
        return good and bool(targets), "; ".join(outcomes)
    B.attempt("§4.2 р3: подмена каждого класса модуля, ParserCreate и os.pread на бросающий MemoryError",
              f"каждый исход — (True, []) или (False, ['{B.PERR}']), исключений наружу нет", body)


# ================================================================ check_file: the name rule (§4.1, §4.5, §4.6)
SCRIPT_SVG = B.svg_str("<script>alert(1)</script>" + B.RECT)
HID_DOCTYPE = ("<!-- diagram -->\n" + B.ENTITY_X + SCRIPT_SVG).encode("utf-8")
HID_FAR = ('<?xml version="1.0"?>\n<!-- ' + "x" * 70000 + " -->\n" + SCRIPT_SVG).encode("utf-8")
HID_SJIS = ('<?xml version="1.0" encoding="Shift_JIS"?>\n<!-- ' + "x" * 5000 + " -->\n" + SCRIPT_SVG).encode("ascii")
COMMENT_SCRIPT = B.svg("<script>alert(1)</script>" + B.RECT, pre="<!-- diagram -->\n")
COMMENT_CLEAN = B.svg(B.RECT, pre="<!-- diagram -->\n")
# (name, bytes, file name, what live `file` must say, label mime, static, verdict, exact reasons or None, check, text)
NAME_CASES = [
    ("ИМЯ: корень спрятан за комментарием и DOCTYPE x, внутри script, имя h.svg", HID_DOCTYPE, "h.svg",
     "text/html", "text/html", "reject", "reject", [NAME], None, ""),
    ("ИМЯ: то же, имя H.SVG заглавными", HID_DOCTYPE, "H.SVG", "text/html", "text/html", "reject", "reject", [NAME], None, ""),
    ("ИМЯ: то же, имя h.svg. с точкой в конце", HID_DOCTYPE, "h.svg.", "text/html", "text/html", "reject", "reject",
     [NAME], None, ""),
    ("ИМЯ: то же, имя «h.svg » с пробелом в конце", HID_DOCTYPE, "h.svg ", "text/html", "text/html", "reject", "reject",
     [NAME], None, ""),
    ("ИМЯ: то же, имя «h.Svg. .» — точки и пробелы вперемешку", HID_DOCTYPE, "h.Svg. .", "text/html", "text/html",
     "reject", "reject", [NAME], None, ""),
    ("ИМЯ: корень дальше 64 КиБ, внутри script, имя far.svg", HID_FAR, "far.svg", "text/xml", "text/xml", "reject",
     "reject", [NAME], None, ""),
    ("ИМЯ: объявлена Shift_JIS (expat не разбирает), внутри script, имя sjis.svg", HID_SJIS, "sjis.svg", "text/xml",
     "text/xml", "reject", "reject", [NAME], None, ""),
    ("ИМЯ: настоящий PNG с именем p.svg", B.PNG, "p.svg", "image/png", "image/png", "reject", "reject", [NAME], None, ""),
    ("ИМЯ: обычный текст с именем notes.svg", B.TEXT, "notes.svg", "text/plain", "text/plain", "reject", "reject",
     [NAME], None, ""),
    ("ИМЯ, ПОРЯДОК: gzip с именем z.svg — причина типа, потом причина имени", B.SVGZ, "z.svg", "application/gzip",
     "application/gzip", "reject", "reject", [GZIP_REASON, NAME], None, ""),
    ("МАРШРУТ ПО КОРНЮ, не по имени: SVG со script и комментарием, имя x.txt", COMMENT_SCRIPT, "x.txt", "text/html",
     B.SVG_MIME, "reject", "reject", B.W(B.ELEM), None, ""),
    # ---- pass the static gate, so clamscan runs
    ("ПРЕДЕЛ §4.5: спрятанный корень с именем h.svg.txt идёт прежним маршрутом текста", HID_DOCTYPE, "h.svg.txt",
     "text/html", "text/html", "ok", "quarantine", None, B.no_svg_reasons, "нет причин svg_*"),
    ("ПРЕДЕЛ §4.5: спрятанный корень с именем hsvg (без точки) идёт прежним маршрутом текста", HID_DOCTYPE, "hsvg",
     "text/html", "text/html", "ok", "quarantine", None, B.no_svg_reasons, "нет причин svg_*"),
    ("ИМЯ: чистый SVG с именем ok.SVG — правило ничего не добавляет", B.SIMPLE, "ok.SVG", B.SVG_MIME,
     B.SVG_MIME, "ok", "quarantine", [], None, ""),
    ("МАРШРУТ ПО КОРНЮ: чистый SVG с комментарием и именем drawing.txt идёт как SVG", COMMENT_CLEAN, "drawing.txt",
     "text/plain", B.SVG_MIME, "ok", "quarantine", [], None, ""),
]


def last_journal_record():
    with open(os.environ["FILE_SAFETY_JOURNAL"], encoding="utf-8") as stream:
        lines = [line for line in stream if line.strip()]
    return json.loads(lines[-1])


def journal_matches(label, path):
    """§4.4: the journal line of this check carries the label's own reasons, verdict and digest."""
    record = last_journal_record()
    same = (record.get("file") == path and record.get("reasons") == label.get("static_reasons")
            and record.get("verdict") == label.get("verdict") and record.get("sha256") == label.get("sha256"))
    return same, "" if same else f" ЖУРНАЛ: {B.tail(record)}"


def name_group():
    print("== check_file: правило имени, маршрут по корню, журнал ==")
    for (name, data, filename, file_mime, want_mime, want_static, want_verdict,
         want_reasons, check, check_text) in NAME_CASES:
        def body(data=data, filename=filename, file_mime=file_mime, want_mime=want_mime, want_static=want_static,
                 want_verdict=want_verdict, want_reasons=want_reasons, check=check):
            path = os.path.join(B.WORK, filename)
            with open(path, "wb") as stream:
                stream.write(data)
            try:
                live = B.live_file_mime(path)
                if live != file_mime:
                    return False, f"ПРЕДУСЛОВИЕ: file назвал {live}, фикстура рассчитана на {file_mime}"
                before = hashlib.sha256(data).hexdigest()
                label = B.entry("check_file")(path)
                with open(path, "rb") as stream:
                    after = hashlib.sha256(stream.read()).hexdigest()
                journal_ok, journal_note = journal_matches(label, path)
            finally:
                for leftover in (path, path + ".safety.json"):
                    if os.path.exists(leftover):
                        os.unlink(leftover)
            reasons = label.get("static_reasons")
            good = after == before and label.get("sha256") == before and label.get("mime") == want_mime
            good &= label.get("static") == want_static and label.get("verdict") == want_verdict
            good &= want_reasons is None or reasons == want_reasons
            good &= check is None or check(reasons)
            good &= journal_ok
            notes = ("" if after == before else " ФАЙЛ ИЗМЕНЁН") + journal_note
            return good, (f"mime={label.get('mime')} static={label.get('static')} verdict={label.get('verdict')} "
                          f"reasons={B.tail(reasons)}{notes}")
        parts = [f"mime={want_mime} static={want_static} verdict={want_verdict}"]
        if want_reasons is not None:
            parts.append(f"reasons={want_reasons}")
        if check_text:
            parts.append(check_text)
        parts.append("журнал несёт те же причины; файл цел")
        B.attempt(name, " ".join(parts), body)

    def oversize():
        limit = B.entry("MAX_FILE_SIZE_BYTES")
        path = os.path.join(B.WORK, "big.svg")
        with open(path, "wb") as stream:
            stream.truncate(limit + 1)   # sparse: the size alone decides, nothing is read
        try:
            label = B.entry("check_file")(path)
            journal_ok, journal_note = journal_matches(label, path)
        finally:
            for leftover in (path, path + ".safety.json"):
                if os.path.exists(leftover):
                    os.unlink(leftover)
        reasons = label.get("static_reasons")
        good = (label.get("static") == "reject" and label.get("verdict") == "reject" and isinstance(reasons, list)
                and len(reasons) == 1 and str(reasons[0]).startswith("size_") and journal_ok)
        return good, f"mime={label.get('mime')} static={label.get('static')} reasons={B.tail(reasons)}{journal_note}"
    B.attempt("ИМЯ: файл больше предела размера с именем big.svg — тип не определён, правило имени молчит",
              "static=reject, одна причина size_…, причины имени нет", oversize)


# ================================================================ worker from the command line (§4.4)
def run_worker(path):
    proc = subprocess.run([sys.executable, "-I", sys.argv[1], path], capture_output=True, text=True,
                          timeout=300, env=os.environ.copy())
    try:
        return proc.returncode, json.loads(proc.stdout), ""
    except ValueError:
        return proc.returncode, {}, f" stdout не JSON: {proc.stdout[:120]!r} stderr: {proc.stderr[-160:]!r}"


def worker_group():
    print("== §4.4: воркер из командной строки — принятый SVG, код 0 ==")

    def accepted():
        if B.FSC is None:
            raise B.Missing(f"модуль не загружен ({B.LOAD_ERROR})")
        svg_path, txt_path = os.path.join(B.WORK, "accepted.svg"), os.path.join(B.WORK, "accepted.txt")
        with open(svg_path, "wb") as stream:
            stream.write(B.INKSCAPE)
        with open(txt_path, "wb") as stream:
            stream.write(B.TEXT)
        code_svg, label_svg, note_svg = run_worker(svg_path)
        on_disk = {}
        if os.path.exists(svg_path + ".safety.json"):
            with open(svg_path + ".safety.json", encoding="utf-8") as stream:
                on_disk = json.load(stream)
        code_txt, label_txt, note_txt = run_worker(txt_path)
        good = (code_svg == 0 and code_txt == 0 and label_svg.get("verdict") == "quarantine"
                and label_svg.get("mime") == B.SVG_MIME and label_svg.get("static") == "ok"
                and label_svg.get("static_reasons") == [] and label_svg.get("av") == "clean"
                and label_svg.get("av") == label_txt.get("av") and on_disk == label_svg
                and label_svg.get("sha256") == hashlib.sha256(B.INKSCAPE).hexdigest())
        return good, (f"SVG: код {code_svg} verdict={label_svg.get('verdict')} mime={label_svg.get('mime')} "
                      f"av={label_svg.get('av')} метка на диске равна stdout={on_disk == label_svg}{note_svg}; "
                      f"текст: код {code_txt} av={label_txt.get('av')}{note_txt}")
    B.attempt("§4.4 р3 воркер: образец Inkscape — код 0, quarantine, av=clean как у текста, метка на диске та же",
              f"SVG: код 0 verdict=quarantine mime={B.SVG_MIME} av=clean; текст: код 0 av=clean", accepted)

    def hidden():
        if B.FSC is None:
            raise B.Missing(f"модуль не загружен ({B.LOAD_ERROR})")
        path = os.path.join(B.WORK, "hidden.svg")
        with open(path, "wb") as stream:
            stream.write(HID_DOCTYPE)
        code, label, note = run_worker(path)
        good = (code == 1 and label.get("verdict") == "reject" and label.get("static_reasons") == [NAME]
                and label.get("av") == "SKIPPED:static_reject")
        return good, (f"код {code} verdict={label.get('verdict')} mime={label.get('mime')} "
                      f"reasons={B.tail(label.get('static_reasons'))} av={label.get('av')}{note}")
    B.attempt("§4.4 р3 воркер: спрятанный корень с именем hidden.svg — код 1, причина имени, антивирус не запускался",
              f"код 1 verdict=reject reasons={[NAME]} av=SKIPPED:static_reject", hidden)


def main():
    if len(sys.argv) != 2:
        print("запуск: python3 tests/svg_r3_cases.py <путь к file_safety_check.py>")
        return 2
    with tempfile.TemporaryDirectory(prefix="svg-2411-r3-") as work:
        B.WORK = work
        # The gate journals every check; keep test runs out of the production journal.
        os.environ["FILE_SAFETY_JOURNAL"] = os.path.join(work, "journal.test.log")
        try:
            spec = importlib.util.spec_from_file_location("fsc_2411_r3", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            B.FSC = module
        except Exception as exc:
            B.LOAD_ERROR = f"{type(exc).__name__}: {exc}"
            print("✗ МОДУЛЬ НЕ ЗАГРУЖЕН:", B.LOAD_ERROR, "— все случаи ниже провалятся", flush=True)
        css_group()
        css_diff_group()
        encoding_group()
        link_group()
        image_group()
        robustness_group()
        name_group()
        worker_group()
    print(f"ИТОГО: {B.COUNT['total'] - B.COUNT['failed']}/{B.COUNT['total']}")
    return 1 if B.COUNT["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
