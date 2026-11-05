#!/usr/bin/env python3
"""#2407: WebP bytes past the RIFF-declared length must be rejected like PNG/JPEG/GIF tails.

Usage: python3 webp_tail_cases.py <path to file_safety_check.py>
Fixtures are fixed bytes (generated once with Pillow 12.1.1 / libwebp), so Pillow is not
needed here; mime comes from the real `file`, as in production. Reasons are compared as an
ordered list, because order is what the bot and the journal show.
"""
import importlib.util, io, os, sys, tempfile, zipfile

spec = importlib.util.spec_from_file_location("fsc", sys.argv[1])
fsc = importlib.util.module_from_spec(spec); spec.loader.exec_module(fsc)

LOSSLESS = bytes.fromhex("524946461e000000574542505650384c110000002f0140000007508f22d7a3ff8188e87f0000")
LOSSY = bytes.fromhex("524946463600000057454250565038202a0000009001009d012a0200020002c04c25a00274ba00039800feed903f885dec6dbffcf4cfeb6fe3ff19000000")
ALPHA = bytes.fromhex("524946465a00000057454250565038580a00000010000000010000010000414c504805000000008080808000565038202e000000f001009d012a0200020002c04c25a00274ba01f80000620000ecf32bf9bffc2adc310cd57ff3b06c4bb5e0000000")
# VP8X + EXIF + XMP (XMP payload 37 bytes, padded inside the RIFF bound)
META = bytes.fromhex("524946469800000057454250565038580a0000000c000000010000010000565038202a0000009001009d012a0200020002c04c25a00274ba00039800feed903f885dec6dbffcf4cfeb6fe3ff19000000455849461a0000004d4d002a000000080001010e0002000000027800000000000000584d5020250000003c783a786d706d65746120786d6c6e733a783d2761646f62653a6e733a6d6574612f272f3e00")
# VP8X + ANIM + two ANMF frames
ANIM = bytes.fromhex("52494646b800000057454250565038580a00000002000000010000010000414e494d06000000000000000000414e4d464200000000000000000001000001000064000002565038202a0000003001009d012a0200020002c04c25a000037000feed903ffe22e9ef00867fffe7a67ff72fff5b7a800000414e4d464200000000000000000001000001000064000000565038202a0000003401009d012a0200020000004c25a0000370009bffff8b43fb79ff06b735ffff9827feff7fdfef5c4000")

def zip_bytes():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("run.sh", "echo hidden\n")
    return b.getvalue()

def stretched(base, payload):
    # Known limit (astra r1 P1): the sender moves both declared lengths over the payload,
    # so it sits INSIDE the VP8 chunk. Same class as a payload in a PNG tEXt/private chunk —
    # embedded data passes for every image type; that is why the verdict is quarantine.
    d = bytearray(base + payload)
    d[4:8] = (len(d) - 8).to_bytes(4, "little")
    d[16:20] = (int.from_bytes(base[16:20], "little") + len(payload)).to_bytes(4, "little")
    return bytes(d)

SEVEN_Z = b"7z\xbc\xaf\x27\x1c" + b"\x00" * 26
TAIL = "trailing_data_after_webp_terminator"
# (name, data, route, expected status, expected ordered reasons)
CASES = [
    ("чистый VP8L", LOSSLESS, "poly", "ok", []),
    ("чистый VP8", LOSSY, "poly", "ok", []),
    ("чистый VP8X с альфой", ALPHA, "poly", "ok", []),
    ("чистый VP8X + EXIF + XMP (нечётный XMP)", META, "poly", "ok", []),
    ("чистая анимация ANIM/ANMF", ANIM, "poly", "ok", []),
    ("VP8L + скрипт", LOSSLESS + b"\n<script>alert(1)</script>\n", "poly", "reject", [TAIL]),
    ("VP8 + 7z", LOSSY + SEVEN_Z, "poly", "reject", [TAIL]),
    ("VP8X + один байт", ALPHA + b"A", "poly", "reject", [TAIL]),
    ("VP8L + нулевой байт", LOSSLESS + b"\x00", "poly", "reject", [TAIL]),
    ("VP8 + хвост 300 байт", LOSSY + b"B" * 300, "poly", "reject", [TAIL]),
    ("анимация + хвост", ANIM + b"echo hidden\n", "poly", "reject", [TAIL]),
    ("EXIF/XMP + хвост", META + b"echo hidden\n", "poly", "reject", [TAIL]),
    ("VP8L + zip (порядок причин)", LOSSLESS + zip_bytes(), "poly", "reject", ["polyglot_zip_signature", TAIL]),
    ("VP8 обрезан", LOSSY[:-4], "poly", "reject", ["image_truncated"]),
    ("длина 0xffffffff на малом файле", LOSSY[:4] + b"\xff\xff\xff\xff" + LOSSY[8:], "poly", "reject", ["image_truncated"]),
    ("МАРШРУТ static_gate: VP8L + скрипт", LOSSLESS + b"echo hidden\n", "gate", "reject", [TAIL]),
    ("МАРШРУТ static_gate: чистый VP8", LOSSY, "gate", "ok", []),
    ("ПРЕДЕЛ (известно, проходит): груз внутри растянутого VP8-чанка", stretched(LOSSY, b"echo hidden\n"), "gate", "ok", []),
]

failed = 0
with tempfile.TemporaryDirectory() as d:
    for name, data, route, want_status, want in CASES:
        path = os.path.join(d, "x.webp")
        with open(path, "wb") as f:
            f.write(data)
        fd = os.open(path, os.O_RDONLY)
        try:
            mime = fsc.detect_mime_fd(fd)
            if route == "gate":
                status, reasons = fsc.static_gate(fd, mime, os.fstat(fd).st_size)
            else:
                ok, reasons = fsc.polyglot_check(fd, mime, len(data))
                status = "ok" if ok else "reject"
        finally:
            os.close(fd)
        good = mime == "image/webp" and status == want_status and list(reasons) == want
        failed += not good
        print(("✓" if good else "✗"), name, "|", mime, status, list(reasons), "| ждали", want_status, want)

    # `file` names webp only from this header, so the malformed branch is reached directly
    direct = [
        ("RIFX вместо RIFF", b"RIFX" + LOSSLESS[4:]),
        ("WAVE вместо WEBP", LOSSLESS[:8] + b"WAVE" + LOSSLESS[12:]),
        ("заголовок короче 12 байт", LOSSLESS[:11]),
    ]
    for name, data in direct:
        path = os.path.join(d, "x.bin")
        with open(path, "wb") as f:
            f.write(data)
        fd = os.open(path, os.O_RDONLY)
        try:
            got = fsc.webp_length_reasons(fd, len(data))
        finally:
            os.close(fd)
        good = got == ["webp_header_malformed"]
        failed += not good
        print(("✓" if good else "✗"), name, "(прямой вызов) |", got)

total = len(CASES) + len(direct)
print(f"ИТОГО: {total - failed}/{total}")
sys.exit(1 if failed else 0)
