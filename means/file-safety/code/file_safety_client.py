#!/usr/bin/python3
"""Client of the socket check service (#2559, part 2): manual check and isolation probe.

ЗАЧЕМ. После установки юнитов мерить надо саму песочницу, а не верить юнит-файлу:
проба просит экземпляр службы прочесть пути агента и выйти в сеть и печатает, что ему
удалось. Пути — аргументами: в публичном коде им не место.

  file_safety_client.py probe [--socket S] --file F [--file F ...] [--unix S ...]
      rc 0 — изолирован; 1 — утечка или не доказано; 2 — контрольный путь недоступен мне самому
  file_safety_client.py check [--socket S] FILE      печатает ярлык; rc 0 — quarantine

Контрольные пути — те, что агент заведомо читает (файл секретов, свой дом) и с какими
сокетами заведомо соединяется: только тогда отказ внутри службы что-то доказывает.
"""
import argparse
import json
import os
import socket
import sys

SOCKET = "/run/file-safety.sock"
REPLY_MAX = 256 * 1024
# Служба гасит экземпляр на 160-й секунде (RuntimeMaxSec) и добивает через 5 с; клиенту ждать дольше.
TIMEOUT = 170
DYNAMIC_UIDS = range(61184, 65520)  # диапазон DynamicUser в systemd
HIDDEN = {"EACCES", "EPERM", "ENOENT", "ENOTDIR"}
UNIX_HIDDEN = HIDDEN | {"EAFNOSUPPORT"}
SOCKET_BLOCKED = {"EAFNOSUPPORT", "EPERM", "EACCES"}
WRITE_BLOCKED = {"EROFS", "EACCES", "EPERM"}


class NotConclusive(Exception):
    pass


def precheck(files, unix):
    """Проба доказывает только то, что агенту самому доступно: иначе ENOENT внутри — пустой звук."""
    if not files:
        raise NotConclusive("нужен хотя бы один контрольный файл (--file)")
    for p in files:
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            os.read(fd, 1)
            os.close(fd)
        except OSError as e:
            raise NotConclusive("контрольный файл недоступен мне самому: %s (%s)" % (p, e.strerror))
    for p in unix:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect(p)
        except OSError as e:
            raise NotConclusive("контрольный сокет недоступен мне самому: %s (%s)" % (p, e.strerror))
        finally:
            s.close()


def static_uids(path="/etc/passwd"):
    """uid статических учёток: попавший сюда uid экземпляра — не динамический, даже из диапазона."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {int(p[2]) for p in (line.split(":") for line in f) if len(p) >= 7 and p[2].isdigit()}
    except OSError:
        return None


def judge(reply, files, unix, static=None):
    res = reply.get("results") if isinstance(reply.get("results"), dict) else {}

    def kind(key, blocked):
        v = res.get(key)
        if v == "allowed":
            return "LEAK"
        if isinstance(v, str) and v.startswith("error:") and v[len("error:"):] in blocked:
            return "blocked"
        return "unproven"
    checks = {"code_write": kind("code_write", WRITE_BLOCKED),
              "inet": kind("inet", SOCKET_BLOCKED), "inet6": kind("inet6", SOCKET_BLOCKED)}
    for p in files:
        checks["file:" + p] = kind("file:" + p, HIDDEN)
    for p in unix:
        checks["unix:" + p] = kind("unix:" + p, UNIX_HIDDEN)
    uid = reply.get("uid")
    # статический пользователь с тем же именем занял бы место динамического — uid должен это показать
    checks["uid"] = ("blocked" if type(uid) is int and uid in DYNAMIC_UIDS and uid != os.getuid()
                     and static is not None and uid not in static else "unproven")
    return {"isolated": bool(files) and all(v == "blocked" for v in checks.values()), "checks": checks}


def exchange(sock_path, header, fd=None, size=0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(sock_path)
        try:
            s.sendall(json.dumps(header).encode() + b"\n")
            left = size
            while left:
                chunk = os.read(fd, min(65536, left))
                if not chunk:
                    raise ValueError("file shrank while sending")
                s.sendall(chunk)
                left -= len(chunk)
            s.shutdown(socket.SHUT_WR)
        except BrokenPipeError:
            # the service may refuse early (size limit) and close: its reply is still worth reading
            pass
        reply = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            reply += chunk
            if len(reply) > REPLY_MAX:
                raise ValueError("reply longer than limit")
    finally:
        s.close()
    if not reply:
        raise ValueError("service closed the connection without a reply")
    return json.loads(reply)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "check"])
    ap.add_argument("--socket", default=SOCKET)
    ap.add_argument("--mime-extra", default="", help="comma-separated, check only")
    ap.add_argument("--file", action="append", default=[], help="probe: control file the agent can read")
    ap.add_argument("--unix", action="append", default=[], help="probe: unix socket the agent can connect to")
    ap.add_argument("args", nargs="*")
    # options may stand between the command and its paths
    a = ap.parse_intermixed_args(argv)
    if a.cmd == "probe":
        if a.args:
            ap.error("probe takes paths only via --file/--unix")
        try:
            precheck(a.file, a.unix)
        except NotConclusive as e:
            print(json.dumps({"isolated": None, "not_conclusive": str(e)}, ensure_ascii=False))
            return 2
        out = exchange(a.socket, {"v": 1, "probe": "isolation", "files": a.file, "unix": a.unix})
        verdict = judge(out, a.file, a.unix, static_uids())
        print(json.dumps({"reply": out, "verdict": verdict}, ensure_ascii=False, indent=2))
        return 0 if verdict["isolated"] else 1
    if len(a.args) != 1:
        ap.error("check takes exactly one FILE")
    path = os.path.abspath(a.args[0])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        size = os.fstat(fd).st_size
        extra = [m for m in a.mime_extra.split(",") if m]
        out = exchange(a.socket, {"v": 1, "name": path, "size": size, "mime_extra": extra}, fd, size)
    finally:
        os.close(fd)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("verdict") == "quarantine" and "error" not in out else 1


if __name__ == "__main__":
    sys.exit(main())
