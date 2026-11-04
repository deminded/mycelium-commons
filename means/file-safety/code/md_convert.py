#!/usr/bin/env python3
"""Disposable PDF/DOCX/HTML -> Markdown converter; stdout is exactly one JSON.

Run as fs-triage, never in the intake process holding the bot token. outdir
must be a private, empty directory. Parsers inherit the uid's network policy.
"""
import ctypes
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile

# Bound parser work and expanded DOCX/PDF media: small inputs can expand to GBs.
MAX_PAGES = 200
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_MEDIA_COUNT = 200
MAX_ADDRESS_SPACE = 1024 * 1024 * 1024
MAX_CPU_SECONDS = 30
MAX_OPEN_FILES = 256
CLEANUP_TIMEOUT = 2  # Retry-loop budget, not a bound on convert()/file cleanup.
SCAN_TIMEOUT = 1
SIGNAL_TIMEOUT = 1  # Reserved afresh after each scan, including an expired scan.
OUTPUT_POLL_SECONDS = 0.2
CONVERT_TIMEOUT = 60  # One budget shared by all tools, validation and ZIP writing.


class OutputCap(Exception):
    pass


class ConvertTimeout(Exception):
    pass


class NoOutput(Exception):
    pass


class SubreaperUnavailable(Exception):
    pass


class CleanupUnknown(Exception):
    pass


def become_subreaper():
    # Orphaned parser grandchildren (including setsid/double-fork daemons)
    # must be adopted here rather than escape to init under the shared uid.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    except (OSError, AttributeError) as exc:
        raise SubreaperUnavailable() from exc


def descendants(deadline=None):
    """Best-effort PPID snapshot plus unreadable entries; keep known children."""
    children, errors = {}, []
    me = os.getpid()
    found, pending = {me}, [me]
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if deadline is not None and time.monotonic() >= deadline:
                    errors.append("deadline")
                    break
                if not entry.name.isdigit():
                    continue
                try:
                    with open(entry.path + "/stat", "rb") as stream:
                        fields = stream.read().rsplit(b")", 1)[1].split()
                    pid, parent = int(entry.name), int(fields[1])
                    children.setdefault(parent, []).append(pid)
                    # Keep already proven descendants even if the scan expires
                    # before the graph phase (especially direct/adopted children).
                    if parent in found and pid not in found:
                        found.add(pid)
                        pending.append(pid)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, ValueError, IndexError):
                    errors.append(entry.name)
    except OSError:
        errors.append("proc")
    while pending:
        if deadline is not None and time.monotonic() >= deadline:
            errors.append("graph_deadline")
            break
        parent = pending.pop()
        for pid in children.get(parent, ()):
            if deadline is not None and time.monotonic() >= deadline:
                errors.append("graph_deadline")
                return found - {me}, errors
            if pid not in found:
                found.add(pid)
                pending.append(pid)
    return found - {me}, errors


def reap_descendants():
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    unknown = False
    empty_scans = 0
    while time.monotonic() < deadline:
        children, errors = descendants(min(deadline, time.monotonic() + SCAN_TIMEOUT))
        # Scanning cannot spend the budget reserved for known PIDs.
        signal_deadline = time.monotonic() + SIGNAL_TIMEOUT
        unknown |= bool(errors)
        for pid in children:
            if time.monotonic() >= signal_deadline:
                raise CleanupUnknown()
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                unknown = True
        for pid in children:
            if time.monotonic() >= signal_deadline:
                raise CleanupUnknown()
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError):
                pass  # Deeper children may only be adopted on the next scan.
            except OSError:
                unknown = True
        empty_scans = empty_scans + 1 if not children else 0
        if empty_scans >= 2:
            if unknown:
                raise CleanupUnknown()
            return
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    raise CleanupUnknown()


def safe_stem(src):
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(os.path.basename(src))[0])[:100]
    return stem if stem and stem not in (".", "..") else "file"


