#!/usr/bin/env python3
"""
triage_daemon — inbox poller for the semantic gate (runs as uid fs-triage).

The intake worker drops <name> into inbox/ (the file to triage) and the daemon
answers with outbox/<name>.json = {summary, flags}. An optional .<name>.kind
requests disposable conversion first; the reader then sees Markdown, and the
archive is published as outbox/<name>.md.zip with metadata in the JSON. Each
file is handed to a FRESH triage_reader.py process — one-shot, no shared state between files, so a
reader corrupted by one file can't carry anything into the next.

The daemon holds no long-lived model context and does no interpretation of its
own: it moves bytes and enforces the timeout. All the untrusted reading happens
in the disposable child.
"""
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from md_convert import MAX_OUTPUT_BYTES

INBOX = "/opt/mycelium/fs-triage/inbox"
OUTBOX = "/opt/mycelium/fs-triage/outbox"
READER = "/opt/mycelium/fs-triage/triage_reader.py"
CONVERTER = "/opt/mycelium/fs-triage/md_convert.py"
WORK_ROOT = "/opt/mycelium/fs-triage/work"
CONVERT_TIMEOUT = 60  # Keep in sync with md_convert.py; separate from reader time.
CONVERT_CLEANUP_GRACE = 5  # Let the converter kill/reap setsid descendants first.
READER_TIMEOUT = 90
POLL_SECONDS = 3
REJECTED_MAX_OBJECTS = 128
REJECTED_CLEANUP_SECONDS = 0.2


class WorkUnavailable(Exception):
    pass


class CleanupBudgetReached(Exception):
    pass


def cleanup_rejected(outbox=None):
    """One bounded, fd-relative cleanup pass by the producer uid, never intake."""
    deadline = time.monotonic() + REJECTED_CLEANUP_SECONDS
    visited = 0

    def check_budget():
        if visited >= REJECTED_MAX_OBJECTS or time.monotonic() >= deadline:
            raise CleanupBudgetReached()

    def remove(parent, name, depth=0):
        nonlocal visited
        check_budget()
        visited += 1
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if depth >= 64:
                    print("[warn] rejected cleanup depth limit; manual cleanup needed", flush=True)
                    return
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            remove(fd, entry.name, depth + 1)
                    if time.monotonic() >= deadline:
                        raise CleanupBudgetReached()
                    os.rmdir(name, dir_fd=parent)
                finally:
                    os.close(fd)
            else:
                os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[warn] rejected cleanup failed: {type(exc).__name__}", flush=True)

    queue = rejected = None
    try:
        queue = os.open(outbox or OUTBOX, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        rejected = os.open(".rejected", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=queue)
        with os.scandir(rejected) as entries:
            for entry in entries:
                remove(rejected, entry.name)
    except (FileNotFoundError, CleanupBudgetReached):
        pass
    except OSError as exc:
        print(f"[warn] rejected cleanup failed: {type(exc).__name__}", flush=True)
    finally:
        for fd in (rejected, queue):
            if fd is not None:
                os.close(fd)
    return visited


def convert_file(src, kind, work, converter):
    # A process group lets the daemon stop the converter AND its parser on timeout.
    with subprocess.Popen(
        [sys.executable, converter, src, kind, work],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        start_new_session=True,
    ) as proc:
        try:
            stdout, _ = proc.communicate(timeout=CONVERT_TIMEOUT + CONVERT_CLEANUP_GRACE)
        finally:
            # Emergency fallback for a stuck converter. Normal cleanup (including
            # setsid descendants) is done by the converter's subreaper.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    if proc.returncode:
        raise ValueError("converter failed without a result")
    return json.loads(stdout)


def work_file(path, work):
    """Only accept regular converter output inside this disposable directory."""
    if (os.path.commonpath([os.path.realpath(path), work]) != work
            or not stat.S_ISREG(os.lstat(path).st_mode)):
        raise ValueError("invalid converter output path")
    return path


def process(name, *, inbox=None, outbox=None, work_root=None, reader=None, converter=None):
    # Explicit dependency paths support local tests without touching production.
    inbox, outbox = inbox or INBOX, outbox or OUTBOX
    reader, converter = reader or READER, converter or CONVERTER
    work_root = work_root or WORK_ROOT
    src = os.path.join(inbox, name)
    kind_path = os.path.join(inbox, "." + name + ".kind")
    out = os.path.join(outbox, name + ".json")
    tmp = out + ".tmp"
    zip_out = os.path.join(outbox, name + ".md.zip")
    zip_tmp = zip_out + ".tmp"
    work = None
    converted = None
    conversion_flags = []
    try:
        try:
            try:
                with open(kind_path, encoding="ascii") as f:
                    line = f.read(16)
                    kind = line.strip() if len(line) < 16 and len(line.splitlines()) == 1 else ""
            except (FileNotFoundError, UnicodeError):
                kind = ""
            target = src
            result = None
            if kind in ("pdf", "docx", "html"):
                try:
                    try:
                        os.makedirs(work_root, mode=0o700, exist_ok=True)
                        space = os.statvfs(work_root)
                        if space.f_bavail * space.f_frsize < MAX_OUTPUT_BYTES * 2:
                            raise WorkUnavailable()
                    except OSError as exc:
                        raise WorkUnavailable() from exc
                    work = os.path.realpath(tempfile.mkdtemp(prefix="convert-", dir=work_root))
                    conversion = convert_file(src, kind, work, converter)
                    conversion_flags = conversion["flags"]
                    if not conversion["ok"]:
                        result = {"summary": "", "flags": conversion_flags}
                    else:
                        target = work_file(conversion["md"], work)
                        archive = work_file(conversion["zip"], work)
                        metadata = {key: conversion[key] for key in
                                    ("tool", "md_sha256", "md_bytes", "media_count")}
                        # Archive is complete before publishing the JSON queue reply.
                        shutil.copyfile(archive, zip_tmp)
                        os.replace(zip_tmp, zip_out)
                        converted = metadata
                except WorkUnavailable:
                    result = {"summary": "", "flags": ["convert_work_unavailable"]}
                except subprocess.TimeoutExpired:
                    result = {"summary": "", "flags": ["convert_timeout"]}
                except Exception as e:
                    result = {"summary": "", "flags": ["convert_error:" + type(e).__name__]}
            if result is None:
                proc = subprocess.run(
                    [sys.executable, reader, target],
                    capture_output=True, text=True, timeout=READER_TIMEOUT,
                )
                result = json.loads(proc.stdout)
                if conversion_flags:
                    result["flags"] = list(dict.fromkeys(result["flags"] + conversion_flags))
        except subprocess.TimeoutExpired:
            result = {"summary": "", "flags": ["triage_timeout"] + conversion_flags}
        except Exception as e:
            result = {"summary": "", "flags": ["triage_daemon_error:" + type(e).__name__]}
        if converted is not None:
            result["converted"] = converted
        # write atomically so the consumer never reads a half-written result
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, out)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        for path in (src, kind_path, tmp, zip_tmp):
            try:
                os.remove(path)   # inbox is a queue, not storage
            except OSError:
                pass


def main():
    print(f"[start] triage daemon watching {INBOX}", flush=True)
    while True:
        cleanup_rejected()
        try:
            names = [n for n in os.listdir(INBOX) if not n.startswith(".")]
        except FileNotFoundError:
            names = []
        for name in sorted(names):
            try:
                process(name)
                print(f"[triaged] {name}", flush=True)
            except Exception as e:
                print(f"[err] {name}: {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
