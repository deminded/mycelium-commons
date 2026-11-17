#!/usr/bin/env python3
"""#2411 §4.3: метка image/svg+xml уходит на смысловой разбор как текст, без конвертации;
подсказка о форматах при отказе по типу называет image/svg+xml.
Редакция 3 требований (после ревью круга 1 и подсадок): в очередь разбора попадают те же байты; прочие
image/… по-прежнему SKIPPED:non_textual; подсказка называет весь белый список, а не один новый тип.

Запуск: python3 tests/intake_svg_cases.py [путь к intake_poller.py]
По умолчанию — code/intake_poller.py рядом с tests/. Без сети, токена, fs-triage и боевых каталогов:
приёмник грузится и подменяется на тех же границах, что в intake_2387_cases.py и md_convert_cases.py.
Оракул написан по SPEC.md без чтения кода приёмника. Случай КОНТРОЛЬ (text/plain) проверяет саму
обвязку: если он красный, красный и SVG-случай ничего не говорит о §4.3.
"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "code/intake_poller.py"
SVG_MIME = "image/svg+xml"
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><text>x</text></svg>\n'


def load():
    # The poller reads its production token at import; only that read is replaced.
    spec = importlib.util.spec_from_file_location("intake_svg_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch("builtins.open", return_value=io.StringIO("test-token")):
        spec.loader.exec_module(module)
    return module


def triage_case(mime, skipped=False):
    """Hand-off model of md_convert_cases.intake_case: the worker answers at once in the outbox.
    skipped=True expects no hand-off at all: the type stays out of the semantic reading."""
    intake = load()
    with tempfile.TemporaryDirectory(prefix="intake-2411-") as tmp:
        root = Path(tmp)
        inbox, outbox = root / "inbox", root / "outbox"
        inbox.mkdir()
        outbox.mkdir()
        intake.TRIAGE_INBOX, intake.TRIAGE_OUTBOX = str(inbox), str(outbox)
        # Bounded wait: a hand-off the model misses must fail in seconds, not hang the run.
        intake.TRIAGE_WAIT_SECONDS, intake.TRIAGE_POLL = 2, 0.01
        qpath = root / "quarantined.svg"
        qpath.write_bytes(SVG_BYTES)
        actual_rename = os.rename
        kinds, queued = [], []

        def handoff(src, dest, **kwargs):
            if str(dest) == qpath.name and kwargs.get("dst_dir_fd") is not None:
                # A ".kind" file next to the task is how conversion is ordered (md_convert_cases.py).
                kinds.append((inbox / ("." + qpath.name + ".kind")).exists())
                actual_rename(src, dest, **kwargs)
                # Ed. 3: what the worker will read is the file as it landed in the queue, not the source.
                task_fd = os.open(dest, os.O_RDONLY, dir_fd=kwargs["dst_dir_fd"])
                try:
                    queued.append(os.read(task_fd, 1 << 20))
                finally:
                    os.close(task_fd)
                (outbox / (qpath.name + ".json")).write_text(json.dumps({"summary": "ok", "flags": []}))
            else:
                actual_rename(src, dest, **kwargs)

        with mock.patch.object(intake.os, "rename", side_effect=handoff), \
                mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())), \
                mock.patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            result = intake.triage(str(qpath), mime)
        intact = qpath.read_bytes() == SVG_BYTES
        archive = (root / (qpath.name + ".md.zip")).exists()
    if skipped:
        good = kinds == [] and result == {"status": "SKIPPED:non_textual"} and intact and not archive
    else:
        good = (kinds == [False] and isinstance(result, dict) and result.get("summary") == "ok"
                and result.get("status") != "SKIPPED:non_textual" and "converted" not in result
                and intact and not archive and queued == [SVG_BYTES])
    got = (f"передач={len(kinds)} kind={kinds} результат={result!r} исходник цел={intact} md.zip={archive} "
           f"байты в очереди равны исходным={queued == [SVG_BYTES]}")
    return good, got


# The gate's whitelist as it stood on main before #2411, in the hint's own spelling ("text/" is shown as "text/*").
OLD_KINDS = ["text/*", "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "application/json",
             "application/zip", "application/x-mimearchive", "message/rfc822",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]


def hint_case(full=False):
    """Reject by type through handle_file; the hint itself is not replaced.
    full=True: the hint still names every type the gate accepted before SVG was added."""
    module = load()
    with tempfile.TemporaryDirectory(prefix="intake-2411-hint-") as tmp:
        root = Path(tmp)
        for attr in ("INCOMING", "QUARANTINE"):
            (root / attr).mkdir()
            setattr(module, attr, str(root / attr))
        module.OFFSET_FILE = str(root / "offset")
        module.GROUP_ID = -100123
        message = {"chat": {"id": module.GROUP_ID}, "message_id": 7, "message_thread_id": 91,
                   "document": {"file_id": "file-id", "file_name": "x.bin", "file_size": 4}}
        label = {"verdict": "reject", "av": "clean", "mime": "application/x-test", "sha256": "abc",
                 "static_reasons": ["mime_not_whitelisted:application/x-test"]}
        texts = []

        def api(method, **params):
            if method == "sendMessage":
                texts.append(params.get("text", ""))
            return {"ok": True}

        with mock.patch.object(module, "download", side_effect=lambda file_id, path: Path(path).write_bytes(b"data")), \
                mock.patch.object(module, "triage", return_value={"status": "SKIPPED:test"}), \
                mock.patch.object(module, "api", side_effect=api), \
                mock.patch.object(module.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(label))), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                redirect_stdout(io.StringIO()):
            module.handle_file(message)
    hinted = [t for t in texts if "Принимаются" in t]
    wanted = OLD_KINDS if full else [SVG_MIME]
    missing = [kind for kind in wanted if not all(kind in t for t in hinted)]
    good = bool(hinted) and not missing
    return good, (f"сообщений={len(texts)} с «Принимаются»={len(hinted)} не названы={missing} "
                  f"текст={(hinted or texts or [''])[0][:400]!r}")


CASES = [
    ("КОНТРОЛЬ обвязки: text/plain уходит на разбор, kind не пишется", lambda: triage_case("text/plain"),
     "одна передача без kind, summary=ok, не SKIPPED:non_textual, без converted и md.zip"),
    ("§4.3: image/svg+xml уходит на разбор как текст, конвертация не назначена", lambda: triage_case(SVG_MIME),
     "одна передача без kind, summary=ok, не SKIPPED:non_textual, без converted и md.zip"),
    ("§4.3: подсказка «Принимаются: …» при отказе по типу называет image/svg+xml", hint_case,
     "в сообщении с «Принимаются» есть image/svg+xml"),
    ("§4.3 р3: подсказка называет и все прежние типы белого списка", lambda: hint_case(full=True),
     "в сообщении с «Принимаются» названы все одиннадцать прежних типов"),
] + [
    (f"§4.3 р3: {mime} по-прежнему не идёт на смысловой разбор", lambda mime=mime: triage_case(mime, skipped=True),
     "передач нет, результат {'status': 'SKIPPED:non_textual'}")
    for mime in ("image/png", "image/jpeg", "image/gif", "image/webp")
]


def main():
    failed = 0
    for name, case, want in CASES:
        try:
            good, got = case()
        except Exception as exc:
            good, got = False, f"ИСКЛЮЧЕНИЕ {type(exc).__name__}: {str(exc)[:300]}"
        failed += not good
        print("✓" if good else "✗", name, "| получено:", got, "| ждали:", want, flush=True)
    print(f"ИТОГО: {len(CASES) - failed}/{len(CASES)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
