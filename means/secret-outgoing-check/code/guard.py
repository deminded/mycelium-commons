"""A guard to call immediately before an application sends text or files."""
import os
import sys

import secret_scan


class OutgoingSecretError(ValueError):
    """Outgoing content was refused; the exception contains only labels."""

    def __init__(self, labels):
        self.labels = list(labels)
        super().__init__("Outgoing content refused: " + secret_scan.describe(self.labels))


def _audit_allow(allow, values):
    """Reject values disguised as labels before the audit log can expose them."""
    allow = tuple(allow)
    if any(not isinstance(label, str) or ":" not in label for label in allow):
        raise OutgoingSecretError(["invalid-allow-label"])
    if secret_scan.scan_texts(allow, values=values):
        raise OutgoingSecretError(["invalid-allow-label"])
    if allow:
        print("secret-outgoing-check: intentional handout: " + secret_scan.describe(allow),
              file=sys.stderr)
    return allow


def check_outgoing(texts=(), files=(), allow=()) -> list[str]:
    """Return hit labels; an empty inventory raises DetectorBlind instead of clearing content."""
    # Normalize integration shortcuts before loading or auditing anything.
    if isinstance(texts, bytes):
        raise TypeError("texts must contain str, not bytes; decode bytes explicitly")
    texts = (texts,) if isinstance(texts, str) else tuple(texts)
    if any(isinstance(text, bytes) for text in texts):
        raise TypeError("texts must contain str, not bytes; decode bytes explicitly")
    files = (files,) if isinstance(files, (str, os.PathLike)) else files
    values = secret_scan.load_values(strict=True)
    allow = _audit_allow(allow, values)
    hits = secret_scan.scan_texts(texts, values=values, allow=allow)
    for path in files:
        hits += [hit for hit in secret_scan.scan_file(path, values=values, allow=allow)
                 if hit not in hits]
    return hits


def assert_clean(texts=(), files=(), allow=()):
    """Refuse delivery before any sending side effect.

        def safe_send(text, paths=(), allow=()):
            assert_clean(texts=[text], files=paths, allow=allow)
            return existing_send(text, paths)
    """
    hits = check_outgoing(texts=texts, files=files, allow=allow)
    if hits:
        raise OutgoingSecretError(hits)
