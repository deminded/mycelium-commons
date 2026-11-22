#!/usr/bin/env python3
"""Run #2532 regressions against single changes in disposable copies of the three modules."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from intake_2387_mutations import replace_once
from intake_2532_cases import CASES, base

POLL = "intake_poller.py"
DAEMON = "triage_daemon.py"
CONVERTER = "md_convert.py"
TESTS = Path(__file__).resolve().parent
CASE_FILE = "intake_2532_cases.py"
SIDE_OPEN = "os.open(sidecar_partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,"
READ = 'stream.read(MAX_ARCHIVE_MD_NAME_BYTES + 2).removesuffix(b"\\n")'
SIZE_NUL = 'len(raw) > MAX_ARCHIVE_MD_NAME_BYTES or b"\\0" in raw'
CATEGORIES = '("Cc", "Cf", "Zl", "Zp")'
SEPARATORS = 'ch in "/\\\\\\0"'
ENTRY = "entry = md_name if path == md and md_name else os.path.relpath(path, root)"
ARCHIVE_OPEN = 'with open(path, "rb") as src, archive.open(entry, "w")'
UTF8_BIT_DROPPED = (
    'class EntryWithoutFlag(zipfile.ZipInfo):\n'
    '                def _encodeFilenameFlags(self):\n'
    '                    return self.filename.encode("utf-8"), self.flag_bits & ~0x800\n'
    '            entry = EntryWithoutFlag(entry)\n'
    '            ' + ARCHIVE_OPEN
)
MUTATIONS = [
    (POLL, "original name dropped before triage", "filtered",
     'triage(qdest, str(label.get("mime", "")), name)', 'triage(qdest, str(label.get("mime", "")))'),
    (POLL, "sidecar takes disk name", "filtered", "attachment_name(original_name)", "name"),
    (POLL, "sidecar skips the name filter", "filtered", "attachment_name(original_name)", "original_name"),
    (POLL, "sidecar loses suffix", "filtered",
     'attachment_name(original_name) + ".md"', "attachment_name(original_name)"),
    (POLL, "sidecar created for plain text", "handoff-nonconvert",
     'if kind:\n            sidecars = [(".kind", kind)]',
     'if kind or original_name is not None:\n            sidecars = [(".kind", kind)] if kind else []'),
    (POLL, "sidecar without exclusive creation", "handoff-convert", SIDE_OPEN,
     SIDE_OPEN.replace(" | os.O_EXCL", "")),
    (POLL, "sidecar follows symlinks", "handoff-convert", SIDE_OPEN,
     SIDE_OPEN.replace(" | os.O_NOFOLLOW", "")),
    (POLL, "sidecar mode changed", "handoff-convert",
     "                             0o640, dir_fd=queue_fd)",
     "                             0o644, dir_fd=queue_fd)"),
    (POLL, "sidecar group not set", "handoff-convert",
     'os.fchown(target.fileno(), -1, triage_gid)\n                    target.write',
     'target.write'),
    (POLL, "job published before the sidecars", "handoff-convert",
     '        os.rename(partial, name, src_dir_fd=queue_fd, dst_dir_fd=queue_fd)\n',
     ''),
    (POLL, "published sidecars left on a failed handoff", "handoff-job-rename-fails",
     "owned.add(sidecar)", "pass"),
    (DAEMON, "read without a bound", "bounded-read", READ, 'stream.read().removesuffix(b"\\n")'),
    (DAEMON, "name shortened at the exact limit", "long-name", "MAX_ARCHIVE_MD_NAME_BYTES + 2",
     "MAX_ARCHIVE_MD_NAME_BYTES - 1"),
    (DAEMON, "oversize sidecar passed to the converter", "oversize-sidecar", SIZE_NUL, 'b"\\0" in raw'),
    (DAEMON, "invalid UTF-8 silently repaired", "non-utf8-sidecar", 'raw.decode("utf-8")',
     'raw.decode("utf-8", "ignore")'),
    (DAEMON, "only first line used", "two-lines-sidecar",
     'return name if name.splitlines() == [name] else ""', 'return name.split("\\n")[0]'),
    (DAEMON, "NUL passed in argv", "nul-sidecar", SIZE_NUL, "len(raw) > MAX_ARCHIVE_MD_NAME_BYTES"),
    (DAEMON, "name argument omitted", "filtered", "args.append(md_name)", "pass"),
    (DAEMON, "sidecar left after processing", "cleanup-convert-timeout",
     "(src, kind_path, md_name_path, tmp, zip_tmp)", "(src, kind_path, tmp, zip_tmp)"),
    (CONVERTER, "new CLI argument refused", "filtered", "if len(sys.argv) in (4, 5):", "if len(sys.argv) == 4:"),
    (CONVERTER, "empty argument treated as absent", "direct-invalid",
     "if md_name is not None and not valid_md_name(md_name):",
     "if md_name and not valid_md_name(md_name):"),
    (CONVERTER, "invalid-name flag dropped", "direct-invalid", 'flags.append("convert_invalid_md_name")', "pass"),
    (CONVERTER, "character count used instead of bytes", "direct-invalid",
     'size = len(name.encode("utf-8"))', "size = len(name)"),
    (CONVERTER, "name limit increased by one byte", "direct-invalid",
     "0 < size <= MAX_ARCHIVE_MD_NAME_BYTES", "0 < size <= MAX_ARCHIVE_MD_NAME_BYTES + 1"),
    (CONVERTER, "surrogate reaches ZIP encoder", "direct-invalid",
     "except UnicodeEncodeError:\n        return False", "except UnicodeEncodeError:\n        return True"),
    (CONVERTER, "single dot allowed", "direct-invalid", 'name not in (".", "..")', 'name != ".."'),
    (CONVERTER, "double dot allowed", "direct-invalid", 'name not in (".", "..")', 'name != "."'),
    (CONVERTER, "forward slash allowed", "direct-invalid", SEPARATORS, SEPARATORS.replace("/", "")),
    (CONVERTER, "backslash allowed", "direct-invalid", SEPARATORS, 'ch in "/\\0"'),
    (CONVERTER, "NUL allowed", "direct-invalid",
     "for ch in name))", 'for ch in name if ch != "\\0"))'),
    *[(CONVERTER, f"{category} allowed", "direct-invalid", CATEGORIES,
       repr(tuple(other for other in ("Cc", "Cf", "Zl", "Zp") if other != category)))
      for category in ("Cc", "Cf", "Zl", "Zp")],
    (CONVERTER, "media entry also renamed", "russian-docx", ENTRY,
     "entry = md_name or os.path.relpath(path, root)"),
    (CONVERTER, "working Markdown file also renamed", "filtered",
     'md = os.path.join(outdir, stem + ".md")', 'md = os.path.join(outdir, md_name or stem + ".md")'),
    (CONVERTER, "UTF-8 bit cleared", "russian-docx",
     "            " + ARCHIVE_OPEN, "            " + UTF8_BIT_DROPPED),
]


def run(tests, *cases):
    return subprocess.run([sys.executable, str(tests / CASE_FILE), *cases],
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                          capture_output=True, text=True, timeout=90)


def main():
    with tempfile.TemporaryDirectory(prefix="md-2532-mut-") as directory:
        root = Path(directory)
        tests, code = root / "tests", root / "code"
        tests.mkdir()
        code.mkdir()
        for name in (CASE_FILE, "md_convert_cases.py"):
            shutil.copyfile(TESTS / name, tests / name)
        sources = {name: (base.CODE / name).read_text(encoding="utf-8") for name in (POLL, DAEMON, CONVERTER)}
        for name, source in sources.items():
            (code / name).write_text(source, encoding="utf-8")
        control = run(tests)
        assert control.returncode == 0 and f"2532: {len(CASES)} PASS, 0 FAIL, 0 SKIP" in control.stdout, (
            control.stdout, control.stderr)
        killed = survived = 0
        for module, title, case, old, new in MUTATIONS:
            source = sources[module]
            mutant = replace_once(source, old, new)
            if title == "job published before the sidecars":
                anchor = '        if kind:\n            sidecars = [(".kind", kind)]'
                mutant = replace_once(mutant, anchor, old + anchor)
            compile(mutant, f"{module}:{title}", "exec")
            target = code / module
            target.write_text(mutant, encoding="utf-8")
            result = run(tests, case)
            target.write_text(source, encoding="utf-8")
            if result.returncode == 1 and f"FAIL {case}:" in result.stdout:
                killed += 1
                print(f"KILLED {module}: {title} [{case}]", flush=True)
            elif result.returncode == 0 and f"PASS {case}" in result.stdout:
                survived += 1
                print(f"SURVIVED {module}: {title} [{case}]", flush=True)
            else:
                raise AssertionError((title, result.stdout, result.stderr))
        print(f"mutations 2532: {killed} KILLED, {survived} SURVIVED")
        return bool(survived)


if __name__ == "__main__":
    sys.exit(main())