def parser_limits():
    for key, value in (
        (resource.RLIMIT_AS, MAX_ADDRESS_SPACE),
        (resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES),
        (resource.RLIMIT_CPU, MAX_CPU_SECONDS),
        (resource.RLIMIT_NOFILE, MAX_OPEN_FILES),
    ):
        resource.setrlimit(key, (value, value))


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ConvertTimeout()
    return seconds


def check_live_output(outdir, deadline):
    """Count all output, even unexpected names, without following directory links."""
    total = files = directories = 0

    def visit(fd, depth):
        nonlocal total, files, directories
        if depth > 64:
            raise OutputCap()
        with os.scandir(fd) as entries:
            for entry in entries:
                remaining(deadline)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    directories += 1
                    if directories > MAX_MEDIA_COUNT + 1:
                        raise OutputCap()
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, depth + 1)
                    finally:
                        os.close(child)
                else:
                    files += 1
                    total += info.st_size
                    if files > MAX_MEDIA_COUNT + 1 or total > MAX_OUTPUT_BYTES:
                        raise OutputCap()
    fd = os.open(outdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(fd, 0)
    finally:
        os.close(fd)


def run_tool(args, outdir, deadline):
    # A separate process group is killed on cap/timeout, before subreaper cleanup.
    remaining(deadline)
    proc = subprocess.Popen(
        args, cwd=outdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=parser_limits, start_new_session=True,
    )
    try:
        while True:
            check_live_output(outdir, deadline)
            try:
                code = proc.wait(timeout=min(OUTPUT_POLL_SECONDS, remaining(deadline)))
                check_live_output(outdir, deadline)
                if code == -signal.SIGXFSZ:
                    raise OutputCap()
                return code
            except subprocess.TimeoutExpired:
                remaining(deadline)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # No unbounded wait even after SIGKILL; outer cleanup handles descendants.
        try:
            proc.wait(timeout=CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise CleanupUnknown() from exc


def output_files(md, media, flags, deadline=None):
    """lstat every entry; never descend into or archive a symlink/special file."""
    files = []
    total = 0
    media_count = 0

    def visit(path, is_media=False):
        nonlocal total, media_count
        if deadline is not None:
            remaining(deadline)
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
            media_count += int(is_media)
            if total > MAX_OUTPUT_BYTES or media_count > MAX_MEDIA_COUNT:
                raise OutputCap()
            files.append(path)
        elif stat.S_ISDIR(info.st_mode) and is_media:
            with os.scandir(path) as entries:
                for entry in entries:
                    visit(entry.path, True)
        elif "convert_skipped_nonregular" not in flags:
            flags.append("convert_skipped_nonregular")

    visit(md)
    if os.path.lexists(media):
        visit(media, True)
    return files, media_count


def make_archive(md, media, dest, flags, deadline):
    files, media_count = output_files(md, media, flags, deadline)
    root = os.path.dirname(md)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            remaining(deadline)
            # Stream in bounded chunks and check the common timeout while zipping.
            with open(path, "rb") as src, archive.open(os.path.relpath(path, root), "w") as out:
                while True:
                    remaining(deadline)
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    remaining(deadline)
    return media_count


def convert(src, kind, outdir):
    result = {"ok": False, "md": "", "zip": "", "tool": "",
              "md_sha256": "", "md_bytes": 0, "media_count": 0, "flags": []}
    flags = result["flags"]
    created = []
    deadline = time.monotonic() + CONVERT_TIMEOUT
    subreaper = False
    try:
        become_subreaper()
        subreaper = True
        _, proc_errors = descendants(time.monotonic() + SCAN_TIMEOUT)
        if proc_errors:
            raise CleanupUnknown()  # Fail before tools if /proc is incomplete.
        if kind not in ("pdf", "docx", "html"):
            raise ValueError("unknown kind")
        src, outdir = os.path.abspath(src), os.path.abspath(outdir)
        os.makedirs(outdir, mode=0o700, exist_ok=True)
        if os.listdir(outdir):
            raise FileExistsError("outdir must be empty")
        stem = safe_stem(src)
        md = os.path.join(outdir, stem + ".md")
        media = os.path.join(outdir, stem + "_md")
        dest = os.path.join(outdir, stem + ".md.zip")
        tools = ("pdftotext", "pdfimages") if kind == "pdf" else ("pandoc",)
        result["tool"] = "+".join(tools)
        for tool in tools:
            if shutil.which(tool) is None:
                flags.append("convert_tool_missing:" + tool)
        if flags:
            return result
        created = [md, media, dest]
        if kind == "docx":
            # Check declared expanded media size before invoking a ZIP parser.
            # Still validate actual parser output below; metadata is untrusted.
            with zipfile.ZipFile(src) as docx:
                images = [i for i in docx.infolist() if i.filename.startswith("word/media/")
                          and not i.is_dir()]
                if (sum(i.file_size for i in images) > MAX_OUTPUT_BYTES
                        or len(images) > MAX_MEDIA_COUNT):
                    raise OutputCap()
            codes = [run_tool(["pandoc", "-f", "docx", "-t", "gfm", "--sandbox",
                               "--extract-media=" + stem + "_md", "-o", md, src], outdir, deadline)]
        elif kind == "html":
            codes = [run_tool(["pandoc", "-f", "html", "-t", "gfm", "--sandbox",
                               "-o", md, src], outdir, deadline)]
        else:
            os.mkdir(media, mode=0o700)
            codes = [run_tool(["pdftotext", "-layout", "-l", str(MAX_PAGES), src, md],
                              outdir, deadline)]
            codes.append(run_tool(["pdfimages", "-png", "-l", str(MAX_PAGES), src,
                                   os.path.join(media, "img")], outdir, deadline))
        # Stop all remaining parser processes before inspecting their output.
        reap_descendants()
        # Preserve the output-cap diagnosis on partial oversized output, but do
        # not misreport a failed tool as a missing file.
        if os.path.lexists(md):
            files, _ = output_files(md, media, flags, deadline)
        else:
            files = []
        if any(codes):
            raise subprocess.CalledProcessError(next(c for c in codes if c), tools)
        if md not in files or os.stat(md).st_size == 0:
            raise NoOutput()
        if kind == "pdf":
            with open(md, "a", encoding="utf-8") as out:
                for path in sorted(files):
                    if path != md:
                        link = os.path.relpath(path, outdir).replace(os.sep, "/")
                        out.write(f"\n\n![{os.path.basename(path)}]({link})\n")
        count = make_archive(md, media, dest, flags, deadline)
        with open(md, "rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                remaining(deadline)
                digest.update(chunk)
        remaining(deadline)
        result.update(ok=True, md=md, zip=dest, md_sha256=digest.hexdigest(),
                      md_bytes=os.stat(md).st_size, media_count=count)
    except SubreaperUnavailable:
        flags.append("convert_subreaper_unavailable")
    except CleanupUnknown:
        flags.append("convert_cleanup_unknown")
    except NoOutput:
        flags.append("convert_no_output")
    except (ConvertTimeout, subprocess.TimeoutExpired):
        flags.append("convert_timeout")
    except OutputCap:
        flags.append("convert_output_cap")
    except Exception as exc:
        flags.append("convert_error:" + type(exc).__name__)
    finally:
        if subreaper:
            try:
                reap_descendants()
            except Exception as exc:
                result.update(ok=False, md="", zip="", md_sha256="", md_bytes=0, media_count=0)
                flag = ("convert_cleanup_unknown" if isinstance(exc, CleanupUnknown)
                        else "convert_cleanup_error:" + type(exc).__name__)
                if flag not in flags:
                    flags.append(flag)
        if not result["ok"]:
            for path in created:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
    return result


def main():
    if len(sys.argv) == 4:
        result = convert(*sys.argv[1:])
    else:
        result = {"ok": False, "md": "", "zip": "", "tool": "", "md_sha256": "",
                  "md_bytes": 0, "media_count": 0, "flags": ["convert_error:ValueError"]}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
