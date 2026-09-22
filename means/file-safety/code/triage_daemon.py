#!/usr/bin/env python3
"""
triage_daemon — inbox poller for the semantic gate (runs as uid fs-triage).

The intake worker drops <name> into inbox/ (the file to triage) and the daemon
answers with outbox/<name>.json = {summary, flags}. Each file is handed to a
FRESH triage_reader.py process — one-shot, no shared state between files, so a
reader corrupted by one file can't carry anything into the next.

The daemon holds no long-lived model context and does no interpretation of its
own: it moves bytes and enforces the timeout. All the untrusted reading happens
in the disposable child.
"""
import json
import os
import subprocess
import sys
import time

INBOX = "/opt/mycelium/fs-triage/inbox"
OUTBOX = "/opt/mycelium/fs-triage/outbox"
READER = "/opt/mycelium/fs-triage/triage_reader.py"
READER_TIMEOUT = 90
POLL_SECONDS = 3


def process(name):
    src = os.path.join(INBOX, name)
    out = os.path.join(OUTBOX, name + ".json")
    tmp = out + ".tmp"
    try:
        proc = subprocess.run(
            [sys.executable, READER, src],
            capture_output=True, text=True, timeout=READER_TIMEOUT,
        )
        result = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        result = {"summary": "", "flags": ["triage_timeout"]}
    except Exception as e:
        result = {"summary": "", "flags": ["triage_daemon_error:" + type(e).__name__]}
    # write atomically so the consumer never reads a half-written result
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, out)
    try:
        os.remove(src)   # inbox is a queue, not storage
    except OSError:
        pass


def main():
    print(f"[start] triage daemon watching {INBOX}", flush=True)
    while True:
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
