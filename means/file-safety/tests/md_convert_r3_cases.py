#!/usr/bin/env python3
"""Offline r3 regressions: real local producers, harmless control files, no network."""
import ast
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import md_convert_cases as base
import md_convert_r2_cases as r2

mc, daemon = base.mc, base.daemon


def handoff_links(root, target):
    intake, source, _, _ = r2.intake_setup(root)
    control = root / 'control'
    control.write_text('UNCHANGED')
    inbox = Path(intake.TRIAGE_INBOX)
    old = inbox / ('.' + source.name + ('.partial' if target == 'data' else '.kind.partial'))
    old.symlink_to(control)
    actual_open = os.open
    creates = []

    def opened(path, flags, *args, **kwargs):
        if flags & os.O_WRONLY and kwargs.get('dir_fd') is not None:
            creates.append(str(path))
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
            assert args[0] == 0o640
            if (target == 'data' and len(creates) == 1) or (target == 'kind' and len(creates) == 2):
                # A collision at the actual random name must also be harmless.
                os.symlink(control, path, dir_fd=kwargs['dir_fd'])
        return actual_open(path, flags, *args, **kwargs)

    with mock.patch.object(intake.os, 'open', side_effect=opened):
        result = r2.intake_run(intake, source)
    assert control.read_text() == 'UNCHANGED', 'partial followed a producer symlink'
    assert creates, 'no exclusive dirfd handoff'
    assert 'handoff_failed' in result.get('status', ''), result


def quota_independent(root):
    intake = base.load_intake()
    tree = ast.parse((base.CODE / 'intake_poller.py').read_text())
    assert not any(isinstance(n, ast.ImportFrom) and n.module == 'md_convert' for n in ast.walk(tree))
    assert intake.MAX_CONVERT_ZIP_BYTES >= mc.MAX_OUTPUT_BYTES + 1024 * 1024
    from triage_reader import ALLOWED_FLAGS
    assert ALLOWED_FLAGS <= intake.RESULT_KNOWN_FLAGS


