#!/usr/bin/env python3
"""
file-safety-check — static+AV gate for the Mycelium file-exchange circuit.

Source of truth: /opt/workspace/vault/Projects/Грибница/file-safety-design.md
Implements phases 2 (static gate), 3 (ClamAV), 5 (honest label), 7 (journal).
Phase 4 (LLM triage in an isolated uid) is wired as an explicit SKIPPED stub.

Design invariant (do not weaken): the label NEVER says "safe". A file that
passes every gate we actually run is still UNTRUSTED DATA — verdict is only
ever "reject" or "quarantine". Processing discipline (read as data, not
instructions, in an isolated session) is what actually protects the agent —
see cookbook-agent.md, "Файлы = недоверенные данные".

TOCTOU invariant (codex review #2): every check is bound to ONE inode. The
target is opened once with O_NOFOLLOW; size/type come from fstat(fd); child
scanners (file, clamscan) receive the same open file description via
/proc/self/fd/<fd>; the label is written only after re-checking that the
path still names that inode. A path swap mid-check can therefore neither
redirect what gets scanned nor attach our verdict to a different file.
"""

import errno
import hashlib
import json
import os
import shutil
import stat as stat_mod
import subprocess
import sys
import time
import urllib.parse
import xml.parsers.expat
import zipfile

# ---------------------------------------------------------------------------
# Config — kept as top-of-file constants (not scattered inline) so the quotas
# and whitelist can be tuned without hunting through the logic below.
# ---------------------------------------------------------------------------

# mime-types allowed through the static gate. Extend here, not inline, so the
# policy stays auditable in one place. Prefixes ending in "/" match any subtype.
MIME_WHITELIST_PREFIXES = (
    "text/",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)
# Word .docx (OOXML) — admitted 03.11.2026 at the node owner's request (Грибница 8971).
# It is a zip container: listed in ARCHIVE_MIME_TYPES too (bomb quotas apply, the
# "zip inside a non-zip" polyglot rule does not), and every zip that turns out to be an
# OOXML package must also pass ooxml_check().
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MIME_WHITELIST_EXACT = {
    DOCX_MIME,
    "application/pdf",
    "application/json",
    "application/zip",
    "application/x-mimearchive",  # MHTML / message archives (.mht/.mhtml)
    "message/rfc822",
}

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB — generous default for text/docs/images

# Archive (zip) quotas — a static gate can't fully sandbox extraction, but
# reading the central directory writes nothing to disk, so these checks are
# cheap and safe to run unconditionally on any zip.
ZIP_MAX_RATIO = 100          # uncompressed / compressed size
ZIP_MAX_FILE_COUNT = 10_000
ZIP_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
ZIP_MAX_NESTED_DEPTH = 15    # path separators inside the archive as a depth proxy

ARCHIVE_MIME_TYPES = {"application/zip", DOCX_MIME}

# Nested-archive extensions. A zip whose members are themselves archives can
# hide a bomb the flat central-directory listing scores as tiny — depth-by-'/'
# never sees zip-in-zip. At MVP we refuse such archives rather than recurse.
ARCHIVE_EXTENSIONS = (".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar",
                      ".tgz", ".tbz2", ".jar", ".war", ".apk", ".zipx",
                      ".lz", ".lzma", ".cab", ".arj",
                      # Office/ODF files are zips too: a .docx packed inside a plain zip
                      # would reach quarantine without ever meeting ooxml_check().
                      ".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx",
                      ".xltm", ".xlsb", ".xlam", ".pptx", ".pptm", ".potx", ".potm",
                      ".ppsx", ".ppsm", ".ppam", ".sldx", ".sldm", ".vsdx", ".vsdm",
                      ".odt", ".ods", ".odp", ".odg", ".odf", ".odb", ".odm", ".ott",
                      ".ots", ".otp", ".otg", ".odc", ".otc", ".odi", ".oti", ".otf", ".oth")

# --- OOXML (Office Open XML) content rules -----------------------------------------
# Parts are read with a real XML parser and matched by EXPANDED name (namespace URI +
# local name): prefix spelling, quoting, character references, comments, CDATA and
# attribute order then carry no security meaning. A regex version of this check was
# bypassed eight ways in review (codex sol, 03.11.2026) — do not go back to regexes.
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_WML = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "http://purl.oclc.org/ooxml/wordprocessingml/main")
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
# A document may point outside itself only by hyperlink: a link fires on a click. Any
# other external relationship (attachedTemplate, frame, a picture linked by URL...) makes
# the editor fetch remote content on open — the remote-template channel.
OOXML_HYPERLINK_REL_TYPES = {
    "http://schemas.openxmlformats.org/officedocument/2006/relationships/hyperlink",
    "http://purl.oclc.org/ooxml/officedocument/relationships/hyperlink",
}
# Relationship kinds that carry code or opaque objects even with an internal target.
OOXML_ACTIVE_REL_KINDS = {"oleobject", "package", "control", "activexcontrolbinary",
                          "vbaproject", "afchunk", "subdocument"}
