#!/usr/bin/env python3
"""Execute actual 2387 shells in r4's offline filesystem/command model."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

from deploy_r4_cases import Fixture as R4Fixture, TESTS
import deploy_2387_lib as lib


class Fixture(R4Fixture):
    def __init__(self, root):
        super().__init__(root)
        model = root / 'bin/model'
        code = model.read_text()
        checks = '''if name=='journalctl':
    if '--since' in args: print(os.environ.get('ZIP2387_JOURNAL', 'intake started'))
    sys.exit(0)
if name=='systemctl' and args==['stop','mycelium-intake'] and not os.environ.get('ZIP2387_ROLLBACK'):
    backup=next((root/'root').glob('md-zip-2387-*'))
    assert (backup/'READY').is_file()
    assert (backup/'intake_poller.py').read_bytes()==(root/'opt/mycelium/file-safety/intake_poller.py').read_bytes()
    assert (backup/'prepared-intake_poller.py').is_file()
'''
        model.write_text(code.replace("if name=='systemctl':", checks + "if name=='systemctl':"))
        (root / 'bin/journalctl').symlink_to(model)
        # Make source differ, so rollback must restore the saved original bytes.
        with (self.source / 'intake_poller.py').open('a') as stream:
            stream.write('\n# 2387 deployment fixture\n')
        self.env['MYCELIUM_GROUP_ID'] = '999'

    def deploy(self):
        return subprocess.run(['bash', str(TESTS / 'deploy_2387.sh'), str(self.source)],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def backup(self):
        return next((self.root / 'root').glob('md-zip-2387-*'))

    def rollback(self):
        return subprocess.run(['bash', str(self.backup() / 'rollback_2387.sh'), str(self.backup())],
                              env=dict(self.env, ZIP2387_ROLLBACK='1'), capture_output=True, text=True, timeout=30)

    def unchanged_other_code(self):
        assert (self.triage / 'triage_daemon.py').read_bytes() == self.original['triage_daemon.py'][0]
        assert not (self.triage / 'md_convert.py').exists()
        assert not self.drop.exists()
        self.untouched_data()


def okay(result):
    assert result.returncode == 0, result.stdout + result.stderr


def failed(fixture, result, before_stop=False):
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'FAIL 2387 phase=' in result.stderr, result.stdout + result.stderr
    calls = fixture.model()['calls']
    if before_stop:
        assert not any(c[:2] == ['systemctl', 'stop'] for c in calls), calls
    assert not list(fixture.root.rglob('*.r4.*')), 'temporary file leaked'
    fixture.unchanged_other_code()


def happy(root):
    f = Fixture(root)
    okay(f.deploy())
    plan = json.loads((f.backup() / 'manifest.json').read_text())
    installed = (f.intake / 'intake_poller.py').read_text()
    assert f"GROUP_ID = {plan['group_id']}" in installed and plan['group_id'] != 999
    assert '2387 deployment fixture' in installed
    assert sum(c[:2] == ['systemctl', 'is-active'] for c in f.model()['calls']) >= 33
    journal = [c for c in f.model()['calls'] if c[0] == 'journalctl' and '--since' in c]
    assert len(journal) == 1 and journal[0][journal[0].index('--since') + 1].startswith('@')
    for c in f.model()['calls']:
        if c[0] == 'systemctl' and c[1] in ('stop', 'start'):
            assert c[-1] == 'mycelium-intake', c
    f.unchanged_other_code()
    deploy_calls = f.model()['calls']
    f.reset()
    okay(f.rollback())
    assert (f.intake / 'intake_poller.py').read_bytes() == f.original['intake_poller.py'][0]
    f.unchanged_other_code()
    rollback_calls = f.model()['calls']
    # Failure at every external operation, including backup, install, hash check,
    # active observation, journal access and the saved standalone rollback.
    for rollback, calls in ((False, deploy_calls), (True, rollback_calls)):
        for index in range(1, len(calls) + 1):
            case = Fixture(root / f'fault-{rollback}-{index}')
            if rollback:
                okay(case.deploy())
            case.reset(index)
            result = case.rollback() if rollback else case.deploy()
            prior = calls[:index]
            before_stop = not any(c[:2] == ['systemctl', 'stop'] for c in prior)
            failed(case, result, before_stop=before_stop)
        print(f'PASS {"rollback" if rollback else "deploy"} faults: {len(calls)} operations', flush=True)


def refusal(root, variant):
    f = Fixture(root)
    if variant in ('source', 'live-id', 'source-id'):
        path = (f.intake if variant == 'live-id' else f.source) / 'intake_poller.py'
        with path.open('a') as stream:
            stream.write('\ninvalid python !\n' if variant == 'source' else '\nGROUP_ID = 0\n')
    elif variant == 'queue':
        (f.intake / 'incoming/partial').write_text('pending')
    elif variant == 'command':
        # A fully restricted PATH: journalctl really is missing.
        for name in ('bash', 'dirname', 'date'):
            (root / 'bin' / name).symlink_to(shutil.which(name))
        (root / 'bin/journalctl').unlink()
        f.env['PATH'] = str(root / 'bin')
    failed(f, f.deploy(), before_stop=True)


def late_failure(root, variant):
    f = Fixture(root)
    if variant == 'traceback':
        f.env['ZIP2387_JOURNAL'] = 'Traceback (most recent call last): boom'
    else:
        model = root / 'bin/model'
        injection = {
            'hash': "if name=='mv':\n    with open(args[-2], 'a') as stream: stream.write('# corruption')\n",
            'drain': "if name=='systemctl' and args==['stop','mycelium-intake']:\n    (root/'opt/mycelium/fs-triage/outbox/orphan').write_text('{}')\n",
            'signal': "if name=='install':\n    os.kill(os.getppid(), 15); sys.exit(0)\n",
        }[variant]
        model.write_text(model.read_text().replace("if name=='systemctl':", injection + "if name=='systemctl':"))
    failed(f, f.deploy())
    if variant in ('hash', 'drain', 'signal'):
        assert ['systemctl', 'start', 'mycelium-intake'] not in f.model()['calls']


def corrupt_backup(root):
    f = Fixture(root)
    okay(f.deploy())
    with (f.backup() / 'intake_poller.py').open('a') as stream:
        stream.write('# corrupted backup')
    f.reset()
    failed(f, f.rollback(), before_stop=True)


def health_clock(root):
    with mock.patch.object(lib.r4, 'TEST_ROOT', ''), \
            mock.patch.object(lib.time, 'sleep') as sleep, \
            mock.patch.object(lib.subprocess, 'run', return_value=types.SimpleNamespace(stdout='')) as command:
        lib.health('1234.5')
    assert sleep.call_args_list == [mock.call(1)] * 30
    assert command.call_args_list[:-1] == [mock.call(
        ['systemctl', 'is-active', '--quiet', 'mycelium-intake'], check=True)] * 31
    assert command.call_args_list[-1] == mock.call(
        ['journalctl', '-u', 'mycelium-intake', '--since', '@1234.5', '--no-pager', '-o', 'cat'],
        capture_output=True, text=True, check=True)


def main():
    cases = [('deploy/rollback + every external failure', happy)]
    cases += [(f'preflight {v}', lambda root, v=v: refusal(root, v))
              for v in ('source', 'live-id', 'source-id', 'queue', 'command')]
    cases += [(f'late failure {v}', lambda root, v=v: late_failure(root, v))
              for v in ('traceback', 'hash', 'drain', 'signal')]
    cases += [('corrupt backup refused before stop', corrupt_backup)]
    cases += [('production health observes all 30 seconds', health_clock)]
    failures = 0
    for name, case in cases:
        with tempfile.TemporaryDirectory(prefix='deploy-2387-') as tmp:
            try:
                case(Path(tmp))
                print('PASS', name, flush=True)
            except Exception as exc:
                failures += 1
                print('FAIL', name, type(exc).__name__, str(exc), flush=True)
    print(f'deploy 2387: {len(cases) - failures} PASS, {failures} FAIL')
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