def cleanup_proc_error(root):
    # Real escaped child; one unrelated proc entry is unreadable only after preflight.
    pidfile = root / 'pid'
    wrapper = root / 'wrapper.py'
    wrapper.write_text(f'''import builtins,json,os,sys,time
sys.path.insert(0, {str(base.CODE)!r})
import md_convert as mc
original = builtins.open
def opened(path, *a, **kw):
    if str(path) == '/proc/1/stat': raise PermissionError('injected')
    return original(path, *a, **kw)
def tool(args, outdir, deadline):
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.close(1)
        os.close(2)
        time.sleep(30)
        os._exit(0)
    with original({str(pidfile)!r}, 'w') as f: f.write(str(pid))
    with original(args[args.index('-o')+1], 'w') as f: f.write('output')
    builtins.open = opened
    return 0
mc.run_tool = tool
mc.shutil.which = lambda _: 'tool'
print(json.dumps(mc.convert('sample.html','html',{str(root / 'out')!r})))
''')
    pid = None
    mc.become_subreaper()  # The test runner also owns failed-baseline orphans.
    try:
        proc = subprocess.run([sys.executable, str(wrapper)], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        pid = int(pidfile.read_text())
        assert not Path(f'/proc/{pid}').exists(), 'known child survived a proc entry error'
        assert not result['ok'] and any(f.startswith('convert_cleanup_') for f in result['flags']), result
    finally:
        if pid is None and pidfile.exists(): pid = int(pidfile.read_text())
        if pid:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass


def cleanup_deadline(root):
    # Never calls a blocking wait, even if a killed child cannot be reaped yet.
    seen = []
    def wait(pid, options):
        seen.append(options)
        assert options == os.WNOHANG, 'blocking waitpid in cleanup'
        return (0, 0)
    with mock.patch.object(mc, 'descendants', return_value=({999999}, [])), \
            mock.patch.object(mc.os, 'kill'), mock.patch.object(mc.os, 'waitpid', side_effect=wait), \
            mock.patch.object(mc, 'CLEANUP_TIMEOUT', 0.05, create=True):
        started = time.monotonic()
        try: mc.reap_descendants()
        except Exception as exc:
            assert type(exc).__name__ == 'CleanupUnknown', str(exc)
        else: raise AssertionError('unknown cleanup was accepted')
        assert time.monotonic() - started < 0.5 and seen


def live_output_cap(root, count=False):
    tools = root / 'tools'
    tools.mkdir()
    pidfile = root / 'producer.pid'
    marker = root / 'finished'
    tool = tools / 'pandoc'
    tool.write_text(f'''#!{sys.executable}
import os,pathlib,time
pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))
for i in range(8):
    pathlib.Path('chunk'+str(i)).write_bytes(b'x'*1024)
time.sleep(2)
pathlib.Path({str(marker)!r}).touch()
''')
    tool.chmod(0o700)
    with mock.patch.dict(os.environ, PATH=str(tools)), \
            mock.patch.object(mc, 'MAX_OUTPUT_BYTES', 1024 * 1024 if count else 4096), \
            mock.patch.object(mc, 'MAX_MEDIA_COUNT', 2 if count else 200):
        started = time.monotonic()
        result = mc.convert(str(root / 'sample.html'), 'html', str(root / 'out'))
    assert 'convert_output_cap' in result['flags'], result
    assert time.monotonic() - started < 1.8 and not marker.exists(), 'cap checked only after producer exit'
    assert not Path('/proc/' + pidfile.read_text()).exists()


def work_unavailable(root, error=False):
    paths = base.daemon_paths(root)
    (root / 'inbox' / 'sample').write_text('input')
    (root / 'inbox' / '.sample.kind').write_text('pdf')
    patch = dict(side_effect=OSError('work unavailable')) if error else dict(return_value=SimpleNamespace(f_bavail=1, f_frsize=4096))
    with mock.patch.object(daemon.os, 'statvfs', **patch), \
            mock.patch.object(daemon, 'convert_file') as convert, \
            mock.patch.object(daemon.subprocess, 'run') as read:
        daemon.process('sample', **paths)
    result = base.assert_daemon_clean(root, 'sample')
    assert result['flags'] == ['convert_work_unavailable'], result
    convert.assert_not_called()
    read.assert_not_called()


def rejected_directory(root, delete_error=False):
    intake, source, _, archive = r2.intake_setup(root)
    intake.JOURNAL_FILE = str(root / 'journal.log')
    archive.unlink()
    archive.mkdir()
    nested = archive / 'private'
    nested.mkdir(mode=0o700)
    (nested / 'data').write_text('data')
    with mock.patch.object(intake.shutil, 'rmtree', side_effect=AssertionError('intake traversed producer tree')), \
            mock.patch.object(intake.os, 'unlink', side_effect=PermissionError('injected')) if delete_error else mock.patch.object(intake.time, 'sleep'):
        result = r2.intake_run(intake, source)
    assert any(f.startswith('convert_zip_rejected:') for f in result['flags']), result
    isolated = list((Path(intake.TRIAGE_OUTBOX) / '.rejected').iterdir())
    assert len(isolated) == 1 and (isolated[0] / 'private' / 'data').exists()
    journal = Path(intake.JOURNAL_FILE).read_text()
    assert 'outbox_rejected' in journal
    if delete_error: assert 'failed' in journal


def rejected_cleanup(root):
    outbox = root / 'outbox'
    rejected = outbox / '.rejected'
    rejected.mkdir(parents=True)
    tree = rejected / 'dir'
    tree.mkdir()
    (tree / 'private').mkdir(mode=0o700)
    for i in range(10): (tree / 'private' / str(i)).touch()
    control = root / 'control'
    control.write_text('UNCHANGED')
    (rejected / 'link').symlink_to(control)
    with mock.patch.object(daemon, 'REJECTED_MAX_OBJECTS', 3):
        daemon.cleanup_rejected(str(outbox))
    assert (tree / 'private').exists(), 'object budget ignored'
    with mock.patch.object(daemon.time, 'monotonic', side_effect=iter([0, 1])):
        assert daemon.cleanup_rejected(str(outbox)) == 0, 'time budget ignored'
    with mock.patch.object(daemon.os, 'unlink', side_effect=PermissionError('injected')):
        daemon.cleanup_rejected(str(outbox))
    for _ in range(10): daemon.cleanup_rejected(str(outbox))
    assert not list(rejected.iterdir()) and control.read_text() == 'UNCHANGED'


def flags_case(root, many=False):
    intake, source, result, _ = r2.intake_setup(root)
    flags = ['convert_error:ValueError'] * 1000 if many else [
        'hidden_instructions', 'reader_output_invalid', 'convert_timeout', 'unknown',
        'reader_bad\nBOT', 'triage_bad\x00x', 'convert_bad\u202ex']
    result.write_text(json.dumps({'summary': 'preview', 'flags': flags}))
    tri = r2.intake_run(intake, source)
    assert 'summary' in tri and tri['summary'] == 'preview', tri
    assert len(tri['flags']) <= 12 and any(f.startswith('result_flags_rejected:') for f in tri['flags']), tri
    assert all(isinstance(f, str) and '\n' not in f and '\x00' not in f and '\u202e' not in f for f in tri['flags']), tri
    if not many: assert 'hidden_instructions' in tri['flags'] and 'unknown' not in tri['flags'], tri
    if many:
        assert 'result_flags_rejected:989' in tri['flags'], tri
        intake.add_result_refusal(tri, 'convert_zip_rejected:missing')
        assert len(tri['flags']) == 12 and 'result_flags_rejected:990' in tri['flags'], tri


def reply_budget(root):
    intake = base.load_intake()
    sent = []
    with mock.patch.object(intake, 'api', side_effect=lambda method, **kw: sent.append(kw['text'])):
        intake.reply(1, 'x' * 10000)
    assert len(sent[0]) == 3500


def missing_zip(root):
    intake, source, _, archive = r2.intake_setup(root)
    archive.unlink()
    result = r2.intake_run(intake, source)
    assert 'converted' not in result and 'convert_zip_rejected:missing' in result['flags'], result


def deploy_checks(root):
    sys.path.insert(0, str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location('deploy_r4', Path(__file__).with_name('check_deploy.py'))
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    calls = []
    def run(args, **kw):
        calls.append(args)
        if args[0] == 'systemctl':
            return subprocess.CompletedProcess(args, 0, '1234')
        return subprocess.CompletedProcess(args, 0, json.dumps({'filesystems': [
            {'target': str(root / 'work'), 'fstype': 'tmpfs',
             'options': 'rw,nosuid,nodev,noexec,size=256M,nr_inodes=4096,mode=1777'}]}))
    with mock.patch.object(deploy.subprocess, 'run', side_effect=run):
        assert deploy.check_work_mount(str(root / 'work')) == []
    assert any(c[:5] == ['nsenter', '-t', '1234', '-m', 'findmnt'] for c in calls)
    def default_mode(args, **kw):
        answer = run(args, **kw)
        return subprocess.CompletedProcess(args, 0, answer.stdout.replace(',mode=1777', ''))
    with mock.patch.object(deploy.subprocess, 'run', side_effect=default_mode):
        assert deploy.check_work_mount(str(root / 'work')) == []
    for fstype, options in [('ext4', 'rw'), ('tmpfs', 'rw,size=257M,nr_inodes=4096,mode=1777'),
                            ('tmpfs', 'rw,size=256M,nr_inodes=4097,mode=1777'),
                            ('tmpfs', 'rw,size=256M,mode=1777'),
                            ('tmpfs', 'rw,size=256M,nr_inodes=4096,mode=700')]:
        info = {'filesystems': [{'target': str(root / 'work'), 'fstype': fstype, 'options': options}]}
        def bad_run(args, **kw):
            return subprocess.CompletedProcess(args, 0, '1234' if args[0] == 'systemctl' else json.dumps(info))
        with mock.patch.object(deploy.subprocess, 'run', side_effect=bad_run):
            assert deploy.check_work_mount(str(root / 'work'))
    for code in (1, 2):
        with mock.patch.object(deploy.subprocess, 'run', side_effect=subprocess.CalledProcessError(code, 'nsenter')):
            assert deploy.check_work_mount(str(root / 'work'))


def main():
    cases = [('F1 data partial symlinks', lambda r: handoff_links(r, 'data')),
             ('F1 kind partial symlinks', lambda r: handoff_links(r, 'kind')),
             ('F2 independent intake quota', quota_independent),
             ('F4/r4 modeled service namespace tmpfs', deploy_checks),
             ('F3 proc error keeps killing known children', cleanup_proc_error),
             ('F3 bounded WNOHANG cleanup', cleanup_deadline),
             ('F4 live byte cap', live_output_cap),
             ('F4 live file count cap', lambda r: live_output_cap(r, True)),
             ('F4 low work space', work_unavailable),
             ('F4 statvfs failure', lambda r: work_unavailable(r, True)),
             ('F5 isolate private directory', rejected_directory),
             ('F5 deletion failure visible', lambda r: rejected_directory(r, True)),
             ('F5 bounded daemon cleanup', rejected_cleanup),
             ('F6 newline control bidi unknown flags', flags_case),
             ('F6 1000 flags', lambda r: flags_case(r, True)),
             ('F6 chat length', reply_budget), ('F7 missing zip', missing_zip)]
    passed = failed = 0
    for name, case in cases:
        try:
            with tempfile.TemporaryDirectory(prefix='md-convert-r3-') as directory:
                case(Path(directory))
            print('PASS ' + name, flush=True)
            passed += 1
        except Exception as exc:
            print(f'FAIL {name}: {type(exc).__name__}: {exc}', flush=True)
            failed += 1
    print(f'прошло {passed}, упало {failed}')
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
