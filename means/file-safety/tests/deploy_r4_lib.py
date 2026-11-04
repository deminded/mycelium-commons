#!/usr/bin/env python3
"""Local helpers for the r4 recipe. MD_R4_TEST_ROOT redirects EVERY server path.

Only the three source files, two newly created directories and one drop-in are
installed. Never traverses queue entries, changes existing runtime attributes,
mounts on the host, or restores offset/journal automatically.
"""
import ast
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time

TEST_ROOT = os.environ.get('MD_R4_TEST_ROOT', '')
ROOT = Path(TEST_ROOT) if TEST_ROOT else Path('/')
TRIAGE = ROOT / 'opt/mycelium/fs-triage'
INTAKE = ROOT / 'opt/mycelium/file-safety'
DROP = ROOT / 'etc/systemd/system/fs-triage-worker.service.d/md-convert-work.conf'
WORK = TRIAGE / 'work'
REJECTED = TRIAGE / 'outbox/.rejected'
OFFSET = INTAKE / '.intake_offset'
TMPFS = f'{WORK}:rw,nosuid,nodev,noexec,size=256M,nr_inodes=4096,mode=1777'
DROP_TEXT = f'[Service]\nTemporaryFileSystem={TMPFS}\n'
FILES = {'triage_daemon.py': TRIAGE / 'triage_daemon.py',
         'md_convert.py': TRIAGE / 'md_convert.py',
         'intake_poller.py': INTAKE / 'intake_poller.py'}
UNITS = ('mycelium-intake', 'fs-triage-worker')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def attrs(path, directory=False):
    s = os.lstat(path)
    require((stat.S_ISDIR if directory else stat.S_ISREG)(s.st_mode), f'{path}: unexpected type/symlink')
    if not directory:
        require(s.st_nlink == 1, f'{path}: hardlink refused')
    return dict(uid=s.st_uid, gid=s.st_gid, mode=stat.S_IMODE(s.st_mode))


def parents(path, trusted=False, missing_drop_dir=False):
    for parent in reversed(path.absolute().parents):
        if missing_drop_dir and parent == DROP.parent and not os.path.lexists(parent):
            continue
        a = attrs(parent, True)
        if trusted and (not TEST_ROOT or parent == ROOT or ROOT in parent.parents):
            require(a['uid'] == os.geteuid() and not a['mode'] & 0o022,
                    f'{parent}: untrusted configuration parent')


def triage_ids():
    if TEST_ROOT:
        return os.geteuid(), os.getegid()
    uid, gid = pwd.getpwnam('fs-triage').pw_uid, grp.getgrnam('fs-triage').gr_gid
    intake = pwd.getpwnam('file-safety')
    require(gid in os.getgrouplist('file-safety', intake.pw_gid), 'file-safety cannot assign fs-triage gid')
    return uid, gid


def offset():
    attrs(OFFSET)
    with open(OFFSET, encoding='ascii') as stream:
        value = stream.read(128).strip()
    require(value and len(value) < 128, 'offset empty/too long: reconcile state; never reset to 0')
    return int(value)


def inventory():
    result = {}
    for directory in (TRIAGE / 'inbox', TRIAGE / 'outbox', INTAKE / 'incoming', INTAKE / 'quarantine'):
        attrs(directory, True)
        entries = []
        with os.scandir(directory) as items:
            for item in items:
                s = item.stat(follow_symlinks=False)
                entries.append(dict(name=item.name, mode=s.st_mode, size=s.st_size,
                                    uid=s.st_uid, gid=s.st_gid, mtime_ns=s.st_mtime_ns))
        result[str(directory)] = sorted(entries, key=lambda e: e['name'])
    return result


def literal_group_id(text):
    """Inspect trusted poller code without executing imports/token reads or API."""
    matches = re.findall(r'^GROUP_ID\s*=\s*(-?\d+)\b', text, re.M)
    require(len(matches) == 1, f'GROUP_ID: expected exactly one literal line, got {len(matches)}')
    value = int(matches[0])
    require(value != 0, 'GROUP_ID: zero is not a routable group; refusing before intake restart')
    tree = ast.parse(text)
    compile(tree, '<prepared-intake-poller>', 'exec')
    stores = [n for n in ast.walk(tree) if isinstance(n, ast.Name)
              and n.id == 'GROUP_ID' and isinstance(n.ctx, ast.Store)]
    assignments = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'GROUP_ID']
    require(len(stores) == len(assignments) == 1, 'GROUP_ID: expected one top-level assignment')
    actual = ast.literal_eval(assignments[0].value)
    require(type(actual) is int and actual == value, 'GROUP_ID: AST literal differs from extracted id')
    return value


