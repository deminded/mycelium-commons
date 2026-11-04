#!/usr/bin/env python3
"""Read-only r4 verification against the pre-deploy manifest; no rights migration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import deploy_r4_lib as recipe

TRIAGE_CODE = ('triage_daemon.py', 'md_convert.py')
INTAKE_CODE = ('intake_poller.py',)


def quantity(value):
    match = re.fullmatch(r'(\d+)([kmgt]?)(?:b)?', value, re.I)
    if not match:
        raise ValueError(f'unknown quantity: {value}')
    return int(match[1]) * 1024 ** ('kmgt'.index(match[2].lower()) + 1 if match[2] else 0)


def check_work_mount(path):
    """Query the service's mount namespace, never host findmnt."""
    try:
        result = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', 'fs-triage-worker'],
                                capture_output=True, text=True, timeout=5, check=True)
        pid = result.stdout.strip()
        if not re.fullmatch(r'[1-9][0-9]*', pid):
            raise ValueError('worker MainPID missing')
        result = subprocess.run(['nsenter', '-t', pid, '-m', 'findmnt', '--json', '--mountpoint', str(path),
                                 '--output', 'TARGET,FSTYPE,OPTIONS'],
                                capture_output=True, text=True, timeout=5, check=True)
        mounts = json.loads(result.stdout)['filesystems']
        if len(mounts) != 1:
            raise ValueError('expected one work mount')
        mount = mounts[0]
        if mount['fstype'] != 'tmpfs' or os.path.abspath(mount['target']) != os.path.abspath(path):
            raise ValueError('work must be a separate service tmpfs')
        options = dict(option.split('=', 1) for option in mount['options'].split(',') if '=' in option)
        if not 0 < quantity(options.get('size', '')) <= 256 * 1024 * 1024:
            raise ValueError('work tmpfs size must be <=256 MiB')
        if not 0 < quantity(options.get('nr_inodes', '')) <= 4096:
            raise ValueError('work tmpfs nr_inodes must be <=4096')
        # tmpfs omits mode=1777 from mount options because it is the default.
        if options.get('mode', '1777') != '1777':
            raise ValueError('service work tmpfs must be writable: mode=1777')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        return [f'{path}: {exc}']
    return []


def check_deploy(directory=None, intake_directory=None, backup=None):
    errors = []
    if backup is None:
        return ['r4 needs --backup with the pre-deploy manifest (owners/modes are preserved, not hardcoded)']
    try:
        plan = recipe.load(backup)
        for name, path in recipe.FILES.items():
            expected = plan['files'][name]
            if expected['path'] != str(path):
                raise ValueError(f'{path}: manifest path mismatch')
            if recipe.attrs(path) != {k: expected[k] for k in ('uid', 'gid', 'mode')}:
                errors.append(f'{path}: owner/group/mode differs from pre-deploy attributes')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected['sha256']:
                errors.append(f'{path}: installed content differs from planned r4 source')
        if recipe.literal_group_id(recipe.FILES['intake_poller.py'].read_text()) != plan['group_id']:
            errors.append('intake_poller.py: GROUP_ID differs from preflight manifest')
        uid, gid = recipe.triage_ids()
        for path, mode in ((recipe.WORK, 0o700), (recipe.REJECTED, 0o770)):
            if recipe.attrs(path, True) != dict(uid=uid, gid=gid, mode=mode):
                errors.append(f'{path}: expected fs-triage uid/gid and mode={mode:o}')
        recipe.parents(recipe.DROP, trusted=True)
        info = recipe.attrs(recipe.DROP)
        if info != dict(uid=os.geteuid(), gid=os.getegid(), mode=0o644):
            errors.append('drop-in: unexpected uid/gid/mode')
        if recipe.DROP.read_text() != recipe.DROP_TEXT:
            errors.append('drop-in: unexpected TemporaryFileSystem configuration')
        result = subprocess.run(['systemctl', 'show', '-p', 'TemporaryFileSystem', 'fs-triage-worker'],
                                capture_output=True, text=True, timeout=5, check=True)
        configured = result.stdout.strip().removeprefix('TemporaryFileSystem=').split()
        if configured.count(recipe.TMPFS) != 1 or sum(x.split(':', 1)[0] == str(recipe.WORK) for x in configured) != 1:
            errors.append('systemctl show TemporaryFileSystem: expected exactly one managed work mount')
        errors += check_work_mount(recipe.WORK)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        errors.append(f'r4 deployment verification: {type(exc).__name__}: {exc}')
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?')  # Legacy smoke CLI refuses without a manifest.
    parser.add_argument('--backup')
    parser.add_argument('--mount-only', action='store_true')
    args = parser.parse_args()
    errors = check_work_mount(recipe.WORK) if args.mount_only else check_deploy(directory=args.directory, backup=args.backup)
    for error in errors:
        print('FAIL deploy: ' + error)
    if not errors:
        print('PASS deploy r4: preserved file attributes/content, work/.rejected, drop-in, effective service tmpfs')
    sys.exit(bool(errors))
