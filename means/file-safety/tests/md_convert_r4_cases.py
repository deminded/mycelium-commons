#!/usr/bin/env python3
"""Offline r4 regressions; synthetic /proc only, no spawned process trees."""
import contextlib
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import md_convert_cases as base
import md_convert_r2_cases as r2

mc = base.mc


def handoff_group(root, missing=False, denied=False):
    intake, source, _, _ = r2.intake_setup(root)
    events = []
    real_rename = os.rename
    def chown(fd, uid, gid):
        assert uid == -1 and gid == 12345
        assert os.fstat(fd).st_size == 0, 'gid set after writing'
        events.append(('chown', os.fstat(fd).st_ino))
        if denied:
            raise PermissionError('injected fchown failure')
    def rename(*args, **kw):
        events.append(('rename', args[1]))
        return real_rename(*args, **kw)
    with mock.patch('grp.getgrnam', side_effect=KeyError('fs-triage') if missing else None,
                    return_value=SimpleNamespace(gr_gid=12345)), \
            mock.patch.object(intake.os, 'fchown', side_effect=chown), \
            mock.patch.object(intake.os, 'rename', side_effect=rename), \
            mock.patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())):
        result = intake.triage(str(source), 'application/pdf')
    if missing or denied:
        assert 'handoff_failed' in result.get('status', ''), result
        assert ('group_missing:fs-triage' in result['status']) if missing else 'PermissionError' in result['status']
        assert not any(e[0] == 'rename' for e in events)
        assert not list(Path(intake.TRIAGE_INBOX).iterdir())
    else:
        assert [e[0] for e in events] == ['chown', 'chown', 'rename', 'rename'], events
        assert events[0][1] != events[1][1]
        assert result.get('summary') == 'ok', result


@contextlib.contextmanager
def proc_model(count, clock, reverse=True):
    me = os.getpid()
    # Descending order exposed the old repeated dictionary walk's O(N*depth).
    entries = [SimpleNamespace(name=str(me + i), path=f'/model/{i}') for i in (range(count, 0, -1) if reverse else range(1, count+1))]
    def opened(path, *a, **kw):
        i = int(path.split('/')[2])
        return io.BytesIO(f'{me+i} (model) S {me+i-1} 0'.encode())
    with mock.patch.object(mc.os, 'scandir', return_value=contextlib.nullcontext(entries)), \
            mock.patch('builtins.open', side_effect=opened), \
            mock.patch.object(mc.time, 'monotonic', side_effect=clock):
        yield {me+i for i in range(1, count+1)}


def deep_linear(root):
    ticks = [0]
    def clock():
        ticks[0] += 1
        return 0
    with proc_model(12000, clock) as expected:
        children, errors = mc.descendants(100)
    assert children == expected and not errors
    assert 24000 <= ticks[0] <= 60010, f'no per-node graph deadline checks: {ticks}'


def graph_deadline(root):
    count, ticks = 3000, [0]
    def clock():
        ticks[0] += 1
        return 0 if ticks[0] < count + 20 else 100
    with proc_model(count, clock):
        children, errors = mc.descendants(50)
    assert errors and len(children) < count, (len(children), errors)
    assert 0 < len(children), 'lost already discovered children'
    assert ticks[0] < count + 30, ticks


def scan_then_signal(root):
    # The real scan exhausts its budget in the graph, after finding children.
    count, ticks, expired = 1000, [0], [False]
    original = mc.descendants
    def clock():
        ticks[0] += 1
        return 10 if expired[0] else 0
    def scan(deadline):
        result = original(deadline)
        expired[0] = True
        return result[0], result[1] + ['deadline']
    killed = []
    with proc_model(count, clock), mock.patch.object(mc, 'descendants', side_effect=scan), mock.patch.object(mc.os, 'kill', side_effect=lambda p, s: killed.append((p, s))), \
            mock.patch.object(mc.os, 'waitpid', return_value=(0, 0)):
        try:
            mc.reap_descendants()
        except mc.CleanupUnknown:
            pass
        else:
            raise AssertionError('incomplete scan accepted')
    assert killed and all(s == signal.SIGKILL for _, s in killed), killed


def scan_partial_known(root):
    ticks = [0]
    def clock():
        ticks[0] += 1
        return 0 if ticks[0] == 1 else 100
    with proc_model(4, clock, reverse=False):
        children, errors = mc.descendants(50)
    assert children == {os.getpid()+1} and errors, (children, errors)


def scan_deadline(root):
    with proc_model(10, lambda: 100):
        children, errors = mc.descendants(50)
    assert not children and errors


def main():
    cases = [('R3-1 gid before publication', handoff_group),
             ('R3-1 group missing refuses handoff', lambda r: handoff_group(r, missing=True)),
             ('R3-1 fchown failure refuses handoff', lambda r: handoff_group(r, denied=True)),
             ('R3-6 deep linear graph', deep_linear),
             ('R3-6 graph deadline keeps partial result', graph_deadline),
             ('R3-6 scan exhaustion still signals', scan_then_signal),
             ('R3-6 scan deadline keeps proven child', scan_partial_known),
             ('R3-6 scan deadline fail closed', scan_deadline)]
    failed = 0
    for name, case in cases:
        with tempfile.TemporaryDirectory(prefix='md-r4-') as tmp:
            try:
                case(Path(tmp))
                print('PASS', name)
            except Exception as exc:
                failed += 1
                print('FAIL', name, type(exc).__name__, str(exc))
    print(f'r4: {len(cases)-failed} PASS, {failed} FAIL')
    return bool(failed)

if __name__ == '__main__':
    sys.exit(main())
