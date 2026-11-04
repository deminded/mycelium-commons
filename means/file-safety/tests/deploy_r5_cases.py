#!/usr/bin/env python3
"""REVIEW4 regressions: real poller text, mocked API and local command model."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

from deploy_r4_cases import (Fixture, GROUP_ID, TESTS, assert_failure, incoming_reject_case,
                             ARCHIVE_VARIANTS, ARCHIVE_MUTATIONS)


def no_stops(f, result):
    assert_failure(result)
    assert not any(c[:2] == ['systemctl', 'stop'] for c in f.model()['calls']), result.stdout + result.stderr


def group_refusal(root, variant):
    f = Fixture(root)
    path = f.intake/'intake_poller.py'
    text = path.read_text()
    if variant == 'missing':
        text = re.sub(r'^GROUP_ID = .*\n', '', text, flags=re.M)
    elif variant == 'zero':
        text = re.sub(r'^GROUP_ID = .*$', 'GROUP_ID = 0', text, flags=re.M)
    else:
        text += f'\nGROUP_ID = {GROUP_ID}\n'
    path.write_text(text)
    no_stops(f, f.deploy())


def source_refusal(root, variant):
    f = Fixture(root)
    path = f.source/'intake_poller.py'
    text = path.read_text()
    if variant == 'missing-env-line':
        text = re.sub(r'^GROUP_ID = .*$', 'GROUP_ID = 123', text, flags=re.M)
    elif variant == 'double-env-line':
        text += '\nGROUP_ID = int(os.environ.get("MYCELIUM_GROUP_ID", "0"))\n'
    elif variant == 'overridden-id':
        text += '\nGROUP_ID = 123\n'
    else:
        text += '\ninvalid python !\n'
    path.write_text(text)
    no_stops(f, f.deploy())


def installed_poller(root, verify=True):
    f = Fixture(root)
    # An operator's shell environment must never choose the service's group.
    f.env['MYCELIUM_GROUP_ID'] = '999'
    result = f.deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    path = f.intake/'intake_poller.py'
    text = path.read_text()
    if not verify:
        return f
    assert re.findall(r'^GROUP_ID\s*=\s*(-?\d+)\b', text, re.M) == [str(GROUP_ID)], 'installed poller has no preserved literal'
    plan = json.loads((f.backup()/'manifest.json').read_text())
    assert plan['group_id'] == GROUP_ID
    assert hashlib.sha256(path.read_bytes()).hexdigest() == plan['files']['intake_poller.py']['sha256']
    assert path.read_bytes() == (f.backup()/'prepared-intake_poller.py').read_bytes()
    return f


def routing(root):
    f = installed_poller(root, verify=False)
    module = types.ModuleType('prepared_intake')
    # Execute the real installed module, replacing just the token read. The API
    # itself is replaced below; any accidental urllib network call fails the test.
    with mock.patch('builtins.open', return_value=io.StringIO('test-token')), \
            mock.patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
            mock.patch.dict(os.environ, MYCELIUM_GROUP_ID='999'):
        exec(compile((f.intake/'intake_poller.py').read_text(), 'installed-poller', 'exec'), module.__dict__)
    module.OFFSET_FILE = str(f.intake/'.intake_offset')
    message = {'chat': {'id': GROUP_ID}, 'document': {'file_id': 'mock-file'}, 'message_id': 7}
    other = {'chat': {'id': 999}, 'message_id': 8}
    def api(method, **kw):
        assert method == 'getUpdates' and kw['offset'] == 42
        return {'result': [{'update_id': 42, 'message': message}, {'update_id': 43, 'message': other}]}
    # A BaseException exits the infinite polling loop without its retry handler.
    with mock.patch.object(module, 'api', side_effect=[api('getUpdates', offset=42), KeyboardInterrupt()]) as api_mock, \
            mock.patch.object(module, 'handle_file') as handle, \
            mock.patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
        try:
            module.main()
        except KeyboardInterrupt:
            pass
    handle.assert_called_once_with(message)
    assert all(call.args == ('getUpdates',) for call in api_mock.call_args_list)
    assert api_mock.call_args_list[0].kwargs['offset'] == 42
    assert api_mock.call_args_list[1].kwargs['offset'] == 44
    assert Path(module.OFFSET_FILE).read_text() == '44'


def checker_group(root):
    f = installed_poller(root, verify=False)
    path = f.intake/'intake_poller.py'
    path.write_text(re.sub(r'^GROUP_ID = .*$', 'GROUP_ID = 123', path.read_text(), flags=re.M))
    # Keep content hash consistent to prove the id check is independent of it.
    manifest = f.backup()/'manifest.json'
    plan = json.loads(manifest.read_text())
    plan['group_id'] = GROUP_ID
    plan['files']['intake_poller.py']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(plan))
    result = subprocess.run([sys.executable, '-B', str(TESTS/'check_deploy.py'), '--backup', str(f.backup())],
                            env=f.env, capture_output=True, text=True)
    assert result.returncode and 'GROUP_ID' in result.stdout, result.stdout + result.stderr


def missing_command(root, command):
    f = Fixture(root)
    # A restricted real PATH, not a patch of the command checker.
    for name in ('bash', 'dirname', 'date', 'id', 'findmnt'):
        (root/'bin'/name).symlink_to(shutil.which(name))
    path = root/'bin'/command
    if path.exists():
        path.unlink()
    f.env['PATH'] = str(root/'bin')
    no_stops(f, f.deploy())


def known_blocker(root, kind, after_stop=False, worker=False):
    if kind == 'reject-label':
        return incoming_reject_case(root, mutation='new' if after_stop else None, worker=worker)
    f = Fixture(root)
    where, text = {
        'partial-label': ('opt/mycelium/file-safety/quarantine/bad.safety.json', '{'),
        'wrong-label': ('opt/mycelium/file-safety/quarantine/bad.safety.json', '{"verdict":"safe"}'),
        'array-label': ('opt/mycelium/file-safety/quarantine/bad.safety.json', '[]'),
        'dot-partial': ('opt/mycelium/fs-triage/inbox/.task.partial', 'partial'),
        'outbox': ('opt/mycelium/fs-triage/outbox/task.json', '{}'),
    }[kind]
    if after_stop:
        model = root/'bin/model'
        unit = 'fs-triage-worker' if worker else 'mycelium-intake'
        model.write_text(model.read_text().replace("if name=='systemctl':",
            f"if name=='systemctl' and args==['stop',{unit!r}]:\n    (root/{where!r}).write_text({text!r})\nif name=='systemctl':"))
    else:
        (root/where).write_text(text)
    result = f.deploy()
    if after_stop:
        assert_failure(result)
        assert ['systemctl', 'stop', 'mycelium-intake'] in f.model()['calls']
        assert not any(c[0] == 'install' for c in f.model()['calls'])
    else:
        no_stops(f, result)
    if not after_stop:
        assert where in result.stderr, result.stderr


def label_inventory(root):
    f = Fixture(root)
    for verdict in ('quarantine', 'reject'):
        (f.intake/f'quarantine/{verdict}.safety.json').write_text(json.dumps({'verdict': verdict}))
    result = f.deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    f.reset()
    blockers = (f.intake/'quarantine/a.safety.json', f.intake/'quarantine/b.safety.json',
                f.intake/'incoming/old.safety.json', f.triage/'inbox/.partial', f.triage/'outbox/orphan.json')
    for path in blockers:
        path.write_text('{')
    result = f.deploy()
    no_stops(f, result)
    assert all(str(path) in result.stderr for path in blockers), result.stderr


def temporary_cleanup(root, rollback=False, signal=None):
    f = Fixture(root, prior=True)
    if rollback:
        result = f.deploy()
        assert result.returncode == 0, result.stdout + result.stderr
        f.reset()
    model = root/'bin/model'
    # Fail after mktemp has succeeded; restore-drop uses cp rather than install.
    condition = "name=='cp' and 'prior-drop-in.conf' in ' '.join(args)" if rollback else "name=='install'"
    failure = 'sys.exit(77)' if signal is None else f'os.kill(os.getppid(), {signal}); sys.exit(0)'
    model.write_text(model.read_text().replace("if name=='systemctl':", f"if {condition}:\n    {failure}\nif name=='systemctl':"))
    result = f.rollback() if rollback else f.deploy()
    assert_failure(result)
    assert not list(root.rglob('*.r4.*')), 'temporary file leaked'


def no_bytecode(root):
    f = Fixture(root)
    f.tests = root/'package/tests'
    f.tests.mkdir(parents=True)
    for name in ('deploy_r4.sh', 'rollback_r4.sh', 'r4_common.sh', 'deploy_r4_lib.py', 'check_deploy.py'):
        shutil.copyfile(TESTS/name, f.tests/name)
    f.env.pop('PYTHONDONTWRITEBYTECODE', None)
    result = f.deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    result = f.rollback()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(root.rglob('__pycache__')), 'recipe created bytecode without caller opt-out'


def main():
    cases = [(f'R4-1 live id {v}', lambda r, v=v: group_refusal(r, v)) for v in ('missing', 'zero', 'double')]
    cases += [(f'R4-1 source {v}', lambda r, v=v: source_refusal(r, v)) for v in ('missing-env-line', 'double-env-line', 'overridden-id', 'syntax')]
    cases += [('R4-1 installed literal + manifest', installed_poller),
              ('R4-1 real main routing', routing), ('R4-1 check_deploy id independent of hash', checker_group)]
    cases += [(f'R4-2 missing command {c}', lambda r, c=c: missing_command(r, c)) for c in ('install', 'mv', 'mktemp', 'nsenter', 'findmnt', 'systemctl', 'python3')]
    cases += [(f'R4-2 {k} before stop', lambda r, k=k: known_blocker(r, k)) for k in ('partial-label', 'wrong-label', 'array-label', 'reject-label', 'dot-partial', 'outbox')]
    cases += [(f'R4-2 {k} after stop', lambda r, k=k: known_blocker(r, k, True)) for k in ('partial-label', 'reject-label', 'dot-partial', 'outbox')]
    cases += [(f'R4-2 {k} after worker stop', lambda r, k=k: known_blocker(r, k, True, True)) for k in ('partial-label', 'reject-label', 'dot-partial', 'outbox')]
    cases += [('R4-2 valid labels accepted; all known blockers listed', label_inventory)]
    cases += [(f'R6 reject-label {v}', lambda r, v=v: incoming_reject_case(r, v))
              for v in ARCHIVE_VARIANTS if v != 'archive']
    cases += [(f'R6 archive {m} after {u} stop',
               lambda r, m=m, w=w: incoming_reject_case(r, mutation=m, worker=w))
              for w, u in ((False, 'intake'), (True, 'worker')) for m in ARCHIVE_MUTATIONS if m != 'new']
    cases += [('R4-3 install failure cleanup', temporary_cleanup),
              ('R4-3 restore failure cleanup', lambda r: temporary_cleanup(r, True)),
              ('R4-3 install INT cleanup', lambda r: temporary_cleanup(r, signal=2)),
              ('R4-3 restore TERM cleanup', lambda r: temporary_cleanup(r, True, 15)),
              ('R4-3 no bytecode without caller opt-out', no_bytecode)]
    failed = 0
    for name, case in cases:
        with tempfile.TemporaryDirectory(prefix='deploy-r5-') as tmp:
            try:
                case(Path(tmp))
                print('PASS', name, flush=True)
            except Exception as exc:
                failed += 1
                print('FAIL', name, type(exc).__name__, str(exc), flush=True)
    print(f'r5: {len(cases)-failed} PASS, {failed} FAIL')
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
