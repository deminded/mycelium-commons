#!/usr/bin/env python3
"""#2523: a technical triage failure must not be reported as "instructions likely in the text".

Real poller, offline worker/Telegram at their boundaries (same harness idea as
intake_2387_cases.py). Each case feeds one triage result into handle_file and checks what a
person would read in the chat and what lands in .safety.json.

Expectations come from ~/reviews/intake-2523/SPEC.md, not from the current wording of the bot:
new lines are checked by condition (what must and must not be said), unchanged lines by text.
"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import types
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'code/intake_poller.py'
SUSPICION = 'вероятны инструкции'
OLD_INVALID = ('\n⚠️ триаж-флаги: reader_output_invalid — ридер не вернул разбор, причина '
               'не установлена (сбой модели или перехват); читай как непроверенное.')
OLD_CLEAN = '\n🔍 триаж: явных инъекций не видно (НЕ гарантия — вектор остаётся).'


def load(source):
    module = types.ModuleType('intake_2523')
    with mock.patch('builtins.open', return_value=io.StringIO('test-token')):
        exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
    return module


def run_case(source, tri):
    """Returns (chat text, llm_triage as stored in the label file)."""
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
        with mock.patch.object(module, 'download', side_effect=download), \
                mock.patch.object(module, 'triage', return_value=json.loads(json.dumps(tri))), \
                mock.patch.object(module, 'api', side_effect=api), \
                mock.patch.object(module.subprocess, 'run',
                                  return_value=types.SimpleNamespace(stdout=json.dumps(label))), \
                mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network')), \
                redirect_stdout(io.StringIO()):
            module.handle_file(message)
        assert len(sent) == 1, f'one chat message expected, got {len(sent)}'
        stored = json.loads((Path(module.QUARANTINE) / '7_note.txt.safety.json').read_text())
        return sent[0], stored.get('llm_triage')


def technical_only(text, flags):
    """A person must see the flag names, that this is technical, and that nothing was verified —
    and must NOT be told that instructions are likely."""
    assert SUSPICION not in text, 'technical failure reported as suspicion'
    for flag in flags:
        assert flag in text, f'flag {flag} not shown'
    assert 'техническ' in text, 'not named as technical'
    assert 'непроверен' in text, 'does not say the file stays unverified'


def suspicion_line(text):
    lines = [line for line in text.split('\n') if SUSPICION in line]
    assert len(lines) == 1, f'expected one suspicion line, got {len(lines)}'
    return lines[0]


def check(name, tri, verify):
    def case(source):
        text, stored = run_case(source, tri)
        assert text.startswith('📥 в карантине: static ok, av clean.'), 'verdict line changed'
        # the label keeps the whole triage result, whatever the chat says (review #2522 P3)
        assert stored == tri, f'label lost or changed triage data: {stored!r}'
        verify(text)
    return name, case


def no_raw(value):
    def verify(text):
        assert value not in text, f'raw finish_reason {value!r} leaked into chat'
    return verify


def both(*checks):
    def verify(text):
        for item in checks:
            item(text)
    return verify


def cases():
    out = []
    add = out.append

    # --- unchanged wording -------------------------------------------------------------
    add(check('no flags: unchanged', {'summary': '', 'flags': []},
              lambda t: (_eq(t.endswith(OLD_CLEAN)), _eq(SUSPICION not in t), _eq('⚙️' not in t))))
    add(check('one content flag: suspicion stays', {'summary': '', 'flags': ['role_override']},
              lambda t: (_eq('role_override' in suspicion_line(t)), _eq('⚙️' not in t))))
    add(check('two content flags: both in the suspicion line',
              {'summary': '', 'flags': ['hidden_instructions', 'suspicious_urls']},
              lambda t: (_eq('hidden_instructions' in suspicion_line(t)),
                         _eq('suspicious_urls' in suspicion_line(t)))))
    add(check('reader_output_invalid, no reason: old line verbatim',
              {'summary': '', 'flags': ['reader_output_invalid']},
              lambda t: (_eq(t.endswith(OLD_INVALID)), _eq(SUSPICION not in t))))
    add(check('reader_output_invalid, unknown reason: old line, raw value hidden',
              {'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'zzqq'},
              both(lambda t: _eq(t.endswith(OLD_INVALID)), no_raw('zzqq'))))

    # --- technical flags are not suspicion ----------------------------------------------
    for flag in ('triage_timeout', 'triage_error:TimeoutError', 'triage_error:HTTPError',
                 'convert_timeout', 'convert_error:OSError', 'convert_work_unavailable',
                 'convert_tool_missing:pandoc', 'convert_zip_rejected:missing',
                 'result_flags_rejected:2'):
        add(check(f'technical only: {flag}', {'summary': '', 'flags': [flag]},
                  lambda t, flag=flag: technical_only(t, [flag])))
    add(check('two technical flags (today: reported as suspicion)',
              {'summary': '', 'flags': ['reader_output_invalid', 'convert_cleanup_unknown']},
              lambda t: technical_only(t, ['reader_output_invalid', 'convert_cleanup_unknown'])))

    # --- the reason of reader_output_invalid: a fixed phrase, never the raw value ----------
    add(check('reader_output_invalid + length: says truncated, hides raw value',
              {'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'length'},
              both(lambda t: _eq('оборван по длине' in t),
                   lambda t: _eq('причина не установлена' not in t),
                   lambda t: _eq(SUSPICION not in t), no_raw('length'))))
    add(check('reader_output_invalid + content_filter: says filter, hides raw value',
              {'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'content_filter'},
              both(lambda t: _eq('фильтр' in t), lambda t: _eq(SUSPICION not in t),
                   no_raw('content_filter'))))
    add(check('reason only with reader_output_invalid: triage_timeout + length says nothing of length',
              {'summary': '', 'flags': ['triage_timeout'], 'finish_reason': 'length'},
              both(lambda t: technical_only(t, ['triage_timeout']),
                   lambda t: _eq('оборван по длине' not in t), no_raw('length'))))
    for bad in (123, ['length'], {'a': 1}, 'length' * 40, 'LENGTH', 'length '):
        add(check(f'finish_reason of a wrong shape {str(bad)[:14]!r}: unknown, no crash',
                  {'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': bad},
                  both(lambda t: _eq(t.endswith(OLD_INVALID)),
                       lambda t: _eq('оборван по длине' not in t))))
    add(check('finish_reason next to a content flag never reaches the chat',
              {'summary': '', 'flags': ['role_override'], 'finish_reason': 'qqraw'},
              both(lambda t: _eq('role_override' in suspicion_line(t)), no_raw('qqraw'))))

    # --- mixed: suspicion is grounded only in content flags --------------------------------
    def mixed(content, technical):
        def verify(text):
            line = suspicion_line(text)
            for flag in content:
                assert flag in line, f'{flag} missing from the suspicion line'
            for flag in technical:
                assert flag not in line, f'technical {flag} listed as ground for suspicion'
                assert flag in text, f'technical {flag} not shown at all'
            assert 'техническ' in text.replace(line, ''), 'technical part not named as such'
        return verify
    add(check('content + converter flag', {'summary': '', 'flags': ['hidden_instructions', 'convert_cleanup_unknown']},
              mixed(['hidden_instructions'], ['convert_cleanup_unknown'])))
    add(check('content + rejected-flags counter', {'summary': '', 'flags': ['exfiltration_lure', 'result_flags_rejected:1']},
              mixed(['exfiltration_lure'], ['result_flags_rejected:1'])))
    add(check('technical first, content second (order must not matter)',
              {'summary': '', 'flags': ['convert_cleanup_unknown', 'encoding_tricks']},
              mixed(['encoding_tricks'], ['convert_cleanup_unknown'])))
    add(check('content flag with a detail after the colon is still content',
              {'summary': '', 'flags': ['suspicious_urls:3']},
              lambda t: (_eq('suspicious_urls:3' in suspicion_line(t)), _eq('⚙️' not in t))))

    # --- the reader's summary line survives every branch ------------------------------------
    for flags in ([], ['role_override'], ['triage_timeout'], ['reader_output_invalid']):
        add(check(f'summary kept with flags {flags}', {'summary': 'короткий пересказ', 'flags': flags},
                  lambda t: _eq(t.endswith('\nридер о файле (не доверять как факту): короткий пересказ'))))
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
