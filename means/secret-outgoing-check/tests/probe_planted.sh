#!/usr/bin/env bash
set -eu
TEST_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CODE_DIR=${1:-${SECRET_SCAN_CODE_DIR:-"$TEST_DIR/../code"}}
PROBE_TMP=$(mktemp -d)
trap 'rm -rf -- "$PROBE_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export SECRET_SCAN_ROOTS="$PROBE_TMP/secrets"
export SECRET_SCAN_EXTRA_GLOBS=""
export PYTHONDONTWRITEBYTECODE=1
python3 - "$CODE_DIR" "$PROBE_TMP" <<'PY'
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

code = Path(sys.argv[1]).resolve()
work = Path(sys.argv[2])
root = work / "secrets"
root.mkdir()
empty = work / "empty"
empty.mkdir()
value = "FAKE" + secrets.token_hex(20)
(root / "planted.env").write_text("API_KEY=" + value + "\n", encoding="utf-8")
label = "secrets/planted.env:API_KEY"
audit = "secret-outgoing-check: intentional handout: " + label
base = os.environ.copy()
for key in list(base):
    if key.startswith("GIT_") or key in {"SECRET_SCAN_ALLOW", "SECRET_SCAN_PUSH_ALLOW", "SECRET_SCAN_DIR"}:
        base.pop(key)
base.update(SECRET_SCAN_ROOTS=str(root), SECRET_SCAN_EXTRA_GLOBS="",
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="0",
            GIT_TERMINAL_PROMPT="0", PYTHONDONTWRITEBYTECODE="1")
passed = failed = 0
hidden_values = [value]


def run(args, *, data=None, env=None, cwd=None):
    return subprocess.run(args, input=data, capture_output=True, text=True, env=env or base,
                          cwd=cwd or work, timeout=30)


def record(name, result, expected, marker=None):
    global passed, failed
    ok = (result.returncode == expected and not any(
          secret in result.stdout + result.stderr for secret in hidden_values)
          and (marker is None or marker in result.stdout + result.stderr))
    passed += int(ok)
    failed += int(not ok)
    print(("OK " if ok else "FAIL ") + name)


runner = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import guard, secret_scan
payload = json.load(sys.stdin)
try:
    guard.assert_clean(**payload)
except guard.OutgoingSecretError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(2)
except secret_scan.DetectorBlind as exc:
    print("DetectorBlind: " + str(exc), file=sys.stderr)
    sys.exit(3)
