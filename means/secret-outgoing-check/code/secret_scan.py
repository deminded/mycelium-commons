"""One scanning policy for outgoing text and files.

WHY A SEPARATE MODULE: one rule, several doors. Every door (a send-function guard, the
PreToolUse hook, the git pre-push hook) imports this file, so the list of what counts as a
secret has a single source; a second copy would drift silently.

WHY VALUES AND NOT ONLY FORMATS: a format regex misses tokens of providers it does not know;
the exact value from the secret store is caught whatever its shape. Formats stay as a second
net for keys that never were in the store (pasted by someone, generated in a tool output).

NEVER PRINT A VALUE: hits are reported as "<file>:<NAME>" or "format:<kind>" only.

WHY strict: an empty value list would read as "nothing leaked"; doors call
load_values(strict=True) and treat DetectorBlind as a refusal.

Threat model: an agent's slip under task inertia, not a model that evades on purpose —
compressed or encoded content (zip, pdf streams, base64 of a secret) is not seen.
"""
import fnmatch
import json
import stat
from collections import Counter
import os
import re
from pathlib import Path

HOME = Path.home()
# Roots are overridable so tests and live probes can use a planted fake secret.
DEFAULT_ROOTS = (HOME / "secrets",)

MIN_NAMED = 8       # value under a secret-looking name: short database passwords exist
MIN_RAW = 10        # bare line in a secret file (recovery codes are 10–11 chars)
MAX_FILE = 1 << 20  # a secret file larger than this is not a secret file
MAX_ATTACHMENT = 50 << 20

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|PWD|AUTH|COOKIE|SESSION|CREDENTIAL|PRIVATE|HASH|SALT|"
                         r"BEARER|CODE", re.IGNORECASE)
# In a file named like a secret every name counts — except obvious metadata.
META_NAME = re.compile(r"(HOST|URL|URI|ENDPOINT|ADDR|ADDRESS|USER|USERNAME|LOGIN|PORT|VERSION|"
                       r"SCOPE|TYPE|EXPIRES\w*|_ID|^ID)$", re.IGNORECASE)
ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*[:=]\s*(.*?)\s*$")
TOKENISH = re.compile(r"[A-Za-z0-9_\-]{20,}")

