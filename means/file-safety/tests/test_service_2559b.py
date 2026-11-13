"""#2559 part 2: file_safety_service behind a socket, as systemd Accept=yes runs it.

ЗАЧЕМ. Песочницу (DynamicUser, без дома и сети) без root здесь не поднять; эти тесты
держат то, что от песочницы не зависит: протокол, отказ на кривом входе, тот же ярлык,
что у проверяльщика, уборку частного каталога. Изоляцию меряет проба после установки.
"""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

CODE = Path(__file__).resolve().parent.parent / "code"
SERVICE = CODE / "file_safety_service.py"
TEXT = b"Generated clean text for the socket service test.\n"
MARK = b"FAKE-SIGNATURE-FOR-TEST"


def fake_bin(tmp_path, *, clamscan=True):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    if clamscan:
        s = b / "clamscan"
        # Настоящий clamscan грузит гигабайт баз; подставной различает только метку.
        s.write_text("#!/bin/sh\nfor f; do :; done\nif grep -q '%s' \"$f\"; then echo \"$f: Test.Sig FOUND\"; exit 1; fi\nexit 0\n"
                     % MARK.decode())
        s.chmod(0o755)
    return b


def run(tmp_path, request: bytes, *, path_env=None, timeout=60):
    tmp = tmp_path / "svc-tmp"
    tmp.mkdir(exist_ok=True)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    env = {"PATH": path_env or "%s:/usr/bin:/bin" % fake_bin(tmp_path), "LANG": "C.UTF-8", "TMPDIR": str(tmp)}
    p = subprocess.Popen([sys.executable, "-I", "-B", str(SERVICE)], stdin=child, stdout=child, env=env)
    child.close()
    try:
        parent.sendall(request)
    except BrokenPipeError:
        pass
    parent.shutdown(socket.SHUT_WR)
    chunks = []
    parent.settimeout(timeout)
    while True:
        c = parent.recv(65536)
        if not c:
            break
        chunks.append(c)
    parent.close()
    rc = p.wait(timeout)
    out = b"".join(chunks)
    assert out.endswith(b"\n") and out.count(b"\n") == 1, out
    return json.loads(out), rc, tmp


def frame(data=TEXT, *, name="/media/x/doc.txt", size=None, **extra):
    h = {"v": 1, "name": name, "size": len(data) if size is None else size}
    h.update(extra)
    return json.dumps(h).encode() + b"\n" + data


def test_clean_text_label_matches_checker(tmp_path):
    label, rc, tmp = run(tmp_path, frame())
    assert rc == 0
    assert label["file"] == "/media/x/doc.txt"
    assert label["sha256"] == hashlib.sha256(TEXT).hexdigest()
    assert (label["size"], label["static"], label["av"], label["verdict"]) == (len(TEXT), "ok", "clean", "quarantine")
    assert list(tmp.iterdir()) == []


def test_signature_hit_is_reject(tmp_path):
    label, rc, _ = run(tmp_path, frame(TEXT + MARK))
    assert (label["av"], label["verdict"]) == ("INFECTED:Test.Sig", "reject")


def test_extra_bytes_after_body_are_not_checked(tmp_path):
    label, _, _ = run(tmp_path, frame(TEXT, size=len(TEXT)) + MARK)
    assert label["sha256"] == hashlib.sha256(TEXT).hexdigest() and label["av"] == "clean"


def test_mime_extra_reaches_checker(tmp_path):
    gz = gzip.compress(b"plain\n")
    plain, _, _ = run(tmp_path, frame(gz))
    widened, _, _ = run(tmp_path, frame(gz, mime_extra=["application/gzip"]))
    assert plain["verdict"] == "reject" and plain["mime"] == "application/gzip"
    assert widened["static"] == "ok", widened


