#!/usr/bin/env python3
"""Scan added lines in outgoing commits using git's own pre-push ref ranges.

The shell command alone cannot identify the repository or commit range reliably.
This hook guards accidental disclosure; git --no-verify can bypass any hook.
"""
import os
from pathlib import Path
import subprocess
import sys

SCAN_DIR = Path(__file__).resolve().parent


def _added_lines(local_sha, remote_sha, remote_address):
    if remote_sha and set(remote_sha) == {"0"}:
        rng = [local_sha]
        try:
            advertised = subprocess.run(["git", "ls-remote", "--refs", remote_address],
                                        capture_output=True, text=True, check=True, timeout=20)
            known = []
            for line in advertised.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2:
                    continue
                sha = fields[0]
                if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
                    continue
                exists = subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"],
                                        capture_output=True)
                if exists.returncode == 0:
                    known.append(sha)
            if known:
                rng += ["--not", *sorted(set(known))]
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # A stale local tracking ref cannot clear history for a different destination.
            pass
    else:
        rng = ["%s..%s" % (remote_sha, local_sha)]
    out = subprocess.run(["git", "log", "-p", "-m", "--no-color", "--no-ext-diff",
                          "--no-textconv", "--format=", *rng],
                         capture_output=True, text=True, errors="replace", check=True).stdout
    added, in_hunk = [], False
    for line in out.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
    return added


def main():
    try:
        if not (SCAN_DIR / "secret_scan.py").is_file():
            raise ImportError("missing scanner module")
        sys.path.insert(0, str(SCAN_DIR))
        import secret_scan
        values = secret_scan.load_values(strict=True)
    except Exception as exc:
        reason = str(exc) if type(exc).__name__ == "DetectorBlind" else type(exc).__name__
        print("pre-push: secret detector unavailable: %s; push refused. "
              "Restore the detector and its roots." % reason, file=sys.stderr)
        return 1
    remote_address = sys.argv[2] if len(sys.argv) > 2 else "origin"
    allow = tuple(label.strip() for label in os.environ.get("SECRET_SCAN_PUSH_ALLOW", "").split(",")
                  if label.strip())
    if any(":" not in label for label in allow) or secret_scan.scan_texts(allow, values=values):
        print("Invalid SECRET_SCAN_PUSH_ALLOW: use labels, never secret values.", file=sys.stderr)
        return 1
    if allow:
        print("secret-outgoing-check: intentional handout: " + secret_scan.describe(allow),
              file=sys.stderr)
    hits = []
    for line in sys.stdin:
        parts = line.split()
        # A deleted ref has an all-zero local sha (40 or 64 chars): nothing leaves with it.
        if len(parts) != 4 or set(parts[1]) == {"0"}:
            continue
        try:
            added = _added_lines(parts[1], parts[3], remote_address)
        except (OSError, subprocess.CalledProcessError):
            print("pre-push: could not read outgoing commits; push refused.", file=sys.stderr)
            return 1
        hits += [hit for hit in secret_scan.scan_texts(added, values=values, allow=allow)
                 if hit not in hits]
    if hits:
        print("pre-push: secret labels in outgoing commits: %s. Push refused; remove secrets "
              "from history, or intentionally use SECRET_SCAN_PUSH_ALLOW=<labels>."
              % secret_scan.describe(hits), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
