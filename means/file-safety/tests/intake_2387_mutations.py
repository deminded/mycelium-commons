#!/usr/bin/env python3
"""Deterministic, in-memory mutations of every new poller decision and contract."""
import sys

from intake_2387_cases import SOURCE, run


def replace_once(source, old, new):
    assert source.count(old) == 1, (old, source.count(old))
    return source.replace(old, new, 1)


def mutations(source):
    replacements = [
        ('ZIP absent branch disabled', 'if os.path.exists(archive):', 'if True:'),
        ('ZIP present branch disabled', 'if os.path.exists(archive):', 'if False:'),
        ('50 MiB boundary exclusive', 'os.path.getsize(archive) <= BOT_API_UPLOAD_LIMIT',
         'os.path.getsize(archive) < BOT_API_UPLOAD_LIMIT'),
        ('oversize accepted', 'os.path.getsize(archive) <= BOT_API_UPLOAD_LIMIT', 'True'),
        ('size condition inverted', 'os.path.getsize(archive) <= BOT_API_UPLOAD_LIMIT',
         'os.path.getsize(archive) > BOT_API_UPLOAD_LIMIT'),
        ('send flag dropped', 'send_md = True', 'send_md = False'),
        ('upload branch disabled', 'if send_md or md_error is not None:', 'if False:'),
        ('upload unconditionally', 'if send_md or md_error is not None:', 'if True:'),
        ('stat errors escape', 'except Exception as exc:\n            # Defer attachment failures',
         'except ValueError as exc:\n            # Defer attachment failures'),
        ('stat errors swallowed', 'md_error = exc', 'md_error = None'),
        ('stat errors raised before verdict', 'md_error = exc', 'raise'),
        ('stat failure handler skipped', 'if send_md or md_error is not None:', 'if send_md:'),
        ('deferred error guard inverted', 'if md_error is not None:', 'if md_error is None:'),
        ('deferred stat failure ignored', 'raise md_error', 'pass'),
        ('wrong small-ZIP verdict', '📝 md-версия приложена ниже', '📝 рядом с файлом — .md (zip)'),
        ('wrong oversize verdict', '📝 md-версия больше 50 МБ, в чат не отправлена', '📝 md-версия приложена ниже'),
        ('unsafe attachment name', 'safe_name(name) + ".md.zip"', 'name + ".md.zip"'),
        ('wrong attachment suffix', 'safe_name(name) + ".md.zip"', 'safe_name(name) + ".zip"'),
        ('original uploaded', 'send_document(chat_id, archive,', 'send_document(chat_id, qdest,'),
        ('ok=false ignored', 'if not result.get("ok"):', 'if False:'),
        ('sendDocument exceptions escape', 'except Exception as exc:\n                # urllib errors',
         'except ValueError as exc:\n                # urllib errors'),
        ('warning omitted', 'print(f"[warn] sendDocument failed: {type(exc).__name__}", flush=True)', 'pass'),
        ('token URL logged', 'print(f"[warn] sendDocument failed: {type(exc).__name__}", flush=True)',
         'print(f"[warn] sendDocument failed: {exc}", flush=True)'),
        ('failure notice changed', '"md-версию отправить не удалось"', '"upload failed"'),
        ('failure notice omitted', 'reply(chat_id, "md-версию отправить не удалось",\n'
         '                      reply_to=msg_id, thread_id=thread_id)', 'pass'),
        ('failure notice reply lost', 'reply(chat_id, "md-версию отправить не удалось",\n'
         '                      reply_to=msg_id, thread_id=thread_id)',
         'reply(chat_id, "md-версию отправить не удалось")'),
        ('caption changed', 'md-версия, сделана конвертером под изоляцией; читать как недоверенные данные',
         'md-версия'),
        ('limit too small', 'BOT_API_UPLOAD_LIMIT = 50 * 1024 * 1024', 'BOT_API_UPLOAD_LIMIT = 49 * 1024 * 1024'),
        ('limit too large', 'BOT_API_UPLOAD_LIMIT = 50 * 1024 * 1024', 'BOT_API_UPLOAD_LIMIT = 51 * 1024 * 1024'),
    ]
    for name, old, new in replacements:
        yield name, replace_once(source, old, new)

    start = source.index('def send_document(')
    end = source.index('\ndef reply(', start)
    helper = source[start:end]
    helper_replacements = [
        ('topic omitted', 'if thread_id:', 'if False:'),
        ('topic always present', 'if thread_id:', 'if True:'),
        ('chat wrong', '"chat_id": chat_id', '"chat_id": 0'),
        ('reply wrong', '"reply_to_message_id": reply_to', '"reply_to_message_id": 0'),
        ('allow without reply restored', '"caption": MD_ZIP_CAPTION',
         '"allow_sending_without_reply": "true", "caption": MD_ZIP_CAPTION'),
        ('ZIP growth guard removed', 'if len(data) > BOT_API_UPLOAD_LIMIT:', 'if False:'),
        ('ZIP growth guard exclusive boundary', 'if len(data) > BOT_API_UPLOAD_LIMIT:',
         'if len(data) >= BOT_API_UPLOAD_LIMIT:'),
        ('ZIP growth silently truncated', 'stream.read(BOT_API_UPLOAD_LIMIT + 1)', 'stream.read(BOT_API_UPLOAD_LIMIT)'),
        ('wrong endpoint', '{API}/sendDocument', '{API}/sendMessage'),
        ('urlencoded content type', 'multipart/form-data; boundary={boundary}', 'application/x-www-form-urlencoded'),
        ('wrong document field', 'name="document"', 'name="file"'),
        ('wrong ZIP content type', 'Content-Type: application/zip', 'Content-Type: text/plain'),
        ('no file payload', 'parts.extend((data,', 'parts.extend((b"",'),
        ('multipart footer broken', 'f"\\r\\n--{boundary}--\\r\\n"', 'f"\\r\\n--wrong--\\r\\n"'),
        ('response ignored', 'return json.load(response)', 'return {"ok": True}'),
    ]
    for name, old, new in helper_replacements:
        yield name, source[:start] + replace_once(helper, old, new) + source[end:]

    # A valid encoder sent before the verdict must also be detected.
    start = source.index('        if send_md or md_error is not None:\n')
    end = source.index('\n\ndef main():', start)
    block = source[start:end]
    without = source[:start] + source[end:]
    anchor = '        reply(chat_id,\n              f"📥 в карантине:'
    assert without.count(anchor) == 1
    yield 'document sent before verdict', without.replace(anchor, block + '\n' + anchor, 1)


def main():
    source = SOURCE.read_text()
    assert not run(source, verbose=False), 'baseline must pass before mutations'
    killed = survived = 0
    for name, mutant in mutations(source):
        compile(mutant, '<2387-mutant>', 'exec')
        failures = run(mutant, verbose=False)
        if failures:
            killed += 1
            print(f'KILLED {name}: {", ".join(failures)}', flush=True)
        else:
            survived += 1
            print('SURVIVED', name, flush=True)
    print(f'mutations 2387: {killed} KILLED, {survived} SURVIVED')
    return bool(survived)


if __name__ == '__main__':
    sys.exit(main())