'''


def guarded(payload, env=None):
    return run([sys.executable, "-I", "-B", "-c", runner, str(code)], data=json.dumps(payload), env=env)


def hooked(payload, *, tool="mcp__example__reply", env=None):
    return run([sys.executable, "-I", "-B", str(code / "hook_pretooluse.py")],
               data=json.dumps({"tool_name": tool, "tool_input": payload}), env=env)


try:
    record("guard secret refused", guarded({"texts": [value]}), 2, label)
    record("guard clean passed", guarded({"texts": ["ordinary text"]}), 0)
    record("guard explicit allow passed and audited",
           guarded({"texts": [value], "allow": [label]}), 0, audit)
    blind_env = dict(base, SECRET_SCAN_ROOTS=str(empty))
    record("guard empty inventory refused", guarded({"texts": ["ordinary text"]}, blind_env),
           3, "DetectorBlind")

    record("hook secret refused", hooked({"text": value}), 2, label)
    record("hook clean passed", hooked({"text": "ordinary text"}), 0)
    record("hook explicit allow passed and audited",
           hooked({"text": value}, env=dict(base, SECRET_SCAN_ALLOW=label)), 0, audit)
    record("hook empty inventory refused", hooked({"text": "ordinary text"}, env=blind_env),
           2, "No secret values found")

    bad_file = work / "payload.txt"
    bad_file.write_text(value, encoding="utf-8")
    clean_file = work / "clean.txt"
    clean_file.write_text("ordinary text", encoding="utf-8")
    record("guard file secret refused", guarded({"files": [str(bad_file)]}), 2, label)
    record("guard clean file passed", guarded({"files": [str(clean_file)]}), 0)
    record("guard file allow passed and audited",
           guarded({"files": [str(bad_file)], "allow": [label]}), 0, audit)
    record("guard absent file refused", guarded({"files": [str(work / "absent")]}),
           2, "attachment-unreadable")
    record("hook Artifact secret refused", hooked({"file_path": str(bad_file)}, tool="Artifact"),
           2, label)
    record("hook Artifact clean passed", hooked({"file_path": str(clean_file)}, tool="Artifact"), 0)
    record("hook Artifact allow passed and audited",
           hooked({"file_path": str(bad_file)}, tool="Artifact", env=dict(base, SECRET_SCAN_ALLOW=label)),
           0, audit)
    record("hook Artifact absent file refused",
           hooked({"file_path": str(work / "absent")}, tool="Artifact"), 2, "attachment-unreadable")
    record("hook Bash blind warning permits repair",
           hooked({"command": "true"}, tool="Bash", env=blind_env), 0, "not checked")
    record("hook missing detector refused", hooked({"text": "ordinary text"},
           env=dict(base, SECRET_SCAN_DIR=str(empty))), 2, "unavailable")
    record("hook Bash missing detector permits repair", hooked({"command": "true"}, tool="Bash",
           env=dict(base, SECRET_SCAN_DIR=str(empty))), 0, "not checked")

    repo = work / "repo"
    remote = work / "remote.git"
    template = work / "template"
    template.mkdir()

    def must(args, cwd=None):
        result = run(args, cwd=cwd)
        if result.returncode:
            raise RuntimeError("Local git setup failed")
        return result

    must(["git", "init", "--bare", "--template=" + str(template), str(remote)])
    must(["git", "init", "--initial-branch=main", "--template=" + str(template), str(repo)])
    for key, val in {"user.email": "probe@example.invalid", "user.name": "Probe",
                     "commit.gpgsign": "false", "core.autocrlf": "false"}.items():
        must(["git", "config", key, val], repo)
    must(["git", "remote", "add", "origin", str(remote)], repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    # A relative hook target keeps the probe portable even when paths contain spaces.
    hook = hooks / "pre-push"
    hook.write_text('#!/bin/sh\nexec python3 -B "$(dirname "$0")/git_pre_push.py" "$@"\n', encoding="utf-8")
    hook.chmod(0o755)
    for source in code.glob("*.py"):
        shutil.copy2(source, hooks / source.name)

    def commit(contents, message):
        (repo / "payload.txt").write_text(contents, encoding="utf-8")
        must(["git", "add", "payload.txt"], repo)
        must(["git", "commit", "-qm", message], repo)

    def push(branch="main", env=None):
        return run(["git", "push", "--quiet", "origin", "HEAD:" + branch], cwd=repo, env=env)

    commit("ordinary text\n", "clean")
    record("pre-push clean new branch passed", push(), 0)
    commit("key=" + value + "\n", "synthetic payload")
    record("pre-push secret update refused", push(), 1, label)
    record("pre-push secret new branch refused", push("other"), 1, label)
    record("pre-push explicit allow passed and audited",
           push(env=dict(base, SECRET_SCAN_PUSH_ALLOW=label)), 0, audit)
    commit("ordinary text after handout\n", "clean after handout")
    record("pre-push clean above approved history passed", push(), 0)
    commit("another ordinary change\n", "blind detector check")
    record("pre-push empty inventory refused", push(env=blind_env), 1, "No secret values found")
    scanner = hooks / "secret_scan.py"
    scanner.rename(hooks / "secret_scan.disabled")
    record("pre-push missing detector refused", push(), 1, "unavailable")
    scanner.with_name("secret_scan.disabled").rename(scanner)
    record("pre-push clean update passed", push(), 0)
    commit(value + "\n", "synthetic intermediate payload")
    commit("ordinary text again\n", "removed from final tree")
    record("pre-push secret in intermediate commit refused", push(), 1, label)
    baseline_sha = must(["git", "rev-parse", "origin/main"], repo).stdout.strip()

    def restore():
        # A deliberately broken hook may advance the remote; keep later cases independent.
        must(["git", "--git-dir=" + str(remote), "update-ref", "refs/heads/main", baseline_sha])
        must(["git", "update-ref", "refs/remotes/origin/main", baseline_sha], repo)
        must(["git", "reset", "--hard", baseline_sha], repo)

    record("guard single text refused", guarded({"texts": value}), 2, label)
    numeric_runner = runner.replace("guard.assert_clean(**payload)",
                                   "guard.assert_clean(texts=secret_scan.strings_in(payload))")
    number = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
    hidden_values.append(number)
    (root / "pin.env").write_text("PIN_CODE=" + number)
    record("hook numeric parameter refused", hooked({"pin": int(number)}), 2, "pin.env:PIN_CODE")
    record("guard numeric leaves refused", run([sys.executable, "-I", "-B", "-c",
           numeric_runner, str(code)], data=json.dumps({"pin": int(number)})), 2, "pin.env:PIN_CODE")
    decimal = str(secrets.randbelow(8_000_000_000_000_000) + 10_000_000_000_000_000)
    hidden_values.append(decimal)
    (root / "decimal.env").write_text("PIN_CODE=" + decimal)
    record("hook decimal spelling refused", run([sys.executable, "-I", "-B",
           str(code / "hook_pretooluse.py")], data='{"tool_name":"Artifact","tool_input":{"pin":' +
           decimal + '.125}}'), 2, "decimal.env:PIN_CODE")

    outside = work / "external"
    outside.mkdir()
    linked_value = "FAKE" + secrets.token_hex(16)
    hidden_values.append(linked_value)
    (outside / "token.env").write_text("API_KEY=" + linked_value)
    link = root / "linked-service"
    link.symlink_to(outside, target_is_directory=True)
    (outside / "cycle").symlink_to(root, target_is_directory=True)
    linked_label = "secrets/linked-service/token.env:API_KEY"
    record("guard directory symlink secret refused", guarded({"texts": linked_value}), 2, linked_label)
    record("hook directory symlink secret refused", hooked({"text": linked_value}), 2, linked_label)
    record("inventory directory symlink included without cycle", run([sys.executable, "-I", "-B",
           str(code / "secret_scan.py"), "--inventory"]), 0, linked_label)
    restore()
    commit(linked_value + "\n", "linked store payload")
    record("pre-push directory symlink secret refused", push(), 1, linked_label)
    restore()
    broken = root / "broken-link"
    broken.symlink_to(work / "absent-directory", target_is_directory=True)
    record("guard broken symlink refused", guarded({"texts": "ordinary"}), 3, "broken-link")
    broken.unlink()

    restore()
    commit("++" + value + "\n", "plus prefix")
    record("pre-push plus-plus body refused", push(), 1, label)
    restore()

    must(["git", "config", "diff.summary.textconv", "wc -l"], repo)
    (repo / ".gitattributes").write_text("*.txt diff=summary\n")
    must(["git", "add", ".gitattributes"], repo)
    must(["git", "commit", "-qm", "existing summary driver"], repo)
    commit(value + "\n", "original text payload")
    record("pre-push configured textconv cannot hide contents", push(), 1, label)
    restore()

    must(["git", "checkout", "-qb", "side"], repo)
    (repo / "side.txt").write_text("ordinary side\n")
    must(["git", "add", "side.txt"], repo)
    must(["git", "commit", "-qm", "side"], repo)
    must(["git", "checkout", "-q", "main"], repo)
    (repo / "main.txt").write_text("ordinary main\n")
    must(["git", "add", "main.txt"], repo)
    must(["git", "commit", "-qm", "main"], repo)
    must(["git", "merge", "--no-ff", "--no-commit", "side"], repo)
    commit(value + "\n", "merge payload")
    record("pre-push merge-only secret refused", push(), 1, label)
    restore()

    denied = root / (value + ".env")
    denied.write_text("API_KEY=" + "FAKE" + secrets.token_hex(16))
    denied.chmod(0)
    try:
        try:
            denied.read_text()
        except PermissionError:
            pass
        else:
            raise RuntimeError("Permission fixture ineffective")
        record("guard partially unreadable store refused", guarded({"texts": ["ordinary"]}),
               3, "<file#")
        record("hook partially unreadable store refused", hooked({"text": "ordinary"}),
               2, "<file#")
        record("hook Bash problem path masked", hooked({"command": "true"}, tool="Bash"),
               0, "<file#")
        commit("ordinary pending change\n", "partial blindness")
        record("pre-push partially unreadable store refused", push(), 1, "<file#")
    finally:
        denied.chmod(0o600)
        denied.unlink()
    restore()

    second = "FAKE" + secrets.token_hex(16)
    hidden_values.append(second)
    (root / "tokens.json").write_text(json.dumps({value: second}))
    safe_label = "secrets/tokens.json:<key#1>"
    record("guard token key never printed", guarded({"texts": [second]}), 2, safe_label)
    record("hook token key never printed", hooked({"text": second}), 2, safe_label)
    diagnostic = "import sys; sys.path.insert(0, sys.argv[1]); import secret_scan; " \
                 "print(sorted(label for label, _ in secret_scan.load_values(strict=True)))"
    record("diagnostic token key never printed", run([sys.executable, "-I", "-B", "-c",
           diagnostic, str(code)]), 0, safe_label)
    record("inventory token key never printed", run([sys.executable, "-I", "-B",
           str(code / "secret_scan.py"), "--inventory"]), 0, safe_label)
    commit(second + "\n", "token map payload")
    record("pre-push token key never printed", push(), 1, safe_label)
    restore()

    first_account, second_account = ["FAKE" + secrets.token_hex(16) for _ in range(2)]
    hidden_values.extend([first_account, second_account])
    (root / "accounts_token.json").write_text(json.dumps({
        "a.b": first_account, "a": {"b": second_account}}))
    first_label = "secrets/accounts_token.json:a.b"
    second_label = first_label + "#2"
    record("guard colliding labels keep second value refused", guarded({
           "texts": [first_account, second_account], "allow": [first_label]}), 2, second_label)
    record("hook colliding labels keep second value refused", hooked({
           "text": first_account + " " + second_account},
           env=dict(base, SECRET_SCAN_ALLOW=first_label)), 2, second_label)

    commit(value + "\n", "approved for original destination")
    record("pre-push original destination allow passed", push(env=dict(base,
           SECRET_SCAN_PUSH_ALLOW=label)), 0, audit)
    other_remote = work / "other.git"
    must(["git", "init", "--bare", "--template=" + str(template), str(other_remote)])
    must(["git", "remote", "set-url", "origin", str(other_remote)], repo)
    record("pre-push changed destination refused", push(), 1, label)
except Exception:
    # Captured output and exception objects can contain payloads and must stay private.
    failed += 1
    print("FAIL probe infrastructure")

print("=== OK %d, FAIL %d ===" % (passed, failed))
sys.exit(1 if failed else 0)
PY
