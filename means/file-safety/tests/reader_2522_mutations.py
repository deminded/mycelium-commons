#!/usr/bin/env python3
"""In-memory mutations of the reader's budget and of the recorded reason; every one must be killed."""
import sys

from reader_2522_cases import SOURCE, run


def mutations(source):
    replacements = [
        ('budget back to 500', 'MAX_TOKENS = 3000', 'MAX_TOKENS = 500'),
        ('budget constant not used', '"max_tokens": MAX_TOKENS,', '"max_tokens": 500,'),
        ('missing content crashes', 'choice["message"].get("content")', 'choice["message"]["content"]'),
        ('reason not recorded', 'if result["flags"] == ["reader_output_invalid"]:', 'if False:'),
        ('reason recorded for good answers', 'if result["flags"] == ["reader_output_invalid"]:', 'if True:'),
        ('reason not cleaned', 're.sub(r"[^a-z_]", "", str(finish).lower())[:24]', 'str(finish)'),
        ('reason not cut', 'str(finish).lower())[:24]', 'str(finish).lower())'),
        ('reason always length', 'choice.get("finish_reason")', '"length"'),
    ]
    for name, old, new in replacements:
        assert source.count(old) == 1, (old, source.count(old))
        yield name, source.replace(old, new, 1)


def main():
    source = SOURCE.read_text()
    assert not run(source, verbose=False), 'baseline must pass before mutations'
    killed = survived = 0
    for name, mutant in mutations(source):
        compile(mutant, '<2522-mutant>', 'exec')
        failures = run(mutant, verbose=False)
        if failures:
            killed += 1
            print(f'KILLED {name}: {", ".join(failures)}', flush=True)
        else:
            survived += 1
            print('SURVIVED', name, flush=True)
    print(f'mutations 2522: {killed} KILLED, {survived} SURVIVED')
    return bool(survived)


if __name__ == '__main__':
    sys.exit(main())