# Content types of code and opaque objects; any "macroEnabled" main type as well.
OOXML_ACTIVE_CONTENT_TYPES = {
    "application/vnd.ms-office.vbaproject",
    "application/vnd.ms-office.vbaprojectsignature",
    "application/vnd.ms-office.activex",
    "application/vnd.ms-office.activex+xml",
    "application/vnd.openxmlformats-officedocument.oleobject",
}
# Field codes that pull or run something external when the document opens or updates.
OOXML_ACTIVE_FIELDS = {"DDE", "DDEAUTO", "INCLUDETEXT", "INCLUDEPICTURE", "LINK", "IMPORT"}
OOXML_MAX_FIELD_CODE = 64 * 1024        # real field codes are short; a huge one is a probe
OOXML_MAX_XML_BYTES = 100 * 1024 * 1024  # XML fed to the parser per package
OOXML_CHUNK = 64 * 1024
OOXML_MAX_OPEN_FIELDS = 64               # nesting of complex fields
OOXML_MAX_XML_DEPTH = 256                # element nesting in one part
OOXML_MAX_CT_ENTRIES = 10_000            # Default/Override entries in the manifest
# Field semantics apply only to parts typed as Word stories; custom XML data that merely
# quotes WML vocabulary is data, not a field (review round 2, P2-N2).
OOXML_WML_PART_TYPE_PREFIXES = ("application/vnd.openxmlformats-officedocument.wordprocessingml.",
                                "application/vnd.ms-word.")
OOXML_WML_MAIN_TYPE_MARKERS = ("wordprocessingml.document.main+xml",
                               "wordprocessingml.template.main+xml",
                               "ms-word.document.macroenabled.main+xml",
                               "ms-word.template.macroenabledtemplate.main+xml")
# Namespaces a Word reader understands inside a story. An element from any OTHER namespace
# is, at best, an ignorable extension the reader throws away with its whole subtree — so
# a field fragment under it means something different to the reader and to this scan.
# EXACT identifiers: a namespace URI is a name, not a path — an invented URI under a vendor
# prefix is still unknown to the reader (review round 3 P1: startswith() let it through).
OOXML_KNOWN_NS = frozenset(NS_WML) | {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://purl.oclc.org/ooxml/officeDocument/math",
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing",
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
    "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "http://purl.oclc.org/ooxml/drawingml/picture",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "http://schemas.microsoft.com/office/word/2010/wordml",
    "http://schemas.microsoft.com/office/word/2012/wordml",
    "http://schemas.microsoft.com/office/word/2015/wordml/symex",
    "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "http://schemas.microsoft.com/office/word/2018/wordml",
    "http://schemas.microsoft.com/office/word/2018/wordml/cex",
    "http://schemas.microsoft.com/office/word/2023/wordml/word16du",
    "http://schemas.microsoft.com/office/drawing/2010/main",
    "urn:schemas-microsoft-com:vml",
    "urn:schemas-microsoft-com:office:office",
    "urn:schemas-microsoft-com:office:word",
}
# Relationship kinds whose target the reader opens as a Word story (fields live there).
OOXML_STORY_REL_KINDS = {"officedocument", "header", "footer", "footnotes", "endnotes",
                         "comments", "glossarydocument"}
# Kinds that exist only in Word packages: their target must be WML on ANY route (zip or docx),
# so member order — which decides what libmagic calls the file — cannot switch the check off.
OOXML_WML_ONLY_REL_KINDS = {"header", "footer", "footnotes", "endnotes", "glossarydocument"}
# Declared types that say nothing about a part — never a valid type for a story.
OOXML_GENERIC_TYPES = {"application/xml", "text/xml", "application/octet-stream", "text/plain"}

# Image formats whose spec fixes an end-of-stream terminator. Anything after
# that terminator is a smuggling channel (polyglot / appended payload) — the
# image renders fine everywhere while carrying an invisible tail. Reject.
PNG_TAIL = b"\x00\x00\x00\x00IEND\xaeB`\x82"  # IEND chunk is always these 12 bytes
JPEG_TAIL = b"\xff\xd9"                        # EOI marker
GIF_TAIL = b";"                                # 0x3B trailer
# WebP has no terminator marker, but its RIFF header states the payload length:
# 'RIFF' + uint32 LE size + 'WEBP'. Bytes past size + 8 are the same channel.
WEBP_RIFF_HEADER_LEN = 12