FORMATS = (
    ("openrouter", re.compile(r"sk-or-v1-[0-9a-f]{32,}")),
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("telegram_bot", re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_\-]{33}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class DetectorBlind(RuntimeError):
    """The store is empty or incompletely readable; outgoing content cannot be cleared."""


def _roots():
    env = os.environ.get("SECRET_SCAN_ROOTS")
    if env is not None:
        return tuple(Path(p) for p in env.split(os.pathsep) if p)
    return DEFAULT_ROOTS


def _unquote(raw):
    raw = raw.strip()
    if raw[:1] in "'\"" and raw.count(raw[0]) >= 2:
        return raw[1:raw.index(raw[0], 1)]
    # A rotation note must not become part of the stored value.
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _rejection(value, named):
    value = value.strip()
    if value.lower() in {"true", "false", "none", "null"}:
        return "literal"
    if value.isdigit():
        return None if (named and len(value) >= 8) or len(value) >= 16 else "short-number"
    if len(value) < (MIN_NAMED if named else MIN_RAW):
        return "short"
    if not named and " " in value:
        return "space"
    if value.startswith(("http://", "https://")):
        host, _, tail = value.split("://", 1)[1].partition("/")
        # A plain API address is not a secret; a capability URL or user:pass@host is.
        if "@" not in host and not TOKENISH.search(tail):
            return "plain-address"
    return None


def _keep(value, named):
    return None if _rejection(value, named) else value.strip()


def _is_secret_name(name, whole_file):
    return bool(SECRET_NAME.search(name)) or (whole_file and not META_NAME.search(name))


def _take(raw, named, parts, entries, skipped):
    reason = _rejection(raw, named)
    if reason:
        skipped[reason] += 1
    else:
        entries.append((parts, raw.strip()))


def _from_json(obj, parts, entries, under, skipped):
    if isinstance(obj, dict):
        for number, (key, val) in enumerate(obj.items(), 1):
            sep = ":" if all(part[2] == "file" for part in parts) else "."
            _from_json(val, parts + [(sep, str(key), "json-key", number)], entries,
                       _is_secret_name(str(key), under), skipped)
    elif isinstance(obj, list):
        for index, val in enumerate(obj):
            _from_json(val, parts + [(":" if all(p[2] == "file" for p in parts) else "",
                                      "[%d]" % index, "index", index)],
                       entries, under, skipped)
    elif not isinstance(obj, str):
        skipped["non-string"] += 1
    elif not under:
        skipped["nonsecret-name"] += 1
    else:
        if all(part[2] == "file" for part in parts):
            parts = parts + [(":", "value", "key", 1)]
        _take(obj, True, parts, entries, skipped)


def _from_text(text, parts, entries, whole_file, skipped):
    names = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            skipped["blank"] += 1
            continue
        if line.lstrip().startswith(("#", ";", "[", "-----")):
            skipped["comment"] += 1
            continue
        m = ASSIGN.match(line)
        value_part = m.group(2) if m else ""
        if m and value_part.strip("= ") != "":
            name = m.group(1)
            if _is_secret_name(name, whole_file):
                raw = _unquote(value_part)
                variants = names.setdefault(name, [])
                if raw not in variants:
                    variants.append(raw)
                suffix = "" if variants.index(raw) == 0 else "#%d" % (variants.index(raw) + 1)
                _take(raw, True, parts + [(":", name + suffix, "key", number)],
                      entries, skipped)
            else:
                skipped["nonsecret-name"] += 1
            continue
        # Base64 padding must not turn a bare credential into an empty assignment.
        _take(line.strip(), False, parts + [(":", "line%d" % number, "line", number)],
              entries, skipped)


def _store_files():
    files, problems, visited = {}, set(), set()

    def visit(path, label=None):
        try:
            mode = path.stat().st_mode
            if label is None:
                label = path.name if stat.S_ISDIR(mode) else path.parent.name + "/" + path.name
            if stat.S_ISREG(mode):
                files.setdefault(path, label)
            elif stat.S_ISDIR(mode):
                real = os.path.realpath(path)
                if real in visited:
                    return
                # scandir surfaces traversal errors that recursive glob may suppress.
                with os.scandir(path) as children:
                    paths = sorted(Path(child.path) for child in children)
                # Mark only successfully enumerated directories, so failures remain visible.
                visited.add(real)
                for child in paths:
                    visit(child, label + "/" + child.name)
        except OSError:
            problems.add(str(path))

    for root in _roots():
        visit(root)

    def optional_stat(path, missing_ok=False):
        try:
            return path.stat()
        except FileNotFoundError:
            if not missing_ok:
                problems.add(str(path))
        except OSError:
            problems.add(str(path))
        return None

    def expand(directory, components):
        if not components:
            yield directory
            return
        head, *tail = components
        if not any(c in head for c in "*?["):
            child = directory / head
            if optional_stat(child, missing_ok=True) is not None:
                yield from expand(child, tail)
            return
        if head == "**":
            yield from expand(directory, tail)
        try:
            with os.scandir(directory) as children:
                paths = sorted(Path(child.path) for child in children)
        except NotADirectoryError:
            return
        except OSError:
            problems.add(str(directory))
            return
        for child in paths:
            if head == "**" or (tail and fnmatch.fnmatchcase(child.name, head)):
                info = optional_stat(child)
                if info is not None and stat.S_ISDIR(info.st_mode) and not child.is_symlink():
                    yield from expand(child, components if head == "**" else tail)
            elif fnmatch.fnmatchcase(child.name, head):
                yield child

    for pattern in os.environ.get("SECRET_SCAN_EXTRA_GLOBS", "").split(os.pathsep):
        if pattern:
            pattern_path = Path(pattern)
            base = Path(pattern_path.anchor) if pattern_path.is_absolute() else HOME
            components = pattern_path.parts[1:] if pattern_path.is_absolute() else pattern_path.parts
            for path in expand(base, components):
                info = optional_stat(path)
                if info is None or not stat.S_ISREG(info.st_mode):
                    continue
                try:
                    label = str(path.relative_to(HOME))
                except ValueError:
                    label = path.parent.name + "/" + path.name
                visit(path, label)
    return files, problems


def _safe_labels(entries, file_parts):
    values = {value for _, value in entries}
    used = {}
    fallback_number = 0

    def unsafe(text):
        return any(value in text for value in values)

    def render(parts):
        chunks = []
        for sep, segment, kind, number in parts:
            if unsafe(segment) or (kind in {"file", "json-key", "key"} and
                    any(rx.search(segment) for _, rx in FORMATS)) or (
                    kind == "json-key" and TOKENISH.fullmatch(segment)):
                segment = "<%s#%d>" % ("file" if kind == "file" else "key", number)
            chunks.append(sep + segment)
        return "".join(chunks)

    def fallback(number):
        # Shorter than any accepted value for the first 99999 labels; beyond that the final
        # invariant check below still refuses instead of printing something unsafe.
        return "<%d>" % number

    labels = []
    for parts, value in entries:
        base = render(parts)
        if unsafe(base):
            # A secret can span separators; masking each segment also removes those matches.
            base = "".join(sep + "<%s#%d>" % ("file" if kind == "file" else "key", number)
                           for sep, _, kind, number in parts).lstrip("/")
        label, suffix = base, 1
        while unsafe(label) or (label in used and used[label] != value):
            suffix += 1
            label = base + "#%d" % suffix
            if unsafe(label):
                while True:
                    fallback_number += 1
                    label = fallback(fallback_number)
                    if label not in used or used[label] == value:
                        break
                base = label
        used[label] = value
        labels.append(label)
    safe_files = [render(parts) for parts in file_parts]
    safe_files = [fallback(len(entries) + i + 1) if unsafe(label) else label
                  for i, label in enumerate(safe_files)]
    # These checks cover cross-segment matches as well as collisions from literal dots or masks.
    # Explicit raise, not assert: python -O strips asserts, and a broken label invariant must
    # stop every door (they treat any load error as "detector unavailable").
    if any(unsafe(label) for label in labels + safe_files):
        raise RuntimeError("label invariant violated: a label carries a stored value")
    if len(set(labels)) != len(set(zip(labels, (v for _, v in entries)))):
        raise RuntimeError("label invariant violated: one label maps to several values")
    return labels, safe_files


def _path_parts(label):
    return [("/" if n > 1 else "", part, "file", n)
            for n, part in enumerate(str(label).split("/"), 1)]


def _safe_paths(entries, paths):
    # Diagnostics use the same masks and cross-segment checks as inventory labels.
    return _safe_labels(entries, [_path_parts(path) for path in paths])[1]


def _load(strict=False):
    entries, records, file_parts = [], [], []
    files, problems = _store_files()
    for path, label in files.items():
        parts = _path_parts(label)
        skipped = Counter()
        start = len(entries)
        try:
            if path.stat().st_size > MAX_FILE:
                skipped["file-too-large"] += 1
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                whole_file = bool(SECRET_NAME.search(path.name))
                parsed = False
                if path.suffix == ".json":
                    try:
                        obj = json.loads(text)
                        parsed = True
                    except ValueError:
                        pass
                if parsed:
                    _from_json(obj, parts, entries, whole_file, skipped)
                else:
                    _from_text(text, parts, entries, whole_file, skipped)
        except OSError:
            problems.add(str(path))
            skipped["unreadable"] += 1
        records.append((start, len(entries), skipped))
        file_parts.append(parts)
    for name in sorted(problems - {str(path) for path in files}):
        path = Path(name)
        label = path.parent.name + "/" + path.name
        file_parts.append(_path_parts(label))
        records.append((len(entries), len(entries), Counter({"unreadable-path": 1})))
    labels, safe_files = _safe_labels(entries, file_parts)
    out = [(label, value) for label, (_, value) in zip(labels, entries)]
    if strict and problems:
        raise DetectorBlind("Unreadable or missing secret paths: " +
                            ", ".join(_safe_paths(entries, sorted(problems))))
    if strict and not out:
        raise DetectorBlind(
            "No secret values found; checked roots: %s; extra globs: %s"
            % (", ".join(_safe_paths(entries, _roots())) or "(none)",
               os.pathsep.join(_safe_paths(entries, filter(None, os.environ.get(
                   "SECRET_SCAN_EXTRA_GLOBS", "").split(os.pathsep)))) or "(none)"))
    report = [{"file": name, "taken": end - start, "labels": labels[start:end],
               "skipped": dict(sorted(skipped.items()))}
              for name, (start, end, skipped) in zip(safe_files, records)]
    return out, report


def load_values(strict=False):
    """Load safe, unambiguous (label, value) pairs; strict also refuses partial blindness."""
    out, _ = _load(strict)
    # Longest first: masking an allowed long value must not leave a shorter one behind.
    return sorted(set(out), key=lambda item: (-len(item[1]), item[0]))


def inventory():
    """Per-file counts, safe labels and rejection reason codes, without stored values.

    taken counts accepted leaves/lines, including repeated copies of the same pair.
    file-too-large and unreadable count files; unreadable-path counts paths whose
    enumeration/stat failed. Other reasons count rejected leaves/lines.
    """
    return _load()[1]


def strings_in(obj):
    """String and numeric leaves (keys included: a key can carry text)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield str(obj)
    elif isinstance(obj, dict):
        for key, val in obj.items():
            yield str(key)
            yield from strings_in(val)
    elif isinstance(obj, (list, tuple)):
        for val in obj:
            yield from strings_in(val)


def _allowed_values(values, allow):
    return sorted({value for label, value in values if label in allow}, key=len, reverse=True)


def scan_texts(texts, values=None, allow=()):
    """Labels of secrets present in any of the texts; never the values themselves.

    An allowed label masks ITS VALUE before everything else: the same token
    found again by a format or in a .bak copy is the same handout, not a second leak.
    """
    values = load_values() if values is None else values
    allow = set(allow)
    allowed = _allowed_values(values, allow)
    hits = []
    for text in texts:
        if not text:
            continue
        for value in allowed:
            text = text.replace(value, "\x00")
        squeezed = re.sub(r"\s+", "", text)  # a value split by a soft wrap still counts
        for label, value in values:
            if label not in hits and label not in allow and value not in allowed \
                    and (value in text or value in squeezed):
                hits.append(label)
        for kind, rx in FORMATS:
            tag = "format:%s" % kind
            if tag not in hits and tag not in allow and rx.search(text):
                hits.append(tag)
    return hits


def scan_file(path, values=None, allow=()):
    values = load_values() if values is None else values
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ATTACHMENT + 1)
    except OSError:
        return ["attachment-unreadable"]  # An unread attachment cannot be considered clean.
    if len(data) > MAX_ATTACHMENT:
        return ["attachment-too-large-to-scan"]
    allow = set(allow)
    for value in _allowed_values(values, allow):
        data = data.replace(value.encode("utf-8"), b"\x00")
    hits = [label for label, value in values
            if label not in allow and value.encode("utf-8") in data]
    text = data.decode("utf-8", errors="replace")
    hits += [h for h in scan_texts([text], values=[], allow=allow) if h not in hits]
    return hits


def describe(hits):
    return ", ".join(hits)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect store coverage without values")
    parser.add_argument("--inventory", action="store_true", help="print per-file coverage as JSON")
    args = parser.parse_args()
    if args.inventory:
        print(json.dumps(inventory(), ensure_ascii=True, indent=2))