@pytest.mark.parametrize("request_bytes, error", [
    (b'{"v": 1, "name": "/a", "size": 1}', "header_unterminated_or_too_long"),
    (b"{" + b" " * 5000 + b"}\n", "header_unterminated_or_too_long"),
    # ровно HEADER_MAX + 1 байт вместе с "\n": readline отдаёт строку целиком, мерить надо длину
    (b'{"v": 1, "name": "/a", "size": 0, "x": "' + b"a" * (4097 - 43) + b'"}\n', "header_unterminated_or_too_long"),
    (b"not json\n", "header_not_json"),
    (b'["v", 1]\n', "header_version"),
    (b'{"v": 2, "name": "/a", "size": 0}\n', "header_version"),
    (b'{"v": 1, "name": "", "size": 0}\n', "bad_name"),
    (b'{"v": 1, "name": "/a\\u0000b", "size": 0}\n', "bad_name"),
    (b'{"v": 1, "name": "/' + b"a" * 1100 + b'", "size": 0}\n', "bad_name"),
    (b'{"v": 1, "name": "/a", "size": true}\n', "bad_size"),
    (b'{"v": 1, "name": "/a", "size": -1}\n', "bad_size"),
    (b'{"v": 1, "name": "/a", "size": "3"}\n', "bad_size"),
    (b'{"v": 1, "name": "/a", "size": 0, "mime_extra": ["AUDIO/OGG;x"]}\n', "bad_mime_extra"),
    (b'{"v": 1, "name": "/a", "size": 0, "mime_extra": "audio/ogg"}\n', "bad_mime_extra"),
    (b'{"v": 1, "probe": "other"}\n', "unknown_probe"),
    (b'{"v": 1, "probe": "isolation", "files": ["relative"]}\n', "bad_probe_files"),
    (b'{"v": 1, "probe": "isolation", "unix": "/run/x"}\n', "bad_probe_unix"),
    (b'{"v": true, "name": "/a", "size": 0}\n', "header_version"),
    (b'{"v": 1.0, "name": "/a", "size": 0}\n', "header_version"),
])
def test_bad_request_is_refused(tmp_path, request_bytes, error):
    label, rc, _ = run(tmp_path, request_bytes)
    assert label == {"error": error} and rc == 2


def test_short_body_is_refused_and_cleaned(tmp_path):
    label, rc, tmp = run(tmp_path, frame(TEXT, size=len(TEXT) + 100))
    assert label == {"error": "short_body"} and rc == 2
    assert list(tmp.iterdir()) == []


def test_oversize_refused_before_reading_body(tmp_path):
    label, rc, tmp = run(tmp_path, frame(b"", size=10 ** 12))
    assert label == {"error": "size_exceeds_limit"} and rc == 2
    assert list(tmp.iterdir()) == []


def test_checker_crash_is_an_error_not_a_label(tmp_path):
    # без file(1) проверяльщик не может определить тип — это сбой, а не «чисто»
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    label, rc, tmp = run(tmp_path, frame(), path_env=str(empty))
    assert label == {"error": "check_failed:FileNotFoundError"} and rc == 1
    assert list(tmp.iterdir()) == []


