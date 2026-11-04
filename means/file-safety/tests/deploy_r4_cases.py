#!/usr/bin/env python3
"""Run real recipe shells with only temporary paths and fake service commands.

Every external operation in a successful deploy/rollback is failed once. No
server unit, real namespace or mount is touched. install/cp/mv operate on local
fixture files; systemctl/nsenter are entirely modeled.
"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest import mock

TESTS = Path(__file__).resolve().parent
REAL_PYTHON = sys.executable
GROUP_ID = -1009999999999  # fixture only — not a real group id
COMMANDS = ('systemctl', 'install', 'nsenter', 'cp', 'mv', 'mkdir', 'rm', 'touch', 'mktemp', 'python3')
REAL = {c: shutil.which(c) for c in COMMANDS if c not in ('systemctl', 'nsenter')}
MODEL = r'''import json,os,pathlib,subprocess,sys
root=pathlib.Path(os.environ['MD_R4_TEST_ROOT'])
state_file=root/'model.json'
state=json.loads(state_file.read_text())
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
state['count']+=1
state['calls'].append([name]+args)
fail=state['count']==state.get('fail_at')
state_file.write_text(json.dumps(state))
if fail:
    print('INJECTED failure: '+name+' '+repr(args),file=sys.stderr)
    sys.exit(77)
if name=='systemctl':
    op=args[0]
    unit=args[-1]
    if op in ('stop','start'):
        state['units'][unit]='inactive' if op=='stop' else 'active'
        state_file.write_text(json.dumps(state))
    elif op=='is-active':
        value=state['units'][unit]
        if '--quiet' not in args: print(value)
        sys.exit(0 if value=='active' else 3)
    elif op=='show':
        if 'MainPID' in args: print('1234')
        else:
            drop=root/'etc/systemd/system/fs-triage-worker.service.d/md-convert-work.conf'
            print(next(l for l in drop.read_text().splitlines() if l.startswith('TemporaryFileSystem=')))
    elif op!='daemon-reload':
        sys.exit(2)
    sys.exit(0)
if name=='nsenter':
    path=str(root/'opt/mycelium/fs-triage/work')
    mount={'target':path,'fstype':'tmpfs','options':'rw,size=262144k,nr_inodes=4096,mode=1777'}
    print(json.dumps({'filesystems':[mount]}) if '--json' in args else str(mount))
    sys.exit(0)
real=json.loads(os.environ['MD_R4_REAL'])[name]
os.execv(real,[real]+args)
'''


class Fixture:
    def __init__(self, root, prior=False):
        self.root = root
        self.tests = TESTS
        self.triage = root / 'opt/mycelium/fs-triage'
        self.intake = root / 'opt/mycelium/file-safety'
        self.drop = root / 'etc/systemd/system/fs-triage-worker.service.d/md-convert-work.conf'
        self.source = root / 'source'
        for p in (self.triage/'inbox', self.triage/'outbox', self.intake/'incoming', self.intake/'quarantine',
                  self.drop.parent.parent, root/'root', self.source, root/'bin'):
            p.mkdir(parents=True, exist_ok=True)
        for directory in (root, *[p for p in root.rglob('*') if p.is_dir()]):
            directory.chmod(0o755)
        self.original = {}
        for i, (name, parent) in enumerate((('triage_daemon.py', self.triage), ('intake_poller.py', self.intake))):
            path = parent / name
            text = (TESTS.parent/'code'/name).read_text() if name == 'intake_poller.py' else 'original '+name+'\n'
            if name == 'intake_poller.py':
                text = re.sub(r'^GROUP_ID = .*$', f'GROUP_ID = {GROUP_ID}          # супергруппа Грибницы', text, flags=re.M)
            path.write_text(text)
            path.chmod(0o751 - i)
            self.original[name] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for name in ('triage_daemon.py', 'intake_poller.py', 'md_convert.py'):
            shutil.copyfile(TESTS.parent/'code'/name, self.source/name)
        (self.intake/'.intake_offset').write_text('42\n')
        (self.intake/'journal.log').write_text('keep journal\n')
        (self.intake/'quarantine/original').write_text('keep quarantine\n')
        if prior:
            self.drop.parent.mkdir(mode=0o750)
            self.drop.write_text('[Service]\n# prior unrelated settings\n')
            self.drop.chmod(0o640)
        model = root/'bin/model'
        model.write_text('#!'+REAL_PYTHON+'\n'+MODEL)
        model.chmod(0o700)
        for name in COMMANDS:
            (root/'bin'/name).symlink_to(model)
        self.env = dict(os.environ, MD_R4_TEST_ROOT=str(root), MD_R4_REAL=json.dumps(REAL),
                        PATH=str(root/'bin')+':'+os.environ['PATH'], PYTHONDONTWRITEBYTECODE='1')
        self.reset()

    def reset(self, fail_at=None):
        (self.root/'model.json').write_text(json.dumps(dict(count=0, calls=[], fail_at=fail_at,
            units={'mycelium-intake':'active', 'fs-triage-worker':'active'})))

    def model(self):
        return json.loads((self.root/'model.json').read_text())

    def deploy(self):
        return subprocess.run(['bash', str(self.tests/'deploy_r4.sh'), str(self.source)],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def backup(self):
        return next((self.root/'root').glob('md-convert-2350-*'))

    def rollback(self, backup=None):
        backup = backup or self.backup()
        return subprocess.run(['bash', str(backup/'rollback_r4.sh'), str(backup)],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def untouched_data(self):
        assert (self.intake/'.intake_offset').read_text() == '42\n'
        assert (self.intake/'journal.log').read_text() == 'keep journal\n'
        assert (self.intake/'quarantine/original').read_text() == 'keep quarantine\n'


def assert_failure(result):
    out = result.stdout+result.stderr
    assert result.returncode != 0, out
    assert 'FAIL r4 phase=' in out and 'backup=' in out, out
    assert 'mycelium-intake:' in out and 'fs-triage-worker:' in out, out
    assert 'Rollback command: bash ' in out, out


def happy_and_faults(root):
    happy = Fixture(root/'happy', prior=True)
    result = happy.deploy()
    assert result.returncode == 0, result.stdout+result.stderr
    calls = happy.model()['calls']
    deploy_count = len(calls)
    assert any(c[:4] == ['systemctl', 'show', '-p', 'TemporaryFileSystem'] for c in calls)
    assert any(c[0] == 'nsenter' and '-m' in c for c in calls)
    assert not any(c[0] in ('chmod','chown','mount','umount') for c in calls)
    happy.untouched_data()
    manifest = json.loads((happy.backup()/'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        s = Path(expected['path']).stat()
        assert (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) == (expected['uid'], expected['gid'], expected['mode'])
    # Existing new directories retain their attributes, even when rollback sees a symlink.
    control = root/'control'
    control.write_text('unchanged')
    control.chmod(0o600)
    rejected = happy.triage/'outbox/.rejected'
    rejected.rmdir()
    rejected.symlink_to(control)
    happy.reset()
    result = happy.rollback()
    assert result.returncode == 0, result.stdout+result.stderr
    rollback_count = happy.model()['count']
    assert control.read_text() == 'unchanged' and stat.S_IMODE(control.stat().st_mode) == 0o600
    assert happy.drop.read_text() == '[Service]\n# prior unrelated settings\n'
    assert stat.S_IMODE(happy.drop.stat().st_mode) == 0o640
    assert stat.S_IMODE(happy.drop.parent.stat().st_mode) == 0o750
    assert not (happy.triage/'md_convert.py').exists()
    for name, (data, mode) in happy.original.items():
        p = happy.triage/name if name == 'triage_daemon.py' else happy.intake/name
        assert p.read_bytes() == data and stat.S_IMODE(p.stat().st_mode) == mode
    happy.untouched_data()
    print(f'PASS deploy/rollback success; preserved attributes, prior drop-in, runtime symlink untouched')
    # Fail every observed operation, including helper subprocesses, backup, install,
    # reload, both starts, namespace checks and final verification.
    for i in range(1, deploy_count+1):
        fixture = Fixture(root/f'deploy-{i}', prior=True)
        fixture.reset(i)
        result = fixture.deploy()
        assert_failure(result)
        observed = fixture.model()['calls']
        failure = next(j for j in range(len(observed)) if j+1 == i)
        if not any(c[:2] == ['systemctl','stop'] for c in observed[:failure+1]):
            assert not any(c[:2] == ['systemctl','stop'] for c in observed), observed
        assert not list(fixture.root.rglob('*.r4.*')), 'temporary file leaked'
        fixture.untouched_data()
    print(f'PASS deploy faults: {deploy_count} external operations, each nonzero + phase/states/rollback; pre-stop faults never stop')
    # One ready backup is enough for testing rollback failures; successful deploy
    # generates the exact saved recipe which rollback is required to use.
    for i in range(1, rollback_count+1):
        fixture = Fixture(root/f'rollback-{i}', prior=True)
        deployed = fixture.deploy()
        assert deployed.returncode == 0, deployed.stdout+deployed.stderr
        fixture.reset(i)
        result = fixture.rollback()
        assert_failure(result)
        assert not list(fixture.root.rglob('*.r4.*')), 'temporary file leaked'
        fixture.untouched_data()
    print(f'PASS rollback faults: {rollback_count} external operations, each nonzero + diagnostics')


def preflight_rejects(root):
    for target in ('offset-empty', 'offset-invalid', 'source-link', 'code-link', 'code-hardlink',
                   'drop-link', 'drop-hardlink', 'drop-parent-link', 'drop-ancestor-link', 'inactive', 'low-disk',
                   'work-mode', 'rejected-link'):
        f = Fixture(root/target, prior=True)
        control = f.root/'control'
        control.write_text('control')
        if target == 'offset-empty': (f.intake/'.intake_offset').write_text('')
        elif target == 'offset-invalid': (f.intake/'.intake_offset').write_text('oops')
        elif target in ('source-link', 'code-link', 'code-hardlink', 'drop-link', 'drop-hardlink'):
            p = f.source/'md_convert.py' if target == 'source-link' else f.drop if target.startswith('drop') else f.triage/'triage_daemon.py'
            p.unlink()
            os.link(control, p) if 'hardlink' in target else p.symlink_to(control)
        elif target == 'drop-parent-link':
            f.drop.unlink(); f.drop.parent.rmdir()
            f.drop.parent.symlink_to(f.source, target_is_directory=True)
        elif target == 'drop-ancestor-link':
            parent = f.drop.parent.parent
            parent.rename(parent.with_name('renamed'))
            parent.symlink_to(parent.with_name('renamed'), target_is_directory=True)
        elif target == 'work-mode':
            (f.triage/'work').mkdir(mode=0o750)
        elif target == 'rejected-link':
            (f.triage/'outbox/.rejected').symlink_to(f.source, target_is_directory=True)
        elif target == 'inactive':
            state = f.model(); state['units']['fs-triage-worker'] = 'inactive'
            (f.root/'model.json').write_text(json.dumps(state))
        elif target == 'low-disk':
            # Inject only disk_usage inside the real helper process, using sitecustomize.
            (f.root/'sitecustomize.py').write_text("import shutil\nshutil.disk_usage=lambda p: type('S',(),{'free':1})()\n")
            f.env['PYTHONPATH'] = str(f.root)
        result = f.deploy()
        assert_failure(result)
        assert not any(c[:2] == ['systemctl','stop'] for c in f.model()['calls']), target
        assert control.read_text() == 'control'
    print('PASS 13 preflight rejections: links/hardlinks/parents, offset, inactive services, low disk, runtime directories; zero stops')


def queue_refusals(root):
    for name in ('inbox-pending', 'inbox-dot', 'incoming', 'outbox-json', 'offset-after-stop', 'journal-after-stop', 'label-after-stop', 'partial-label'):
        f = Fixture(root/name)
        if name == 'inbox-pending': (f.triage/'inbox/task').write_text('pending')
        elif name == 'inbox-dot': (f.triage/'inbox/.partial').write_text('pending')
        elif name == 'incoming': (f.intake/'incoming/file').write_text('unfinished')
        elif name == 'outbox-json': (f.triage/'outbox/file.json').write_text('{}')
        elif name == 'partial-label':
            (f.intake/'quarantine/original.safety.json').write_text('{')
        else:
            model = f.root/'bin/model'
            change = { 'offset-after-stop': "(root/'opt/mycelium/file-safety/.intake_offset').write_text('')",
                       'journal-after-stop': "(root/'opt/mycelium/file-safety/journal.log').write_text('interrupted append')",
                       'label-after-stop': "(root/'opt/mycelium/file-safety/quarantine/original.safety.json').write_text('{')"}[name]
            model.write_text(model.read_text().replace("state['units'][unit]='inactive' if op=='stop' else 'active'",
                "state['units'][unit]='inactive' if op=='stop' else 'active'\n        if op=='stop' and unit=='mycelium-intake': " + change))
        result = f.deploy()
        assert_failure(result)
        calls = f.model()['calls']
        early = name in ('inbox-dot', 'incoming', 'outbox-json', 'partial-label')
        if early:
            assert not any(c[:2] == ['systemctl', 'stop'] for c in calls), name
        else:
            assert ['systemctl','stop','mycelium-intake'] in calls
            assert (f.backup()/'state-before-stop.json').exists()
        assert ['systemctl','stop','fs-triage-worker'] not in calls
        assert not any(c[0] == 'install' for c in calls)
        if name not in ('offset-after-stop', 'journal-after-stop'): f.untouched_data()
    print('PASS 8 queue refusals: 4 known blockers zero stops; drain timeout and 3 post-stop changes retain snapshots')


ARCHIVE_VARIANTS = ('archive', 'limit', 'data', 'dangling-data', 'wrong', 'array', 'broken',
                    'link', 'hardlink', 'large', 'fifo', 'directory')
ARCHIVE_MUTATIONS = ('new', 'delete', 'rename', 'size', 'mtime', 'sha256')


def incoming_reject_case(root, variant='archive', mutation=None, worker=False):
    f = Fixture(root)
    label = f.intake/'incoming/old.safety.json'
    label.write_text('{"verdict": "reject"}')
    if variant in ('limit', 'large'):
        label.write_bytes(label.read_bytes().ljust(64 * 1024 + (variant == 'large'), b' '))
    elif variant == 'data':
        label.with_name('old').write_text('unfinished')
    elif variant == 'dangling-data':
        label.with_name('old').symlink_to(root/'absent')
    elif variant in ('wrong', 'array', 'broken'):
        label.write_text({'wrong': '{"verdict":"quarantine"}', 'array': '[]', 'broken': '{'}[variant])
    elif variant in ('link', 'hardlink', 'fifo', 'directory'):
        label.rename(root/'control')
        if variant == 'link': label.symlink_to(root/'control')
        elif variant == 'hardlink': os.link(root/'control', label)
        elif variant == 'fifo': os.mkfifo(label)
        else: label.mkdir()
    if mutation:
        model = root/'bin/model'
        unit = 'fs-triage-worker' if worker else 'mycelium-intake'
        target = "root/'opt/mycelium/file-safety/incoming/old.safety.json'"
        changes = {
            'new': "(root/'opt/mycelium/file-safety/incoming/new.safety.json').write_text('{\"verdict\":\"reject\"}')",
            'delete': f'({target}).unlink()',
            'rename': f"({target}).rename(({target}).with_name('renamed.safety.json'))",
            'size': f"({target}).write_text('{{\"verdict\": \"reject\"}} ')",
            'mtime': f's=os.stat({target}); os.utime({target}, ns=(s.st_atime_ns,s.st_mtime_ns+1000000000))',
            # Same size and restored mtime: only the content digest changes.
            'sha256': f"s=os.stat({target}); ({target}).write_text('{{\"verdict\":\"reject\" }}'); os.utime({target}, ns=(s.st_atime_ns,s.st_mtime_ns))",
        }
        model.write_text(model.read_text().replace("if name=='systemctl':",
            f"if name=='systemctl' and args==['stop',{unit!r}]:\n    {changes[mutation]}\nif name=='systemctl':"))
    if variant in ('archive', 'limit'):
        original = label.read_bytes()
        timestamp = label.stat().st_mtime_ns
    result = f.deploy()
    calls = f.model()['calls']
    if mutation:
        assert_failure(result)
        assert 'task state changed during stop:' in result.stderr, result.stderr
        assert f'phase={"stop-worker" if worker else "drain"}' in result.stderr, result.stderr
        assert (['systemctl', 'stop', 'fs-triage-worker'] in calls) == worker, calls
        assert not any(c[0] == 'install' for c in calls), calls
        assert (f.backup()/f'state-{"stopped" if worker else "drained"}.json').exists()
    elif variant in ('archive', 'limit'):
        assert result.returncode == 0, result.stdout + result.stderr
        assert label.read_bytes() == original and label.stat().st_mtime_ns == timestamp
        for tag in ('before-stop', 'drained', 'stopped'):
            saved = json.loads((f.backup()/f'state-{tag}.json').read_text())
            assert saved['archival_reject_labels'] == [dict(name=label.name, size=len(original),
                mtime_ns=timestamp, sha256=hashlib.sha256(original).hexdigest())], saved
    else:
        assert_failure(result)
        assert not any(c[:2] == ['systemctl', 'stop'] for c in calls), calls
        assert str(label) in result.stderr, result.stderr
    f.untouched_data()


def incoming_reject_cases(root):
    for variant in ARCHIVE_VARIANTS:
        incoming_reject_case(root/variant, variant)
    for worker in (False, True):
        for mutation in ARCHIVE_MUTATIONS:
            incoming_reject_case(root/f'{worker}-{mutation}', mutation=mutation, worker=worker)
    print('PASS incoming-reject: archive and 64 KiB accepted; 10 invalid entries zero stops; 12 archive mutations refused before install')


def absent_and_repeat(root):
    f = Fixture(root/'no-prior')
    original_dirs = {}
    for path, mode in ((f.triage/'work', 0o700), (f.triage/'outbox/.rejected', 0o770)):
        path.mkdir(mode=mode)
        path.chmod(mode)
        original_dirs[path] = path.stat().st_ino
    result = f.deploy()
    assert result.returncode == 0, result.stdout+result.stderr
    first_backup = f.backup()
    result = f.rollback(first_backup)
    assert result.returncode == 0 and not f.drop.exists(), result.stdout+result.stderr
    for path, ino in original_dirs.items():
        assert path.stat().st_ino == ino
    assert stat.S_IMODE((f.triage/'work').stat().st_mode) == 0o700
    assert stat.S_IMODE((f.triage/'outbox/.rejected').stat().st_mode) == 0o770
    # Repeated installation backs up an existing md_convert and r4 drop-in.
    result = f.deploy()
    assert result.returncode == 0, result.stdout+result.stderr
    old_md = (f.triage/'md_convert.py').read_bytes()
    old_drop = f.drop.read_bytes()
    previous = set((f.root/'root').glob('md-convert-2350-*'))
    result = f.deploy()
    assert result.returncode == 0, result.stdout+result.stderr
    latest = next(iter(set((f.root/'root').glob('md-convert-2350-*')) - previous))
    result = f.rollback(latest)
    assert result.returncode == 0, result.stdout+result.stderr
    assert (f.triage/'md_convert.py').read_bytes() == old_md
    assert f.drop.read_bytes() == old_drop
    print('PASS absent/prior drop-in and repeated deploy rollback; existing runtime directory inodes/modes unchanged')


def signals(root):
    for name, number in (('INT', 2), ('TERM', 15)):
        f = Fixture(root/name)
        model = f.root/'bin/model'
        model.write_text(model.read_text().replace("if name=='systemctl':", f"if name=='systemctl' and args==['stop','mycelium-intake']:\n    import signal\n    os.kill(os.getppid(), {number})\n    sys.exit(0)\nif name=='systemctl':"))
        result = f.deploy()
        assert_failure(result)
        assert result.returncode == (130 if name == 'INT' else 143), result
    print('PASS INT/TERM traps before first stop: nonzero, phase, both states, exact rollback command')


def main():
    with tempfile.TemporaryDirectory(prefix='deploy-r4-') as tmp:
        root = Path(tmp)
        preflight_rejects(root/'preflight')
        queue_refusals(root/'queues')
        incoming_reject_cases(root/'incoming-reject')
        signals(root/'signals')
        absent_and_repeat(root/'repeat')
        happy_and_faults(root/'matrix')
    print('PASS all r4 command-model cases (no real units/mounts)')

if __name__ == '__main__':
    main()
