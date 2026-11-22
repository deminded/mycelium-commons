#!/usr/bin/env python3
"""Offline r4 rollback regressions for jobs with a Markdown name sidecar."""
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

from deploy_r4_cases import GROUP_ID, Fixture, assert_failure
from intake_2532_cases import PDF, base, sidecar

JOB = "7_input.pdf"
MD_NAME = "Отчёт.pdf.md"
LEGACY = "1b79927"
LOG = "rollback-md-names.jsonl"


def installed_files(fixture):
    paths = (fixture.triage / "triage_daemon.py", fixture.triage / "md_convert.py",
             fixture.intake / "intake_poller.py", fixture.drop)
    return {path: path.read_bytes() for path in paths if path.exists()}


def setup(root, *, legacy=False):
    fixture = Fixture(root / "server", prior=True)
    if legacy:
        for path in (fixture.triage / "triage_daemon.py", fixture.triage / "md_convert.py",
                     fixture.intake / "intake_poller.py"):
            source = subprocess.check_output([
                "git", "-C", str(fixture.tests), "show", f"{LEGACY}:means/file-safety/code/{path.name}",
            ])
            if path.name == "intake_poller.py":
                source = re.sub(rb"^GROUP_ID = .*$", f"GROUP_ID = {GROUP_ID}".encode(), source, flags=re.M)
            path.write_bytes(source)
    original = installed_files(fixture)
    result = fixture.deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    fixture.reset()
    return fixture, original


def enqueue(fixture):
    poller = base.load_intake()
    source = fixture.intake / "quarantine" / JOB
    base.write_pdf(source)
    poller.TRIAGE_INBOX = str(fixture.triage / "inbox")
    poller.TRIAGE_OUTBOX = str(fixture.triage / "outbox")
    poller.TRIAGE_WAIT_SECONDS = 0
    with mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())):
        poller.triage(str(source), PDF, "Отчёт.pdf")
    inbox = fixture.triage / "inbox"
    assert sidecar(inbox, JOB).read_text(encoding="utf-8") == MD_NAME + "\n"


def restored_process(fixture):
    triage = fixture.triage
    reader = fixture.root / "reader.py"
    reader.write_text('print(\'{"summary":"","flags":[]}\')\n')
    paths = {key: str(path) for key, path in dict(
        inbox=triage / "inbox", outbox=triage / "outbox", work_root=triage / "work",
        reader=reader, converter=triage / "md_convert.py",
    ).items()}
    command = [sys.executable, "-c", "import triage_daemon, json, sys; "
               "triage_daemon.process(sys.argv[1], **json.loads(sys.argv[2]))",
               JOB, json.dumps(paths)]
    subprocess.run(command, cwd=triage, env=fixture.env, check=True)


def assert_rollback_order(fixture):
    calls = fixture.model()["calls"]
    stop_intake = calls.index(["systemctl", "stop", "mycelium-intake"])
    stop_worker = calls.index(["systemctl", "stop", "fs-triage-worker"])
    cleanup = next(i for i, call in enumerate(calls) if call[0] == "python3" and call[2] == "rollback-md-names")
    install = next(i for i, call in enumerate(calls) if call[0] == "install")
    start_worker = calls.index(["systemctl", "start", "fs-triage-worker"])
    start_intake = calls.index(["systemctl", "start", "mycelium-intake"])
    assert stop_intake < stop_worker < cleanup < install < start_worker < start_intake, calls
    assert not any(call[:2] == ["systemctl", "is-active"] and call[-1] == "fs-triage-worker"
                   for call in calls), calls


def assert_removed(result, backup, *names):
    assert f"Rollback removed {len(names)} .md-name sidecar(s)" in result.stdout, result.stdout
    log = backup / LOG
    assert log.is_file(), f"missing removal log: {log}"
    assert [json.loads(line) for line in log.read_text().splitlines()] == sorted(names)


