#!/usr/bin/python3
"""One attachment check behind a systemd socket (#2559, part 2).

ЗАЧЕМ. Разбор вложения (libmagic, zipfile/XML, libclamav) — самый вероятный вход для
вредоносного файла от постороннего. Под учёткой агента эксплойт получил бы её дом:
токены ботов, ключи, сессию Telegram. Здесь тот же file_safety_check идёт в экземпляре
file-safety@.service: динамический uid, без /home, без сети, с частным /tmp и сроком
жизни (../deploy/). Агент передаёт байты, а не путь: к его каталогам у службы доступа
нет — и так должно быть.

Протокол: одно соединение — один файл.
  запрос: строка JSON до HEADER_MAX байт с "\\n" в конце
            {"v": 1, "name": "<путь у агента>", "size": N, "mime_extra": ["audio/ogg", ...]}
          и ровно N байт следом;
          или {"v": 1, "probe": "isolation", "paths": [...]} — самопроверка песочницы.
  ответ:  одна строка JSON — ярлык file_safety_check, где "file" = name;
          {"error": "..."} — нарушение протокола или сбой проверки.
Код возврата агенту не нужен: он читает только ответ.
"""
import errno
import json
import os
import re
import shutil
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HEADER_MAX = 4096
CHUNK = 1 << 16
NAME_MAX = 1024
MIME_EXTRA_MAX = 16
PROBE_PATHS_MAX = 8
# Адрес без DNS: проба должна мерить запрет сети, а не работу резолвера.
PROBE_CONNECT = ("1.1.1.1", 53)
MIME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


class ProtocolError(Exception):
    pass


def _reply(obj):
    out = sys.stdout.buffer
    out.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    out.flush()


def _read_header(inp):
    line = inp.readline(HEADER_MAX + 1)
    if not line.endswith(b"\n") or len(line) > HEADER_MAX:
        raise ProtocolError("header_unterminated_or_too_long")
    try:
        header = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("header_not_json")
    if not isinstance(header, dict) or header.get("v") != 1:
        raise ProtocolError("header_version")
    return header


def _validate_check(header):
    name, size = header.get("name"), header.get("size")
    if not isinstance(name, str) or not name or len(name) > NAME_MAX or "\0" in name:
        raise ProtocolError("bad_name")
    # bool — подкласс int: True не должно читаться как размер 1
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ProtocolError("bad_size")
    extra = header.get("mime_extra", [])
    if (not isinstance(extra, list) or len(extra) > MIME_EXTRA_MAX
            or not all(isinstance(m, str) and MIME_RE.match(m) for m in extra)):
        raise ProtocolError("bad_mime_extra")
    return name, size, extra


def _receive(inp, size, path):
    # O_EXCL|O_NOFOLLOW: каталог свежий и наш, но заготовленная ссылка не должна увести запись
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    left = size
    with os.fdopen(fd, "wb") as f:
        while left:
            chunk = inp.read(min(CHUNK, left))
            if not chunk:
                raise ProtocolError("short_body")
            f.write(chunk)
            left -= len(chunk)


def _attempt(action):
    try:
        action()
    except TimeoutError:
        # тишина — не запрет: пакет мог уйти и потеряться
        return "timeout"
    except OSError as e:
        return "denied:" + errno.errorcode.get(e.errno, str(e.errno))
    return "allowed"


def _list(path):
    os.listdir(path)


def _read(path):
    with open(path, "rb") as f:
        f.read(1)


def _write_in(directory):
    probe = os.path.join(directory, ".isolation-probe-%d" % os.getpid())
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.unlink(probe)


def _connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(3)
        s.connect(PROBE_CONNECT)
    finally:
        s.close()


def isolation_probe(header):
    paths = header.get("paths", [])
    if (not isinstance(paths, list) or len(paths) > PROBE_PATHS_MAX
            or not all(isinstance(p, str) and p.startswith("/") and "\0" not in p for p in paths)):
        raise ProtocolError("bad_probe_paths")
    checks = {"code_write": _attempt(lambda: _write_in(HERE)),
              "net_connect": _attempt(_connect)}
    for p in paths:
        checks["read:" + p] = _attempt(
            (lambda p=p: _list(p)) if os.path.isdir(p) else (lambda p=p: _read(p)))
    uid = os.getuid()
    return {"probe": "isolation", "uid": uid, "checks": checks,
            "isolated": uid != 0 and all(v.startswith("denied:") for v in checks.values())}


def main():
    inp = sys.stdin.buffer
    workdir = None
    try:
        header = _read_header(inp)
        if "probe" in header:
            if header["probe"] != "isolation":
                raise ProtocolError("unknown_probe")
            _reply(isolation_probe(header))
            return 0
        name, size, extra = _validate_check(header)
        workdir = tempfile.mkdtemp(prefix="file-safety-")
        # Журнал проверяльщика остаётся в частном /tmp экземпляра; журнал агента пишет агент.
        # Оба значения проверяльщик читает из окружения, поэтому — до импорта.
        os.environ["FILE_SAFETY_JOURNAL"] = os.path.join(workdir, "journal.log")
        os.environ["FILE_SAFETY_MIME_EXTRA"] = ",".join(extra)
        sys.path.insert(0, HERE)
        import file_safety_check as fsc
        if size > fsc.MAX_FILE_SIZE_BYTES:
            raise ProtocolError("size_exceeds_limit")
        path = os.path.join(workdir, "input")
        _receive(inp, size, path)
        label = fsc.check_file(path)
        label["file"] = name
        _reply(label)
        return 0
    except ProtocolError as e:
        _reply({"error": str(e)})
        return 2
    except Exception as e:
        # Сбой разбора — не «чисто»: агент получит ошибку и отправит файл в карантин.
        _reply({"error": "check_failed:%s" % type(e).__name__})
        return 1
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
