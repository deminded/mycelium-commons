#!/usr/bin/env python3
"""Intake-only recipe; reuse r4 path, queue and GROUP_ID validation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import deploy_r4_lib as r4

TARGET = r4.INTAKE / 'intake_poller.py'
UNIT = 'mycelium-intake'
RECOVERY = ('rollback_2387.sh', '2387_common.sh', 'r4_common.sh',
            'deploy_2387_lib.py', 'deploy_r4_lib.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def commands():
    r4.require(r4.TEST_ROOT or os.geteuid() == 0, 'run as root')
    names = ('bash', 'python3', 'dirname', 'date', 'mkdir', 'cp', 'touch',
             'install', 'mktemp', 'mv', 'rm', 'systemctl', 'journalctl')
    missing = [name for name in names if shutil.which(name) is None]
    r4.require(not missing, f'missing commands: {missing}')


def preflight(source):
    commands()
    r4.parents(TARGET)
    old_attrs = r4.attrs(TARGET)
    group_id = r4.literal_group_id(TARGET.read_text())
    source = Path(source).resolve()
    r4.parents(source / 'intake_poller.py')
    r4.attrs(source / 'intake_poller.py')
    prepared = r4.prepared_poller(source, group_id)
    for name in RECOVERY:
        r4.attrs(Path(__file__).parent / name)
    r4.parents(r4.ROOT / 'root/placeholder', trusted=True)
    r4.require(shutil.disk_usage(r4.ROOT / 'root').free >= 50 * 1024 * 1024,
               '/root needs >=50 MiB free')
    r4.attrs(r4.INTAKE / 'journal.log')
    queues = r4.inventory()
    r4.transition_checks(queues)
    # Worker stays running throughout, including drain. Only intake is restarted.
    for unit in r4.UNITS:
        subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
    # Probe journal access before stop as well; journalctl errors are fatal.
    subprocess.run(['journalctl', '-u', UNIT, '-n', '0', '--no-pager'],
                   capture_output=True, text=True, check=True)
    return dict(kind='md-zip-2387', source=str(source), group_id=group_id,
                file=dict(path=str(TARGET), **old_attrs, old_sha256=sha(TARGET),
                          sha256=hashlib.sha256(prepared).hexdigest()), offset=r4.offset())


def validate(backup, ready=False):
    commands()
    r4.parents(backup / 'manifest.json', trusted=True)
    r4.attrs(backup / 'manifest.json')
    plan = r4.load(backup)
    r4.require(plan['kind'] == 'md-zip-2387' and plan['file']['path'] == str(TARGET),
               'backup target mismatch')
    if ready:
        r4.attrs(backup / 'READY')
    for name in RECOVERY:
        r4.attrs(backup / name)
    old = backup / 'intake_poller.py'
    info = plan['file']
    r4.require(r4.attrs(old) == {k: info[k] for k in ('uid', 'gid', 'mode')},
               'backup attributes mismatch')
    r4.require(sha(old) == info['old_sha256'], 'backup content mismatch')
    r4.require(r4.literal_group_id(old.read_text()) == plan['group_id'], 'backup GROUP_ID mismatch')
    prepared = backup / 'prepared-intake_poller.py'
    r4.attrs(prepared)
    r4.require(sha(prepared) == info['sha256'], 'prepared content mismatch')
    r4.require(r4.literal_group_id(prepared.read_text()) == plan['group_id'], 'prepared GROUP_ID mismatch')
    r4.parents(TARGET)
    r4.attrs(TARGET)
    return plan


def check_file(plan, old=False):
    info = plan['file']
    r4.require(r4.attrs(TARGET) == {k: info[k] for k in ('uid', 'gid', 'mode')},
               'installed attributes mismatch')
    r4.require(sha(TARGET) == info['old_sha256' if old else 'sha256'], 'installed SHA256 mismatch')
    r4.require(r4.literal_group_id(TARGET.read_text()) == plan['group_id'], 'installed GROUP_ID mismatch')


def health(since):
    # Production observes all 30 seconds. Offline PATH model skips wall-clock delay.
    for _ in range(30):
        subprocess.run(['systemctl', 'is-active', '--quiet', UNIT], check=True)
        if not r4.TEST_ROOT:
            time.sleep(1)
    subprocess.run(['systemctl', 'is-active', '--quiet', UNIT], check=True)
    journal = subprocess.run(['journalctl', '-u', UNIT, '--since', '@' + since,
                              '--no-pager', '-o', 'cat'],
                             capture_output=True, text=True, check=True)
    r4.require('traceback' not in journal.stdout.lower(), 'traceback in intake journal after restart')
    print('PASS intake active, no traceback during 30s observation' +
          (' (offline clock model)' if r4.TEST_ROOT else ''))


def main():
    action, *args = sys.argv[1:]
    if action == 'preflight':
        print(json.dumps(preflight(args[0]), indent=2))
    elif action == 'prepare':
        backup = Path(args[0])
        plan = r4.load(backup)
        prepared = r4.prepared_poller(plan['source'], plan['group_id'])
        r4.require(hashlib.sha256(prepared).hexdigest() == plan['file']['sha256'], 'source changed')
        (backup / 'prepared-intake_poller.py').write_bytes(prepared)
    elif action in ('backup-check', 'validate-backup'):
        validate(Path(args[0]), ready=action == 'validate-backup')
    elif action in ('check', 'live-check', 'restored-check'):
        check_file(r4.load(args[0]), old=action != 'check')
    elif action == 'attrs':
        info = r4.load(args[0])['file']
        print(f"{info['uid']} {info['gid']} {info['mode']:o}")
    elif action == 'snapshot':
        r4.snapshot(*args)
    elif action == 'drain':
        r4.drain(args[0])
    elif action == 'health':
        health(args[0])
    elif action == 'now':
        print(time.time())
    else:
        raise ValueError(f'unknown action {action}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'FAIL 2387 helper: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