def _module():
    spec = importlib.util.spec_from_file_location("fss_under_test", SERVICE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m








# --- клиент: ручная проверка и проба изоляции через путь сокета

import shutil
import tempfile
import threading

CLIENT = CODE / "file_safety_client.py"


def _client():
    spec = importlib.util.spec_from_file_location("fsc_client_under_test", CLIENT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Inetd:
    """Как systemd с Accept=yes: на каждое соединение — экземпляр службы со stdin/stdout = сокет."""

    def __init__(self, tmp_path, canned=None):
        self.dir = tempfile.mkdtemp(prefix="fsc-", dir="/tmp")
        self.path = os.path.join(self.dir, "s")
        self.canned, self.tmp_path = canned, tmp_path
        self.hits = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(2)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                if self.canned is not None:
                    self.hits += 1
                    while conn.recv(65536):
                        pass
                    conn.sendall(json.dumps(self.canned).encode() + b"\n")
                    continue
                env = {"PATH": "%s:/usr/bin:/bin" % fake_bin(self.tmp_path), "LANG": "C.UTF-8",
                       "TMPDIR": str(self.tmp_path)}
                subprocess.run([sys.executable, "-I", "-B", str(SERVICE)], stdin=conn, stdout=conn,
                               env=env, timeout=60)

    def close(self):
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def test_client_check_through_service(tmp_path, capsys):
    f = tmp_path / "doc.txt"
    f.write_bytes(TEXT)
    srv = Inetd(tmp_path)
    try:
        rc = _client().main(["check", "--socket", srv.path, str(f)])
    finally:
        srv.close()
    label = json.loads(capsys.readouterr().out)
    assert rc == 0 and label["file"] == str(f) and label["sha256"] == hashlib.sha256(TEXT).hexdigest()




def test_client_check_error_reply_is_failure_even_with_verdict(tmp_path, capsys):
    f = tmp_path / "doc.txt"
    f.write_bytes(TEXT)
    srv = Inetd(tmp_path, canned={"error": "check_failed:X", "verdict": "quarantine"})
    try:
        assert _client().main(["check", "--socket", srv.path, str(f)]) == 1
    finally:
        srv.close()


# --- договор юнита: ослабление защиты должно ронять тест, а не проходить зелёным

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "file-safety@.service"
SOCKET_UNIT = UNIT.with_name("file-safety.socket")
REQUIRED = {
    "DynamicUser": "yes", "ProtectHome": "yes", "ProtectSystem": "strict", "PrivateTmp": "yes",
    "PrivateNetwork": "yes", "PrivateDevices": "yes", "PrivateIPC": "yes", "NoNewPrivileges": "yes",
    "RestrictAddressFamilies": "none", "TemporaryFileSystem": "/run:ro", "CapabilityBoundingSet": "",
    "RestrictNamespaces": "yes", "RestrictSUIDSGID": "yes", "RestrictRealtime": "yes",
    "LockPersonality": "yes", "ProtectHostname": "yes", "ProtectClock": "yes",
    "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes", "ProtectKernelLogs": "yes",
    "ProtectControlGroups": "yes", "ProtectProc": "invisible", "SystemCallArchitectures": "native",
    "SystemCallFilter": "@system-service", "SystemCallErrorNumber": "EPERM", "NoExecPaths": "/tmp /var/tmp",
    "RuntimeMaxSec": "160", "TimeoutStopSec": "5", "MemoryMax": "1800M", "TasksMax": "16", "CPUQuota": "100%",
    "StandardInput": "socket", "StandardOutput": "socket", "StandardError": "journal", "UMask": "0077",
    "Environment": "PATH=/usr/bin:/bin LANG=C.UTF-8",
    "ExecStart": "/usr/bin/python3 -I -B /opt/file-safety/file_safety_service.py",
}
# Меняют смысл обещанного: чужая учётка, возврат путей и /run, недобивание потомков.
FORBIDDEN = {"User", "Group", "SupplementaryGroups", "AmbientCapabilities", "ReadWritePaths", "BindPaths",
             "BindReadOnlyPaths", "PrivateUsers", "Restart", "KillMode", "SendSIGKILL", "Delegate",
             "ExecStartPre", "ExecStartPost", "ExecStop", "PermissionsStartOnly"}


def _unit_settings(path):
    pairs, section = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, _, value = line.partition("=")
        pairs.setdefault((section, key), []).append(value)
    return pairs


def test_unit_keeps_every_hardening_option():
    s = _unit_settings(UNIT)
    for key, value in REQUIRED.items():
        assert s.get(("Service", key)) == [value], (key, s.get(("Service", key)))
    assert not {k for (_, k) in s} & FORBIDDEN, {k for (_, k) in s} & FORBIDDEN
    assert {sec for (sec, _) in s} == {"Unit", "Service"}


def test_socket_unit_is_private_to_the_agent():
    s = _unit_settings(SOCKET_UNIT)
    assert s[("Socket", "ListenStream")] == ["/run/file-safety.sock"] and s[("Socket", "SocketMode")] == ["0600"]
    assert s[("Socket", "SocketUser")] == ["claude-user"] and s[("Socket", "Accept")] == ["yes"]
    assert s[("Socket", "MaxConnections")] == ["1"]


# --- проба изоляции: служба отдаёт сырые исходы, судит клиент (ревью p2-r1, P2-3)

def _listening(dirpath):
    path = os.path.join(dirpath, "ctl")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(4)   # connect проходит и без accept
    return s, path


def test_probe_returns_raw_outcomes_without_verdict(tmp_path):
    m = _module()
    d = tempfile.mkdtemp(prefix="fsp-", dir="/tmp")
    listener, ctl = _listening(d)
    readable = tmp_path / "secret.env"
    readable.write_text("x")
    missing = str(tmp_path / "missing")
    try:
        out = m.isolation_probe({"files": [str(readable), missing], "unix": [ctl, d + "/nope"]})
    finally:
        listener.close()
        shutil.rmtree(d, ignore_errors=True)
    r = out["results"]
    assert r["file:" + str(readable)] == "allowed" and r["file:" + missing] == "error:ENOENT"
    assert r["unix:" + ctl] == "allowed" and r["unix:" + d + "/nope"] == "error:ENOENT"
    # ожидание — с самого хоста: проба должна пробовать, а не повторять заготовленный ответ
    assert r["inet"] == "allowed" and r["inet6"] == m._outcome(lambda: m._create(socket.AF_INET6))
    assert r["code_write"] == "allowed" and out["code_write_cleanup"] == "allowed"
    assert not list(CODE.glob(".isolation-probe-*"))
    assert "isolated" not in out and out["uid"] == os.getuid() and out["gid"] == os.getgid()


def test_outcome_names_errno_or_exception_type():
    m = _module()

    def refused():
        raise ConnectionRefusedError(111, "refused")

    def silent():
        raise TimeoutError("timed out")
    assert m._outcome(refused) == "error:ECONNREFUSED"
    assert m._outcome(silent) == "error:TimeoutError"
    assert m._outcome(lambda: None) == "allowed"


DYN = 61234


def _blocked(files, unix=(), uid=DYN, **results):
    res = {"code_write": "error:EROFS", "inet": "error:EAFNOSUPPORT", "inet6": "error:EPERM"}
    res.update({"file:" + p: "error:EACCES" for p in files})
    res.update({"unix:" + p: "error:EAFNOSUPPORT" for p in unix})
    res.update(results)
    return {"probe": "isolation", "uid": uid, "gid": uid, "results": res}


@pytest.mark.parametrize("change, rc, check", [
    ({}, 0, None),
    ({"FILE": "error:ENOENT"}, 0, None),          # путь, который клиент сам читает, внутри не виден
    ({"FILE": "allowed"}, 1, "LEAK"),
    ({"FILE": "error:EIO"}, 1, "unproven"),
    ({"UNIX": "allowed"}, 1, "LEAK"),
    ({"UNIX": "error:ECONNREFUSED"}, 1, "unproven"),  # файл сокета виден — это не скрытие
    ({"inet": "allowed"}, 1, "LEAK"),
    ({"inet6": "error:TimeoutError"}, 1, "unproven"),
    ({"code_write": "allowed"}, 1, "LEAK"),
    ({"uid": os.getuid()}, 1, "unproven"),
    ({"uid": 0}, 1, "unproven"),
    ({"uid": 995}, 1, "unproven"),                # статический системный uid вместо динамического
    ({"uid": True}, 1, "unproven"),
    ({"uid": 61235}, 1, "unproven"),             # uid из диапазона, но есть в /etc/passwd (подложен ниже)
    ({"uid": 70000}, 1, "unproven"),             # не статический, но и не из диапазона DynamicUser
])
def test_client_probe_judges_each_check(tmp_path, capsys, change, rc, check):
    f = tmp_path / "control.env"
    f.write_text("x")
    d = tempfile.mkdtemp(prefix="fsp-", dir="/tmp")
    listener, ctl = _listening(d)
    change = dict(change)
    uid = change.pop("uid", DYN)
    results = {("file:" + str(f) if k == "FILE" else "unix:" + ctl if k == "UNIX" else k): v
               for k, v in change.items()}
    srv = Inetd(tmp_path, canned=_blocked([str(f)], [ctl], uid=uid, **results))
    cli = _client()
    cli.static_uids = lambda path="/etc/passwd": {0, 995, os.getuid(), 61235}
    try:
        got = cli.main(["probe", "--socket", srv.path, "--file", str(f), "--unix", ctl])
    finally:
        srv.close()
        listener.close()
        shutil.rmtree(d, ignore_errors=True)
    out = json.loads(capsys.readouterr().out)
    assert got == rc and out["verdict"]["isolated"] is (rc == 0)
    if check:
        assert check in out["verdict"]["checks"].values()


@pytest.mark.parametrize("args", [["--file", "/nonexistent/control"], [], ["--file", "{F}", "--unix", "/nonexistent/s"]])
def test_client_probe_without_conclusive_controls_sends_nothing(tmp_path, capsys, args):
    f = tmp_path / "control.env"
    f.write_text("x")
    srv = Inetd(tmp_path, canned=_blocked([str(f)]))
    try:
        rc = _client().main(["probe", "--socket", srv.path] + [a.replace("{F}", str(f)) for a in args])
    finally:
        srv.close()
    assert rc == 2 and srv.hits == 0
    assert json.loads(capsys.readouterr().out)["isolated"] is None


def test_client_probe_error_reply_is_not_isolated(tmp_path, capsys):
    f = tmp_path / "control.env"
    f.write_text("x")
    srv = Inetd(tmp_path, canned={"error": "bad_probe_files"})
    try:
        assert _client().main(["probe", "--socket", srv.path, "--file", str(f)]) == 1
    finally:
        srv.close()


def test_write_probe_counts_creation_even_if_cleanup_fails(tmp_path, monkeypatch):
    m = _module()

    def no_unlink(path, *a, **k):
        raise PermissionError(1, "unlink denied")
    monkeypatch.setattr(m.os, "unlink", no_unlink)
    created, cleanup = m._write_probe(str(tmp_path))
    monkeypatch.undo()
    assert (created, cleanup) == ("allowed", "error:EPERM")
    assert list(tmp_path.glob(".isolation-probe-*"))


def test_write_probe_denied_creation(tmp_path):
    m = _module()
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        assert m._write_probe(str(d)) == ("error:EACCES", "not-needed")
    finally:
        d.chmod(0o700)
