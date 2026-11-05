#!/usr/bin/env python3
"""#2407: WebP bytes past the RIFF-declared length must be rejected like PNG/JPEG/GIF tails.

Usage: python3 webp_tail_cases.py <path to file_safety_check.py>
Fixtures are fixed bytes (generated once with Pillow), so no Pillow is needed here;
mime comes from the real `file`, as in production.
"""
import importlib.util, io, os, sys, tempfile, zipfile

spec = importlib.util.spec_from_file_location("fsc", sys.argv[1])
fsc = importlib.util.module_from_spec(spec); spec.loader.exec_module(fsc)

LOSSLESS = bytes.fromhex("524946461e000000574542505650384c110000002f0140000007508f22d7a3ff8188e87f0000")
LOSSY = bytes.fromhex("524946463600000057454250565038202a0000009001009d012a0200020002c04c25a00274ba00039800feed903f885dec6dbffcf4cfeb6fe3ff19000000")
ALPHA = bytes.fromhex("524946465a00000057454250565038580a00000010000000010000010000414c504805000000008080808000565038202e000000f001009d012a0200020002c04c25a00274ba01f80000620000ecf32bf9bffc2adc310cd57ff3b06c4bb5e0000000")

def zip_bytes():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("run.sh", "echo hidden\n")
    return b.getvalue()

SEVEN_Z = b"7z\xbc\xaf\x27\x1c" + b"\x00" * 26
CASES = [
    ("чистый VP8L", LOSSLESS, set()),
    ("чистый VP8", LOSSY, set()),
    ("чистый VP8X с альфой", ALPHA, set()),
    ("VP8L + скрипт", LOSSLESS + b"\n<script>alert(1)</script>\n", {"trailing_data_after_webp_terminator"}),
    ("VP8 + 7z", LOSSY + SEVEN_Z, {"trailing_data_after_webp_terminator"}),
    ("VP8X + один байт", ALPHA + b"A", {"trailing_data_after_webp_terminator"}),
    ("VP8L + нулевой байт", LOSSLESS + b"\x00", {"trailing_data_after_webp_terminator"}),
    ("VP8L + zip", LOSSLESS + zip_bytes(), {"polyglot_zip_signature", "trailing_data_after_webp_terminator"}),
    ("VP8 обрезан", LOSSY[:-4], {"image_truncated"}),
]

failed = 0
with tempfile.TemporaryDirectory() as d:
    for name, data, want in CASES:
        path = os.path.join(d, "x.webp")
        with open(path, "wb") as f:
            f.write(data)
        fd = os.open(path, os.O_RDONLY)
        try:
            mime = fsc.detect_mime_fd(fd)
            ok, reasons = fsc.polyglot_check(fd, mime, len(data))
        finally:
            os.close(fd)
        good = mime == "image/webp" and set(reasons) == want and ok == (not want)
        failed += not good
        print(("✓" if good else "✗"), name, "| mime", mime, "| reasons", sorted(reasons), "| ждали", sorted(want))
# `file` names webp only from this header, so the malformed branch is reached directly
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "x.bin")
    with open(path, "wb") as f:
        f.write(b"RIFX" + LOSSLESS[4:])
    fd = os.open(path, os.O_RDONLY)
    try:
        got = fsc.webp_length_reasons(fd, len(LOSSLESS))
    finally:
        os.close(fd)
    good = got == ["webp_header_malformed"]
    failed += not good
    print(("✓" if good else "✗"), "заголовок не RIFF/WEBP (прямой вызов) | reasons", got)
total = len(CASES) + 1
print(f"ИТОГО: {total - failed}/{total}")
sys.exit(1 if failed else 0)
