#!/usr/bin/env python3
"""#2223: расширение MIME только на время вызова; без AV и внешних данных."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
import file_safety_check as gate


def main():
    cases = [
        ("без переменной", None, "audio/ogg", False),
        ("точный ogg", "audio/ogg", "audio/ogg", True),
        ("пробелы и пустые элементы", " , audio/ogg, ,audio/mpeg,video/mp4,", "audio/ogg", True),
        ("мусор", "audio/,Audio/ogg,audio/*,audio/ogg;bad,ошибка", "audio/ogg", False),
        ("мусор рядом с допустимым", "bad,audio/ogg", "audio/ogg", True),
        ("расширение не префикс", "application/octet", "application/octet-stream", False),
        ("octet-stream не назван", "audio/ogg", "application/octet-stream", False),
        ("octet-stream назван явно", "application/octet-stream", "application/octet-stream", True),
        ("после расширения снова умолчания", None, "audio/ogg", False),
        ("допустимый алфавит", "application/x.test+1-2", "application/x.test+1-2", True),
    ]
    failed = 0
    defaults = set(gate.MIME_WHITELIST_EXACT)
    with tempfile.TemporaryDirectory() as folder:
        sample = Path(folder) / "generated.ogg"
        sample.write_bytes(b"OggS" + b"\0" * 64)
        for name, extra, mime, accepted in cases:
            try:
                with patch.dict(os.environ):
                    os.environ.pop("FILE_SAFETY_MIME_EXTRA", None)
                    if extra is not None:
                        os.environ["FILE_SAFETY_MIME_EXTRA"] = extra
                    stderr = io.StringIO()
                    with sample.open("rb") as handle, contextlib.redirect_stderr(stderr):
                        status, reasons = gate.static_gate(handle.fileno(), mime, sample.stat().st_size)
                    assert (status == "ok") == accepted, (status, reasons)
                    assert (f"mime_not_whitelisted:{mime}" in reasons) == (not accepted)
                    if extra and ("bad" in extra or "ошибка" in extra):
                        assert "FILE_SAFETY_MIME_EXTRA" in stderr.getvalue()
                    assert gate.MIME_WHITELIST_EXACT == defaults
                print(f"OK: {name}")
            except Exception as exc:
                failed += 1
                print(f"FAIL: {name}: {exc}")
    print(f"ИТОГ: {'все сошлись' if not failed else 'есть ошибки'} ({len(cases)} случаев)")
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
