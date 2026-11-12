#!/usr/bin/env python3
"""Semantic reader: the answer budget and what the label says when the answer does not parse."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import types
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'code/triage_reader.py'


def load(source):
    module = types.ModuleType('reader_2522')
    exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
    return module


def run_reader(source, message, finish='stop', network_error=None):
    """Run main() on a real temporary file with the model call replaced; return (result, request body)."""
    module = load(source)
    with tempfile.TemporaryDirectory(prefix='reader-2522-') as tmp:
        key = Path(tmp) / 'key'
        key.write_text('test-key\n')
        target = Path(tmp) / 'doc.md'
        target.write_text('# Отчёт\n\nТекст документа.\n', encoding='utf-8')
        module.KEY_FILE = str(key)
        sent = []

        def urlopen(req, timeout):
            sent.append(req)
            assert timeout == module.TIMEOUT
            if network_error is not None:
                raise network_error
            return io.BytesIO(json.dumps(
                {'choices': [{'finish_reason': finish, 'message': message}]}).encode())

        out = io.StringIO()
        with mock.patch('urllib.request.urlopen', side_effect=urlopen), \
                mock.patch.object(sys, 'argv', ['triage_reader.py', str(target)]), redirect_stdout(out):
            assert module.main() == 0
        lines = out.getvalue().splitlines()
        assert len(lines) == 1, 'reader prints exactly one JSON line'
        body = json.loads(sent[0].data) if sent else None
        return json.loads(lines[0]), body, module


def good_answer(source):
    result, body, module = run_reader(source, {'content': '```json\n{"summary": "о чём файл", '
                                                          '"flags": ["role_override", "made_up"]}\n```'})
    assert result == {'summary': 'о чём файл', 'flags': ['role_override']}, result
    # the budget must leave room for the answer after the model's own reasoning (#2522):
    # with 500 the reasoning alone used all of it on any real document
    assert body['max_tokens'] == module.MAX_TOKENS and module.MAX_TOKENS >= 2000, body['max_tokens']
    assert body['model'] == module.MODEL and body['temperature'] == 0


def budget_ran_out(source):
    for message in ({'content': '', 'reasoning_content': 'long thoughts'},
                    {'content': None, 'reasoning_content': 'long thoughts'},
                    {'reasoning_content': 'long thoughts'}):
        result, _, _ = run_reader(source, message, finish='length')
        assert result == {'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'length'}, result


def prose_instead_of_json(source):
    result, _, _ = run_reader(source, {'content': 'Файл безопасен, флагов нет.'}, finish='stop')
    assert result == {'summary': '', 'flags': ['reader_output_invalid'], 'finish_reason': 'stop'}, result


def finish_reason_is_cleaned(source):
    result, _, _ = run_reader(source, {'content': ''}, finish='len"gth\n<b>' + 'x' * 80)
    assert result['flags'] == ['reader_output_invalid']
    assert result['finish_reason'] == ('lengthb' + 'x' * 80)[:24], result['finish_reason']
    result, _, _ = run_reader(source, {'content': ''}, finish=None)
    assert result['finish_reason'] == 'none', result


def network_failure(source):
    result, _, _ = run_reader(source, {}, network_error=OSError('connection refused'))
    assert result == {'summary': '', 'flags': ['triage_error:OSError']}, result


def reason_survives_intake(source):
    """The poller validates the reader's result before it writes the label: the recorded reason
    must pass that validation untouched, and the flag must still read as 'reader gave no result'."""
    import os
    from intake_2387_cases import SOURCE as INTAKE, load as load_intake
    intake = load_intake(INTAKE.read_text())
    with tempfile.TemporaryDirectory(prefix='reader-2522-outbox-') as tmp:
        path = Path(tmp) / '7_doc.pdf.json'
        path.write_text(json.dumps({'summary': '', 'flags': ['reader_output_invalid'],
                                    'finish_reason': 'length', 'converted': {'tool': 'pandoc'}}))
        owner = types.SimpleNamespace(pw_uid=os.getuid())
        with mock.patch.object(intake.pwd, 'getpwnam', return_value=owner):
            result = intake.read_outbox_result(str(path))
    assert result['flags'] == ['reader_output_invalid'], result
    assert result['finish_reason'] == 'length' and result['converted'] == {'tool': 'pandoc'}, result


CASES = [('reason survives intake validation', reason_survives_intake),
         ('good answer and budget', good_answer), ('budget ran out', budget_ran_out),
         ('prose instead of JSON', prose_instead_of_json), ('finish reason is cleaned', finish_reason_is_cleaned),
         ('network failure', network_failure)]


def run(source, verbose=True):
    failed = []
    for title, case in CASES:
        try:
            case(source)
            if verbose:
                print('PASS', title, flush=True)
        except Exception as exc:
            failed.append(title)
            if verbose:
                print('FAIL', title, type(exc).__name__, str(exc)[:200], flush=True)
    return failed


if __name__ == '__main__':
    failures = run(SOURCE.read_text())
    print(f'2522: {len(CASES) - len(failures)} PASS, {len(failures)} FAIL')
    sys.exit(bool(failures))
