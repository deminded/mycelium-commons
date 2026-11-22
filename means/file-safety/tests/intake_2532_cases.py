#!/usr/bin/env python3
"""Offline regression cases for the Markdown name passed through the triage queue."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock
import zipfile

import md_convert_cases as base

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
NAME_LIMIT = 180 + len(".md")
SIDECAR = ".md-name"
INVALID_NAME = "convert_invalid_md_name"
mc, daemon = base.mc, base.daemon


def sidecar(inbox, name):
    return inbox / ("." + name + SIDECAR)


def setup(root):
    intake = base.load_intake()
    paths = base.daemon_paths(root)
    paths["converter"] = str(base.CODE / "md_convert.py")
    for attr, directory in [
        ("TRIAGE_INBOX", "inbox"), ("TRIAGE_OUTBOX", "outbox"),
        ("INCOMING", "incoming"), ("QUARANTINE", "quarantine"),
    ]:
        path = root / directory
        path.mkdir(exist_ok=True)
        setattr(intake, attr, str(path))
    intake.TRIAGE_WAIT_SECONDS = 0.01
    intake.TRIAGE_POLL = 0.01
    return intake, paths


def end_to_end(root, original, expected, mime=PDF):
    intake, paths = setup(root)
    inbox = root / "inbox"
    queued = "7_" + intake.safe_name(original)
    quarantine = root / "quarantine" / queued
    archive_path = Path(str(quarantine) + ".md.zip")
    fixture = base.write_docx if mime == DOCX else base.write_pdf
    actual_rename = os.rename
    actual_run = subprocess.run
    entries = {}

    def rename(src, dst, **kw):
        actual_rename(src, dst, **kw)
        if dst == queued and kw.get("dst_dir_fd") is not None:
            for entry in inbox.iterdir():
                assert entry.name.isascii(), entry
            path = sidecar(inbox, queued)
            if path.exists():
                entries["sidecar"] = path.read_bytes()
            daemon.process(queued, **paths)

    def run(args, **kw):
        if args[1] == intake.WORKER:
            label = {"verdict": "quarantine", "mime": mime, "av": "clean"}
            return subprocess.CompletedProcess(args, 0, json.dumps(label))
        return actual_run(args, **kw)

    with mock.patch.object(intake, "download", side_effect=lambda _, path: fixture(Path(path))), \
            mock.patch.object(intake, "reply"), \
            mock.patch.object(intake, "send_document", return_value={"ok": True}) as send, \
            mock.patch.object(intake.subprocess, "run", side_effect=run), \
            mock.patch.object(intake.os, "rename", side_effect=rename), \
            mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())), \
            mock.patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())):
        intake.handle_file({"document": {"file_id": "fixture", "file_name": original},
                            "chat": {"id": 1}, "message_id": 7})

    send.assert_called_once_with(1, str(archive_path), expected + ".zip", 7, thread_id=None)
    assert quarantine.is_file() and quarantine.name.isascii()
    assert not list(inbox.iterdir()) and not list((root / "outbox").iterdir())
    legacy_name = mc.safe_stem(queued) + ".md"
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert expected in names and legacy_name not in names, names
        if not expected.isascii():
            assert archive.getinfo(expected).flag_bits & 0x800, names
        text = archive.read(expected).decode("utf-8")
        assert archive.testzip() is None
    assert entries == {"sidecar": (expected + "\n").encode("utf-8")}, entries
    label = json.loads(Path(str(quarantine) + ".safety.json").read_text())
    assert Path(label["llm_triage"]["summary"]).name == legacy_name, label
    return names, text, mc.safe_stem(queued)


def russian_docx(root):
    base.require("pandoc")
    names, text, stem = end_to_end(root, "Отчёт за ноябрь.docx", "Отчёт_за_ноябрь.docx.md", DOCX)
    links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    links += re.findall(r'<img\s+src="([^"]+)"', text)
    assert "Hello DOCX triage" in text and len(links) == 1, text
    assert links[0].startswith(stem + "_md/") and links[0] in names, (links, names)


def filtered(root):
    base.require("pdftotext", "pdfimages")
    end_to_end(root, 'Отчёт"\nноябрь.pdf', "Отчёт__ноябрь.pdf.md")


def long_name(root):
    base.require("pdftotext", "pdfimages")
    for directory, original, expected in [
        ("split", "a" + "я" * 100 + ".pdf", "a" + "я" * 89 + ".md"),
        ("exact", "я" * 100 + ".pdf", "я" * 90 + ".md"),
    ]:
        subroot = root / directory
        subroot.mkdir()
        end_to_end(subroot, original, expected)


def queue_job(root, payload, kind=b"pdf\n"):
    _, paths = setup(root)
    name = "7_input.pdf"
    inbox = root / "inbox"
    base.write_pdf(inbox / name)
    if kind is not None:
        (inbox / ("." + name + ".kind")).write_bytes(kind)
    if payload is not None:
        sidecar(inbox, name).write_bytes(payload)
    return name, paths


def fallback(root, payload):
    base.require("pdftotext", "pdfimages")
    name, paths = queue_job(root, payload)
    daemon.process(name, **paths)
    with zipfile.ZipFile(root / "outbox" / (name + ".md.zip")) as archive:
        assert "7_input.md" in archive.namelist(), archive.namelist()
    result = base.assert_daemon_clean(root, name)
    assert result["flags"] == [], result


def direct_invalid(root, invalid):
    base.require("pdftotext", "pdfimages")
    src = root / "7_input.pdf"
    base.write_pdf(src)
    for number, bad in enumerate(invalid):
        outdir = root / str(number)
        if "\0" in bad or "\udcff" in bad:
            result = mc.convert(str(src), "pdf", str(outdir), bad)
        else:
            proc = subprocess.run([sys.executable, str(base.CODE / "md_convert.py"),
                                   str(src), "pdf", str(outdir), bad],
                                  capture_output=True, text=True, timeout=75, check=True)
            result = json.loads(proc.stdout)
        assert result["ok"] and result["flags"] == [INVALID_NAME], (repr(bad), result)
        with zipfile.ZipFile(result["zip"]) as archive:
            assert archive.namelist() == ["7_input.md", "7_input_md/img-000.png"], archive.namelist()


def bounded_read(root):
    raw = ("я" * 90 + ".md").encode("utf-8") + b"\n"
    path = root / "md-name"
    path.write_bytes(raw)
    actual_open = open

    class BoundedReader:
        def __enter__(self):
            self.stream = actual_open(path, "rb")
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= NAME_LIMIT + 2, size
            return self.stream.read(size)

    with mock.patch.object(daemon, "open", return_value=BoundedReader(), create=True):
        assert daemon.read_md_name(path) == raw[:-1].decode("utf-8")


def handoff(root, mode):
    intake, _ = setup(root)
    inbox = root / "inbox"
    original = "Отчёт.pdf"
    queued = "7_" + intake.safe_name(original)
    actual_open, actual_rename, actual_chown = os.open, os.rename, os.fchown
    opened, chowned, published = [], [], []

    def open_partial(path, flags, mode=0o777, *, dir_fd=None):
        fd = actual_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and flags & os.O_WRONLY:
            opened.append((path, flags, mode, fd))
        return fd

    def chown(fd, uid, gid):
        chowned.append((fd, uid, gid))
        actual_chown(fd, uid, gid)

    def rename(src, dst, **kw):
        if not kw:
            return actual_rename(src, dst)
        assert kw["src_dir_fd"] == kw["dst_dir_fd"]
        if mode == "name-rename-fails" and dst.endswith(SIDECAR):
            raise PermissionError("sidecar publication failed")
        if mode == "job-rename-fails" and dst == queued:
            raise PermissionError("job publication failed")
        actual_rename(src, dst, **kw)
        published.append(dst)

    mime = "text/plain" if mode == "nonconvert" else PDF
    gate = SimpleNamespace(stdout=json.dumps({"verdict": "quarantine", "mime": mime, "av": "clean"}))
    with mock.patch.object(intake.os, "open", side_effect=open_partial), \
            mock.patch.object(intake.os, "fchown", side_effect=chown), \
            mock.patch.object(intake.os, "rename", side_effect=rename), \
            mock.patch.object(intake, "download", side_effect=lambda _, path: Path(path).write_bytes(b"input")), \
            mock.patch.object(intake, "reply"), \
            mock.patch.object(intake.subprocess, "run", return_value=gate), \
            mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())):
        intake.handle_file({"document": {"file_id": "fixture", "file_name": original},
                            "chat": {"id": 1}, "message_id": 7})

    label = root / "quarantine" / (queued + ".safety.json")
    status = json.loads(label.read_text())["llm_triage"]
    metadata = ["." + queued + ".kind", sidecar(inbox, queued).name]
    if mode.endswith("fails"):
        assert status == "SKIPPED:handoff_failed:PermissionError", status
        assert not list(inbox.iterdir()), list(inbox.iterdir())
        assert published == (metadata[:1] if mode == "name-rename-fails" else metadata), published
    else:
        assert status == "SKIPPED:timeout", status
        assert published == ([] if mode == "nonconvert" else metadata) + [queued], published
    assert len(opened) == len(chowned) == (1 if mode == "nonconvert" else 3), (opened, chowned)
    for (partial, flags, permissions, fd), ownership in zip(opened, chowned):
        assert partial.startswith(".") and partial.endswith(".partial"), partial
        assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and permissions == 0o640
        assert ownership == (fd, -1, os.getgid()), ownership


def daemon_cleanup(root, outcome):
    name, paths = queue_job(root, "Отчёт.pdf.md\n".encode("utf-8"),
                            kind=None if outcome == "nonconvert" else b"pdf\n")
    if outcome == "work-unavailable":
        with mock.patch.object(daemon.os, "statvfs", side_effect=OSError("unavailable")):
            daemon.process(name, **paths)
        expected = ["convert_work_unavailable"]
    elif outcome == "convert-timeout":
        with mock.patch.object(daemon, "convert_file", side_effect=subprocess.TimeoutExpired("convert", 1)):
            daemon.process(name, **paths)
        expected = ["convert_timeout"]
    elif outcome == "convert-error":
        (root / "inbox" / name).write_bytes(b"invalid PDF")
        daemon.process(name, **paths)
        expected = ["convert_error:CalledProcessError"]
    elif outcome == "reader-timeout":
        Path(paths["reader"]).write_text("import time; time.sleep(10)")
        with mock.patch.object(daemon, "READER_TIMEOUT", 0.1):
            daemon.process(name, **paths)
        expected = ["triage_timeout"]
    elif outcome == "publish-error":
        actual_replace = os.replace

        def publish(src, dst):
            if dst.endswith(".json"):
                raise PermissionError("result publication failed")
            actual_replace(src, dst)

        with mock.patch.object(daemon.os, "replace", side_effect=publish):
            try:
                daemon.process(name, **paths)
            except PermissionError:
                pass
            else:
                raise AssertionError("publication unexpectedly succeeded")
        assert not list((root / "inbox").iterdir()) and not list((root / "work").iterdir())
        assert not list((root / "outbox").glob("*.tmp"))
        return
    else:
        daemon.process(name, **paths)
        expected = []
    assert base.assert_daemon_clean(root, name)["flags"] == expected


CASES = {
    "russian-docx": russian_docx,
    "filtered": filtered,
    "long-name": long_name,
    "missing-sidecar": lambda root: fallback(root, None),
    "empty-sidecar": lambda root: fallback(root, b""),
    "oversize-sidecar": lambda root: fallback(root, b"a" * (NAME_LIMIT + 1) + b"\n"),
    "non-utf8-sidecar": lambda root: fallback(root, b"x\xff.md\n"),
    "two-lines-sidecar": lambda root: fallback(root, b"first.md\nsecond.md\n"),
    "nul-sidecar": lambda root: fallback(root, b"x\0.md\n"),
    "bounded-read": bounded_read,
    "direct-invalid": lambda root: direct_invalid(root, (
        "", ".", "..", "a/b.md", "a\\b.md", "a\0b.md", "a\x7fb.md", "a\u202eb.md",
        "a\u2028b.md", "a\u2029b.md", "a\udcffb.md", "a" + "я" * 90 + ".md",
    )),
    **{f"handoff-{mode}": lambda root, mode=mode: handoff(root, mode)
       for mode in ("convert", "nonconvert", "name-rename-fails", "job-rename-fails")},
    **{f"cleanup-{outcome}": lambda root, outcome=outcome: daemon_cleanup(root, outcome)
       for outcome in ("work-unavailable", "convert-timeout", "convert-error",
                       "reader-timeout", "publish-error", "nonconvert")},
}


def main():
    passed = failed = skipped = 0
    for name in sys.argv[1:] or CASES:
        try:
            with tempfile.TemporaryDirectory(prefix="intake-2532-") as tmp:
                CASES[name](Path(tmp))
        except base.Skip as exc:
            skipped += 1
            print(f"SKIP {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"2532: {passed} PASS, {failed} FAIL, {skipped} SKIP")
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
