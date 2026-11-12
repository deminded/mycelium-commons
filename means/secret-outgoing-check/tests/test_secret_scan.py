"""All inventories and payloads are synthetic and confined to temporary directories."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import pytest

CODE = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE))
import guard
import git_pre_push
import secret_scan


def check(condition, message="Unexpected scanner behavior"):
    # Assertion introspection must not reveal payloads if a regression occurs.
    if not condition:
        pytest.fail(message, pytrace=False)


def fake():
    return "FAKE" + secrets.token_hex(16)


@pytest.fixture(autouse=True)
def inventory(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    (root / "sub").mkdir(parents=True)
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(root))
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", "")
    monkeypatch.delenv("SECRET_SCAN_ALLOW", raising=False)
    monkeypatch.delenv("SECRET_SCAN_PUSH_ALLOW", raising=False)
    monkeypatch.delenv("SECRET_SCAN_DIR", raising=False)
    values = dict(key=fake(), password="FAKE" + secrets.token_hex(4), raw=fake(),
                  json=fake(), plain=fake(), phrase="FAKE " + secrets.token_hex(8) + " phrase",
                  short="FAKE" + secrets.token_hex(2),
                  numeric=str(secrets.randbelow(9_000_000_000) + 1_000_000_000),
                  capurl="https://hooks.example.org/services/" + fake(), commented=fake(),
                  padded="FAKE" + secrets.token_hex(12) + "==", topjson=fake(),
                  github="ghp_" + secrets.token_hex(20))
    (root / "provider.env").write_text(
        "export API_KEY='%s'\nEMAIL_ADDR=public@example.org\n"
        "AUTH_URL=https://api.example.org/v1\nEMAIL_PASS=%s\n" %
        (values["key"], values["password"]), encoding="utf-8")
    (root / "bot.token").write_text("# comment line here\n" + values["raw"], encoding="utf-8")
    (root / "sub" / "disk.json").write_text(json.dumps({"access_token": values["json"],
                                                         "expires_in": 3600}), encoding="utf-8")
    (root / "bridge_token.txt").write_text("HOST=public.example.org\nagent: " + values["plain"],
                                           encoding="utf-8")
    (root / "misc.env").write_text(
        'BACKUP_PASSPHRASE="%s"\nDB_PASSWORD=%s\nPIN_CODE=%s\nWEBHOOK_SECRET=%s\n'
        'MAIL_PASS=%s # rotated today\nGITHUB_TOKEN=%s\n' %
        tuple(values[k] for k in ("phrase", "short", "numeric", "capurl", "commented", "github")),
        encoding="utf-8")
    (root / "misc.env.bak").write_text("GITHUB_TOKEN=" + values["github"], encoding="utf-8")
    (root / "raw.key").write_text(values["padded"], encoding="utf-8")
    (root / "plain_token.json").write_text(json.dumps(values["topjson"]), encoding="utf-8")
    return root, values


def test_values_loaded_by_name_and_shape(inventory):
    _, v = inventory
    pairs = secret_scan.load_values(strict=True)
    labels = {label for label, _ in pairs}
    check({"secrets/provider.env:API_KEY", "secrets/provider.env:EMAIL_PASS",
           "secrets/bot.token:line2", "secrets/sub/disk.json:access_token",
           "secrets/bridge_token.txt:agent"} <= labels)
    check(set(v.values()) <= {value for _, value in pairs})
    check(not {"public@example.org", "https://api.example.org/v1", "public.example.org",
               "# comment line here"} & {value for _, value in pairs})


@pytest.mark.parametrize("name", ["key", "password", "raw", "json", "plain", "phrase", "short",
                                  "numeric", "capurl", "commented", "padded", "topjson"])
def test_each_value_found_without_disclosure(inventory, name):
    value = inventory[1][name]
    hits = secret_scan.scan_texts(["payload: " + value])
    check(bool(hits))
    check(value not in secret_scan.describe(hits))


def test_soft_wrap(inventory):
    value = inventory[1]["key"]
    check(bool(secret_scan.scan_texts([value[:10] + " \n\t" + value[10:]])))


def test_ordinary_text_and_rotation_comment():
    check(secret_scan.scan_texts(["Step 2 complete, public@example.org, public.example.org, "
                                  "https://api.example.org/v1, rotated today"]) == [])


EXPECTED_FORMATS = {"openrouter", "anthropic", "openai", "github", "github_pat", "slack",
                    "aws", "google", "telegram_bot", "private_key"}


def test_format_catalog_is_complete():
    check({name for name, _ in secret_scan.FORMATS} == EXPECTED_FORMATS)


def format_payloads():
    suffix = secrets.token_hex(24)
    payloads = {"openrouter": "sk-or-v1-" + suffix, "anthropic": "sk-ant-" + suffix,
                "openai": "sk-proj-" + suffix, "github": "ghp_" + suffix,
                "github_pat": "github_pat_" + suffix, "slack": "xoxb-" + suffix,
                "aws": "AKIA" + secrets.token_hex(8).upper(),
                "google": "AIza" + suffix[:35],
                "telegram_bot": "123456789:AA" + suffix[:33],
                "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----"}
    return payloads


@pytest.mark.parametrize("kind", sorted(EXPECTED_FORMATS))
def test_formats_without_values(kind):
    check("format:" + kind in secret_scan.scan_texts([format_payloads()[kind]], values=[]))


def test_string_leaves_include_keys():
    check(list(secret_scan.strings_in({"title": ["body", {"key": "value"}], "n": 1})) ==
          ["title", "body", "key", "value", "n", "1"])


@pytest.mark.parametrize("method", ["check", "assert"])
def test_guard_refuses_and_reports_only_labels(inventory, method):
    value = inventory[1]["key"]
    if method == "check":
        check(guard.check_outgoing(texts=[value]) == ["secrets/provider.env:API_KEY"])
    else:
        with pytest.raises(guard.OutgoingSecretError) as caught:
            guard.assert_clean(texts=[value])
        check(caught.value.labels == ["secrets/provider.env:API_KEY"])
        check(value not in str(caught.value))


def test_guard_clean():
    check(guard.check_outgoing(texts=["ordinary text"]) == [])
    check(guard.assert_clean(texts=["ordinary text"]) is None)


def test_guard_wrapper_checks_before_side_effect(inventory):
    delivered = []

    def send(payload):
        guard.assert_clean(texts=secret_scan.strings_in(payload))
        delivered.append(payload)

    for payload in ({"operation": "edit", "text": inventory[1]["raw"]},
                    {"operation": "topic", "title": inventory[1]["json"]}):
        with pytest.raises(guard.OutgoingSecretError):
            send(payload)
    check(delivered == [])


@pytest.mark.parametrize("method", ["check", "assert"])
def test_guard_intentional_handout(inventory, capsys, method):
    label = "secrets/provider.env:API_KEY"
    func = guard.check_outgoing if method == "check" else guard.assert_clean
    result = func(texts=[inventory[1]["key"]], allow=[label])
    check(result == [] if method == "check" else result is None)
    check(capsys.readouterr().err == "secret-outgoing-check: intentional handout: " + label + "\n")


def test_allow_one_label_does_not_cover_another(inventory):
    hits = guard.check_outgoing(texts=[inventory[1]["key"] + " " + inventory[1]["raw"]],
                                allow=["secrets/provider.env:API_KEY"])
    check(hits == ["secrets/bot.token:line2"])


@pytest.mark.parametrize("prefix", ["", "x:"])
def test_allow_value_is_refused_without_disclosure(inventory, capsys, prefix):
    value = inventory[1]["short"]
    with pytest.raises(guard.OutgoingSecretError) as caught:
        guard.assert_clean(texts=["ordinary text"], allow=[prefix + value])
    output = capsys.readouterr()
    check(value not in str(caught.value) + output.out + output.err)


@pytest.mark.parametrize("as_file", [False, True])
def test_label_covers_format_and_backup_copy(inventory, tmp_path, as_file):
    value = inventory[1]["github"]
    label = "secrets/misc.env:GITHUB_TOKEN"
    path = tmp_path / "outgoing.txt"
    path.write_text(value, encoding="utf-8")
    kwargs = {"files": [path]} if as_file else {"texts": [value]}
    check(guard.check_outgoing(**kwargs, allow=[label]) == [])


def test_file_content_and_non_utf8_bytes(inventory, tmp_path):
    path = tmp_path / "outgoing.bin"
    path.write_bytes(b"\xff\x00" + inventory[1]["key"].encode() + b"\xfe")
    with pytest.raises(guard.OutgoingSecretError) as caught:
        guard.assert_clean(files=[path])
    check(caught.value.labels == ["secrets/provider.env:API_KEY"])


def test_missing_file_is_not_clean(tmp_path):
    check(guard.check_outgoing(files=[tmp_path / "absent"]) == ["attachment-unreadable"])


def test_attachment_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "MAX_ATTACHMENT", 32)
    path = tmp_path / "attachment"
    path.write_bytes(b"." * 32)
    check(secret_scan.scan_file(path) == [])
    path.write_bytes(b"." * 33)
    check(secret_scan.scan_file(path) == ["attachment-too-large-to-scan"])


@pytest.mark.parametrize("method", ["load", "check", "assert"])
def test_blind_detector(tmp_path, monkeypatch, method):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(root))
    check(secret_scan.load_values() == [])
    funcs = {"load": lambda: secret_scan.load_values(strict=True),
             "check": guard.check_outgoing, "assert": guard.assert_clean}
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        funcs[method]()
    check(str(root) in str(caught.value))


def test_unusable_inventory_is_blind(tmp_path, monkeypatch):
    root = tmp_path / "unusable"
    root.mkdir()
    (root / "short.env").write_text("KEY=tiny\nAUTH=false\n", encoding="utf-8")
    (root / "large.env").write_text("KEY=" + fake(), encoding="utf-8")
    monkeypatch.setattr(secret_scan, "MAX_FILE", 24)
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(root))
    with pytest.raises(secret_scan.DetectorBlind):
        secret_scan.load_values(strict=True)


def test_missing_roots_are_blind(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(tmp_path / "missing"))
    with pytest.raises(secret_scan.DetectorBlind):
        secret_scan.load_values(strict=True)


def test_multiple_file_and_directory_roots(inventory, tmp_path, monkeypatch):
    root, v = inventory
    single = tmp_path / "single.env"
    value = fake()
    single.write_text("API_TOKEN=" + value, encoding="utf-8")
    monkeypatch.setenv("SECRET_SCAN_ROOTS", os.pathsep.join([str(root), str(single)]))
    pairs = secret_scan.load_values(strict=True)
    check((tmp_path.name + "/single.env:API_TOKEN", value) in pairs)
    check(v["key"] in {val for _, val in pairs})


def test_extra_globs_are_explicit_and_relative_to_home(tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir()
    value = fake()
    (home / "client.json").write_text(json.dumps({"api_token": value}), encoding="utf-8")
    monkeypatch.setattr(secret_scan, "HOME", home)
    check(value not in {val for _, val in secret_scan.load_values(strict=True)})
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", "*.json" + os.pathsep + "*.absent")
    check(("client.json:api_token", value) in secret_scan.load_values(strict=True))


def run_hook(payload, env=None):
    return subprocess.run([sys.executable, "-I", "-B", str(CODE / "hook_pretooluse.py")],
                          input=json.dumps(payload), text=True, capture_output=True,
                          env=env or os.environ.copy(), timeout=15)


@pytest.mark.parametrize("tool", ["Bash", "WebFetch", "WebSearch", "mcp__example__reply"])
def test_hook_refuses_values_and_allows_clean(inventory, tool):
    value = inventory[1]["key"]
    result = run_hook({"tool_name": tool, "tool_input": {"text": value}})
    check(result.returncode == 2)
    check(value not in result.stdout + result.stderr)
    check("SECRET_SCAN_ALLOW=<labels>" in result.stderr)
    check(run_hook({"tool_name": tool, "tool_input": {"text": "ordinary text"}}).returncode == 0)


@pytest.mark.parametrize("tool", ["Bash", "WebFetch", "WebSearch", "mcp__example__reply"])
def test_hook_key_header_exception(tool):
    result = run_hook({"tool_name": tool, "tool_input": {
        "text": "-----BEGIN OPENSSH PRIVATE KEY-----"}})
    check(result.returncode == (0 if tool in {"Bash", "WebFetch", "WebSearch"} else 2))


def test_hook_key_header_exception_keeps_exact_value_hits(inventory):
    (inventory[0] / "header.env").write_text(
        'API_KEY="-----BEGIN OPENSSH PRIVATE KEY-----"', encoding="utf-8")
    check(run_hook({"tool_name": "Bash", "tool_input": {
        "command": "-----BEGIN OPENSSH PRIVATE KEY-----"}}).returncode == 2)


@pytest.mark.parametrize("shape", ["file_path", "file_paths", "files_from", "files_string", "missing"])
def test_hook_artifact_files(inventory, tmp_path, shape):
    path = tmp_path / "outgoing.txt"
    path.write_text(inventory[1]["key"], encoding="utf-8")
    inputs = {"file_path": {"file_path": str(path)}, "file_paths": {"file_paths": [str(path)]},
              "files_from": {"files": {"doc": {"from": str(path)}}},
              "files_string": {"files": {"doc": str(path)}},
              "missing": {"file_path": str(tmp_path / "absent")}}
    result = run_hook({"tool_name": "Artifact", "tool_input": inputs[shape]})
    check(result.returncode == 2)
    check(inventory[1]["key"] not in result.stdout + result.stderr)


@pytest.mark.parametrize("tool", ["Bash", "mcp__example__reply", "WebFetch"])
@pytest.mark.parametrize("failure", ["missing", "blind"])
def test_hook_unavailable_detector(tmp_path, tool, failure):
    env = os.environ.copy()
    empty = tmp_path / "empty"
    empty.mkdir()
    if failure == "missing":
        env["SECRET_SCAN_DIR"] = str(empty)
    else:
        env["SECRET_SCAN_ROOTS"] = str(empty)
    result = run_hook({"tool_name": tool, "tool_input": {"text": "ordinary text"}}, env)
    check(result.returncode == (0 if tool == "Bash" else 2))
    if tool == "Bash":
        output = json.loads(result.stdout)["hookSpecificOutput"]
        check(output["hookEventName"] == "PreToolUse")
        check("not checked" in output["additionalContext"])
    else:
        check("unavailable" in result.stderr)


def test_hook_intentional_handout(inventory):
    env = os.environ.copy()
    label = "secrets/provider.env:API_KEY"
    env["SECRET_SCAN_ALLOW"] = label
    result = run_hook({"tool_name": "mcp__example__reply", "tool_input": {"text": inventory[1]["key"]}}, env)
    check(result.returncode == 0)
    check(result.stderr == "secret-outgoing-check: intentional handout: " + label + "\n")


@pytest.mark.parametrize("prefix", ["", "x:"])
def test_hook_allow_rejects_values_without_disclosure(inventory, prefix):
    value = inventory[1]["short"]
    env = dict(os.environ, SECRET_SCAN_ALLOW=prefix + value)
    result = run_hook({"tool_name": "mcp__example__reply", "tool_input": {"text": "ordinary text"}}, env)
    check(result.returncode == 2)
    check(value not in result.stdout + result.stderr)


def test_hook_scans_keys_and_nested_strings(inventory):
    result = run_hook({"tool_name": "mcp__example__reply", "tool_input": {
        "nested": [{inventory[1]["key"]: "ordinary text"}]}})
    check(result.returncode == 2)


def test_inventory_read_error_is_blind(tmp_path, monkeypatch):
    path = tmp_path / "unreadable.env"
    path.write_text("API_KEY=" + fake(), encoding="utf-8")
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(path))

    def denied(*args, **kwargs):
        raise PermissionError("Synthetic read failure")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(secret_scan.DetectorBlind):
        secret_scan.load_values(strict=True)


def test_file_format_without_inventory_value(tmp_path):
    path = tmp_path / "format.txt"
    path.write_text("ghp_" + secrets.token_hex(20), encoding="utf-8")
    check(guard.check_outgoing(files=[path]) == ["format:github"])


def test_guard_allow_does_not_override_blindness(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty"
    path.mkdir()
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(path))
    with pytest.raises(secret_scan.DetectorBlind):
        guard.assert_clean(allow=["format:private_key"])
    check(capsys.readouterr().err == "")


def check_label_invariants(pairs):
    labels = {}
    values = {value for _, value in pairs}
    for label, value in pairs:
        check(not any(secret in label for secret in values), "Label disclosed a loaded value")
        check(label not in labels or labels[label] == value, "Label is ambiguous")
        labels[label] = value


def test_json_paths_arrays_and_allow_isolation(inventory):
    first, second, third = fake(), fake(), fake()
    (inventory[0] / "accounts.json").write_text(json.dumps({
        "service_a": {"token": first}, "service_b": {"token": second},
        "tokens": [third, first]}))
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    check(("secrets/accounts.json:service_a.token", first) in pairs)
    check(("secrets/accounts.json:tokens[0]", third) in pairs)
    check(guard.check_outgoing(texts=[first, second],
                              allow=["secrets/accounts.json:service_a.token"]) ==
          ["secrets/accounts.json:service_b.token"])
    check(bool(guard.check_outgoing(texts=second, allow=["secrets/accounts.json:token"])))


def test_label_masks_keys_filenames_and_global_substrings(inventory, capsys):
    root, v = inventory
    token_key, other = fake(), fake()
    (root / "tokens.json").write_text(json.dumps({v["key"]: other, token_key: other,
        "x" + v["short"] + "y": other, "nested": {"token": other}}))
    (root / (v["raw"] + ".env")).write_text("API_KEY=" + other)
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    check(("secrets/tokens.json:<key#1>", other) in pairs)
    check(("secrets/tokens.json:<key#2>", other) in pairs)
    check(("secrets/tokens.json:<key#3>", other) in pairs)
    with pytest.raises(guard.OutgoingSecretError) as caught:
        guard.assert_clean(texts=other)
    result = run_hook({"tool_name": "Artifact", "tool_input": {"text": other}})
    output = str(caught.value) + result.stdout + result.stderr + json.dumps(secret_scan.inventory())
    check(not any(value in output for _, value in pairs))


def test_json_format_key_is_masked_even_if_not_loaded(inventory):
    key = "xoxb-" + secrets.token_hex(8)
    value = fake()
    (inventory[0] / "tokens.json").write_text(json.dumps({key: value}))
    check(("secrets/tokens.json:<key#1>", value) in secret_scan.load_values(strict=True))


def test_label_collisions_repaired_deterministically(inventory):
    root, _ = inventory
    values = [fake() for _ in range(5)]
    (root / "tokens.json").write_text(json.dumps({
        "a.b": values[0], "a": {"b": values[1]},
        "FAKE" + "a" * 30: values[2], "<key#3>": values[3]}))
    (root / "repeat.env").write_text("API_KEY=%s\nAPI_KEY=%s\nAPI_KEY=%s\n\n%s\n%s\n" %
        (values[0], values[1], values[0], values[3], values[4]))
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    check(pairs == secret_scan.load_values(strict=True))
    check(("secrets/repeat.env:API_KEY#2", values[1]) in pairs)
    check(("secrets/repeat.env:line5", values[3]) in pairs)
    check(("secrets/repeat.env:line6", values[4]) in pairs)
    check(("secrets/tokens.json:a.b#2", values[1]) in pairs)


def test_same_named_file_roots_do_not_share_allow(tmp_path, monkeypatch):
    values, paths = [fake(), fake()], []
    for name, value in zip(("first", "second"), values):
        path = tmp_path / name / "token.env"
        path.parent.mkdir()
        path.write_text("API_KEY=" + value)
        paths.append(str(path))
    monkeypatch.setenv("SECRET_SCAN_ROOTS", os.pathsep.join(paths))
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    check(guard.check_outgoing(texts=values, allow=["first/token.env:API_KEY"]) ==
          ["second/token.env:API_KEY"])


def test_cross_segment_label_leak_repaired(inventory):
    root, _ = inventory
    value = "nested.token"
    (root / "tokens.json").write_text(json.dumps({"source": value,
                                                 "nested": {"token": fake()}}))
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    check(pairs == secret_scan.load_values(strict=True))


@pytest.mark.parametrize("failure", ["missing", "read", "walk", "stat"])
def test_partial_blindness_refuses_with_paths_only(inventory, monkeypatch, failure):
    root, v = inventory
    path = root / "broken.env"
    path.write_text("API_KEY=" + fake())
    if failure == "missing":
        path = root / "absent"
        monkeypatch.setenv("SECRET_SCAN_ROOTS", str(root) + os.pathsep + str(path))
    elif failure == "read":
        original = Path.read_text

        def read(self, *args, **kwargs):
            if self == path:
                raise OSError("Content must not reach diagnostics: " + v["key"])
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    elif failure == "walk":
        path = root / "sub"
        original = os.scandir

        def walk(name):
            if Path(name) == path:
                raise PermissionError("Synthetic traversal failure")
            return original(name)

        monkeypatch.setattr(os, "scandir", walk)
    else:
        original = Path.stat

        def stat(self, *args, **kwargs):
            if self == path:
                raise OSError("Synthetic stat failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    check(bool(secret_scan.load_values()))
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        secret_scan.load_values(strict=True)
    check(str(path) in str(caught.value))
    check(v["key"] not in str(caught.value))


def test_extra_match_unreadable_is_blind(inventory, tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "HOME", tmp_path)
    path = tmp_path / "extra.env"
    path.write_text("API_KEY=" + fake())
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", "*.env" + os.pathsep + "*.absent")
    original = Path.read_text

    def read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("Synthetic read failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        secret_scan.load_values(strict=True)
    check(str(path) in str(caught.value))


def test_inventory_reasons_counts_and_cli(inventory):
    root, _ = inventory
    accepted = fake()
    (root / "coverage.env").write_text(
        "# note\n\nAPI_KEY=tiny\nPIN_CODE=1234567\nAPI_KEY=false\n"
        "a long prose line\nLABEL=ignored-value\nSECRET_URL=https://example.invalid/x\n"
        "API_KEY=" + accepted + "\n" + "123456789012345\n")
    (root / "coverage_token.json").write_text(json.dumps({"a": "short", "b": 1,
                                                         "HOST": fake(), "c": accepted}))
    report = secret_scan.inventory()
    entry = next(row for row in report if row["file"] == "secrets/coverage.env")
    check(entry["taken"] == 1)
    check(entry["skipped"] == {"comment": 1, "blank": 1, "short": 1, "short-number": 2,
                               "literal": 1, "space": 1, "nonsecret-name": 1,
                               "plain-address": 1})
    leaf = next(row for row in report if row["file"] == "secrets/coverage_token.json")
    check(leaf["taken"] == 1 and leaf["skipped"] ==
          {"short": 1, "non-string": 1, "nonsecret-name": 1})
    result = subprocess.run([sys.executable, "-B", str(CODE / "secret_scan.py"), "--inventory"],
                            capture_output=True, text=True, timeout=15)
    check(result.returncode == 0 and json.loads(result.stdout) == report)
    check(not any(value in result.stdout + result.stderr for _, value in secret_scan.load_values()))


def test_store_size_boundary_and_inventory(inventory):
    root, _ = inventory
    path = root / "boundary.env"
    value = fake()
    prefix = "API_KEY=" + value + "\n#"
    path.write_text(prefix + "x" * (secret_scan.MAX_FILE - len(prefix)))
    check(any(val == value for _, val in secret_scan.load_values(strict=True)))
    path.write_text(path.read_text() + "x")
    check(not any(val == value for _, val in secret_scan.load_values(strict=True)))
    row = next(row for row in secret_scan.inventory() if row["file"] == "secrets/boundary.env")
    check(row["taken"] == 0 and row["skipped"] == {"file-too-large": 1})


@pytest.mark.parametrize("value,expected", [("x" * 9, False), ("x" * 10, True),
    ("1" * 15, False), ("1" * 16, True)],
    ids=["short-raw", "raw-threshold", "short-number", "number-threshold"])
def test_raw_selection_boundaries(inventory, value, expected):
    (inventory[0] / "boundary.txt").write_text(value)
    check(any(val == value for _, val in secret_scan.load_values(strict=True)) == expected)


def test_numeric_leaves_and_bool_exclusion():
    check(list(secret_scan.strings_in([17, -12, 1.25, True, False, None])) == ["17", "-12", "1.25"])


def test_hook_numeric_parameter(inventory):
    number = inventory[1]["numeric"]
    result = run_hook({"tool_name": "Artifact", "tool_input": {"pin": int(number)}})
    check(result.returncode == 2)
    check(number not in result.stdout + result.stderr)


@pytest.mark.parametrize("method", [guard.check_outgoing, guard.assert_clean])
def test_guard_single_text(inventory, method):
    value = inventory[1]["key"]
    if method == guard.assert_clean:
        with pytest.raises(guard.OutgoingSecretError):
            method(texts=value)
    else:
        check(bool(method(texts=value)))


@pytest.mark.parametrize("as_string", [True, False])
def test_guard_single_path(inventory, tmp_path, as_string):
    path = tmp_path / "payload.txt"
    path.write_text(inventory[1]["key"])
    check(bool(guard.check_outgoing(files=str(path) if as_string else path)))


@pytest.mark.parametrize("payload", [b"fake", ["text", b"fake"]])
def test_guard_bytes_refused_before_scan_or_audit(monkeypatch, payload):
    def forbidden(*args, **kwargs):
        pytest.fail("Scanning must not precede type validation", pytrace=False)

    monkeypatch.setattr(secret_scan, "load_values", forbidden)
    with pytest.raises(TypeError, match="texts.*str.*bytes"):
        guard.check_outgoing(texts=payload, allow=["format:github"])


def test_git_merge_and_diff_body_state(monkeypatch):
    calls = []
    diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1,2 @@\n+++payload\n+normal\n" \
           "diff --git a/b b/b\n+++ b/b\n+outside\n@@ -1 +1 @@\n+inside\n"

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=diff)

    monkeypatch.setattr(git_pre_push.subprocess, "run", run)
    check(git_pre_push._added_lines("a" * 40, "b" * 40, "unused") ==
          ["++payload", "normal", "inside"])
    check("-m" in calls[0])


@pytest.mark.parametrize("sha_length", [40, 64])
@pytest.mark.parametrize("advertisement", ["known", "unknown", "empty", "error", "timeout"])
def test_git_new_branch_uses_actual_advertisement(monkeypatch, advertisement, sha_length):
    calls = []
    zero = "0" * sha_length
    sha = "b" * sha_length

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "ls-remote":
            check(args == ["git", "ls-remote", "--refs", "destination.git"])
            check(kwargs.get("timeout") == 20)
            if advertisement == "error":
                raise subprocess.CalledProcessError(1, args)
            if advertisement == "timeout":
                raise subprocess.TimeoutExpired(args, 20)
            return subprocess.CompletedProcess(args, 0, stdout="" if advertisement == "empty"
                                               else sha + "\trefs/heads/main\n")
        if args[1] == "cat-file":
            check(args == ["git", "cat-file", "-e", sha + "^{commit}"])
            return subprocess.CompletedProcess(args, int(advertisement != "known"))
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(git_pre_push.subprocess, "run", run)
    git_pre_push._added_lines("a" * sha_length, zero, "destination.git")
    log = calls[-1]
    check(not any("--remotes" in arg for arg in log))
    check(("--not" in log) == (advertisement == "known"))
    if advertisement == "known":
        check(log[-2:] == ["--not", sha])


@pytest.mark.parametrize("sha_length", [40, 64])
def test_git_main_uses_second_hook_argument(monkeypatch, sha_length):
    import io

    seen = []
    monkeypatch.setattr(sys, "argv", ["pre-push", "origin", "destination.git"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("refs/heads/main %s refs/heads/main %s\n" %
                                                ("a" * sha_length, "0" * sha_length)))

    def added(local, remote, address):
        seen.append(address)
        return []

    monkeypatch.setattr(git_pre_push, "_added_lines", added)
    check(git_pre_push.main() == 0)
    check(seen == ["destination.git"])


def test_long_text_name_stays_readable(inventory):
    name = "VERY_LONG_SERVICE_API_TOKEN"
    value = fake()
    (inventory[0] / "names.env").write_text(name + "=" + value)
    check(("secrets/names.env:" + name, value) in secret_scan.load_values(strict=True))


def test_json_root_array_labels(inventory):
    value = fake()
    (inventory[0] / "tokens.json").write_text(json.dumps([{"token": value}, value]))
    pairs = secret_scan.load_values(strict=True)
    check(("secrets/tokens.json:[0].token", value) in pairs)
    check(("secrets/tokens.json:[1]", value) in pairs)


def test_extra_glob_traversal_failure(inventory, tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "HOME", tmp_path)
    directory = tmp_path / "extra"
    directory.mkdir()
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", "extra/**/*.env")
    original = os.scandir

    def denied(path):
        if Path(path) == directory:
            raise PermissionError("Synthetic traversal failure")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        secret_scan.load_values(strict=True)
    check(str(directory) in str(caught.value))
    check(any(row["skipped"] == {"unreadable-path": 1} for row in secret_scan.inventory()))


def test_optional_glob_directory_does_not_add_values(inventory, tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "HOME", tmp_path)
    directory = tmp_path / "client.json"
    directory.mkdir()
    value = fake()
    (directory / "token.env").write_text("API_KEY=" + value)
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", "client*")
    check(not any(val == value for _, val in secret_scan.load_values(strict=True)))


def test_strict_reports_all_missing_paths(inventory, tmp_path, monkeypatch):
    first, second = tmp_path / "absent-one", tmp_path / "absent-two"
    monkeypatch.setenv("SECRET_SCAN_ROOTS", os.pathsep.join(map(str, [inventory[0], first, second])))
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        secret_scan.load_values(strict=True)
    check(all(str(path) in str(caught.value) for path in (first, second)))


def test_generated_masks_cannot_disclose_other_values(inventory):
    (inventory[0] / "masks.env").write_text("API_KEY=secrets/\nOTHER_KEY=<file#1>\n")
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    check(all(len(label) < secret_scan.MIN_NAMED for label, _ in pairs))
    report = secret_scan.inventory()
    check(not any(value in row["file"] for row in report for _, value in pairs))


def test_git_textconv_does_not_hide_added_contents(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="0")
    for key, value in env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    for key in set(os.environ) - set(env):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(repo)

    def git(*args):
        result = subprocess.run(["git", *args], env=env, capture_output=True, text=True)
        check(result.returncode == 0, "Local git fixture failed")
        return result.stdout.strip()

    git("init", "--template=")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "diff.summary.textconv", "wc -l")
    (repo / ".gitattributes").write_text("*.txt diff=summary\n")
    git("add", ".gitattributes")
    git("commit", "-qm", "attributes")
    before = git("rev-parse", "HEAD")
    value = fake()
    (repo / "payload.txt").write_text(value + "\n")
    git("add", "payload.txt")
    git("commit", "-qm", "synthetic payload")
    after = git("rev-parse", "HEAD")
    check(value in git_pre_push._added_lines(after, before, "unused"),
          "Configured textconv hid original contents")


def test_hook_preserves_decimal_numeric_spelling(inventory):
    number = str(secrets.randbelow(8_000_000_000_000_000) + 10_000_000_000_000_000)
    (inventory[0] / "decimal.env").write_text("PIN_CODE=" + number)
    raw = '{"tool_name":"Artifact","tool_input":{"pin":' + number + '.125}}'
    result = subprocess.run([sys.executable, "-B", str(CODE / "hook_pretooluse.py")],
                            input=raw, capture_output=True, text=True, timeout=15)
    check(result.returncode == 2, "Decimal spelling was lost before scanning")
    check(number not in result.stdout + result.stderr)


@pytest.mark.parametrize("kind", sorted(EXPECTED_FORMATS))
def test_format_text_field_name_is_masked(inventory, kind):
    # A known-format token used as a FIELD NAME is never loaded as a value, so only the
    # label masking can keep it out of the package's own output.
    token = format_payloads()[kind]
    match = secret_scan.ASSIGN.match(token + "=x")
    if not match or match.group(1) != token:
        pytest.skip("this token shape cannot be a text field name")
    root, _ = inventory
    value = fake()
    (root / "accounts_token.env").write_text(token + "=" + value)
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    label = next(label for label, item in pairs if item == value)
    check("<key#" in label and token not in label)
    with pytest.raises(guard.OutgoingSecretError) as caught:
        guard.assert_clean(texts=value)
    result = run_hook({"tool_name": "Artifact", "tool_input": {"text": value}})
    check(result.returncode == 2)
    output = str(caught.value) + result.stdout + result.stderr + json.dumps(secret_scan.inventory())
    check(token not in output and value not in output, "Field-name token disclosed")


@pytest.mark.parametrize("location", ["file", "directory"])
@pytest.mark.parametrize("kind", sorted(EXPECTED_FORMATS))
def test_format_path_segments_are_masked(inventory, kind, location):
    token = format_payloads()[kind]
    root, _ = inventory
    value = fake()
    if location == "directory":
        directory = root / token
        directory.mkdir()
        path = directory / "token.env"
    else:
        path = root / (token + ".env")
    path.write_text("API_KEY=" + value)
    pairs = secret_scan.load_values(strict=True)
    check_label_invariants(pairs)
    label = next(label for label, item in pairs if item == value)
    check("<file#2>" in label)
    with pytest.raises(guard.OutgoingSecretError) as caught:
        guard.assert_clean(texts=value)
    result = run_hook({"tool_name": "Artifact", "tool_input": {"text": value}})
    check(result.returncode == 2)
    output = str(caught.value) + result.stdout + result.stderr + json.dumps(secret_scan.inventory())
    check(token not in output and value not in output, "Path token disclosed")


def test_long_ordinary_path_segments_stay_readable(inventory):
    directory = inventory[0] / "ordinary_service_configuration_directory"
    directory.mkdir()
    path = directory / "ordinary_service_configuration_file.env"
    value = fake()
    path.write_text("API_KEY=" + value)
    label = "secrets/" + directory.name + "/" + path.name + ":API_KEY"
    check((label, value) in secret_scan.load_values(strict=True))
    check(any(row["file"] == label.split(":")[0] for row in secret_scan.inventory()))


@pytest.mark.parametrize("failure", ["read", "walk", "stat", "missing"])
@pytest.mark.parametrize("source", ["loaded", "format"])
def test_problem_path_segments_are_masked(inventory, monkeypatch, failure, source):
    root, values = inventory
    token = values["key"] if source == "loaded" else format_payloads()["github"]
    path = root / (token + ".env")
    path.write_text("API_KEY=" + fake())
    if failure == "read":
        original = Path.read_text

        def read(self, *args, **kwargs):
            if self == path:
                raise PermissionError("Synthetic read failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    elif failure == "walk":
        path.unlink()
        path.mkdir()
        original = os.scandir

        def walk(name):
            if Path(name) == path:
                raise PermissionError("Synthetic traversal failure")
            return original(name)

        monkeypatch.setattr(os, "scandir", walk)
    elif failure == "stat":
        original = Path.stat

        def stat(self, *args, **kwargs):
            if self == path:
                raise PermissionError("Synthetic stat failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    else:
        path.unlink()
        monkeypatch.setenv("SECRET_SCAN_ROOTS", os.pathsep.join(map(str, [root, path])))
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        guard.assert_clean(texts="ordinary")
    output = str(caught.value) + json.dumps(secret_scan.inventory())
    check(token not in output and "<file#" in output, "Problem path disclosed a token")


def test_empty_store_roots_and_extra_patterns_are_masked(tmp_path, monkeypatch):
    token = format_payloads()["github"]
    root = tmp_path / token
    root.mkdir()
    pattern = str(root / "*.env")
    monkeypatch.setenv("SECRET_SCAN_ROOTS", str(root))
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", pattern)
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        secret_scan.load_values(strict=True)
    check("No secret values found" in str(caught.value))
    check(token not in str(caught.value) and "<file#" in str(caught.value))


def test_directory_symlink_values_and_inventory(inventory, tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    value = fake()
    (outside / "token.env").write_text("API_KEY=" + value)
    (inventory[0] / "linked-service").symlink_to(outside, target_is_directory=True)
    label = "secrets/linked-service/token.env:API_KEY"
    check((label, value) in secret_scan.load_values(strict=True))
    check(guard.check_outgoing(texts=value) == [label])
    check(any(row["file"] == "secrets/linked-service/token.env" and row["taken"] == 1
              for row in secret_scan.inventory()))
    result = run_hook({"tool_name": "Artifact", "tool_input": {"text": value}})
    check(result.returncode == 2 and value not in result.stdout + result.stderr)


def test_directory_symlink_cycle_and_repeated_root(inventory, monkeypatch):
    root, _ = inventory
    (root / "sub" / "cycle").symlink_to(root, target_is_directory=True)
    (root / "sub" / "repeat").symlink_to(root / "sub", target_is_directory=True)
    monkeypatch.setenv("SECRET_SCAN_ROOTS", os.pathsep.join(map(str, [root, root / "sub"])))
    # A subprocess timeout turns a traversal regression into a bounded failure.
    result = subprocess.run([sys.executable, "-B", str(CODE / "secret_scan.py"), "--inventory"],
                            capture_output=True, text=True, timeout=15)
    check(result.returncode == 0)
    rows = json.loads(result.stdout)
    check(sum(row["file"].endswith("disk.json") for row in rows) == 1)
    check(not any(row["skipped"].get("unreadable-path") for row in rows))
    check(bool(secret_scan.load_values(strict=True)))


@pytest.mark.parametrize("failure", ["broken", "unreadable"])
def test_directory_symlink_problem_refuses(inventory, tmp_path, monkeypatch, failure):
    target = tmp_path / "external"
    link = inventory[0] / "linked-service"
    link.symlink_to(target, target_is_directory=True)
    if failure == "unreadable":
        target.mkdir()
        original = os.scandir

        def denied(path):
            if Path(path) == link:
                raise PermissionError("Synthetic traversal failure")
            return original(path)

        monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(secret_scan.DetectorBlind) as caught:
        secret_scan.load_values(strict=True)
    check(str(link) in str(caught.value))
    check(any(row["file"] == "secrets/linked-service" and
              row["skipped"] == {"unreadable-path": 1} for row in secret_scan.inventory()))


def test_blind_diagnostics_sanitize_all_doors(inventory, capsys):
    value = inventory[1]["key"]
    path = inventory[0] / (value + ".env")
    path.write_text("API_KEY=" + fake())
    path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            path.read_text()
        with pytest.raises(secret_scan.DetectorBlind) as caught:
            guard.assert_clean(texts="ordinary")
        normal = run_hook({"tool_name": "Artifact", "tool_input": {"text": "ordinary"}})
        bash = run_hook({"tool_name": "Bash", "tool_input": {"command": "true"}})
        check(normal.returncode == 2 and bash.returncode == 0)
        check(git_pre_push.main() == 1)
        output = str(caught.value) + normal.stdout + normal.stderr + bash.stdout + bash.stderr
        output += capsys.readouterr().err
        check(value not in output and "<file#" in output)
    finally:
        path.chmod(0o600)


def test_extra_glob_scalar_json_retains_value_label(inventory, tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "HOME", tmp_path)
    value = fake()
    (tmp_path / "client_token.json").write_text(json.dumps(value))
    monkeypatch.setenv("SECRET_SCAN_EXTRA_GLOBS", "client_token.json")
    check(("client_token.json:value", value) in secret_scan.load_values(strict=True))
