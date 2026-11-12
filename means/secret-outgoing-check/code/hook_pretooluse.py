#!/usr/bin/env python3
"""Refuse outgoing tool inputs containing secrets; leave Bash available for repairs."""
import json
import os
from pathlib import Path
import sys

SCAN_DIR = Path(os.environ.get("SECRET_SCAN_DIR", Path(__file__).resolve().parent))
# Command and URL text often quotes key headers in tests and documentation.
TEXT_ONLY = {"Bash", "WebFetch", "WebSearch"}
# Extend this inventory for tools that send file contents rather than just path strings.
FILE_PATH_KEYS = {"Artifact": ("file_path", "file_paths", "files")}


def _files_of(tool, inp):
    if not isinstance(inp, dict):
        return []
    out = []
    for key in FILE_PATH_KEYS.get(tool, ()):
        value = inp.get(key)
        if isinstance(value, dict):
            out.extend(item.get("from") if isinstance(item, dict) else item
                       for item in value.values())
        elif isinstance(value, (list, tuple)):
            out.extend(value)
        else:
            out.append(value)
    # Missing files must reach scan_file: unreadable must never mean clean.
    return [path for path in out if isinstance(path, str)]


def _unavailable(tool, reason):
    if tool != "Bash":
        # No hint about allow here: an allow label cannot cure a blind detector.
        print("Secret detector unavailable: %s. Outgoing tool call refused. "
              "Restore the detector and its roots." % reason, file=sys.stderr)
        return 2
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "WARNING: secret detector unavailable: %s; Bash command was "
                             "not checked for secrets. Repair the detector and its roots." % reason}}))
    return 0


def main():
    try:
        data = json.load(sys.stdin, parse_float=str, parse_int=str)
    except ValueError:
        return 0
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    try:
        # A configured missing directory must not fall back to another installed module.
        if not (SCAN_DIR / "secret_scan.py").is_file():
            raise ImportError("missing scanner module")
        sys.path.insert(0, str(SCAN_DIR))
        import secret_scan
        values = secret_scan.load_values(strict=True)
    except Exception as exc:
        reason = str(exc) if type(exc).__name__ == "DetectorBlind" else type(exc).__name__
        return _unavailable(tool, reason)
    allow = tuple(label.strip() for label in os.environ.get("SECRET_SCAN_ALLOW", "").split(",")
                  if label.strip())
    # Environment input is not trusted to contain labels rather than the secret itself.
    if any(":" not in label for label in allow) or secret_scan.scan_texts(allow, values=values):
        print("Invalid SECRET_SCAN_ALLOW: use labels, never secret values.", file=sys.stderr)
        return 2
    if allow:
        print("secret-outgoing-check: intentional handout: " + secret_scan.describe(allow),
              file=sys.stderr)
    hits = secret_scan.scan_texts(secret_scan.strings_in(inp), values=values, allow=allow)
    if tool in TEXT_ONLY:
        hits = [hit for hit in hits if hit != "format:private_key"]
    for path in _files_of(tool, inp):
        hits += [hit for hit in secret_scan.scan_file(path, values=values, allow=allow)
                 if hit not in hits]
    if not hits:
        return 0
    # The hook's environment is set when the client starts, not per call: say so, or the
    # agent will look for a per-call switch that does not exist.
    print("Outgoing tool call refused; secret labels: %s. If the handout is intentional, send "
          "it through your own sending function guarded with allow=[labels]. Last resort: the "
          "human operator starts the client with SECRET_SCAN_ALLOW=<labels>; it then covers "
          "every call until removed." % secret_scan.describe(hits), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
