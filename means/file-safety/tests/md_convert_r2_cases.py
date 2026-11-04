#!/usr/bin/env python3
"""Offline regressions for review r2; never touches deployment or network."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import md_convert_cases as base

mc = base.mc


def intake_setup(root):
    intake = base.load_intake()
    inbox, outbox = root / "inbox", root / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    (outbox / ".rejected").mkdir(mode=0o770)
    intake.JOURNAL_FILE = str(root / "journal.log")
    intake.TRIAGE_INBOX, intake.TRIAGE_OUTBOX = str(inbox), str(outbox)
    intake.TRIAGE_WAIT_SECONDS, intake.TRIAGE_POLL = 0.1, 0.01
    source = root / "input.pdf"
    source.write_text("original")
    result = outbox / (source.name + ".json")
    result.write_text(json.dumps({"summary": "ok", "flags": [], "converted": {"tool": "fake"}}))
    archive = outbox / (source.name + ".md.zip")
    archive.write_bytes(b"zip bytes")
    return intake, source, result, archive


def intake_run(intake, source, uid=None):
    # fs-triage need not exist on the test host; files use the current uid.
    with mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())), \
            mock.patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid() if uid is None else uid)):
        return intake.triage(str(source), "application/pdf")


def rejected(root, target="zip", variant="symlink"):
    intake, source, result_path, archive = intake_setup(root)
    path = archive if target == "zip" else result_path
    secret = root / "intake-secret"
    secret.write_text('{"summary":"SECRET","flags":[]}')
    path.unlink()
    uid = None
    if variant in ("symlink", "dangling"):
        path.symlink_to(secret if variant == "symlink" else root / "absent")
    elif variant == "fifo":
        os.mkfifo(path)
    elif variant == "directory":
        path.mkdir()
    elif variant == "owner":
        path.write_text('{}')
        uid = os.getuid() + 1
    elif variant == "oversize":
        # Sparse files exercise the actual production bounds without allocating RAM.
        with path.open("wb") as stream:
            stream.truncate(mc.MAX_OUTPUT_BYTES + 2 * 1024 * 1024)
    elif variant == "invalid":
        path.write_text("not json")
    result = intake_run(intake, source, uid)
    assert not os.path.lexists(str(source) + ".md.zip"), "untrusted archive reached quarantine"
    assert not os.path.lexists(path), "rejected outbox entry was not removed"
    assert any(f.startswith("convert_zip_rejected:") for f in result.get("flags", [])), result
    if target == "json":
        assert result.get("summary", "") == "" and "converted" not in result, result
        assert not archive.exists(), "ZIP accepted after result rejection"
    else:
        assert "converted" not in result, result
    assert "SECRET" not in json.dumps(result), result
    assert secret.read_text() == '{"summary":"SECRET","flags":[]}'


def safe_copy(root):
    intake, source, result_path, archive = intake_setup(root)
    # A hard link lets the compromised producer keep changing its original inode.
    retained = root / "producer-inode"
    os.link(archive, retained)
    result = intake_run(intake, source)
    dest = Path(str(source) + ".md.zip")
    assert result["summary"] == "ok" and "converted" in result, result
    assert dest.read_bytes() == b"zip bytes" and not dest.is_symlink()
    assert dest.stat().st_ino != retained.stat().st_ino, "archive was moved rather than copied"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600, oct(dest.stat().st_mode)
    retained.write_bytes(b"changed")
    assert dest.read_bytes() == b"zip bytes", "producer can mutate quarantine"
    assert not archive.exists() and not result_path.exists()


def source_replaced(root, target="zip"):
    intake, source, result_path, archive = intake_setup(root)
    selected = archive if target == "zip" else result_path
    secret = root / "secret"
    secret.write_text('SECRET')
    actual_open = os.open
    seen = []

    def opened(path, flags, *args, **kwargs):
        fd = actual_open(path, flags, *args, **kwargs)
        if str(path) in (str(archive), str(result_path)):
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK, flags
            seen.append(str(path))
        if str(path) == str(selected):
            selected.unlink()
            selected.symlink_to(secret)
        return fd

    with mock.patch.object(intake.os, "open", side_effect=opened):
        result = intake_run(intake, source)
    assert len(seen) == 2 and result["summary"] == "ok", (seen, result)
    assert Path(str(source) + ".md.zip").read_bytes() == b"zip bytes"
    assert secret.read_text() == "SECRET" and not os.path.lexists(selected)


def source_grows(root, target="zip"):
    intake, source, result_path, archive = intake_setup(root)
    selected = archive if target == "zip" else result_path
    actual_fstat = os.fstat
    selected_inode = selected.stat().st_ino

    def inspected(fd):
        info = actual_fstat(fd)
        if info.st_ino == selected_inode:
            with selected.open("ab") as stream:
                stream.write(b"x" * 1024)
        return info

    with mock.patch.object(intake, "MAX_CONVERT_ZIP_BYTES", 128), \
            mock.patch.object(intake, "MAX_TRIAGE_RESULT_BYTES", 128), \
            mock.patch.object(intake.os, "fstat", side_effect=inspected):
        result = intake_run(intake, source)
    assert not Path(str(source) + ".md.zip").exists(), result
    assert not selected.exists()
    assert any(f.endswith("size") for f in result["flags"]), result


def reject_schema(root):
    intake, source, result_path, archive = intake_setup(root)
    result_path.write_text('{"summary":"bad", "flags": "not a list"}')
    result = intake_run(intake, source)
    assert result == {"summary": "", "flags": ["convert_zip_rejected:result_schema"]}, result
    assert not archive.exists() and not result_path.exists()


def reject_nonempty_directory(root):
    intake, source, _, archive = intake_setup(root)
    archive.unlink()
    archive.mkdir()
    secret = root / "secret"
    secret.write_text("SECRET")
    (archive / "secret-link").symlink_to(secret)
    (archive / "junk").write_text("junk")
    result = intake_run(intake, source)
    assert "convert_zip_rejected:nonregular" in result["flags"], result
    assert not archive.exists() and secret.read_text() == "SECRET"


def exclusive_destination(root):
    intake, source, _, archive = intake_setup(root)
    dest = Path(str(source) + ".md.zip")
    dest.write_bytes(b"already here")
    result = intake_run(intake, source)
    assert dest.read_bytes() == b"already here", "existing quarantine file overwritten"
    assert any(f.startswith("convert_zip_rejected:") for f in result.get("flags", [])), result
    assert not archive.exists()


def rejection_label(root):
    intake = base.load_intake()
    intake.INCOMING, intake.QUARANTINE = str(root / "incoming"), str(root / "quarantine")
    Path(intake.INCOMING).mkdir()
    Path(intake.QUARANTINE).mkdir()
    inbox, outbox = root / "inbox", root / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    (outbox / ".rejected").mkdir(mode=0o770)
    intake.JOURNAL_FILE = str(root / "journal.log")
    intake.TRIAGE_INBOX, intake.TRIAGE_OUTBOX = str(inbox), str(outbox)
    flag = "convert_zip_rejected:symlink"
    (outbox / "23_sample.pdf.json").write_text('{"summary":"ok","flags":[]}')
    secret = root / "secret"
    secret.write_text("SECRET")
    (outbox / "23_sample.pdf.md.zip").symlink_to(secret)
    replies = []
    label = {"verdict": "quarantine", "av": "clean", "mime": "application/pdf"}
    with mock.patch.object(intake, "download", side_effect=lambda _, dest: Path(dest).write_text("input")), \
            mock.patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(label))), \
            mock.patch.object(intake, "reply", side_effect=lambda _, text, **kw: replies.append(text)), \
            mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())), \
            mock.patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())):
        intake.handle_file({"document": {"file_id": "id", "file_name": "sample.pdf"},
                            "chat": {"id": 1}, "message_id": 23})
    saved = json.loads((Path(intake.QUARANTINE) / "23_sample.pdf.safety.json").read_text())
    assert flag in saved["llm_triage"]["flags"], saved
    assert flag in replies[0] and "SECRET" not in replies[0], replies


def no_output(root, empty=False, code=0, kind="html"):
    def tool(args, outdir, deadline):
        if empty:
            if args[0] == "pdftotext":
                Path(args[-1]).touch()
            elif "-o" in args:
                Path(args[args.index("-o") + 1]).touch()
        return code
    with mock.patch.object(mc.shutil, "which", return_value="pandoc"), \
            mock.patch.object(mc, "run_tool", side_effect=tool):
        result = mc.convert(str(root / ("sample." + kind)), kind, str(root / "out"))
    expected = "convert_no_output" if code == 0 else "convert_error:CalledProcessError"
    assert not result["ok"] and result["flags"] == [expected], result
    assert not list((root / "out").iterdir())


def subreaper_unavailable(root, denied=False):
    failure = {"return_value": SimpleNamespace(prctl=lambda *args: -1)} if denied else {"side_effect": OSError("no prctl")}
    with mock.patch.object(ctypes, "CDLL", **failure), \
            mock.patch.object(mc.shutil, "which", return_value="pandoc"), \
            mock.patch.object(mc, "run_tool") as run:
        result = mc.convert(str(root / "sample.html"), "html", str(root / "out"))
    assert not result["ok"] and "convert_subreaper_unavailable" in result["flags"], result
    run.assert_not_called()


def escaped_descendant(root, mode="ok", through_daemon=False):
    tools = root / "tools"
    tools.mkdir()
    pidfile = root / "escaped.pid"
    parser = tools / "pandoc"
    parser.write_text(f'''#!{sys.executable}
import os,pathlib,sys,time
pidfile = pathlib.Path({str(pidfile)!r})
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        pidfile.write_text(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    os._exit(0)
os.wait()
while not pidfile.exists(): time.sleep(0.01)
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('converted')
''' + ("time.sleep(60)\n" if mode == "timeout" else f"sys.exit({1 if mode == 'error' else 0})\n"))
    parser.chmod(0o700)
    wrapper = root / "wrapper.py"
    wrapper.write_text(f'''import sys,json
sys.path.insert(0, {str(base.CODE)!r})
import md_convert as mc
mc.CONVERT_TIMEOUT = {0.4 if mode == 'timeout' else 5}
print(json.dumps(mc.convert(*sys.argv[1:])))
''')
    pid = None
    try:
        if through_daemon:
            paths = base.daemon_paths(root)
            paths["converter"] = str(wrapper)
            (root / "inbox" / "sample.html").write_text("input")
            (root / "inbox" / ".sample.html.kind").write_text("html")
            with mock.patch.dict(os.environ, PATH=str(tools)), \
                    mock.patch.object(base.daemon, "CONVERT_TIMEOUT", 0.4):
                base.daemon.process("sample.html", **paths)
            result = base.assert_daemon_clean(root, "sample.html")
        else:
            proc = subprocess.run([sys.executable, str(wrapper), str(root / "sample.html"), "html", str(root / "out")],
                                  env=dict(os.environ, PATH=str(tools)), capture_output=True, text=True, timeout=10)
            assert proc.returncode == 0, proc.stderr
            result = json.loads(proc.stdout)
        pid = int(pidfile.read_text())
        assert not Path(f"/proc/{pid}").exists(), f"setsid grandchild {pid} survived convert ({mode})"
        if not through_daemon:
            assert result["ok"] == (mode == "ok"), result
        if mode == "timeout":
            assert "convert_timeout" in result["flags"], result
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def deploy_checks(root):
    # r4 preserves arbitrary existing code attributes, recorded before deployment.
    from deploy_r4_cases import Fixture
    f = Fixture(root / 'r4-deploy')
    result = f.deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    checker = Path(__file__).with_name('check_deploy.py')
    def check():
        return subprocess.run([sys.executable, str(checker), '--backup', str(f.backup())],
                              env=f.env, capture_output=True, text=True, timeout=10)
    assert check().returncode == 0
    for path in (f.triage/'triage_daemon.py', f.triage/'md_convert.py', f.intake/'intake_poller.py',
                 f.triage/'work', f.triage/'outbox/.rejected', f.drop):
        old = stat.S_IMODE(path.stat().st_mode)
        path.chmod(old ^ 0o010)
        result = check()
        assert result.returncode and 'FAIL deploy' in result.stdout, result
        path.chmod(old)
    path = f.triage/'md_convert.py'
    data = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    path.unlink()
    path.symlink_to(f.triage/'triage_daemon.py')
    assert check().returncode
    path.unlink(); path.write_bytes(data); path.chmod(mode)
    path.write_text('changed code')
    assert check().returncode
    proc = subprocess.run(['bash', str(checker.with_name('server_smoke.sh')), '--check-deploy', str(root)],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0 and 'FAIL deploy' in proc.stdout + proc.stderr, proc


def main():
    cases = [(f"P1 {target}: {variant}", lambda r, t=target, v=variant: rejected(r, t, v))
             for target in ("zip", "json")
             for variant in ("symlink", "dangling", "fifo", "directory", "owner", "oversize")]
    cases += [
        ("P1 JSON: invalid", lambda r: rejected(r, "json", "invalid")),
        ("P1 ZIP: copy to private new inode", safe_copy),
        ("P1 ZIP: descriptor survives path replacement", source_replaced),
        ("P1 JSON: descriptor survives path replacement", lambda r: source_replaced(r, "json")),
        ("P1 ZIP: growth after fstat", source_grows),
        ("P1 JSON: growth after fstat", lambda r: source_grows(r, "json")),
        ("P1 JSON: reject wrong schema", reject_schema),
        ("P1 ZIP: remove nonempty rejected directory", reject_nonempty_directory),
        ("P1 ZIP: exclusive destination", exclusive_destination),
        ("P1 ZIP: rejection in label and chat", rejection_label),
        ("P2: double fork + setsid, success", escaped_descendant),
        ("P2: double fork + setsid, failure", lambda r: escaped_descendant(r, "error")),
        ("P2: double fork + setsid, timeout", lambda r: escaped_descendant(r, "timeout")),
        ("P2: daemon timeout allows setsid cleanup", lambda r: escaped_descendant(r, "timeout", True)),
        ("P2: prctl unavailable, fail closed", subreaper_unavailable),
        ("P2: prctl denied, fail closed", lambda r: subreaper_unavailable(r, True)),
        ("P2/r4: preserved attributes, content, symlinks and CLI", deploy_checks),
        ("P3: success without md", no_output),
        ("P3: success with empty md", lambda r: no_output(r, True)),
        ("P3: failed tool without md", lambda r: no_output(r, code=1)),
        ("P3 PDF: success without md", lambda r: no_output(r, kind="pdf")),
        ("P3 PDF: success with empty md", lambda r: no_output(r, empty=True, kind="pdf")),
    ]
    # Select cases to avoid the old intake blocking forever on a FIFO before fixes.
    selected = set(sys.argv[1:])
    passed = failed = 0
    for name, case in cases:
        if selected and not any(s in name for s in selected):
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="md-convert-r2-") as directory:
                root = Path(directory)
                base.build_fixtures(root)
                case(root)
            print("PASS " + name, flush=True)
            passed += 1
        except Exception as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            failed += 1
    print(f"прошло {passed}, упало {failed}")
    return int(failed != 0)


if __name__ == "__main__":
    sys.exit(main())
