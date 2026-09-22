#!/usr/bin/env python3
"""
file-safety-check — static+AV gate for the Mycelium file-exchange circuit.

Source of truth: /opt/workspace/vault/Projects/Грибница/file-safety-design.md
Implements phases 2 (static gate), 3 (ClamAV), 5 (honest label), 7 (journal).
Phase 4 (LLM triage in an isolated uid) is wired as an explicit SKIPPED stub.

Design invariant (do not weaken): the label NEVER says "safe". A file that
passes every gate we actually run is still UNTRUSTED DATA — verdict is only
ever "reject" or "quarantine". Processing discipline (read as data, not
instructions, in an isolated session) is what actually protects the agent —
see cookbook-agent.md, "Файлы = недоверенные данные".

TOCTOU invariant (codex review #2): every check is bound to ONE inode. The
target is opened once with O_NOFOLLOW; size/type come from fstat(fd); child
scanners (file, clamscan) receive the same open file description via
/proc/self/fd/<fd>; the label is written only after re-checking that the
path still names that inode. A path swap mid-check can therefore neither
redirect what gets scanned nor attach our verdict to a different file.
"""

import errno
import hashlib
import json
import os
import shutil
import stat as stat_mod
import subprocess
import sys
import time
import zipfile

# ---------------------------------------------------------------------------
# Config — kept as top-of-file constants (not scattered inline) so the quotas
# and whitelist can be tuned without hunting through the logic below.
# ---------------------------------------------------------------------------

# mime-types allowed through the static gate. Extend here, not inline, so the
# policy stays auditable in one place. Prefixes ending in "/" match any subtype.
MIME_WHITELIST_PREFIXES = (
    "text/",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)
MIME_WHITELIST_EXACT = {
    "application/pdf",
    "application/json",
    "application/zip",
    "application/x-mimearchive",  # MHTML / message archives (.mht/.mhtml)
    "message/rfc822",
}

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB — generous default for text/docs/images

# Archive (zip) quotas — a static gate can't fully sandbox extraction, but
# reading the central directory writes nothing to disk, so these checks are
# cheap and safe to run unconditionally on any zip.
ZIP_MAX_RATIO = 100          # uncompressed / compressed size
ZIP_MAX_FILE_COUNT = 10_000
ZIP_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
ZIP_MAX_NESTED_DEPTH = 15    # path separators inside the archive as a depth proxy

ARCHIVE_MIME_TYPES = {"application/zip"}

# Nested-archive extensions. A zip whose members are themselves archives can
# hide a bomb the flat central-directory listing scores as tiny — depth-by-'/'
# never sees zip-in-zip. At MVP we refuse such archives rather than recurse.
ARCHIVE_EXTENSIONS = (".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar",
                      ".tgz", ".tbz2", ".jar", ".war", ".apk", ".zipx",
                      ".lz", ".lzma", ".cab", ".arj")

# Image formats whose spec fixes an end-of-stream terminator. Anything after
# that terminator is a smuggling channel (polyglot / appended payload) — the
# image renders fine everywhere while carrying an invisible tail. Reject.
PNG_TAIL = b"\x00\x00\x00\x00IEND\xaeB`\x82"  # IEND chunk is always these 12 bytes
JPEG_TAIL = b"\xff\xd9"                        # EOI marker
GIF_TAIL = b";"                                # 0x3B trailer

# journal lives next to the worker by default — the same file works unchanged
# on the dev host (~/file-safety/) and on mycelium (/opt/mycelium/file-safety/)
JOURNAL_PATH = os.environ.get(
    "FILE_SAFETY_JOURNAL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal.log"),
)


# ---------------------------------------------------------------------------
# fd helpers — everything reads through the one fd opened in check_file()
# ---------------------------------------------------------------------------

def reopen(fd, mode="rb"):
    # /proc/self/fd/<fd> resolves straight to the open file description's
    # inode (no path re-resolution), and opening it yields an INDEPENDENT
    # offset — so zipfile can seek freely without disturbing other readers.
    return open(f"/proc/self/fd/{fd}", mode)


