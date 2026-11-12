#!/usr/bin/env bash
set -eu
TEST_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CODE_DIR=${1:-${SECRET_SCAN_CODE_DIR:-"$TEST_DIR/../code"}}
MUTATION_TMP=$(mktemp -d)
trap 'rm -rf -- "$MUTATION_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export SECRET_SCAN_ROOTS="$MUTATION_TMP/secrets"
export SECRET_SCAN_EXTRA_GLOBS=""
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$SECRET_SCAN_ROOTS"
python3 - "$CODE_DIR" "$TEST_DIR/probe_planted.sh" "$MUTATION_TMP" <<'PY'
import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

source = Path(sys.argv[1]).resolve()
probe = Path(sys.argv[2]).resolve()
work = Path(sys.argv[3])
passed = failed = 0


def probe_run(code):
    return subprocess.run(["bash", str(probe), str(code)], capture_output=True, text=True,
                          env=os.environ.copy(), timeout=120)


def report(ok, name):
    global passed, failed
    passed += int(ok)
    failed += int(not ok)
    print(("OK " if ok else "FAIL ") + name)


def valid_probe_output(result):
    summary = re.search(r"^=== OK (\d+), FAIL (\d+) ===$", result.stdout, re.M)
    return summary and int(summary[1]) + int(summary[2]) == 53 and "FAIL probe infrastructure" not in result.stdout


try:
    baseline = probe_run(source)
    baseline_ok = baseline.returncode == 0 and valid_probe_output(baseline)
    report(baseline_ok, "baseline probe passes")
    mutations = ("scanner always clean", "hook always passes", "strict ignores empty inventory",
                 "git skips merge diffs", "git trusts stale remote refs",
                 "strict ignores partial blindness", "JSON keys returned verbatim",
                 "git enables textconv", "directory symlinks skipped",
                 "git parses additions by prefix", "numeric leaves skipped",
                 "guard splits single string", "distinct value labels merged")
    for index, name in enumerate(mutations):
        target = work / ("mutation-%d" % index)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        filename = ("hook_pretooluse.py" if index == 1 else
                    "git_pre_push.py" if index in (3, 4, 7, 9) else
                    "guard.py" if index == 11 else "secret_scan.py")
        path = target / filename
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        if index == 0:
            lines = text.splitlines(keepends=True)
            functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name in {"scan_texts", "scan_file"}]
            if len(functions) != 2:
                raise RuntimeError("Mutation anchor missing")
            for node in sorted(functions, key=lambda node: node.lineno, reverse=True):
                lines.insert(node.body[0].lineno - 1, "    return []\n")
            text = "".join(lines)
        elif index == 1:
            text = "import sys\nsys.exit(0)\n"
        else:
            edits = {
                2: ("if strict and not out:", "if False:"),
                3: ('"-p", "-m",', '"-p",'),
                4: ('rng = [local_sha]', 'rng = [local_sha, "--not", "--remotes=origin"]'),
                5: ("if strict and problems:", "if False:"),
                6: ("labels, safe_files = _safe_labels(entries, file_parts)",
                    "labels, safe_files = _safe_labels(entries, file_parts)\n"
                    "    labels = [\"\".join(sep + segment for sep, segment, _, _ in parts).lstrip(\"/\")\n"
                    "              if any(kind == \"file\" and segment.endswith(\".json\")\n"
                    "                     for _, segment, kind, _ in parts) else label\n"
                    "              for label, (parts, _) in zip(labels, entries)]"),
                7: ('"--no-textconv", ', ''),
                8: ('elif stat.S_ISDIR(mode):',
                    'elif stat.S_ISDIR(mode) and not path.is_symlink():'),
                9: ('elif in_hunk and line.startswith("+"):',
                    'elif line.startswith("+") and not line.startswith("++"):'),
                10: ('    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):\n'
                     '        yield str(obj)\n', ''),
                11: ('texts = (texts,) if isinstance(texts, str) else tuple(texts)',
                     'texts = tuple(texts)'),
                12: ('labels, safe_files = _safe_labels(entries, file_parts)',
                     'labels, safe_files = _safe_labels(entries, file_parts)\n'
                     '    labels = [label.removesuffix("#2") for label in labels]'),
            }
            anchor, replacement = edits[index]
            if text.count(anchor) != 1:
                raise RuntimeError("Mutation anchor missing")
            text = text.replace(anchor, replacement)
        # A syntax error must not count as evidence that a behavioral mutation was noticed.
        compile(text, str(path), "exec")
        path.write_text(text, encoding="utf-8")
        result = probe_run(target)
        report(bool(baseline_ok and result.returncode != 0 and valid_probe_output(result)),
               name + " detected")
except Exception:
    report(False, "mutation infrastructure")

print("=== OK %d, FAIL %d ===" % (passed, failed))
sys.exit(1 if failed else 0)
PY
