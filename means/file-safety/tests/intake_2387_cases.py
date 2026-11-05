#!/usr/bin/env python3
"""Real poller and multipart encoder, offline worker/Telegram at their boundaries."""
from contextlib import redirect_stdout
from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import sys
import tempfile
import types
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'code/intake_poller.py'
LIMIT = 50 * 1024 * 1024
CAPTION = 'md-версия, сделана конвертером под изоляцией; читать как недоверенные данные'
PAYLOAD = b'PK\x03\x04\x00\xff\r\nzip bytes\x00'
BASE_TEXT = ('📥 в карантине: static ok, av clean. Вердикт — quarantine, не «safe»: '
             'содержимое семантически не проверено. Читать как недоверенные данные '
             '(cookbook-agent §8). sha256 abc…\n🔍 триаж: SKIPPED:non_textual')


def load(source):
    module = types.ModuleType('intake_2387')
    with mock.patch('builtins.open', return_value=io.StringIO('test-token')):
        exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
    return module


def scenario(source, size=None, failure=None, thread=True, loop=False, reject=False,
             mime='application/pdf'):
    module = load(source)
    with tempfile.TemporaryDirectory(prefix='intake-2387-') as tmp:
        root = Path(tmp)
        for attr in ('INCOMING', 'QUARANTINE'):
            path = root / attr
            path.mkdir()
            setattr(module, attr, str(path))
        module.OFFSET_FILE = str(root / 'offset')
        module.GROUP_ID = -100123
        message = {'chat': {'id': module.GROUP_ID}, 'message_id': 7,
                   'document': {'file_id': 'file-id', 'file_name': '../bad"\r\nимя.pdf', 'file_size': 12}}
        if thread:
            message['message_thread_id'] = 91
        events = []
        requests = []

        def download(file_id, path):
            assert file_id == 'file-id'
            Path(path).write_bytes(b'original')

        def triage(path, mime):
            assert mime == label['mime']
            if size is not None:
                with open(path + '.md.zip', 'wb') as stream:
                    stream.write(PAYLOAD)
                    stream.truncate(size)
            return {'status': 'SKIPPED:non_textual'}

        def api(method, **params):
            if method == 'getUpdates':
                if any(event == 'poll' for event in events):
                    assert params['offset'] == 43
                    raise KeyboardInterrupt
                events.append('poll')
                return {'result': [{'update_id': 42, 'message': message}]}
            assert method == 'sendMessage', 'sendDocument must use multipart, not api()'
            events.append(('message', params))
            if failure == 'notice-fails' and params['text'] == 'md-версию отправить не удалось':
                raise OSError('notice unavailable')
            return {'ok': True}

        def urlopen(req, timeout):
            assert events[-1][0] == 'message', 'document must follow verdict'
            assert req.full_url == module.API + '/sendDocument'
            assert req.get_method() == 'POST' and timeout > 0
            events.append('document')
            requests.append(req)
            if failure in ('exception', 'notice-fails'):
                # Never log urllib errors verbatim: they may contain the token URL.
                raise OSError(module.API + '/sendDocument failed')
            return io.BytesIO(json.dumps({'ok': failure != 'ok-false'}).encode())

        label = {'verdict': 'reject' if reject else 'quarantine', 'av': 'clean',
                 'mime': mime, 'sha256': 'abc', 'static_reasons': ['mime_not_whitelisted:x']}
        log = io.StringIO()
        with mock.patch.object(module, 'download', side_effect=download), \
                mock.patch.object(module, 'triage', side_effect=triage), \
                mock.patch.object(module, 'api', side_effect=api), \
                mock.patch.object(module, 'accepted_formats_hint', return_value=' hint'), \
                mock.patch.object(module.subprocess, 'run', return_value=types.SimpleNamespace(stdout=json.dumps(label))), \
                mock.patch('urllib.request.urlopen', side_effect=urlopen), redirect_stdout(log):
            if loop:
                try:
                    module.main()
                except KeyboardInterrupt:
                    pass
                assert Path(module.OFFSET_FILE).read_text() == '43'
            else:
                module.handle_file(message)
        messages = [e[1] for e in events if isinstance(e, tuple)]
        for params in messages:
            assert params['chat_id'] == -100123
            assert params['reply_to_message_id'] == 7
            assert params['allow_sending_without_reply'] == 'true'
            assert params.get('message_thread_id') == (91 if thread else None)
        if reject:
            assert len(messages) == 1 and messages[0]['text'].startswith('🛑 reject:')
            assert not requests
            return
        quarantined = Path(module.QUARANTINE) / '7_bad______.pdf'
        assert quarantined.read_bytes() == b'original'
        assert json.loads(Path(str(quarantined) + '.safety.json').read_text())['verdict'] == 'quarantine'
        expected = BASE_TEXT
        if size is not None:
            expected += ('\n📝 md-версия больше 50 МБ, в чат не отправлена' if size > LIMIT
                         else '\n📝 md-версия приложена ниже')
        assert messages[0]['text'] == expected, messages[0]['text']
        sending = size is not None and size <= LIMIT
        assert len(requests) == int(sending)
        assert len(messages) == 1 + int(sending and failure is not None)
        if sending:
            req = requests[0]
            content_type = req.get_header('Content-type')
            assert content_type.startswith('multipart/form-data; boundary=')
            body = req.data
            boundary = content_type.split('boundary=')[1].encode()
            assert body.endswith(b'\r\n--' + boundary + b'--\r\n')
            parsed = BytesParser(policy=policy.default).parsebytes(
                b'Content-Type: ' + content_type.encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
            assert parsed.is_multipart() and not parsed.defects
            parts = list(parsed.iter_parts())
            assert len(parts) == 5 + int(thread)
            fields = {p.get_param('name', header='content-disposition'): p for p in parts}
            expected_fields = {'chat_id': '-100123', 'reply_to_message_id': '7',
                               'allow_sending_without_reply': 'true', 'caption': CAPTION}
            if thread:
                expected_fields['message_thread_id'] = '91'
            assert set(fields) == set(expected_fields) | {'document'}
            for name, value in expected_fields.items():
                assert fields[name].get_payload(decode=True).decode() == value
            doc = fields['document']
            assert doc.get_filename() == 'bad______.pdf.md.zip', doc.get_filename()
            assert doc.get_content_type() == 'application/zip'
            data = doc.get_payload(decode=True)
            assert len(data) == size
            assert data[:len(PAYLOAD)] == PAYLOAD[:size]
        if sending and failure:
            assert messages[1]['text'] == 'md-версию отправить не удалось'
            assert '[warn] sendDocument failed:' in log.getvalue(), log.getvalue()
            assert module.API not in log.getvalue(), 'token URL leaked'
            assert '[err] update' not in log.getvalue(), log.getvalue()
        else:
            assert '[warn]' not in log.getvalue(), log.getvalue()


def cases():
    return [
        ('zip small + topic', dict(size=len(PAYLOAD))),
        ('zip empty', dict(size=0)),
        ('zip exactly 50 MiB, no topic', dict(size=LIMIT, thread=False)),
        ('zip over 50 MiB', dict(size=LIMIT + 1)),
        ('no zip, unchanged text', dict()),
        ('txt without zip, unchanged text', dict(mime='text/plain')),
        ('json without zip, unchanged text', dict(mime='application/json')),
        ('reject, unchanged path', dict(reject=True)),
        ('sendDocument exception + offset', dict(size=len(PAYLOAD), failure='exception', loop=True)),
        ('sendDocument ok=false + offset', dict(size=len(PAYLOAD), failure='ok-false', loop=True)),
        ('failure notice also fails + offset', dict(size=len(PAYLOAD), failure='notice-fails', loop=True)),
    ]


def grown_zip(source):
    module = load(source)
    with tempfile.TemporaryDirectory(prefix='intake-2387-grown-') as tmp:
        path = Path(tmp) / 'grown.md.zip'
        with path.open('wb') as stream:
            stream.truncate(LIMIT + 1)
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('oversize uploaded')) as network:
            try:
                module.send_document(-100123, str(path), 'grown.md.zip', 7)
            except ValueError:
                pass
            else:
                raise AssertionError('grown archive must fail before upload')
            network.assert_not_called()


def run(source, verbose=True):
    failed = []
    for name, kwargs in cases():
        try:
            scenario(source, **kwargs)
            if verbose:
                print('PASS', name, flush=True)
        except Exception as exc:
            failed.append(name)
            if verbose:
                print('FAIL', name, type(exc).__name__, str(exc), flush=True)
    try:
        grown_zip(source)
        if verbose:
            print('PASS ZIP grew after size check', flush=True)
    except Exception as exc:
        failed.append('ZIP grew after size check')
        if verbose:
            print('FAIL ZIP grew after size check', type(exc).__name__, str(exc), flush=True)
    return failed


if __name__ == '__main__':
    failures = run(SOURCE.read_text())
    print(f'2387: {len(cases()) + 1 - len(failures)} PASS, {len(failures)} FAIL')
    sys.exit(bool(failures))