def prepared_poller(source, group_id):
    text = (Path(source) / 'intake_poller.py').read_text()
    text, count = re.subn(r'^GROUP_ID[ \t]*=[^\n]*\bMYCELIUM_GROUP_ID\b[^\n]*$',
                          f'GROUP_ID = {group_id}          # супергруппа Грибницы', text, flags=re.M)
    require(count == 1, f'intake_poller.py: expected one MYCELIUM_GROUP_ID line replacement, got {count}')
    require(literal_group_id(text) == group_id, 'prepared poller GROUP_ID mismatch')
    return text.encode('utf-8')


def archival_reject_label(path):
    """Predicate: return the archive fingerprint if valid, otherwise None.

    Read at most 64 KiB + 1 byte, without following links or blocking on a
    substituted FIFO; check the opened inode as well as the directory entry.
    """
    suffix = '.safety.json'
    if path.parent != INTAKE / 'incoming' or not path.name.endswith(suffix):
        return None
    def signature(s):
        # atime can change because of this read; it is not task state.
        return (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    try:
        original = os.lstat(path)
        require(stat.S_ISREG(original.st_mode) and original.st_nlink == 1,
                'not a regular single-link label')
        require(original.st_size <= 64 * 1024, 'label too large')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            require(stat.S_ISREG(opened.st_mode) and signature(opened) == signature(original),
                    'label replaced before open')
            data = stream.read(64 * 1024 + 1)
            require(len(data) <= 64 * 1024, 'label grew')
            final = os.fstat(stream.fileno())
        require(signature(final) == signature(opened) and signature(os.lstat(path)) == signature(final),
                'label changed during read')
        label = json.loads(data)
        require(isinstance(label, dict) and label.get('verdict') == 'reject', 'not reject')
        require(not os.path.lexists(path.with_name(path.name[:-len(suffix)])), 'data still present')
        return dict(name=path.name, size=final.st_size, mtime_ns=final.st_mtime_ns,
                    sha256=hashlib.sha256(data).hexdigest())
    except (OSError, ValueError, RecursionError):
        return None


def transition_checks(queues):
    """Read-only known blockers, both before stop and across the drain window."""
    errors = []
    for directory, entries in queues.items():
        path = Path(directory)
        for entry in entries:
            name = entry['name']
            if (path == INTAKE / 'incoming' and not archival_reject_label(path / name) or
                    path == TRIAGE / 'inbox' and name.startswith('.') or
                    path == TRIAGE / 'outbox' and name != '.rejected'):
                errors.append(f'{path / name}: unfinished/orphan queue entry')
            if path != INTAKE / 'quarantine' or not name.endswith('.safety.json'):
                continue
            label_path = path / name
            try:
                attrs(label_path)
                fd = os.open(label_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, encoding='utf-8') as stream:
                    require(os.fstat(stream.fileno()).st_size <= 1024 * 1024,
                            'label too large for transition check')
                    text = stream.read(1024 * 1024 + 1)
                    require(len(text) <= 1024 * 1024, 'label grew during transition check')
                    label = json.loads(text)
                require(isinstance(label, dict) and label.get('verdict') in ('reject', 'quarantine'),
                        'incomplete label')
            except (OSError, ValueError) as exc:
                errors.append(f'{label_path}: {type(exc).__name__}: {exc}')
    require(not errors, 'reconcile before retry:\n' + '\n'.join(errors))


def preflight(source):
    require(TEST_ROOT or os.geteuid() == 0, 'run as root')
    commands = ('install', 'mv', 'mktemp', 'nsenter', 'findmnt', 'systemctl', 'python3',
                'cp', 'mkdir', 'rm', 'touch', 'id', 'date', 'dirname', 'bash')
    missing = [name for name in commands if shutil.which(name) is None]
    require(not missing, f'preflight missing required commands: {missing}')
    uid, gid = triage_ids()
    plan = dict(files={}, drop=None, drop_sha256=None, offset=offset(), queues=inventory(),
                tmpfs=TMPFS, units={}, source=str(Path(source).resolve()))
    for name, dest in FILES.items():
        parents(dest)
        attrs(Path(source) / name)
        if os.path.lexists(dest):
            a = attrs(dest)
            plan['files'][name] = dict(path=str(dest), existed=True, **a)
        else:
            require(name == 'md_convert.py', f'{dest}: required original missing')
            plan['files'][name] = dict(path=str(dest), existed=False, **attrs(FILES['triage_daemon.py']))
        plan['files'][name]['old_sha256'] = hashlib.sha256(dest.read_bytes()).hexdigest() if os.path.lexists(dest) else None
        plan['files'][name]['sha256'] = hashlib.sha256((Path(source) / name).read_bytes()).hexdigest()
    plan['group_id'] = literal_group_id(FILES['intake_poller.py'].read_text())
    prepared = prepared_poller(source, plan['group_id'])
    plan['files']['intake_poller.py']['sha256'] = hashlib.sha256(prepared).hexdigest()
    transition_checks(plan['queues'])
    parents(DROP, trusted=True, missing_drop_dir=True)
    # The drop-in directory may be absent; validate its existing ancestors above.
    if os.path.lexists(DROP):
        plan['drop'] = attrs(DROP)
        plan['drop_sha256'] = hashlib.sha256(DROP.read_bytes()).hexdigest()
        require(plan['drop']['uid'] == os.geteuid() and not plan['drop']['mode'] & 0o022,
                'drop-in must be protected')
    for path, mode in ((WORK, 0o700), (REJECTED, 0o770)):
        parents(path)
        if os.path.lexists(path):
            require(attrs(path, True) == dict(uid=uid, gid=gid, mode=mode),
                    f'{path}: existing attributes differ; no normalization in r4')
    parents(ROOT / 'root' / 'placeholder', trusted=True)
    require(shutil.disk_usage(ROOT / 'root').free >= 50 * 1024 * 1024, '/root needs >=50 MiB free')
    # Save journal for inspection only, never copy it back on rollback.
    attrs(INTAKE / 'journal.log')
    for unit in UNITS:
        p = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, check=True)
        require(p.stdout.strip() == 'active', f'{unit}: not active')
        plan['units'][unit] = p.stdout.strip()
    return plan


