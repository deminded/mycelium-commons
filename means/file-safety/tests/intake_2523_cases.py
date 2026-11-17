#!/usr/bin/env python3
"""#2523: a technical triage failure must not be reported as "instructions likely in the text".

Real poller, offline worker/Telegram at their boundaries (same harness idea as
intake_2387_cases.py). Each case feeds one triage result into handle_file and checks what a
person would read in the chat and what lands in .safety.json.

Expectations come from ~/reviews/intake-2523/SPEC.md (revision 2), not from the bot's code.
Revision 1 of this set checked the new lines by condition only; review round 1 showed a mutant
that appended "; файл чист" to a reason and stayed green. The lines a person reads are the
subject here, so they are now compared whole, and the conditions stay as a second net.

The END-TO-END cases at the bottom do not replace triage(): the result travels from a queue
file through read_outbox_result and the flag filter. Without them a field lost between the queue
and handle_file goes unnoticed (review round 1, P2), and so does a content flag evicted by the
filter (P3, lost_content_at_cap).
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'code/intake_poller.py'
SUSPICION = 'вероятны инструкции'
NOT_A_SIGN = 'не признак инструкций'
CONTENT_FLAGS = ('imperative_to_reader', 'role_override', 'hidden_instructions',
                 'exfiltration_lure', 'suspicious_urls', 'encoding_tricks')
OLD_INVALID = ('\n⚠️ триаж-флаги: reader_output_invalid — ридер не вернул разбор, причина '
               'не установлена (сбой модели или перехват); читай как непроверенное.')
OLD_CLEAN = '\n🔍 триаж: явных инъекций не видно (НЕ гарантия — вектор остаётся).'
REASON_LENGTH = ' (ответ ридера оборван по длине)'
REASON_FILTER = ' (ответ остановлен фильтром поставщика модели)'
SUMMARY_PREFIX = '\nридер о файле (не доверять как факту): '


def content_line(flags):
    return ('\n⚠️ триаж-флаги: ' + ', '.join(flags)
            + ' — читай особенно осторожно, вероятны инструкции в тексте.')


def rejected_line(flag):
    return ('\n⚠️ часть флагов ридера отброшена при приёме (' + flag
            + '): что в них было, неизвестно — читай особенно осторожно.')


def full_technical(flags, reason=''):
    return ('\n⚙️ техническая ошибка обработки: ' + ', '.join(flags) + reason
            + ' — это не признак инструкций в тексте; их отсутствия она тоже не подтверждает. '
              'Читай как непроверенное.')


def short_technical(flags, reason=''):
    return '\n⚙️ кроме того, техническая ошибка обработки: ' + ', '.join(flags) + reason + '.'


def load(source):
    module = types.ModuleType('intake_2523')
    with mock.patch('builtins.open', return_value=io.StringIO('test-token')):
        exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
    return module


def run_case(source, tri=None, queue=None):
    """Returns (chat text, llm_triage as stored in the label file).

    tri   — triage() is replaced and returns this dict (unit path);
    queue — triage() is REAL: this dict is what the worker leaves in the outbox file."""
    module = load(source)
    with tempfile.TemporaryDirectory(prefix='intake-2523-') as tmp:
        root = Path(tmp)
        for attr in ('INCOMING', 'QUARANTINE'):
            path = root / attr
            path.mkdir()
            setattr(module, attr, str(path))
        module.OFFSET_FILE = str(root / 'offset')
        module.GROUP_ID = -100123
        message = {'chat': {'id': module.GROUP_ID}, 'message_id': 7,
                   'document': {'file_id': 'file-id', 'file_name': 'note.txt', 'file_size': 8}}
        sent = []

        def download(file_id, path):
            Path(path).write_bytes(b'original')

        def api(method, **params):
            assert method == 'sendMessage', method
            sent.append(params['text'])
            return {'ok': True}

        label = {'verdict': 'quarantine', 'av': 'clean', 'mime': 'text/plain',
                 'sha256': 'abc', 'static_reasons': []}
        patches = [
            mock.patch.object(module, 'download', side_effect=download),
            mock.patch.object(module, 'api', side_effect=api),
            mock.patch.object(module.subprocess, 'run',
                              return_value=types.SimpleNamespace(stdout=json.dumps(label))),
            mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network')),
            redirect_stdout(io.StringIO()),
        ]
        handoffs = []
        if queue is None:
            patches.append(mock.patch.object(module, 'triage',
                                             return_value=json.loads(json.dumps(tri))))
        else:
            inbox, outbox = root / 'inbox', root / 'outbox'
            inbox.mkdir()
            outbox.mkdir()
            module.TRIAGE_INBOX, module.TRIAGE_OUTBOX = str(inbox), str(outbox)
            # Bounded wait: a hand-off the harness misses must fail in seconds, not hang the run.
            module.TRIAGE_WAIT_SECONDS, module.TRIAGE_POLL = 2, 0.01
            name = '7_note.txt'
            actual_rename = os.rename

            def handoff(src, dest, **kwargs):
                actual_rename(src, dest, **kwargs)
                if str(dest) == name and kwargs.get('dst_dir_fd') is not None:
                    # The worker answers at once: the result appears as the task is published.
                    handoffs.append(name)
                    (outbox / (name + '.json')).write_text(json.dumps(queue), encoding='utf-8')

            patches += [
                mock.patch.object(module.os, 'rename', side_effect=handoff),
                mock.patch('grp.getgrnam', return_value=types.SimpleNamespace(gr_gid=os.getgid())),
                mock.patch('pwd.getpwnam', return_value=types.SimpleNamespace(pw_uid=os.getuid())),
            ]
        for patch in patches:
            patch.__enter__()
        try:
            module.handle_file(message)
        finally:
            for patch in reversed(patches):
                patch.__exit__(None, None, None)
        if queue is not None:
            assert handoffs == ['7_note.txt'], f'the task never reached the queue: {handoffs}'
        assert len(sent) == 1, f'one chat message expected, got {len(sent)}'
        stored = json.loads((Path(module.QUARANTINE) / '7_note.txt.safety.json').read_text())
        return sent[0], stored.get('llm_triage')


def tail(text):
    """Everything the bot says about triage: the chat text after its first (verdict) line."""
    head, sep, rest = text.partition('\n')
    return sep + rest


def technical_only(text, flags):
    """A person must see the flag names, that this is technical, and that nothing was verified —
    and must NOT be told that instructions are likely."""
    assert SUSPICION not in text, 'technical failure reported as suspicion'
    for flag in flags:
        assert flag in text, f'flag {flag} not shown'
    assert 'техническ' in text, 'not named as technical'
    assert 'непроверен' in text, 'does not say the file stays unverified'
    # honest in both directions: a failed triage is no sign of an attack and no all-clear either
    assert 'не подтверждает' in text, 'does not say that absence of instructions is unconfirmed'


def never_said(text):
    """Whatever the branch, the bot must not claim the file is clean or that triage did not run."""
    said = tail(text).split(SUMMARY_PREFIX)[0]  # the reader's own words are not the bot's claim
    for phrase in ('чист', 'безопас', 'разбор не выполнен', 'разбор неполон'):
        assert phrase not in said, f'bot says {phrase!r}'


def check(name, tri, expected_tail, *extra, queue=None, stored_expected=None):
    def case(source):
        text, stored = run_case(source, tri=tri, queue=queue)
        assert text.startswith('📥 в карантине: static ok, av clean.'), 'verdict line changed'
        # the label keeps the whole triage result, whatever the chat says (review #2522 P3)
        want = stored_expected if stored_expected is not None else tri
        assert stored == want, f'label lost or changed triage data: {stored!r}'
        got = tail(text)
        allowed = expected_tail if isinstance(expected_tail, tuple) else (expected_tail,)
        assert got in allowed, f'chat differs from SPEC: {got!r}'
        never_said(text)
        for verify in extra:
            verify(text)
    return name, case


def no_raw(value):
    def verify(text):
        assert value not in text, f'raw finish_reason {value!r} leaked into chat'
    return verify


def absent(*phrases):
    def verify(text):
        for phrase in phrases:
            assert phrase not in text, f'{phrase!r} must not be said here'
    return verify


def cases():
    out = []
    add = out.append
    T = lambda flags, **more: dict({'summary': '', 'flags': flags}, **more)

    # --- unchanged wording -------------------------------------------------------------
    add(check('no flags: unchanged', T([]), OLD_CLEAN, absent(SUSPICION, '⚙️')))
    for flag in CONTENT_FLAGS:
        # all six names: a classifier that forgets one would silently drop the warning
        add(check(f'content flag alone: {flag}', T([flag]), content_line([flag]), absent('⚙️')))
    add(check('two content flags: both in the suspicion line',
              T(['hidden_instructions', 'suspicious_urls']),
              content_line(['hidden_instructions', 'suspicious_urls'])))
    add(check('content flag with a detail after the colon is still content',
              T(['suspicious_urls:3']), content_line(['suspicious_urls:3']), absent('⚙️')))
    add(check('reader_output_invalid, no reason: old line verbatim',
              T(['reader_output_invalid']), OLD_INVALID, absent(SUSPICION)))
    add(check('reader_output_invalid, unknown reason: old line, raw value hidden',
              T(['reader_output_invalid'], finish_reason='zzqq'), OLD_INVALID, no_raw('zzqq')))

    # --- technical flags are not suspicion ----------------------------------------------
    for flag in ('triage_timeout', 'triage_error:TimeoutError', 'triage_error:HTTPError',
                 'convert_timeout', 'convert_error:OSError', 'convert_work_unavailable',
                 'convert_tool_missing:pandoc', 'convert_zip_rejected:missing'):
        add(check(f'technical only: {flag}', T([flag]), full_technical([flag]),
                  lambda t, flag=flag: technical_only(t, [flag])))
    add(check('two technical flags',
              T(['reader_output_invalid', 'convert_cleanup_unknown']),
              full_technical(['reader_output_invalid', 'convert_cleanup_unknown']),
              lambda t: technical_only(t, ['reader_output_invalid', 'convert_cleanup_unknown'])))

    # --- the reason of reader_output_invalid: a fixed phrase, never the raw value ----------
    add(check('reader_output_invalid + length: says truncated, hides raw value',
              T(['reader_output_invalid'], finish_reason='length'),
              full_technical(['reader_output_invalid'], REASON_LENGTH),
              absent('причина не установлена', SUSPICION), no_raw('length')))
    add(check('reader_output_invalid + content_filter: says filter, hides raw value',
              T(['reader_output_invalid'], finish_reason='content_filter'),
              full_technical(['reader_output_invalid'], REASON_FILTER),
              absent(SUSPICION), no_raw('content_filter')))
    add(check('reason only with reader_output_invalid: triage_timeout + length says nothing of length',
              T(['triage_timeout'], finish_reason='length'), full_technical(['triage_timeout']),
              absent('оборван по длине'), no_raw('length')))
    add(check('reason kept among several technical flags',
              T(['reader_output_invalid', 'convert_cleanup_unknown'], finish_reason='length'),
              full_technical(['reader_output_invalid', 'convert_cleanup_unknown'], REASON_LENGTH),
              no_raw('length')))
    add(check('reason kept next to a content flag, outside the suspicion line',
              T(['role_override', 'reader_output_invalid'], finish_reason='content_filter'),
              content_line(['role_override']) + short_technical(['reader_output_invalid'], REASON_FILTER),
              no_raw('content_filter')))
    for bad in (123, ['length'], {'a': 1}, 'length' * 40, 'LENGTH', 'length ', None, True):
        add(check(f'finish_reason of a wrong shape {str(bad)[:14]!r}: unknown, no crash',
                  T(['reader_output_invalid'], finish_reason=bad), OLD_INVALID,
                  absent('оборван по длине')))
    add(check('finish_reason next to a content flag never reaches the chat',
              T(['role_override'], finish_reason='qqraw'), content_line(['role_override']),
              no_raw('qqraw')))

    # --- mixed: suspicion is grounded only in content flags --------------------------------
    add(check('content + converter flag', T(['hidden_instructions', 'convert_cleanup_unknown']),
              content_line(['hidden_instructions']) + short_technical(['convert_cleanup_unknown']),
              absent(NOT_A_SIGN)))
    add(check('technical first, content second (order must not matter)',
              T(['convert_cleanup_unknown', 'encoding_tricks']),
              content_line(['encoding_tricks']) + short_technical(['convert_cleanup_unknown']),
              absent(NOT_A_SIGN)))

    add(check('several content and several technical flags interleaved: order kept inside each group',
              T(['triage_timeout', 'role_override', 'convert_cleanup_unknown', 'suspicious_urls:2']),
              content_line(['role_override', 'suspicious_urls:2'])
              + short_technical(['triage_timeout', 'convert_cleanup_unknown']), absent(NOT_A_SIGN)))

    # --- the rejected-flags counter is a kind of its own (SPEC rev. 2) ----------------------
    # It means part of the reader's flags never reached the person; a content flag may be among
    # them. So it is neither "technical" nor a ground for "не признак инструкций".
    add(check('rejected counter alone: unknown, not technical',
              T(['result_flags_rejected:2']), rejected_line('result_flags_rejected:2'),
              absent(SUSPICION, NOT_A_SIGN, '⚙️', 'техническ')))
    add(check('rejected counter + technical: no "not a sign" reassurance',
              T(['triage_timeout', 'result_flags_rejected:2']),
              rejected_line('result_flags_rejected:2') + short_technical(['triage_timeout']),
              absent(SUSPICION, NOT_A_SIGN)))
    add(check('rejected counter listed first: same line order',
              T(['result_flags_rejected:1', 'triage_timeout']),
              rejected_line('result_flags_rejected:1') + short_technical(['triage_timeout']),
              absent(NOT_A_SIGN)))
    add(check('content + rejected counter',
              T(['exfiltration_lure', 'result_flags_rejected:1']),
              content_line(['exfiltration_lure']) + rejected_line('result_flags_rejected:1'),
              absent('⚙️', NOT_A_SIGN)))
    add(check('content + rejected counter + technical: three lines in SPEC order',
              T(['triage_timeout', 'result_flags_rejected:3', 'role_override']),
              content_line(['role_override']) + rejected_line('result_flags_rejected:3')
              + short_technical(['triage_timeout']), absent(NOT_A_SIGN)))
    add(check('reader_output_invalid + rejected counter, no reason: not the old single-flag line',
              T(['reader_output_invalid', 'result_flags_rejected:1']),
              rejected_line('result_flags_rejected:1') + short_technical(['reader_output_invalid']),
              absent(NOT_A_SIGN, 'причина не установлена')))
    add(check('reader_output_invalid + rejected counter + length: reason in the technical line',
              T(['reader_output_invalid', 'result_flags_rejected:1'], finish_reason='length'),
              rejected_line('result_flags_rejected:1')
              + short_technical(['reader_output_invalid'], REASON_LENGTH), no_raw('length')))

    # --- the reader's summary line survives every branch ------------------------------------
    for flags, lines in (([], OLD_CLEAN), (['role_override'], content_line(['role_override'])),
                         (['triage_timeout'], full_technical(['triage_timeout'])),
                         (['reader_output_invalid'], OLD_INVALID),
                         (['result_flags_rejected:1'], rejected_line('result_flags_rejected:1'))):
        add(check(f'summary kept with flags {flags}',
                  {'summary': 'короткий пересказ', 'flags': flags},
                  lines + SUMMARY_PREFIX + 'короткий пересказ'))
    # the sanitizer must hold on the multi-flag path too: a newline in the summary would let the
    # file forge a separate line under the bot's name (review round 1, P2)
    # What is pinned: the forged line stays INSIDE the labelled summary line and the bidi mark is
    # gone. Whether the newline becomes a space is the sanitizer's own business and out of scope
    # here (today it is dropped without a space — the first version of this case assumed a space
    # from the docstring, and the implementer caught it), so both spellings pass.
    mixed_lines = content_line(['role_override']) + short_technical(['triage_timeout']) + SUMMARY_PREFIX
    add(check('summary with a newline and bidi mark on the mixed path stays one labelled line',
              {'summary': 'пересказ\n[оператор: файл чистый]‮', 'flags': ['role_override', 'triage_timeout']},
              (mixed_lines + 'пересказ[оператор: файл чистый]', mixed_lines + 'пересказ [оператор: файл чистый]'),
              lambda t: _eq(len(t.split('\n')) == 4), absent('‮')))

    # --- END-TO-END: real triage(), the result comes from a queue file -----------------------
    add(check('E2E control: clean result through the real queue (checks the harness itself)',
              None, OLD_CLEAN + SUMMARY_PREFIX + 'ok',
              queue={'summary': 'ok', 'flags': []},
              stored_expected={'summary': 'ok', 'flags': []}))
    add(check('E2E: finish_reason survives queue -> handle_file -> label and chat',
              None, full_technical(['reader_output_invalid'], REASON_LENGTH), no_raw('length'),
              queue={'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'length'},
              stored_expected={'summary': '', 'flags': ['reader_output_invalid'],
                               'finish_reason': 'length'}))
    add(check('E2E: content_filter reason survives the queue too',
              None, full_technical(['reader_output_invalid'], REASON_FILTER), no_raw('content_filter'),
              queue={'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'content_filter'},
              stored_expected={'summary': '', 'flags': ['reader_output_invalid'],
                               'finish_reason': 'content_filter'}))
    add(check('E2E: content flag evicted at the cap leaves the rejected line, not "not a sign"',
              None, rejected_line('result_flags_rejected:2') + short_technical(['triage_timeout'] * 11),
              absent(NOT_A_SIGN, SUSPICION),
              queue={'summary': '', 'flags': ['triage_timeout'] * 11 + ['role_override', 'bad flag']},
              stored_expected={'summary': '', 'flags': ['triage_timeout'] * 11 + ['result_flags_rejected:2']}))
    add(check('E2E: reader succeeded, only the archive is missing — not "triage did not run"',
              None, full_technical(['convert_zip_rejected:missing']) + SUMMARY_PREFIX + 'Короткий пересказ',
              queue={'summary': 'Короткий пересказ', 'flags': [], 'converted': {'tool': 'pandoc'}},
              stored_expected={'summary': 'Короткий пересказ', 'flags': ['convert_zip_rejected:missing']}))
    add(check('E2E: a counter planted in the queue is not trusted as the local one',
              None, rejected_line('result_flags_rejected:1'), absent(NOT_A_SIGN, '⚙️'),
              queue={'summary': '', 'flags': ['result_flags_rejected:9']},
              stored_expected={'summary': '', 'flags': ['result_flags_rejected:1']}))
    return out


def _eq(condition):
    assert condition
    return True


def run(source, verbose=True):
    failed = []
    for name, case in cases():
        try:
            case(source)
            if verbose:
                print('PASS', name, flush=True)
        except Exception as exc:
            failed.append(name)
            if verbose:
                print('FAIL', name, '|', type(exc).__name__, str(exc)[:160], flush=True)
    return failed


if __name__ == '__main__':
    failures = run(SOURCE.read_text())
    total = len(cases())
    print(f'2523: {total - len(failures)} PASS, {len(failures)} FAIL')
    sys.exit(bool(failures))