def rollback_inactive_worker(root):
    base.require("pdftotext", "pdfimages")
    fixture, original = setup(root, legacy=True)
    backup = fixture.backup()
    deploy_files = {path: path.read_bytes() for path in backup.iterdir()}
    # A live deployment may have advanced the offset and journal since its snapshot.
    (fixture.intake / ".intake_offset").write_text("43\n")
    (fixture.intake / "journal.log").write_text("keep journal\nnew intake\n")
    enqueue(fixture)
    inbox, outbox = fixture.triage / "inbox", fixture.triage / "outbox"
    md_name = sidecar(inbox, JOB)
    remaining = {name: (inbox / name).read_bytes() for name in (JOB, f".{JOB}.kind")}
    state = fixture.model()
    state["units"]["fs-triage-worker"] = "inactive"
    (fixture.root / "model.json").write_text(json.dumps(state))

    result = fixture.rollback(backup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_rollback_order(fixture)
    assert installed_files(fixture) == original
    assert {path.name: path.read_bytes() for path in inbox.iterdir()} == remaining
    assert_removed(result, backup, md_name.name)
    assert all(path.read_bytes() == data for path, data in deploy_files.items())
    assert (fixture.intake / ".intake_offset").read_text() == "43\n"
    assert (fixture.intake / "journal.log").read_text() == "keep journal\nnew intake\n"

    restored_process(fixture)
    assert not list(inbox.iterdir()), list(inbox.iterdir())
    result_path = outbox / (JOB + ".json")
    zip_path = outbox / (JOB + ".md.zip")
    assert json.loads(result_path.read_text())["flags"] == []
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist()[0] == "7_input.md", archive.namelist()
    result_path.unlink()
    zip_path.unlink()
    fixture.reset()
    result = fixture.deploy()
    assert result.returncode == 0, result.stdout + result.stderr


def rollback_stuck_worker(root):
    # The model reports an active worker, but never removes any queue entries.
    fixture, original = setup(root)
    enqueue(fixture)
    inbox = fixture.triage / "inbox"
    result = fixture.rollback()
    assert result.returncode == 0, result.stdout + result.stderr
    assert_rollback_order(fixture)
    assert installed_files(fixture) == original
    assert {path.name for path in inbox.iterdir()} == {JOB, f".{JOB}.kind"}


def rollback_unexpected_type(root, *, symlink):
    fixture, _ = setup(root)
    installed = installed_files(fixture)
    inbox = fixture.triage / "inbox"
    normal = sidecar(inbox, JOB)
    normal.write_text(MD_NAME + "\n", encoding="utf-8")
    invalid = sidecar(inbox, "9_input.pdf")
    target = root / "symlink-target"
    if symlink:
        target.write_text("keep target\n")
        invalid.symlink_to(target)
    else:
        invalid.mkdir()

    result = fixture.rollback()
    assert_failure(result)
    assert "phase=rollback-md-names" in result.stderr, result.stderr
    assert f"{invalid}: expected a regular file (no symlinks)" in result.stderr, result.stderr
    state = fixture.model()
    assert state["units"] == {"mycelium-intake": "inactive", "fs-triage-worker": "inactive"}, state
    assert installed_files(fixture) == installed
    assert normal.read_text(encoding="utf-8") == MD_NAME + "\n"
    assert not (fixture.backup() / LOG).exists()
    if symlink:
        assert invalid.is_symlink() and target.read_text() == "keep target\n"
    else:
        assert invalid.is_dir()


def rollback_only_md_names(root):
    fixture, _ = setup(root)
    enqueue(fixture)
    inbox = fixture.triage / "inbox"
    md_name = sidecar(inbox, JOB)
    orphan = sidecar(inbox, "9_orphan.pdf")
    orphan.write_text("orphan.md\n")
    for name in (f"{md_name.name}.partial", f"{md_name.name}.{'f' * 48}.partial",
                 ".other", ".md-name", "..md-name", "..hidden.md-name", "visible.md-name"):
        (inbox / name).write_text(name)
    removed = (md_name.name, orphan.name)
    remaining = {path.name: path.read_bytes() for path in inbox.iterdir() if path.name not in removed}

    result = fixture.rollback()
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in inbox.iterdir()} == remaining
    assert_removed(result, fixture.backup(), *removed)


CASES = {
    "rollback-inactive-worker": rollback_inactive_worker,
    "rollback-stuck-worker": rollback_stuck_worker,
    "rollback-symlink": lambda root: rollback_unexpected_type(root, symlink=True),
    "rollback-directory": lambda root: rollback_unexpected_type(root, symlink=False),
    "rollback-only-md-names": rollback_only_md_names,
}


def main():
    failed = 0
    for name in sys.argv[1:] or CASES:
        try:
            with tempfile.TemporaryDirectory(prefix="deploy-r4-2532-") as tmp:
                CASES[name](Path(tmp))
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"r4 #2532: {len(sys.argv[1:] or CASES) - failed} PASS, {failed} FAIL")
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