def load(backup):
    return json.loads((Path(backup) / 'manifest.json').read_text())


def snapshot(backup, tag):
    saved = dict(offset=offset(), queues=inventory(), journal_sha256=hashlib.sha256((INTAKE / 'journal.log').read_bytes()).hexdigest())
    saved['archival_reject_labels'] = [
        fingerprint for entry in saved['queues'][str(INTAKE / 'incoming')]
        if (fingerprint := archival_reject_label(INTAKE / 'incoming' / entry['name']))]
    (Path(backup) / f'state-{tag}.json').write_text(json.dumps(saved, indent=2) + '\n')
    subprocess.run(['cp', '-p', '--', str(OFFSET), str(Path(backup) / f'offset-{tag}')], check=True)
    attrs(INTAKE / 'journal.log')
    subprocess.run(['cp', '-p', '--', str(INTAKE / 'journal.log'), str(Path(backup) / f'journal-{tag}')], check=True)
    if tag in ('drained', 'stopped'):
        before = json.loads((Path(backup) / 'state-before-stop.json').read_text())
        require(before['queues'][str(INTAKE / 'quarantine')] == saved['queues'][str(INTAKE / 'quarantine')]
                and before['journal_sha256'] == saved['journal_sha256']
                and before['archival_reject_labels'] == saved['archival_reject_labels'],
                'task state changed during stop: manually reconcile saved quarantine inventory/journal/offset before retry')
    if tag == 'stopped':
        transition_checks(saved['queues'])
    return saved


def drain(backup):
    # Catch blockers created since preflight immediately, before waiting for inbox.
    snapshot(backup, 'after-stop')
    transition_checks(inventory())
    deadline = time.monotonic() + (0.05 if TEST_ROOT else 300)
    while True:
        subprocess.run(['systemctl', 'is-active', '--quiet', 'fs-triage-worker'], check=True)
        with os.scandir(TRIAGE / 'inbox') as entries:
            pending = [e.name for e in entries if not e.name.startswith('.')]
        if not pending:
            break  # process() removes the source only in its final cleanup.
        if time.monotonic() >= deadline:
            snapshot(backup, 'drain-timeout')
            raise ValueError(f'drain timeout (300s); pending={pending!r}; keep intake stopped, reconcile queues')
        time.sleep(0.01 if TEST_ROOT else 1)
    state = snapshot(backup, 'drained')
    transition_checks(state['queues'])
    # Orphaned output, dot-partials/kind, incoming data cannot be silently re-enqueued.
    leftovers = {p: entries for p, entries in state['queues'].items()
                 if Path(p).name != 'quarantine' and
                 any(not (Path(p) == TRIAGE / 'outbox' and e['name'] == '.rejected' or
                          Path(p) == INTAKE / 'incoming' and archival_reject_label(Path(p) / e['name']))
                     for e in entries)}
    require(not leftovers, f'unfinished/orphan queue entries: {leftovers!r}; reconcile using state snapshots')
    (Path(backup) / 'offset-drained-value').write_text(str(state['offset']))


