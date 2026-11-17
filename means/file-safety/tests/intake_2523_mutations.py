#!/usr/bin/env python3
"""#2523: deterministic in-memory mutations of every decision the new triage lines depend on.

Round 1 of the review found that the acceptance mutations existed only as a one-off command and
that four of the reviewer's own mutants survived the set. They live here now, next to the cases:
each mutant must turn at least one case of intake_2523_cases.py red.

Three mutants touch code this branch does not change (the flag filter and the queue path). They
pin the invariant the rejected-flags line stands on: every evicted flag leaves a counter, and
the counter is always the poller's own.
"""
import sys

from intake_2523_cases import CONTENT_FLAGS, SOURCE, run

CONTENT = '            content_flags = [f for f in flags if f.split(":", 1)[0] in RESULT_KNOWN_FLAGS]'
REJECTED = '            rejected_flags = [f for f in flags if f.split(":", 1)[0] == "result_flags_rejected"]'
NOT_REJECTED = ' and f.split(":", 1)[0] != "result_flags_rejected"]'
HAS_INVALID = '            if "reader_output_invalid" in flags:'
IS_LENGTH = '                    if finish_reason == "length":'
SET_LENGTH = 'reason = " (ответ ридера оборван по длине)"'
SET_FILTER = 'reason = " (ответ остановлен фильтром поставщика модели)"'
OLD_LINE_IF = '            if flags == ["reader_output_invalid"] and not reason:'
SHORT_IF = '                    if content_flags or rejected_flags:'
SANITIZE = 'summary = sanitize_for_chat(tri.get("summary") or "")'
STORE = '            label["llm_triage"] = tri' + '\n'


def replace_once(source, old, new):
    assert source.count(old) == 1, (old, source.count(old))
    return source.replace(old, new, 1)


