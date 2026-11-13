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
    (b'{"v": 1, "probe": "isolation", "paths": ["relative"]}\n', "bad_probe_paths"),
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


def test_isolation_probe_reports_each_check(tmp_path, monkeypatch):
    m = _module()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    monkeypatch.setattr(m, "PROBE_CONNECT", ("127.0.0.1", port))
    readable = tmp_path / "secret.env"
    readable.write_text("x")
    out = m.isolation_probe({"v": 1, "probe": "isolation",
                             "paths": [str(readable), str(tmp_path), str(tmp_path / "missing")]})
    c = out["checks"]
    assert c["read:" + str(readable)] == "allowed" and c["read:" + str(tmp_path)] == "allowed"
    assert c["read:" + str(tmp_path / "missing")] == "denied:ENOENT"
    assert c["net_connect"] == "denied:ECONNREFUSED"
    assert c["code_write"] == "allowed"
    assert not list(CODE.glob(".isolation-probe-*"))
    assert out["isolated"] is False and out["uid"] == os.getuid()


def test_attempt_classifies_silence_apart_from_refusal():
    m = _module()

    def silent():
        raise TimeoutError("timed out")

    def refused():
        raise ConnectionRefusedError(111, "refused")
    assert m._attempt(silent) == "timeout"
    assert m._attempt(refused) == "denied:ECONNREFUSED"
    assert m._attempt(lambda: None) == "allowed"


def test_isolation_probe_all_denied_is_isolated(tmp_path, monkeypatch):
    m = _module()
    monkeypatch.setattr(m, "_attempt", lambda action: "denied:EACCES")
    assert m.isolation_probe({"paths": ["/x"]})["isolated"] is (os.getuid() != 0)
    monkeypatch.setattr(m, "_attempt", lambda action: "timeout")
    assert m.isolation_probe({"paths": []})["isolated"] is False


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


@pytest.mark.parametrize("reply, rc", [
    ({"probe": "isolation", "uid": 61234, "isolated": True, "checks": {}}, 0),
    ({"probe": "isolation", "uid": os.getuid(), "isolated": True, "checks": {}}, 1),
    ({"probe": "isolation", "uid": 61234, "isolated": False, "checks": {}}, 1),
    ({"error": "bad_probe_paths"}, 1),
])
def test_client_probe_verdict(tmp_path, capsys, reply, rc):
    srv = Inetd(tmp_path, canned=reply)
    try:
        assert _client().main(["probe", "--socket", srv.path, "/home/x"]) == rc
    finally:
        srv.close()
    assert json.loads(capsys.readouterr().out) == reply


def test_client_check_error_reply_is_failure_even_with_verdict(tmp_path, capsys):
    f = tmp_path / "doc.txt"
    f.write_bytes(TEXT)
    srv = Inetd(tmp_path, canned={"error": "check_failed:X", "verdict": "quarantine"})
    try:
        assert _client().main(["check", "--socket", srv.path, str(f)]) == 1
    finally:
        srv.close()