def ensure_dirs():
    uid, gid = triage_ids()
    for path, mode in ((WORK, 0o700), (REJECTED, 0o770)):
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.mkdir(path.name, mode, dir_fd=fd)
            except FileExistsError:
                require(attrs(path, True) == dict(uid=uid, gid=gid, mode=mode), f'{path}: unexpected existing directory')
                continue
            child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                os.fchown(child, uid, gid)
                os.fchmod(child, mode)  # Only the new inode; never existing runtime paths.
            finally:
                os.close(child)
        finally:
            os.close(fd)


def main():
    action, *args = sys.argv[1:]
    if action == 'preflight':
        print(json.dumps(preflight(args[0]), indent=2))
    elif action == 'prepare-poller':
        plan = load(args[0])
        prepared = prepared_poller(plan['source'], plan['group_id'])
        require(hashlib.sha256(prepared).hexdigest() == plan['files']['intake_poller.py']['sha256'],
                'poller source changed after preflight')
        (Path(args[0]) / 'prepared-intake_poller.py').write_bytes(prepared)
    elif action == 'snapshot':
        snapshot(*args)
    elif action == 'drain':
        drain(*args)
    elif action == 'dirs':
        ensure_dirs()
    elif action == 'drop':
        print(DROP_TEXT, end='')
    elif action == 'offset-check':
        require(offset() == int((Path(args[0]) / 'offset-drained-value').read_text()), 'offset changed since drain; inspect saved state')
    elif action == 'attrs':
        info = load(args[0])['files'][args[1]]
        print(f"{info['uid']} {info['gid']} {info['mode']:o}")
    elif action in ('validate-backup', 'backup-check'):
        backup = Path(args[0])
        parents(backup / 'manifest.json', trusted=True)
        attrs(backup / 'manifest.json')
        plan = load(backup)
        if action == 'validate-backup':
            attrs(backup / 'READY')
        else:
            prepared = backup / 'prepared-intake_poller.py'
            attrs(prepared)
            require(literal_group_id(prepared.read_text()) == plan['group_id'], 'prepared GROUP_ID differs from manifest')
            require(hashlib.sha256(prepared.read_bytes()).hexdigest() == plan['files']['intake_poller.py']['sha256'],
                    'prepared poller content mismatch')
        for name, path in FILES.items():
            require(plan['files'][name]['path'] == str(path), 'backup target mismatch')
            parents(path)
            if os.path.lexists(path):
                attrs(path)
            if plan['files'][name]['existed']:
                require(attrs(backup / name) == {k: plan['files'][name][k] for k in ('uid', 'gid', 'mode')}, f'{name}: corrupt backup attributes')
                require(hashlib.sha256((backup / name).read_bytes()).hexdigest() == plan['files'][name]['old_sha256'], f'{name}: backup content mismatch')
        parents(DROP, trusted=True, missing_drop_dir=True)
        if os.path.lexists(DROP):
            attrs(DROP)
        if plan['drop'] is not None:
            require(attrs(backup / 'prior-drop-in.conf') == plan['drop'], 'prior drop-in backup invalid')
            require(hashlib.sha256((backup / 'prior-drop-in.conf').read_bytes()).hexdigest() == plan['drop_sha256'], 'prior drop-in backup content mismatch')
    elif action == 'existed':
        # Output rather than an expected nonzero status: shell ERR traps stay meaningful.
        plan = load(args[0])
        print(int(plan['drop'] is not None) if args[1] == 'drop' else int(plan['files'][args[1]]['existed']))
    else:
        raise ValueError(f'unknown action {action}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'FAIL r4 helper {sys.argv[1:2]}: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