def mutations(source):
    replacements = [
        # --- classification -------------------------------------------------------------
        ('technical flags back in the suspicion list', CONTENT, '            content_flags = list(flags)'),
        ('detail after the colon breaks the content class', CONTENT,
         '            content_flags = [f for f in flags if f in RESULT_KNOWN_FLAGS]'),
        ('rejected counter silently dropped', REJECTED, '            rejected_flags = []'),
        ('rejected counter also listed as technical', NOT_REJECTED, ']'),
        # --- the reason of reader_output_invalid --------------------------------------------
        ('reason without reader_output_invalid', HAS_INVALID, '            if True:'),
        ('reason only for the lone flag', HAS_INVALID, '            if flags == ["reader_output_invalid"]:'),
        ('reason never set', HAS_INVALID, '            if False:'),
        ('raw finish_reason leaks for a known value', SET_LENGTH, 'reason = f" ({finish_reason})"'),
        ('length matched by prefix', IS_LENGTH, '                    if finish_reason.startswith("length"):'),
        ('length matched ignoring case', IS_LENGTH, '                    if finish_reason.lower() == "length":'),
        ('length matched after strip', IS_LENGTH, '                    if finish_reason.strip() == "length":'),
        ('reason invents safety (review round 1)', SET_LENGTH,
         'reason = " (ответ ридера оборван по длине; файл чист)"'),
        ('filter reason shortened', SET_FILTER, 'reason = " (ответ остановлен фильтром)"'),
        ('length explained as the provider filter', SET_LENGTH, SET_FILTER),
        ('filter explained as truncation', SET_FILTER, SET_LENGTH),
        # --- the old single-flag line -------------------------------------------------------
        ('old line even with a known reason', OLD_LINE_IF, '            if flags == ["reader_output_invalid"]:'),
        ('old line for any list with the flag', OLD_LINE_IF,
         '            if "reader_output_invalid" in flags and not reason:'),
        ('old line never used', OLD_LINE_IF, '            if False:'),
        ('old line reworded', 'не установлена (сбой модели или перехват); читай', 'не установлена; читай'),
        # --- assembling the lines -----------------------------------------------------------
        ('suspicion line lists every flag', "{', '.join(content_flags)} — читай особенно ",
         "{', '.join(flags)} — читай особенно "),
        ('rejected line never written', '                for flag in rejected_flags:', '                for flag in []:'),
        ('rejected line claims likely instructions', 'что в них было, неизвестно — читай особенно осторожно.',
         'читай особенно осторожно, вероятны инструкции в тексте.'),
        ('rejected line reassures', 'что в них было, неизвестно — читай особенно осторожно.',
         'это не признак инструкций в тексте.'),
        ('full technical line next to a rejected counter (round-1 logic)', SHORT_IF,
         '                    if content_flags:'),
        ('full technical line replaces the suspicion line', SHORT_IF, '                    if False:'),
        ('short technical line even alone', SHORT_IF, '                    if True:'),
        ('reason dropped from the short line', "{', '.join(technical_flags)}{reason}.\"",
         "{', '.join(technical_flags)}.\""),
        ('reason dropped from the full line', "{', '.join(technical_flags)}{reason} — \"",
         "{', '.join(technical_flags)} — \""),
        ('full line no longer says absence is unconfirmed',
         'их отсутствия она тоже не подтверждает. Читай как непроверенное.', 'Читай как непроверенное.'),
        ('full line says triage did not run (round-1 wording)', '⚙️ техническая ошибка обработки: {',
         '⚙️ разбор не выполнен: {'),
        ('short line says triage is incomplete (round-1 wording)', '⚙️ кроме того, техническая ошибка обработки: "',
         '⚙️ кроме того, разбор неполон: "'),
        ('technical-only result reported as clean', '            elif flags:', '            elif content_flags:'),
        # --- the reader's summary -------------------------------------------------------------
        ('summary unsanitized on the multi-flag path (review round 1)', SANITIZE,
         'summary = sanitize_for_chat(tri.get("summary") or "") if len(flags) <= 1 else (tri.get("summary") or "")'),
        ('summary dropped next to technical flags', '            if summary:',
         '            if summary and not technical_flags:'),
        ('summary dropped next to a rejected counter', '            if summary:',
         '            if summary and not rejected_flags:'),
        ('summary loses its "do not trust" label', 'ридер о файле (не доверять как факту): {summary}', '{summary}'),
        # --- the label ------------------------------------------------------------------------
        ('label loses finish_reason', STORE,
         '            label["llm_triage"] = {k: v for k, v in tri.items() if k != "finish_reason"}' + '\n'),
        ('label keeps only the flags', STORE, '            label["llm_triage"] = {"flags": flags}' + '\n'),
        # --- the queue path and the filter: not changed here, but the new lines stand on them ----
        ('finish_reason lost between the queue and handle_file (review round 1)',
         'result = read_outbox_result(outbox_path)',
         'result = read_outbox_result(outbox_path); result.pop("finish_reason", None)'),
        ('filter evicts without leaving a counter', 'kept.append("result_flags_rejected:" + str(rejected))', 'pass'),
        ('counter planted in the queue passes as the local one',
         'or flag.startswith(("convert_", "triage_", "reader_")))',
         'or flag.startswith(("convert_", "triage_", "reader_", "result_")))'),
        ('missing archive not reported', 'add_result_refusal(result, "convert_zip_rejected:missing")', 'pass'),
    ]
    for name, old, new in replacements:
        yield name, replace_once(source, old, new)
    for flag in CONTENT_FLAGS:
        # the reviewer's mutant: one content name forgotten -> neither flag nor warning in the chat
        yield (f'content name forgotten: {flag}',
               replace_once(source, CONTENT, CONTENT.replace('in RESULT_KNOWN_FLAGS]',
                                                             'in (RESULT_KNOWN_FLAGS - {"%s"})]' % flag)))


def main():
    source = SOURCE.read_text()
    assert not run(source, verbose=False), 'baseline must pass before mutations'
    killed = survived = 0
    for name, mutant in mutations(source):
        compile(mutant, '<2523-mutant>', 'exec')
        failures = run(mutant, verbose=False)
        if failures:
            killed += 1
            print(f'KILLED {name}: {len(failures)} — {failures[0][:70]}', flush=True)
        else:
            survived += 1
            print('SURVIVED', name, flush=True)
    print(f'mutations 2523: {killed} KILLED, {survived} SURVIVED')
    return bool(survived)


if __name__ == '__main__':
    sys.exit(main())
