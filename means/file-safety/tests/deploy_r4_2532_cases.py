#!/usr/bin/env python3
"""Offline r4 rollback regressions for jobs with a Markdown name sidecar."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock
import zipfile

from deploy_r4_cases import Fixture, assert_failure
from intake_2532_cases import PDF, base, sidecar

JOB = "7_input.pdf"
MD_NAME = "Отчёт.pdf.md"


def setup(root):
    fixture = Fixture(root / "server", prior=True)
    result = fixture.deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    fixture.reset()
    return fixture


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


def model_worker(fixture):
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
    model = fixture.root / "bin/model"
    source = triage / "inbox" / JOB
    hook = f"""if name=='systemctl' and args==['is-active','--quiet','fs-triage-worker']:
    assert state['units']['mycelium-intake']=='inactive'
    assert state['units']['fs-triage-worker']=='active'
    if pathlib.Path({str(source)!r}).is_file():
        subprocess.run({command!r}, cwd={str(triage)!r}, check=True)
"""
    anchor = "if name=='systemctl':"
    model.write_text(model.read_text().replace(anchor, hook + anchor, 1))


def rollback_queue(root):
    base.require("pdftotext", "pdfimages")
    fixture = setup(root)
    backup = fixture.backup()
    deploy_files = {path: path.read_bytes() for path in backup.iterdir()}
    # A live deployment may have advanced the offset and journal since its snapshot.
    (fixture.intake / ".intake_offset").write_text("43\n")
    (fixture.intake / "journal.log").write_text("keep journal\nnew intake\n")
    enqueue(fixture)
    model_worker(fixture)

    result = fixture.rollback(backup)
    assert result.returncode == 0, result.stdout + result.stderr
    inbox, outbox = fixture.triage / "inbox", fixture.triage / "outbox"
    assert not list(inbox.iterdir()), list(inbox.iterdir())
    before = json.loads((backup / "state-rollback-after-stop.json").read_text())
    assert {entry["name"] for entry in before["queues"][str(inbox)]} == {
        JOB, "." + JOB + ".kind", "." + JOB + ".md-name",
    }
    drained = json.loads((backup / "state-rollback-drained.json").read_text())
    assert drained["queues"][str(inbox)] == [] and drained["offset"] == 43
    assert all(path.read_bytes() == data for path, data in deploy_files.items())
    assert (fixture.triage / "triage_daemon.py").read_bytes() == fixture.original["triage_daemon.py"][0]

    result_path = outbox / (JOB + ".json")
    zip_path = outbox / (JOB + ".md.zip")
    assert json.loads(result_path.read_text())["flags"] == []
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist()[0] == MD_NAME, archive.namelist()
    # Reconcile completed artifacts before deploying again, just as with r4 deploy.
    result_path.unlink()
    zip_path.unlink()
    result = fixture.deploy()
    assert result.returncode == 0, result.stdout + result.stderr


def rollback_timeout(root, sidecar_only=False):
    fixture = setup(root)
    inbox = fixture.triage / "inbox"
    if sidecar_only:
        sidecar(inbox, JOB).write_text(MD_NAME + "\n", encoding="utf-8")
    else:
        enqueue(fixture)

    result = fixture.rollback()
    assert_failure(result)
    assert "phase=rollback-drain" in result.stderr and "drain timeout" in result.stderr, result.stderr
    assert "keep intake stopped" in result.stderr
    state = fixture.model()
    assert state["units"] == {"mycelium-intake": "inactive", "fs-triage-worker": "active"}, state
    assert not any(call[0] == "install" for call in state["calls"]), state
    assert (fixture.backup() / "state-rollback-drain-timeout.json").is_file()
    assert sidecar(inbox, JOB).is_file()


CASES = {
    "rollback-queue": rollback_queue,
    "rollback-timeout": rollback_timeout,
    "rollback-sidecar-timeout": lambda root: rollback_timeout(root, sidecar_only=True),
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
