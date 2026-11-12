#!/usr/bin/env python3
"""Attachment name shown to people: letters of any script survive, header syntax does not."""
from pathlib import Path
import sys
import tempfile
from unittest import mock

from intake_2387_cases import SOURCE, load

NAME_LIMIT = 180  # bytes of UTF-8 for the base name; the ".md.zip" suffix is added by the caller

# (title, raw name from Telegram, expected attachment base name)
NAMES = [
    ('russian name survives, space becomes underscore', 'Отчёт за ноябрь.pdf', 'Отчёт_за_ноябрь.pdf'),
    ('decomposed letters are composed', 'Отчёт.pdf', 'Отчёт.pdf'),
    ('quote and line breaks are removed, letters stay', '../bad"\r\nимя.pdf', 'bad___имя.pdf'),
    ('backslash is not a path here and is removed', 'a\\b.txt', 'a_b.txt'),
    ('directory part is dropped', 'каталог/вложение.docx', 'вложение.docx'),
    ('bidi override cannot fake the extension', 'doc‮fdp.exe', 'doc_fdp.exe'),
    ('control characters are removed', 'a\x00b\x1fc\x7f.txt', 'a_b_c_.txt'),
    ('lone surrogate does not break encoding', 'a\ud800b.txt', 'a_b.txt'),
    ('combining vowel signs of other scripts stay', 'हिंदी.pdf', 'हिंदी.pdf'),
    ('only forbidden characters is still a name', '"""', '___'),
    ('empty name', '', 'file'),
    ('missing name', None, 'file'),
]


def name_cases(module):
    for title, raw, expected in NAMES:
        got = module.attachment_name(raw)
        assert got == expected, (title, got)
    # cut by bytes, never inside a character
    long_cyr = module.attachment_name('я' * 500 + '.pdf')
    assert long_cyr == 'я' * (NAME_LIMIT // 2), len(long_cyr.encode('utf-8'))
    uneven = module.attachment_name('a' + '中' * 100)
    assert uneven == 'a' + '中' * 59, len(uneven.encode('utf-8'))
    assert len(uneven.encode('utf-8')) == 178
    # the name on disk stays strict ASCII
    assert module.safe_name('Отчёт за ноябрь.pdf') == '_' * len('Отчёт за ноябрь') + '.pdf'
    assert module.safe_name('../bad"\r\nимя.pdf') == 'bad______.pdf'


def header_cases(module):
    with tempfile.TemporaryDirectory(prefix='intake-2501-') as tmp:
        path = Path(tmp) / 'a.md.zip'
        path.write_bytes(b'PK\x03\x04zip')
        sent = []

        def urlopen(req, timeout):
            sent.append(req)
            import io
            return io.BytesIO(b'{"ok": true}')

        with mock.patch('urllib.request.urlopen', side_effect=urlopen):
            module.send_document(-100123, str(path), 'Отчёт_за_ноябрь.pdf.md.zip', 7)
        assert len(sent) == 1
        wanted = 'name="document"; filename="Отчёт_за_ноябрь.pdf.md.zip"\r\n'.encode('utf-8')
        assert sent[0].data.count(wanted) == 1, 'russian file name must travel as UTF-8 in the header'
        # header syntax in the name is refused before any network call
        for bad in ('a".md.zip', 'a\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n1.md.zip',
                    'a\nb.md.zip', 'a\\b.md.zip', 'a\x00b.md.zip'):
            with mock.patch('urllib.request.urlopen', side_effect=AssertionError('unsafe name uploaded')) as net:
                try:
                    module.send_document(-100123, str(path), bad, 7)
                except ValueError:
                    pass
                else:
                    raise AssertionError('unsafe attachment name must be refused: %r' % bad)
                net.assert_not_called()


def run(source, verbose=True):
    failed = []
    for title, case in (('attachment names', name_cases), ('multipart header', header_cases)):
        try:
            case(load(source))
            if verbose:
                print('PASS', title, flush=True)
        except Exception as exc:
            failed.append(title)
            if verbose:
                print('FAIL', title, type(exc).__name__, str(exc)[:200], flush=True)
    return failed


if __name__ == '__main__':
    failures = run(SOURCE.read_text())
    print(f'2501: {2 - len(failures)} PASS, {len(failures)} FAIL')
    sys.exit(bool(failures))