# journal lives next to the worker by default — the same file works unchanged
# on the dev host (~/file-safety/) and on mycelium (/opt/mycelium/file-safety/)
JOURNAL_PATH = os.environ.get(
    "FILE_SAFETY_JOURNAL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal.log"),
)


# ---------------------------------------------------------------------------
# fd helpers — everything reads through the one fd opened in check_file()
# ---------------------------------------------------------------------------

def reopen(fd, mode="rb"):
    # /proc/self/fd/<fd> resolves straight to the open file description's
    # inode (no path re-resolution), and opening it yields an INDEPENDENT
    # offset — so zipfile can seek freely without disturbing other readers.
    return open(f"/proc/self/fd/{fd}", mode)


def sha256_of_fd(fd):
    # pread is offset-independent — no shared-offset interference, no seeks
    h = hashlib.sha256()
    pos = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, pos)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)
        pos += len(chunk)


def detect_mime_fd(fd):
    # shell out to `file` rather than a python mime-guess lib: `file` sniffs
    # actual content/magic bytes, which is what matters for a security gate.
    # The child gets our open file description via /proc/self/fd, so a path
    # swap after open cannot change what gets sniffed.
    # -L: /proc/self/fd/<fd> is a (magic) symlink — without it `file` reports
    # the link itself, not the content behind it
    out = subprocess.run(
        ["file", "-L", "--mime-type", "-b", f"/proc/self/fd/{fd}"],
        capture_output=True, text=True, check=True, pass_fds=[fd],
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def mime_allowed(mime):
    if mime in MIME_WHITELIST_EXACT:
        return True
    return any(mime.startswith(p) for p in MIME_WHITELIST_PREFIXES)


def zip_bomb_check(fd, size):
    """
    Inspect a zip's central directory (no extraction to disk) and reject
    anything that looks like a zip-bomb: absurd compression ratio, huge file
    count, huge total uncompressed size, or suspiciously deep nesting.
    Returns (ok: bool, reasons: list[str]).
    """
    reasons = []
    # Structured stdlib reader, never extracts. Any parse anomaly -> fail
    # CLOSED (reject on uncertainty) — a text parser here failed OPEN on
    # filenames containing newlines (codex review #5).
    try:
        with reopen(fd) as f, zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        return False, ["zip_unreadable_or_corrupt"]
    except Exception:
        return False, ["zip_listing_parse_error"]

    total_uncompressed = sum(i.file_size for i in infos)
    file_count = len(infos)
    max_depth = max((i.filename.count("/") for i in infos), default=0)
    nested = [i.filename for i in infos
              if i.filename.lower().endswith(ARCHIVE_EXTENSIONS)]

    ratio = (total_uncompressed / size) if size else float("inf")

    if nested:
        # zip-in-zip: the outer listing can't score a bomb hidden one level
        # down, and depth-by-'/' never counts archive recursion. Refuse.
        reasons.append(f"nested_archive:{nested[0]}")
    if file_count > ZIP_MAX_FILE_COUNT:
        reasons.append(f"zip_file_count_{file_count}_exceeds_{ZIP_MAX_FILE_COUNT}")
    if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
        reasons.append(
            f"zip_uncompressed_{total_uncompressed}_exceeds_{ZIP_MAX_UNCOMPRESSED_BYTES}"
        )
    if ratio > ZIP_MAX_RATIO:
        reasons.append(f"zip_ratio_{ratio:.1f}_exceeds_{ZIP_MAX_RATIO}")
    if max_depth > ZIP_MAX_NESTED_DEPTH:
        reasons.append(f"zip_nesting_depth_{max_depth}_exceeds_{ZIP_MAX_NESTED_DEPTH}")

    return (len(reasons) == 0), reasons


class _Refuse(Exception):
    """Fixed-vocabulary reason; nothing from the file is ever echoed into the chat."""


_NESTED = "\x00"   # stands in the outer field code where a nested field begins


class _OoxmlScan:
    """
    Expat event handler for one XML part. Complex fields are tracked on a stack by their
    PARSED fldCharType, so a code split over many runs is joined, nested fields stay
    separate, and each finished code is judged at once (nothing accumulates).
    """

    # Conservative MVP choices (named, not hidden): a field fragment inside markup-
    # compatibility alternatives, inside tracked-change markup, or under an element of an
    # unknown namespace is REFUSED instead of evaluated per reader view — the same stance
    # the gate takes on nested archives (refuse rather than recurse). Cost: such documents
    # (e.g. a page number in a text box wrapped in mc:AlternateContent) are rejected.
    FIELD_ELEMENTS = ("fldChar", "instrText", "delInstrText", "fldSimple")
    REVISION_ELEMENTS = ("ins", "del", "moveFrom", "moveTo")

    def __init__(self, reasons, wml_story=True):
        self.reasons = reasons
        self.wml_story = wml_story
        self.stack = []        # open complex fields: {"code": [...], "len": n, "instr": bool}
        self.in_instr = 0
        self.orphan = None     # instruction text outside any open field
        self.depth = 0
        self.flags = []        # per open element: (alt, revision, foreign)
        self.alt = self.rev = self.foreign = 0
        self.story_targets = []

    def _judge(self, code):
        head = code.lstrip()
        if head.startswith(_NESTED):
            # the keyword itself would come from a nested field's result
            self.reasons.add("ooxml_field_computed_keyword")
            return
        word = ""
        for ch in head:
            if not ch.isalpha():
                break
            word += ch
        if word.upper() in OOXML_ACTIVE_FIELDS:
            self.reasons.add("ooxml_active_field:" + word.upper())

    def _sink(self):
        if self.orphan is not None:
            return self.orphan
        if self.stack and self.stack[-1]["instr"]:
            return self.stack[-1]
        return None

    def _in_instr_phase(self):
        return self.in_instr or (self.stack and self.stack[-1]["instr"])

    def start(self, name, attrs):
        uri, _, local = name.rpartition(" ")
        self.depth += 1
        if self.depth > OOXML_MAX_XML_DEPTH:
            raise _Refuse("ooxml_xml_too_deep")
        is_alt = uri == NS_MC and local == "AlternateContent"
        is_rev = uri in NS_WML and local in self.REVISION_ELEMENTS
        is_foreign = bool(uri) and uri not in OOXML_KNOWN_NS
        if self.wml_story and self._in_instr_phase():
            if is_alt:
                self.reasons.add("ooxml_field_alternatecontent")
            if is_rev:
                self.reasons.add("ooxml_field_tracked_change")
            if is_foreign:
                self.reasons.add("ooxml_field_foreign_markup")
        if self.wml_story and uri in NS_WML and local in self.FIELD_ELEMENTS:
            if self.alt:
                self.reasons.add("ooxml_field_alternatecontent")
            if self.rev or local == "delInstrText":
                self.reasons.add("ooxml_field_tracked_change")
            if self.foreign:
                self.reasons.add("ooxml_field_foreign_markup")
        self.flags.append((is_alt, is_rev, is_foreign))
        self.alt += is_alt; self.rev += is_rev; self.foreign += is_foreign
        if not self.wml_story and not (uri == NS_REL and local == "Relationship"):
            return
        if uri == NS_REL and local == "Relationship":
            rtype = attrs.get("Type", "").strip().lower()
            if (rtype.rsplit("/", 1)[-1] in OOXML_STORY_REL_KINDS
                    and attrs.get("TargetMode", "").strip().lower() != "external"):
                self.story_targets.append((rtype.rsplit("/", 1)[-1], attrs.get("Target", "")))
            if rtype.rsplit("/", 1)[-1] in OOXML_ACTIVE_REL_KINDS:
                self.reasons.add("ooxml_active_relationship")
            if (attrs.get("TargetMode", "").strip().lower() == "external"
                    and rtype not in OOXML_HYPERLINK_REL_TYPES):
                self.reasons.add("ooxml_external_reference")
        elif uri in NS_WML:
            if local == "fldChar":
                kind = attrs.get(uri + " fldCharType", attrs.get("fldCharType", ""))
                if kind == "begin":
                    if len(self.stack) >= OOXML_MAX_OPEN_FIELDS:
                        raise _Refuse("ooxml_field_nesting")
                    if self.stack and self.stack[-1]["instr"]:
                        self.stack[-1]["code"].append(_NESTED)
                        self.stack[-1]["len"] += 1
                        if self.stack[-1]["len"] > OOXML_MAX_FIELD_CODE:
                            raise _Refuse("ooxml_field_too_long")
                    self.stack.append({"code": [], "len": 0, "instr": True})
                elif kind == "separate":
                    if self.stack:
                        self.stack[-1]["instr"] = False
                elif kind == "end":
                    if self.stack:
                        self._judge("".join(self.stack.pop()["code"]))
            elif local in ("instrText", "delInstrText"):
                self.in_instr += 1
                if self.in_instr == 1 and not (self.stack and self.stack[-1]["instr"]):
                    self.orphan = {"code": [], "len": 0}
            elif local == "fldSimple":
                instr = attrs.get(uri + " instr", attrs.get("instr", ""))
                if len(instr) > OOXML_MAX_FIELD_CODE:
                    raise _Refuse("ooxml_field_too_long")
                self._judge(instr)

    def end(self, name):
        uri, _, local = name.rpartition(" ")
        self.depth -= 1
        if self.flags:
            a, r, f = self.flags.pop()
            self.alt -= a; self.rev -= r; self.foreign -= f
        if not self.wml_story:
            return
        if uri in NS_WML and local in ("instrText", "delInstrText") and self.in_instr:
            self.in_instr -= 1
            if self.in_instr == 0 and self.orphan is not None:
                self._judge("".join(self.orphan["code"]))
                self.orphan = None

    def chars(self, data):
        if not self.in_instr or not self.wml_story:
            return
        sink = self._sink()
        if sink is None:
            return
        sink["len"] += len(data)
        if sink["len"] > OOXML_MAX_FIELD_CODE:
            raise _Refuse("ooxml_field_too_long")
        sink["code"].append(data)

    def finish(self):
        for f in self.stack:
            self._judge("".join(f["code"]))
        if self.orphan is not None:
            self._judge("".join(self.orphan["code"]))
        self.stack, self.orphan = [], None


def _parse_part(zf, info, handler, budget):
    """Stream one member through expat; DTDs and entity declarations are refused."""
    p = xml.parsers.expat.ParserCreate(namespace_separator=" ")
    p.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def no_dtd(*_a):
        raise _Refuse("ooxml_xml_dtd")

    p.StartDoctypeDeclHandler = no_dtd
    p.EntityDeclHandler = no_dtd
    p.StartElementHandler = handler.start
    p.EndElementHandler = getattr(handler, "end", None)
    p.CharacterDataHandler = getattr(handler, "chars", None)
    with zf.open(info) as m:
        while True:
            chunk = m.read(OOXML_CHUNK)
            budget[0] += len(chunk)
            if budget[0] > OOXML_MAX_XML_BYTES:
                raise _Refuse("ooxml_xml_too_large")
            p.Parse(chunk, not chunk)
            if not chunk:
                break


def _canon_part(raw):
    """One canonical OPC part name for zip members AND manifest keys: percent-decoded,
    lower-case, no leading slash. A backslash or a "." / ".." segment is not a valid part
    name; refusing it stops one name from meaning different parts in different places
    (round 5: "a%5C..%5Cheader.xml" escaped role and field checks)."""
    n = urllib.parse.unquote(raw)
    if "\\" in n:
        raise _Refuse("ooxml_structure_invalid")
    segs = [x for x in n.split("/") if x]
    if not segs or any(x in (".", "..") for x in segs):
        raise _Refuse("ooxml_structure_invalid")
    return "/".join(segs).lower()


class _ContentTypes:
    """[Content_Types].xml in its only valid shape: root Types, children Default/Override,
    each key declared once. Anything else is refused — a second Override must not be able
    to overwrite a prohibited type (review round 3 P3)."""

    def __init__(self):
        self.defaults, self.overrides = {}, {}
        self.root = None
        self.depth = 0
        self.count = 0

    def start(self, name, attrs):
        uri, _, local = name.rpartition(" ")
        self.depth += 1
        if self.root is None:
            self.root = (uri, local)
            if self.root != (NS_CT, "Types"):
                raise _Refuse("ooxml_structure_invalid")
            return
        if self.depth != 2 or uri != NS_CT or local not in ("Default", "Override"):
            raise _Refuse("ooxml_structure_invalid")
        self.count += 1
        if self.count > OOXML_MAX_CT_ENTRIES:
            raise _Refuse("ooxml_structure_invalid")
        ct = attrs.get("ContentType", "").strip().lower()
        key_attr, table = (("Extension", self.defaults) if local == "Default"
                           else ("PartName", self.overrides))
        raw = attrs.get(key_attr, "").strip()
        key = ("/" + _canon_part(raw)) if local == "Override" else raw.lower()
        if not key or not ct or key in table:
            raise _Refuse("ooxml_structure_invalid")
        table[key] = ct

    def end(self, name):
        self.depth -= 1


def ooxml_check(fd, is_docx):
    """
    Phase 2b. Runs on every zip that passed the quotas. A zip with no [Content_Types].xml
    is not an OOXML package and is left to the generic archive rules — unless libmagic
    called it a .docx, which then fails as malformed. For a package: forbidden content
    types, active or external relationships in ANY .rels, and active field codes in ANY
    part whose declared content type is XML (wherever it lives, whatever its extension).
    Anomalies fail CLOSED. Returns (ok: bool, reasons: list[str]).
    """
    reasons = set()
    budget = [0]
    try:
        with reopen(fd) as f, zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
            # A zip without a manifest is not an OPC package: generic archive rules only.
            if not any(urllib.parse.unquote(i.filename).lstrip("/").lower() == "[content_types].xml"
                       for i in infos):
                return (False, ["ooxml_structure_missing"]) if is_docx else (True, [])
            # ONE part-name form for zip members, manifest keys and relationship targets
            # (round 4: mixing forms let "header%20x.xml" escape every check).
            infos = [i for i in infos if not i.is_dir()]
            names = [_canon_part(i.filename) for i in infos]
            if len(set(names)) != len(names):
                reasons.add("ooxml_duplicate_part")
            ct = _ContentTypes()
            _parse_part(zf, infos[names.index("[content_types].xml")], ct, budget)
            if ct.root is None:
                raise _Refuse("ooxml_structure_invalid")
            declared_all = list(ct.defaults.values()) + list(ct.overrides.values())
            for value in declared_all:
                if "macroenabled" in value or value in OOXML_ACTIVE_CONTENT_TYPES:
                    reasons.add("ooxml_active_content_type")
            def declared_type(n):
                leaf = n.rsplit("/", 1)[-1]
                return ct.overrides.get("/" + n) or ct.defaults.get(
                    leaf.rsplit(".", 1)[-1] if "." in leaf else "")

            def is_wml_type(t):
                return bool(t) and t.startswith(OOXML_WML_PART_TYPE_PREFIXES) and t.endswith("xml")

            # 1) every .rels: active/external relationships, and which parts are stories
            present = set(names)
            main_ok = False
            stories = []
            for info, n in zip(infos, names):
                if reasons:
                    break      # decided: no need to build more state from a hostile file
                if not n.endswith(".rels"):
                    continue
                scan = _OoxmlScan(reasons, wml_story=False)
                _parse_part(zf, info, scan, budget)
                # source part of "dir/_rels/name.rels" is "dir/name"; package rels -> root
                src_dir = n[:-len(".rels")].rsplit("/_rels/", 1)[0] if "/_rels/" in n else ""
                for kind, target in scan.story_targets:
                    t = target.strip().split("#", 1)[0].split("?", 1)[0]
                    t = urllib.parse.unquote(t)
                    if "\\" in t:
                        raise _Refuse("ooxml_structure_invalid")
                    path = t.lstrip("/") if t.startswith("/") else (
                        (src_dir + "/" if src_dir else "") + t)
                    parts = []
                    for seg in path.split("/"):
                        if seg in ("", "."):
                            continue
                        if seg == "..":
                            if parts:
                                parts.pop()
                            continue
                        parts.append(seg)
                    stories.append((n, kind, "/".join(parts).lower()))
            for src, kind, story in stories:
                stype = declared_type(story)
                if (src == "_rels/.rels" and kind == "officedocument" and story in present
                        and stype and any(m in stype for m in OOXML_WML_MAIN_TYPE_MARKERS)):
                    main_ok = True
            wml_package = is_docx or main_ok
            for src, kind, story in stories:
                if story not in present:
                    continue
                stype = declared_type(story)
                # a story the reader would open must be declared as WML; a generic, missing
                # or foreign type would hide it from the field scan below
                if not is_wml_type(stype) and (
                        kind in OOXML_WML_ONLY_REL_KINDS or wml_package
                        or stype in OOXML_GENERIC_TYPES or stype is None):
                    reasons.add("ooxml_structure_invalid")
            # a .docx must name an EXISTING main part of a Word main type from the package
            # relationships — a declared-but-absent Override is not a main part (round 3 P3)
            if is_docx and not main_ok:
                reasons.add("ooxml_structure_missing")
            # 2) field semantics in every part typed as a Word story
            for info, n in zip(infos, names):
                if reasons:
                    break
                if n == "[content_types].xml" or n.endswith("/") or n.endswith(".rels"):
                    continue
                if is_wml_type(declared_type(n)):
                    scan = _OoxmlScan(reasons, wml_story=True)
                    _parse_part(zf, info, scan, budget)
                    scan.finish()
    except _Refuse as r:
        reasons.add(str(r))
    except zipfile.BadZipFile:
        return False, ["zip_unreadable_or_corrupt"]
    except xml.parsers.expat.ExpatError:
        reasons.add("ooxml_xml_malformed")
    except Exception:
        reasons.add("ooxml_parse_error")
    return (len(reasons) == 0), sorted(reasons)


def webp_length_reasons(fd, size):
    """Exact-length rule for WebP; no allowance for an odd RIFF pad byte,
    because conformant encoders write even sizes and ambiguity is not allowed."""
    head = os.pread(fd, WEBP_RIFF_HEADER_LEN, 0)
    if len(head) < WEBP_RIFF_HEADER_LEN or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return ["webp_header_malformed"]
    declared = int.from_bytes(head[4:8], "little") + 8
    if size < declared:
        return ["image_truncated"]
    if size > declared:
        return ["trailing_data_after_webp_terminator"]
    return []


def polyglot_check(fd, mime, size):
    """
    Codex review #3: `file` reports only the FIRST format it recognizes, so a
    PNG+ZIP (or GIF+JAR etc.) sails through as image/*. Two detectors:
    1. every non-zip file that ALSO opens as a zip (valid end-of-central-
       directory the way any unzip would find it) is a carrier — reject;
    2. for image types whose spec fixes a terminator, any bytes after it are
       a smuggling channel — reject trailing data, ambiguity is not allowed.
    Returns (ok: bool, reasons: list[str]).
    """
    reasons = []

    if mime not in ARCHIVE_MIME_TYPES:
        try:
            with reopen(fd) as f:
                if zipfile.is_zipfile(f):
                    reasons.append("polyglot_zip_signature")
        except Exception:
            reasons.append("polyglot_probe_error")

    tail_spec = None
    if mime == "image/png":
        tail_spec = PNG_TAIL
    elif mime == "image/jpeg":
        tail_spec = JPEG_TAIL
    elif mime == "image/gif":
        tail_spec = GIF_TAIL
    if tail_spec is not None:
        if size < len(tail_spec):
            reasons.append("image_truncated")
        else:
            tail = os.pread(fd, len(tail_spec), size - len(tail_spec))
            if tail != tail_spec:
                reasons.append(f"trailing_data_after_{mime.split('/')[1]}_terminator")
    elif mime == "image/webp":
        reasons.extend(webp_length_reasons(fd, size))

    return (len(reasons) == 0), reasons


def static_gate(fd, mime, size):
    """
    Phase 2. Whitelist + size limit + polyglot guard + (for archives)
    zip-bomb guard. Returns (status: "ok"|"reject", reasons: list[str]).
    """
    reasons = []

    if not mime_allowed(mime):
        reasons.append(f"mime_not_whitelisted:{mime}")

    if size > MAX_FILE_SIZE_BYTES:
        reasons.append(f"size_{size}_exceeds_{MAX_FILE_SIZE_BYTES}")

    ok, poly_reasons = polyglot_check(fd, mime, size)
    if not ok:
        reasons.extend(poly_reasons)

    if mime in ARCHIVE_MIME_TYPES:
        ok, zip_reasons = zip_bomb_check(fd, size)
        if not ok:
            reasons.extend(zip_reasons)

    # Content rules only on a zip that passed the quotas (reading parts of a bomb is the
    # work the quotas exist to refuse) — and on EVERY such zip: libmagic names a package
    # by member order, so a reordered .docx arrives as plain application/zip.
    if mime in ARCHIVE_MIME_TYPES and not reasons:
        ok, ooxml_reasons = ooxml_check(fd, is_docx=(mime == DOCX_MIME))
        if not ok:
            reasons.extend(ooxml_reasons)

    status = "reject" if reasons else "ok"
    return status, reasons


def av_gate(fd):
    """
    Phase 3 — ClamAV scan. Runs clamscan and maps its exit code: 0 clean,
    1 signature match, anything else an error. We return an explicit token
    ("clean" / "INFECTED:<sig>" / "SKIPPED:...") rather than a bool so the
    label records what actually happened — a timeout or missing binary must
    read as "not scanned", never as a silent clean.
    """
    clamscan = shutil.which("clamscan")
    if not clamscan:
        return "SKIPPED:not-installed"
    try:
        # --stdout so the "FOUND" line lands on stdout (not stderr) for parsing;
        # timeout guards against a pathological input hanging the scanner.
        # The scanner reads our fd (follow=2 because /proc/self/fd is a link).
        out = subprocess.run(
            [clamscan, "--no-summary", "--stdout",
             "--follow-file-symlinks=2", f"/proc/self/fd/{fd}"],
            capture_output=True, text=True, timeout=120, pass_fds=[fd],
        )
    except subprocess.TimeoutExpired:
        return "SKIPPED:timeout"
    if out.returncode == 0:
        return "clean"
    if out.returncode == 1:
        # clamscan prints "<path>: <Signature> FOUND" — pull the signature name
        sig = "unknown"
        for line in out.stdout.splitlines():
            if line.strip().endswith("FOUND"):
                sig = line.rsplit(":", 1)[-1].strip()[:-len(" FOUND")].strip()
                break
        return f"INFECTED:{sig}"
    return f"SKIPPED:error_rc{out.returncode}"


def llm_triage_gate(fd):
    """
    Phase 4 stub — semantic/prompt-injection triage needs an isolated uid
    sandbox (no secrets/network/memory-write) per the design's recursion
    caveat: the triage reader is itself a target for injection, so it may
    only run detonated. Explicit SKIPPED rather than a silent pass.
    """
    return "SKIPPED:no-isolation"


# ---------------------------------------------------------------------------
# Label + journal
# ---------------------------------------------------------------------------

def append_journal(record):
    # append-only, one JSON object per line — cheap to grep/tail, matches the
    # "root-log / Журнал субстрата" transparency pattern from the design doc
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_label(label_path, label):
    # O_NOFOLLOW: a pre-planted symlink at the label path must not turn this
    # write into a write somewhere else
    fd = os.open(label_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(label, f, ensure_ascii=False, indent=2)
        f.write("\n")


def check_file(path):
    path = os.path.abspath(path)
    ts = int(time.time())

    try:
        # O_NONBLOCK: opening a planted FIFO read-only otherwise blocks forever
        # waiting for a writer; regular-file reads ignore the flag entirely
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        reason = "symlink_refused" if e.errno == errno.ELOOP else f"open_failed:{e.strerror}"
        label = {
            "file": path, "sha256": "SKIPPED:not-opened", "size": None,
            "mime": "SKIPPED:not-opened", "static": "reject",
            "static_reasons": [reason], "av": "SKIPPED:static_reject",
            "llm_triage": "SKIPPED:static_reject",
            "content_injection": "UNVERIFIED", "verdict": "reject", "ts": ts,
        }
        append_journal({"ts": ts, "file": path, "sha256": label["sha256"],
                        "verdict": "reject", "reasons": [reason]})
        return label

    try:
        return _check_open_file(path, fd, ts)
    finally:
        os.close(fd)


def _check_open_file(path, fd, ts):
    st = os.fstat(fd)
    size = st.st_size

    if not stat_mod.S_ISREG(st.st_mode):
        static_status, static_reasons = "reject", ["not_regular_file"]
        mime = digest = "SKIPPED:not-regular"
    else:
        # Short-circuit oversize BEFORE any full-file work. We already know the
        # verdict from the size alone; a 20GB input must not be streamed through
        # `file` and sha256 just to be rejected. mime/sha are honestly "not run".
        oversize = size > MAX_FILE_SIZE_BYTES
        mime = "SKIPPED:oversize" if oversize else detect_mime_fd(fd)
        digest = "SKIPPED:oversize" if oversize else sha256_of_fd(fd)
        if oversize:
            static_status = "reject"
            static_reasons = [f"size_{size}_exceeds_{MAX_FILE_SIZE_BYTES}"]
        else:
            static_status, static_reasons = static_gate(fd, mime, size)

    # AV/LLM gates only make sense to run once static passes — no point
    # scanning something we're already rejecting on structural grounds.
    av_status = av_gate(fd) if static_status == "ok" else "SKIPPED:static_reject"
    llm_status = llm_triage_gate(fd) if static_status == "ok" else "SKIPPED:static_reject"

    # Honest label per design phase 5: verdict is never "safe". A known-malware
    # AV hit is a hard reject (design phase 3); everything that merely *passes*
    # the checks we ran is "quarantine" — passed ≠ trusted, never "safe".
    infected = av_status.startswith("INFECTED")
    verdict = "reject" if (static_status == "reject" or infected) else "quarantine"

    # TOCTOU tail: the verdict is about the inode we scanned. If the path no
    # longer names that inode, attaching the label would certify a file we
    # never looked at — reject and say so instead.
    try:
        now = os.lstat(path)
        path_still_ours = (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)
    except OSError:
        path_still_ours = False
    if not path_still_ours:
        static_reasons = static_reasons + ["toctou_path_changed"]
        verdict = "reject"

    label = {
        "file": path,
        "sha256": digest,
        "size": size,
        "mime": mime,
        "static": static_status if path_still_ours else "reject",
        "static_reasons": static_reasons,
        "av": av_status,
        "llm_triage": llm_status,
        "content_injection": "UNVERIFIED",
        "verdict": verdict,
        "ts": ts,
    }

    write_label(path + ".safety.json", label)

    append_journal({
        "ts": ts,
        "file": path,
        "sha256": digest,
        "verdict": verdict,
        "reasons": static_reasons,
    })

    return label


def main():
    if len(sys.argv) != 2:
        print("usage: file_safety_check.py <path>", file=sys.stderr)
        return 2

    target = sys.argv[1]
    if not os.path.lexists(target):
        print(f"error: no such path: {target}", file=sys.stderr)
        return 2

    label = check_file(target)
    print(json.dumps(label, ensure_ascii=False, indent=2))
    return 0 if label["verdict"] == "quarantine" else 1


if __name__ == "__main__":
    sys.exit(main())
