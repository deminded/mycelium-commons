#!/usr/bin/env python3
"""Offline regression cases, with generated fixtures and explicit tool SKIPs.

python3 tests/md_convert_cases.py
python3 tests/md_convert_cases.py --fixtures /tmp/empty-dir
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock
import zipfile
import zlib

CODE = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE))
import md_convert as mc
import triage_daemon as daemon


class Skip(Exception):
    pass


def require(*names):
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise Skip("нет инструмента: " + ", ".join(missing))


def png():
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b""))


def write_pdf(path):
    # A real xref, text and one red pixel; all byte offsets are built here.
    stream = b"BT /F1 12 Tf 20 80 Td (Hello PDF triage) Tj ET\nq 10 0 0 10 20 20 cm /Im0 Do Q\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
        b"/Resources << /Font << /F1 4 0 R >> /XObject << /Im0 6 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Length 3 >>\nstream\n\xff\0\0\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


def write_docx(path, big_image=False):
    drawing = '''<w:p><w:r><w:drawing><wp:inline>
      <wp:extent cx="914400" cy="914400"/><wp:docPr id="1" name="image"/>
      <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
      <pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="img.png"/><pic:cNvPicPr/></pic:nvPicPr>
      <pic:blipFill><a:blip r:embed="rId1"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
      <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm>
      <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>
      </a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'''
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0"?>
          <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Default Extension="png" ContentType="image/png"/>
          <Override PartName="/word/document.xml"
          ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
          </Types>''')
        archive.writestr("_rels/.rels", '''<Relationships
          xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
          Target="word/document.xml"/></Relationships>''')
        archive.writestr("word/document.xml", '''<w:document
          xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
          xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
          xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
          <w:body><w:p><w:r><w:t>Hello DOCX triage</w:t></w:r></w:p>'''
          + drawing + '<w:sectPr/></w:body></w:document>')
        archive.writestr("word/_rels/document.xml.rels", '''<Relationships
          xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
          Target="media/img.png"/></Relationships>''')
        with archive.open("word/media/img.png", "w") as image:
            image.write(png())
            if big_image:
                # Highly compressible expanded media, bigger than the real 50 MiB cap.
                for _ in range(mc.MAX_OUTPUT_BYTES // (1024 * 1024) + 1):
                    image.write(b"\0" * (1024 * 1024))


def build_fixtures(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    write_pdf(root / "sample.pdf")
    write_docx(root / "sample.docx")
    (root / "sample.html").write_text('''<!doctype html><html><body><p>Hello HTML triage</p>
      <img src="file:///etc/passwd"><img src="http://example.invalid/x.png">
      </body></html>''', encoding="utf-8")


def cli(src, kind, outdir, env=None):
    proc = subprocess.run([sys.executable, str(CODE / "md_convert.py"), str(src), kind, str(outdir)],
                          capture_output=True, text=True, timeout=75, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", proc.stderr
    assert len(proc.stdout.splitlines()) == 1, proc.stdout
    return json.loads(proc.stdout)


def check_success(result, text):
    assert result["ok"], result
    md = Path(result["md"])
    assert text in md.read_text(), md.read_text()
    assert result["md_sha256"] == hashlib.sha256(md.read_bytes()).hexdigest()
    assert result["md_bytes"] == md.stat().st_size
    with zipfile.ZipFile(result["zip"]) as archive:
        names = archive.namelist()
        assert md.name in names, names
        assert all(not n.startswith("/") and ".." not in Path(n).parts for n in names)
        assert len(names) == result["media_count"] + 1, names


def pdf_case(root):
    require("pdftotext", "pdfimages")
    result = cli(root / "sample.pdf", "pdf", root / "out")
    check_success(result, "Hello PDF triage")
    assert result["media_count"] == 1, result
    assert "(sample_md/img-000.png)" in Path(result["md"]).read_text()


def docx_case(root):
    require("pandoc")
    result = cli(root / "sample.docx", "docx", root / "out")
    check_success(result, "Hello DOCX triage")
    assert result["media_count"] == 1, result
    text = Path(result["md"]).read_text()
    assert "sample_md/" in text and str(root) not in text, text


def docx_bomb(root):
    require("pandoc")
    write_docx(root / "bomb.docx", big_image=True)
    result = cli(root / "bomb.docx", "docx", root / "out")
    assert not result["ok"] and "convert_output_cap" in result["flags"], result
    assert not list((root / "out").iterdir())


def html_case(root):
    require("pandoc")
    result = cli(root / "sample.html", "html", root / "out")
    check_success(result, "Hello HTML triage")
    text = Path(result["md"]).read_text()
    assert "root:x:" not in text and Path("/etc/passwd").read_text() not in text
    assert result["media_count"] == 0, result


def nonregular_case(root):
    md, media = root / "sample.md", root / "sample_md"
    md.write_text("text")
    media.mkdir()
    (media / "pixel.png").write_bytes(png())
    (media / "secret").symlink_to("/etc/passwd")
    (media / "directory").symlink_to(root, target_is_directory=True)
    os.mkfifo(media / "pipe")
    flags = []
    count = mc.make_archive(str(md), str(media), str(root / "result.zip"), flags,
                            time.monotonic() + 5)
    with zipfile.ZipFile(root / "result.zip") as archive:
        assert set(archive.namelist()) == {"sample.md", "sample_md/pixel.png"}
    assert count == 1 and flags == ["convert_skipped_nonregular"], (count, flags)


def missing_tool(root):
    env = dict(os.environ, PATH=str(root / "absent-tools"))
    result = cli(root / "sample.html", "html", root / "out", env)
    assert result["flags"] == ["convert_tool_missing:pandoc"] and not result["ok"], result
    pdf = cli(root / "sample.pdf", "pdf", root / "pdf-out", env)
    assert set(pdf["flags"]) == {"convert_tool_missing:pdftotext", "convert_tool_missing:pdfimages"}


def malformed_pdf(root):
    require("pdftotext", "pdfimages")
    (root / "broken.pdf").write_text("not a pdf")
    result = cli(root / "broken.pdf", "pdf", root / "out")
    assert not result["ok"] and any(f.startswith("convert_error:") for f in result["flags"]), result
    assert not list((root / "out").iterdir())


def cap_case(root, count=False):
    md, media = root / "sample.md", root / "sample_md"
    md.write_text("abc")
    media.mkdir()
    for i in range(3):
        (media / str(i)).write_bytes(b"1234")
    with mock.patch.object(mc, "MAX_MEDIA_COUNT" if count else "MAX_OUTPUT_BYTES", 2 if count else 10):
        try:
            mc.make_archive(str(md), str(media), str(root / "result.zip"), [], time.monotonic() + 5)
        except mc.OutputCap:
            return
    raise AssertionError("cap did not trigger")


def conversion_timeout(root):
    # Real timeout path with a tiny local budget; no real parser is started.
    with mock.patch.object(mc, "CONVERT_TIMEOUT", -1), mock.patch.object(mc.shutil, "which", return_value="tool"):
        result = mc.convert(str(root / "sample.html"), "html", str(root / "out"))
    assert not result["ok"] and result["flags"] == ["convert_timeout"], result
    assert not list((root / "out").iterdir())


def conversion_cap_cleanup(root):
    # A simulated parser exercises the full API's cap failure and cleanup,
    # independent of the explicitly skipped real pandoc integration cases.
    def output(args, outdir, deadline):
        Path(args[args.index("-o") + 1]).write_bytes(b"x" * 11)
        return 0

    with mock.patch.object(mc, "MAX_OUTPUT_BYTES", 10), \
            mock.patch.object(mc.shutil, "which", return_value="pandoc"), \
            mock.patch.object(mc, "run_tool", side_effect=output):
        result = mc.convert(str(root / "sample.html"), "html", str(root / "out"))
    assert not result["ok"] and result["flags"] == ["convert_output_cap"], result
    assert not list((root / "out").iterdir())


def docx_preflight_cap(root):
    # Only the stdlib metadata guard, not a real DOCX conversion.
    with mock.patch.object(mc, "MAX_OUTPUT_BYTES", 10), \
            mock.patch.object(mc.shutil, "which", return_value="pandoc"), \
            mock.patch.object(mc, "run_tool") as run:
        result = mc.convert(str(root / "sample.docx"), "docx", str(root / "out"))
    assert not result["ok"] and result["flags"] == ["convert_output_cap"], result
    run.assert_not_called()
    assert not list((root / "out").iterdir())


def parser_limits_case(root):
    expected = [mc.MAX_ADDRESS_SPACE, mc.MAX_OUTPUT_BYTES, mc.MAX_CPU_SECONDS, mc.MAX_OPEN_FILES]
    code = "import json,resource; print(json.dumps([resource.getrlimit(k)[0] for k in " \
           "(resource.RLIMIT_AS,resource.RLIMIT_FSIZE,resource.RLIMIT_CPU,resource.RLIMIT_NOFILE)]))"
    proc = subprocess.run([sys.executable, "-c", code], preexec_fn=mc.parser_limits,
                          capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0 and json.loads(proc.stdout) == expected, proc


def daemon_paths(root):
    inbox, outbox, work = root / "inbox", root / "outbox", root / "work"
    inbox.mkdir()
    outbox.mkdir()
    reader = root / "reader.py"
    reader.write_text('''import json,sys,pathlib
p = pathlib.Path(sys.argv[1])
print(json.dumps({"summary": str(p), "flags": []}))
''')
    return dict(inbox=str(inbox), outbox=str(outbox), work_root=str(work), reader=str(reader))


def assert_daemon_clean(root, name):
    assert not list((root / "inbox").iterdir()), list((root / "inbox").iterdir())
    assert not (root / "work").exists() or not list((root / "work").iterdir())
    return json.loads((root / "outbox" / (name + ".json")).read_text())


def fake_converter(root, mode="ok"):
    path = root / "converter.py"
    path.write_text('''import hashlib,json,pathlib,sys,zipfile,time
src,kind,out = sys.argv[1:]
out = pathlib.Path(out)
md = out / "converted.md"
md.write_text("converted text")
dest = out / "converted.md.zip"
with zipfile.ZipFile(dest, "w") as z: z.write(md, md.name)
result = {"ok": True, "md": str(md), "zip": str(dest), "tool": "fake",
          "md_sha256": hashlib.sha256(md.read_bytes()).hexdigest(), "md_bytes": md.stat().st_size,
          "media_count": 0, "flags": ["convert_skipped_nonregular"]}
''' + {
        "ok": "print(json.dumps(result))\n",
        "error": 'print(json.dumps({"ok":False,"flags":["convert_output_cap"]}))\n',
        "timeout": 'time.sleep(10)\n',
        "invalid": 'print("not JSON")\n',
    }[mode])
    return str(path)


def daemon_case(root, mode="ok", kind="pdf"):
    paths = daemon_paths(root)
    paths["converter"] = fake_converter(root, mode)
    name = "input.pdf"
    (root / "inbox" / name).write_text("original bytes")
    if kind is not None:
        (root / "inbox" / ("." + name + ".kind")).write_text(kind + "\n")
    with mock.patch.object(daemon, "CONVERT_TIMEOUT", 0.1 if mode == "timeout" else 60):
        daemon.process(name, **paths)
    result = assert_daemon_clean(root, name)
    if kind in (None, "invalid"):
        assert result == {"summary": str(root / "inbox" / name), "flags": []}, result
        assert "converted" not in result
    elif mode == "ok":
        assert result["summary"].endswith(".md") and "/work/" in result["summary"], result
        assert result["flags"] == ["convert_skipped_nonregular"], result
        assert result["converted"]["tool"] == "fake", result
        assert (root / "outbox" / (name + ".md.zip")).exists()
    else:
        assert result["summary"] == "", result  # Reader would have returned a nonempty path.
        expected = {"error": "convert_output_cap", "timeout": "convert_timeout",
                    "invalid": "convert_error:JSONDecodeError"}[mode]
        assert result["flags"] == [expected], result
        assert not (root / "outbox" / (name + ".md.zip")).exists()


def daemon_real_pdf(root):
    require("pdftotext", "pdfimages")
    paths = daemon_paths(root)
    paths["converter"] = str(CODE / "md_convert.py")
    shutil.copyfile(root / "sample.pdf", root / "inbox" / "sample.pdf")
    (root / "inbox" / ".sample.pdf.kind").write_text("pdf\n")
    daemon.process("sample.pdf", **paths)
    result = assert_daemon_clean(root, "sample.pdf")
    assert result["summary"].endswith("sample.md"), result
    assert result["converted"]["media_count"] == 1, result


def daemon_reader_timeout(root):
    paths = daemon_paths(root)
    paths["converter"] = fake_converter(root)
    Path(paths["reader"]).write_text("import time; time.sleep(10)")
    (root / "inbox" / "input").write_text("bytes")
    (root / "inbox" / ".input.kind").write_text("pdf\n")
    with mock.patch.object(daemon, "READER_TIMEOUT", 0.1):
        daemon.process("input", **paths)
    result = assert_daemon_clean(root, "input")
    assert "triage_timeout" in result["flags"] and "converted" in result, result


def daemon_publication_failure(root):
    paths = daemon_paths(root)
    paths["converter"] = fake_converter(root)
    (root / "inbox" / "input").write_text("bytes")
    (root / "inbox" / ".input.kind").write_text("pdf\n")
    actual_replace = os.replace
    published = []

    def publish(src, dest):
        if dest.endswith(".json"):
            assert published == ["zip"], published
            raise PermissionError("simulated JSON publication failure")
        assert src.endswith(".md.zip.tmp") and dest.endswith(".md.zip"), (src, dest)
        with zipfile.ZipFile(src) as archive:
            assert archive.testzip() is None
        actual_replace(src, dest)
        published.append("zip")

    with mock.patch.object(daemon.os, "replace", side_effect=publish):
        try:
            daemon.process("input", **paths)
        except PermissionError:
            pass
        else:
            raise AssertionError("publication unexpectedly succeeded")
    assert not list((root / "inbox").iterdir())
    assert not list((root / "work").iterdir())
    assert not list((root / "outbox").glob("*.tmp"))


def load_intake():
    # Existing intake reads its production token at import; replace only that
    # read in the test. No production directories or network are accessed.
    spec = importlib.util.spec_from_file_location("intake_under_test", CODE / "intake_poller.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch("builtins.open", return_value=io.StringIO("test-token")):
        spec.loader.exec_module(module)
    return module


def intake_case(root, mime="application/pdf", expected_kind="pdf"):
    intake = load_intake()
    inbox, outbox = root / "inbox", root / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    intake.TRIAGE_INBOX, intake.TRIAGE_OUTBOX = str(inbox), str(outbox)
    qpath = root / "quarantined.file"
    qpath.write_text("source")
    actual_rename = os.rename
    metadata = {"tool": "fake", "md_sha256": "abc", "md_bytes": 4, "media_count": 0}

    def handoff(src, dest, **kwargs):
        if str(dest) == qpath.name and kwargs.get("dst_dir_fd") is not None:
            kind_file = inbox / ("." + qpath.name + ".kind")
            if expected_kind:
                assert kind_file.read_text() == expected_kind + "\n"
            else:
                assert not kind_file.exists()
            assert Path(src).name.startswith(".")
            assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
            actual_rename(src, dest, **kwargs)
            if expected_kind:
                (outbox / (qpath.name + ".md.zip")).write_bytes(b"zip bytes")
            (outbox / (qpath.name + ".json")).write_text(json.dumps(
                {"summary": "ok", "flags": [], **({"converted": metadata} if expected_kind else {})}))
        else:
            actual_rename(src, dest, **kwargs)

    with mock.patch.object(intake.os, "rename", side_effect=handoff), \
            mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())), \
            mock.patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())):
        result = intake.triage(str(qpath), mime)
    assert result["summary"] == "ok", result
    assert qpath.read_text() == "source"
    if expected_kind:
        assert result["converted"] == metadata
        assert (root / (qpath.name + ".md.zip")).read_bytes() == b"zip bytes"
    assert not list(outbox.iterdir()), list(outbox.iterdir())


def intake_handoff_failure(root):
    intake = load_intake()
    inbox = root / "inbox"
    inbox.mkdir()
    intake.TRIAGE_INBOX = str(inbox)
    qpath = root / "source.pdf"
    qpath.write_text("source")
    with mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())), \
            mock.patch.object(intake.os, "rename", side_effect=OSError("simulated rename failure")):
        result = intake.triage(str(qpath), "application/pdf")
    assert result == {"status": "SKIPPED:handoff_failed:OSError"}, result
    assert not list(inbox.iterdir()), list(inbox.iterdir())
    assert qpath.read_text() == "source"


def intake_label_case(root):
    intake = load_intake()
    intake.INCOMING, intake.QUARANTINE = str(root / "incoming"), str(root / "quarantine")
    Path(intake.INCOMING).mkdir()
    Path(intake.QUARANTINE).mkdir()
    metadata = {"tool": "fake", "md_sha256": "abc", "md_bytes": 4, "media_count": 0}
    replies = []

    def download(file_id, dest):
        Path(dest).write_text("original")

    def triage(path, mime):
        Path(path + ".md.zip").write_bytes(b"archive")
        return {"summary": "preview", "flags": [], "converted": metadata}

    label = {"verdict": "quarantine", "av": "clean", "mime": "application/pdf", "sha256": "1234"}
    with mock.patch.object(intake, "download", side_effect=download), \
            mock.patch.object(intake, "triage", side_effect=triage), \
            mock.patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(label))), \
            mock.patch.object(intake, "send_document", return_value={"ok": True}) as send, \
            mock.patch.object(intake, "reply", side_effect=lambda chat, text, **kw: replies.append(text)):
        intake.handle_file({"document": {"file_id": "id", "file_name": "sample.pdf"},
                            "chat": {"id": 1}, "message_id": 23})
    saved = json.loads((Path(intake.QUARANTINE) / "23_sample.pdf.safety.json").read_text())
    assert saved["verdict"] == "quarantine" and saved["converted"] == metadata, saved
    assert len(replies) == 1 and "📝 md-версия приложена ниже" in replies[0], replies
    send.assert_called_once_with(1, str(Path(intake.QUARANTINE) / "23_sample.pdf.md.zip"),
                                 "sample.pdf.md.zip", 23, thread_id=None)


def cli_invalid(root):
    result = cli(root / "sample.pdf", "unknown", root / "out")
    assert not result["ok"] and result["flags"] == ["convert_error:ValueError"], result
    proc = subprocess.run([sys.executable, str(CODE / "md_convert.py")], capture_output=True, text=True)
    assert not json.loads(proc.stdout)["ok"] and proc.returncode == 0 and not proc.stderr
    assert mc.safe_stem("/tmp/../../..pdf") not in (".", "..", "")
    assert len(mc.safe_stem("я" * 200 + ".pdf")) == 100


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--fixtures":
        build_fixtures(sys.argv[2])
        return 0
    cases = [
        ("PDF: текст, изображение, относительная ссылка, ZIP и SHA256", pdf_case),
        ("DOCX: абзац и картинка в Markdown/ZIP", docx_case),
        ("DOCX: картинка >50 MiB -> convert_output_cap", docx_bomb),
        ("HTML: file:// и HTTP картинки без чтения /etc/passwd", html_case),
        ("ZIP: симлинки (файл/каталог) и FIFO исключены", nonregular_case),
        ("CLI: отсутствующие инструменты", missing_tool),
        ("PDF: испорченный вход, JSON ошибки и очистка", malformed_pdf),
        ("ZIP: суммарный размер ограничен", cap_case),
        ("ZIP: число картинок ограничено", lambda r: cap_case(r, True)),
        ("Конвертер: общий таймаут и очистка", conversion_timeout),
        ("Конвертер API: квота выхода и очистка (парсер подменён)", conversion_cap_cleanup),
        ("DOCX: ранняя квота media по ZIP metadata (без запуска pandoc)", docx_preflight_cap),
        ("Парсер: все четыре RLIMIT в дочернем процессе", parser_limits_case),
        ("Демон: kind -> .md, ZIP и metadata, очистка", daemon_case),
        ("Демон: без kind -> прежний путь", lambda r: daemon_case(r, kind=None)),
        ("Демон: невалидный kind -> прежний путь и очистка", lambda r: daemon_case(r, kind="invalid")),
        ("Демон: ошибка конвертера -> ридер не вызван", lambda r: daemon_case(r, mode="error")),
        ("Демон: таймаут конвертера -> ридер не вызван", lambda r: daemon_case(r, mode="timeout")),
        ("Демон: невалидный JSON конвертера -> ридер не вызван", lambda r: daemon_case(r, mode="invalid")),
        ("Демон: реальный PDF -> .md и архив", daemon_real_pdf),
        ("Демон: таймаут ридера, архив и очистка сохранены", daemon_reader_timeout),
        ("Демон: ZIP атомарно раньше JSON; очистка при сбое публикации", daemon_publication_failure),
        ("Приём: PDF kind до rename, архив рядом с оригиналом", intake_case),
        ("Приём: DOCX kind до rename", lambda r: intake_case(r,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")),
        ("Приём: HTML kind до rename", lambda r: intake_case(r, "text/html", "html")),
        ("Приём: обычный текст без kind", lambda r: intake_case(r, "text/plain", None)),
        ("Приём: сбой rename удаляет kind и partial", intake_handoff_failure),
        ("Приём: metadata в метке, quarantine сохранён, строка в чате", intake_label_case),
        ("CLI: неверные аргументы и безопасный stem", cli_invalid),
    ]
    passed = skipped = failed = 0
    for name, case in cases:
        try:
            with tempfile.TemporaryDirectory(prefix="md-convert-case-") as directory:
                root = Path(directory)
                build_fixtures(root)
                case(root)
            print("PASS " + name, flush=True)
            passed += 1
        except Skip as exc:
            print(f"SKIP {name}: {exc}", flush=True)
            skipped += 1
        except Exception as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            failed += 1
    print(f"прошло {passed}, SKIP {skipped}, упало {failed}")
    return int(failed != 0)


if __name__ == "__main__":
    sys.exit(main())
