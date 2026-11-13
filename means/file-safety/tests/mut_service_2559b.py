"""Порчи file_safety_service/file_safety_client: каждая должна уронить хотя бы один тест."""
import os, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = {"svc": HERE.parent / "code/file_safety_service.py", "cli": HERE.parent / "code/file_safety_client.py"}
MUTS = [
    ("svc", "bool как размер", "or isinstance(size, bool) ", ""),
    ("svc", "длина заголовка не мерится", ' or len(line) > HEADER_MAX', ""),
    ("svc", "file не подменён на name", '        label["file"] = name\n', ""),
    ("svc", "короткое тело — молча", '                raise ProtocolError("short_body")', "                break"),
    ("svc", "предел размера снят", "        if size > fsc.MAX_FILE_SIZE_BYTES:\n", "        if False:\n"),
    ("svc", "частный каталог не убран", "            shutil.rmtree(workdir, ignore_errors=True)", "            pass"),
    ("svc", "тишина сети — запрет", '        return "timeout"', '        return "denied:timeout"'),
    ("svc", "сбой разбора — пустой ответ", '_reply({"error": "check_failed:%s" % type(e).__name__})', "_reply({})"),
    ("svc", "mime_extra не доходит", 'os.environ["FILE_SAFETY_MIME_EXTRA"] = ",".join(extra)', "pass"),
    ("svc", "относительный путь пробы", ' and p.startswith("/")', ""),
    ("cli", "проба: uid не сверяется", ' and out.get("uid") != os.getuid()', ""),
    ("cli", "check: ошибка = успех", ' and "error" not in out', ""),
]


def main():
    caught = 0
    for key, name, old, new in MUTS:
        f = FILES[key]
        orig = f.read_text(encoding="utf-8")
        if orig.count(old) != 1:
            print("%s: НЕ ПРИМЕНИМА (вхождений %d)" % (name, orig.count(old)))
            continue
        backup = f.with_suffix(".py.orig-mut")
        shutil.copy2(f, backup)
        try:
            f.write_text(orig.replace(old, new), encoding="utf-8")
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                                str(HERE / "test_service_2559b.py")], capture_output=True, text=True, timeout=300,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        finally:
            shutil.move(backup, f)
        ok = r.returncode != 0
        caught += ok
        print("%s: %s (%s)" % (name, "ПОЙМАНА" if ok else "ВЫЖИЛА", r.stdout.strip().splitlines()[-1]))
    print("ИТОГ: поймано %d из %d" % (caught, len(MUTS)))
    return 0 if caught == len(MUTS) else 1


if __name__ == "__main__":
    sys.exit(main())
