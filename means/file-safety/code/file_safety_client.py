#!/usr/bin/python3
"""Client of the socket check service (#2559, part 2): manual check and isolation probe.

ЗАЧЕМ. После установки юнитов мерить надо саму песочницу, а не верить юнит-файлу:
проба просит экземпляр службы прочесть пути агента и выйти в сеть и печатает, что ему
удалось. Пути — аргументами: в публичном коде им не место.

  file_safety_client.py probe [--socket S] PATH...   rc 0 — изолирован и uid не мой
  file_safety_client.py check [--socket S] FILE      печатает ярлык; rc 0 — quarantine
"""
import argparse
import json
import os
import socket
import sys

SOCKET = "/run/file-safety.sock"
REPLY_MAX = 256 * 1024
# Служба гасит экземпляр на 160-й секунде (RuntimeMaxSec); клиенту ждать чуть дольше.
TIMEOUT = 170


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
    ap.add_argument("args", nargs="*")
    # options may stand between the command and its paths
    a = ap.parse_intermixed_args(argv)
    if a.cmd == "probe":
        out = exchange(a.socket, {"v": 1, "probe": "isolation", "paths": a.args})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        # uid совпал с моим — значит, отвечает не песочница, а что-то под моей учёткой
        return 0 if out.get("isolated") is True and out.get("uid") != os.getuid() else 1
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