def sha256_of_fd(fd):
    # pread is offset-independent — no shared-offset interference, no seeks
    h = hashlib.sha256()
    pos = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, pos)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)
        pos += len(chunk)


def detect_mime_fd(fd):
    # shell out to `file` rather than a python mime-guess lib: `file` sniffs
    # actual content/magic bytes, which is what matters for a security gate.
    # The child gets our open file description via /proc/self/fd, so a path
    # swap after open cannot change what gets sniffed.
    # -L: /proc/self/fd/<fd> is a (magic) symlink — without it `file` reports
    # the link itself, not the content behind it
    out = subprocess.run(
        ["file", "-L", "--mime-type", "-b", f"/proc/self/fd/{fd}"],
        capture_output=True, text=True, check=True, pass_fds=[fd],
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def mime_allowed(mime):
    if mime in MIME_WHITELIST_EXACT:
        return True
    return any(mime.startswith(p) for p in MIME_WHITELIST_PREFIXES)


def zip_bomb_check(fd, size):
    """
    Inspect a zip's central directory (no extraction to disk) and reject
    anything that looks like a zip-bomb: absurd compression ratio, huge file
    count, huge total uncompressed size, or suspiciously deep nesting.
    Returns (ok: bool, reasons: list[str]).
    """
    reasons = []
    # Structured stdlib reader, never extracts. Any parse anomaly -> fail
    # CLOSED (reject on uncertainty) — a text parser here failed OPEN on
    # filenames containing newlines (codex review #5).
    try:
        with reopen(fd) as f, zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        return False, ["zip_unreadable_or_corrupt"]
    except Exception:
        return False, ["zip_listing_parse_error"]

    total_uncompressed = sum(i.file_size for i in infos)
    file_count = len(infos)
    max_depth = max((i.filename.count("/") for i in infos), default=0)
    nested = [i.filename for i in infos
              if i.filename.lower().endswith(ARCHIVE_EXTENSIONS)]

    ratio = (total_uncompressed / size) if size else float("inf")

    if nested:
        # zip-in-zip: the outer listing can't score a bomb hidden one level
        # down, and depth-by-'/' never counts archive recursion. Refuse.
        reasons.append(f"nested_archive:{nested[0]}")
    if file_count > ZIP_MAX_FILE_COUNT:
        reasons.append(f"zip_file_count_{file_count}_exceeds_{ZIP_MAX_FILE_COUNT}")
    if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
        reasons.append(
            f"zip_uncompressed_{total_uncompressed}_exceeds_{ZIP_MAX_UNCOMPRESSED_BYTES}"
        )
    if ratio > ZIP_MAX_RATIO:
        reasons.append(f"zip_ratio_{ratio:.1f}_exceeds_{ZIP_MAX_RATIO}")
    if max_depth > ZIP_MAX_NESTED_DEPTH:
        reasons.append(f"zip_nesting_depth_{max_depth}_exceeds_{ZIP_MAX_NESTED_DEPTH}")

    return (len(reasons) == 0), reasons


def polyglot_check(fd, mime, size):
    """
    Codex review #3: `file` reports only the FIRST format it recognizes, so a
    PNG+ZIP (or GIF+JAR etc.) sails through as image/*. Two detectors:
    1. every non-zip file that ALSO opens as a zip (valid end-of-central-
       directory the way any unzip would find it) is a carrier — reject;
    2. for image types whose spec fixes a terminator, any bytes after it are
       a smuggling channel — reject trailing data, ambiguity is not allowed.
    Returns (ok: bool, reasons: list[str]).
    """
    reasons = []

    if mime not in ARCHIVE_MIME_TYPES:
        try:
            with reopen(fd) as f:
                if zipfile.is_zipfile(f):
                    reasons.append("polyglot_zip_signature")
        except Exception:
            reasons.append("polyglot_probe_error")

    tail_spec = None
    if mime == "image/png":
        tail_spec = PNG_TAIL
    elif mime == "image/jpeg":
        tail_spec = JPEG_TAIL
    elif mime == "image/gif":
        tail_spec = GIF_TAIL
    if tail_spec is not None:
        if size < len(tail_spec):
            reasons.append("image_truncated")
        else:
            tail = os.pread(fd, len(tail_spec), size - len(tail_spec))
            if tail != tail_spec:
                reasons.append(f"trailing_data_after_{mime.split('/')[1]}_terminator")

    return (len(reasons) == 0), reasons


def static_gate(fd, mime, size):
    """
    Phase 2. Whitelist + size limit + polyglot guard + (for archives)
    zip-bomb guard. Returns (status: "ok"|"reject", reasons: list[str]).
    """
    reasons = []

    if not mime_allowed(mime):
        reasons.append(f"mime_not_whitelisted:{mime}")

    if size > MAX_FILE_SIZE_BYTES:
        reasons.append(f"size_{size}_exceeds_{MAX_FILE_SIZE_BYTES}")

    ok, poly_reasons = polyglot_check(fd, mime, size)
    if not ok:
        reasons.extend(poly_reasons)

    if mime in ARCHIVE_MIME_TYPES:
        ok, zip_reasons = zip_bomb_check(fd, size)
        if not ok:
            reasons.extend(zip_reasons)

    status = "reject" if reasons else "ok"
    return status, reasons


def av_gate(fd):
    """
    Phase 3 — ClamAV scan. Runs clamscan and maps its exit code: 0 clean,
    1 signature match, anything else an error. We return an explicit token
    ("clean" / "INFECTED:<sig>" / "SKIPPED:...") rather than a bool so the
    label records what actually happened — a timeout or missing binary must
    read as "not scanned", never as a silent clean.
    """
    clamscan = shutil.which("clamscan")
    if not clamscan:
        return "SKIPPED:not-installed"
    try:
        # --stdout so the "FOUND" line lands on stdout (not stderr) for parsing;
        # timeout guards against a pathological input hanging the scanner.
        # The scanner reads our fd (follow=2 because /proc/self/fd is a link).
        out = subprocess.run(
            [clamscan, "--no-summary", "--stdout",
             "--follow-file-symlinks=2", f"/proc/self/fd/{fd}"],
            capture_output=True, text=True, timeout=120, pass_fds=[fd],
        )
    except subprocess.TimeoutExpired:
        return "SKIPPED:timeout"
    if out.returncode == 0:
        return "clean"
    if out.returncode == 1:
        # clamscan prints "<path>: <Signature> FOUND" — pull the signature name
        sig = "unknown"
        for line in out.stdout.splitlines():
            if line.strip().endswith("FOUND"):
                sig = line.rsplit(":", 1)[-1].strip()[:-len(" FOUND")].strip()
                break
        return f"INFECTED:{sig}"
    return f"SKIPPED:error_rc{out.returncode}"


def llm_triage_gate(fd):
    """
    Phase 4 stub — semantic/prompt-injection triage needs an isolated uid
    sandbox (no secrets/network/memory-write) per the design's recursion
    caveat: the triage reader is itself a target for injection, so it may
    only run detonated. Explicit SKIPPED rather than a silent pass.
    """
    return "SKIPPED:no-isolation"


# ---------------------------------------------------------------------------
# Label + journal
# ---------------------------------------------------------------------------

def append_journal(record):
    # append-only, one JSON object per line — cheap to grep/tail, matches the
    # "root-log / Журнал субстрата" transparency pattern from the design doc
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_label(label_path, label):
    # O_NOFOLLOW: a pre-planted symlink at the label path must not turn this
    # write into a write somewhere else
    fd = os.open(label_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(label, f, ensure_ascii=False, indent=2)
        f.write("\n")


def check_file(path):
    path = os.path.abspath(path)
    ts = int(time.time())

    try:
        # O_NONBLOCK: opening a planted FIFO read-only otherwise blocks forever
        # waiting for a writer; regular-file reads ignore the flag entirely
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        reason = "symlink_refused" if e.errno == errno.ELOOP else f"open_failed:{e.strerror}"
        label = {
            "file": path, "sha256": "SKIPPED:not-opened", "size": None,
            "mime": "SKIPPED:not-opened", "static": "reject",
            "static_reasons": [reason], "av": "SKIPPED:static_reject",
            "llm_triage": "SKIPPED:static_reject",
            "content_injection": "UNVERIFIED", "verdict": "reject", "ts": ts,
        }
        append_journal({"ts": ts, "file": path, "sha256": label["sha256"],
                        "verdict": "reject", "reasons": [reason]})
        return label

    try:
        return _check_open_file(path, fd, ts)
    finally:
        os.close(fd)


def _check_open_file(path, fd, ts):
    st = os.fstat(fd)
    size = st.st_size

    if not stat_mod.S_ISREG(st.st_mode):
        static_status, static_reasons = "reject", ["not_regular_file"]
        mime = digest = "SKIPPED:not-regular"
    else:
        # Short-circuit oversize BEFORE any full-file work. We already know the
        # verdict from the size alone; a 20GB input must not be streamed through
        # `file` and sha256 just to be rejected. mime/sha are honestly "not run".
        oversize = size > MAX_FILE_SIZE_BYTES
        mime = "SKIPPED:oversize" if oversize else detect_mime_fd(fd)
        digest = "SKIPPED:oversize" if oversize else sha256_of_fd(fd)
        if oversize:
            static_status = "reject"
            static_reasons = [f"size_{size}_exceeds_{MAX_FILE_SIZE_BYTES}"]
        else:
            static_status, static_reasons = static_gate(fd, mime, size)

    # AV/LLM gates only make sense to run once static passes — no point
    # scanning something we're already rejecting on structural grounds.
    av_status = av_gate(fd) if static_status == "ok" else "SKIPPED:static_reject"
    llm_status = llm_triage_gate(fd) if static_status == "ok" else "SKIPPED:static_reject"

    # Honest label per design phase 5: verdict is never "safe". A known-malware
    # AV hit is a hard reject (design phase 3); everything that merely *passes*
    # the checks we ran is "quarantine" — passed ≠ trusted, never "safe".
    infected = av_status.startswith("INFECTED")
    verdict = "reject" if (static_status == "reject" or infected) else "quarantine"

    # TOCTOU tail: the verdict is about the inode we scanned. If the path no
    # longer names that inode, attaching the label would certify a file we
    # never looked at — reject and say so instead.
    try:
        now = os.lstat(path)
        path_still_ours = (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)
    except OSError:
        path_still_ours = False
    if not path_still_ours:
        static_reasons = static_reasons + ["toctou_path_changed"]
        verdict = "reject"

    label = {
        "file": path,
        "sha256": digest,
        "size": size,
        "mime": mime,
        "static": static_status if path_still_ours else "reject",
        "static_reasons": static_reasons,
        "av": av_status,
        "llm_triage": llm_status,
        "content_injection": "UNVERIFIED",
        "verdict": verdict,
        "ts": ts,
    }

    write_label(path + ".safety.json", label)

    append_journal({
        "ts": ts,
        "file": path,
        "sha256": digest,
        "verdict": verdict,
        "reasons": static_reasons,
    })

    return label


def main():
    if len(sys.argv) != 2:
        print("usage: file_safety_check.py <path>", file=sys.stderr)
        return 2

    target = sys.argv[1]
    if not os.path.lexists(target):
        print(f"error: no such path: {target}", file=sys.stderr)
        return 2

    label = check_file(target)
    print(json.dumps(label, ensure_ascii=False, indent=2))
    return 0 if label["verdict"] == "quarantine" else 1


if __name__ == "__main__":
    sys.exit(main())
