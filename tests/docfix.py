"""Фикстуры документов для проверок движка: docx, минимальный pdf и вспомогательные куски XML.

Всё собирается программно и нейтрально (PRJ-A, «Подсистема-1», example.com): ни одного настоящего
документа в репозитории, ни одного обращения в сеть. Модуль лежит рядом с harness.py и подключается
тем же `from docfix import …` из tests/cases/part_NN.py.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = ('xmlns:w="%s" xmlns:r="%s" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"' % (W, R))


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- куски тела docx

def run(text: str) -> str:
    return '<w:r><w:t xml:space="preserve">%s</w:t></w:r>' % esc(text)


def para(*content: str, style: str = "", num: tuple = ()) -> str:
    """Абзац: `content` — готовые куски (run, ins, del…); строка без тега считается текстом."""
    ppr = ""
    if style or num:
        ppr = "<w:pPr>"
        if style:
            ppr += '<w:pStyle w:val="%s"/>' % style
        if num:
            ppr += '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="%d"/></w:numPr>' % (num[1], num[0])
        ppr += "</w:pPr>"
    body = "".join(c if c.startswith("<") else run(c) for c in content)
    return "<w:p>%s%s</w:p>" % (ppr, body)


def cell(*paras: str, span: int = 1, vmerge: str = "") -> str:
    pr = ""
    if span > 1:
        pr += '<w:gridSpan w:val="%d"/>' % span
    if vmerge:
        pr += '<w:vMerge w:val="restart"/>' if vmerge == "restart" else "<w:vMerge/>"
    return "<w:tc>%s%s</w:tc>" % ("<w:tcPr>%s</w:tcPr>" % pr if pr else "",
                                  "".join(p if p.startswith("<") else para(p) for p in paras) or "<w:p/>")


def row(*cells: str, deleted: bool = False) -> str:
    pr = "<w:trPr><w:del/></w:trPr>" if deleted else ""
    return "<w:tr>%s%s</w:tr>" % (pr, "".join(c if c.startswith("<") else cell(c) for c in cells))


def table(*rows: str) -> str:
    return "<w:tbl>%s</w:tbl>" % "".join(rows)


def ins(text: str) -> str:
    return '<w:ins w:id="1" w:author="Автор-1">%s</w:ins>' % run(text)


def dele(text: str) -> str:
    return ('<w:del w:id="2" w:author="Автор-1"><w:r><w:delText xml:space="preserve">%s</w:delText></w:r></w:del>'
            % esc(text))


def footnote_ref(note_id: int) -> str:
    return '<w:r><w:footnoteReference w:id="%d"/></w:r>' % note_id


def comment_range(comment_id: int, text: str) -> str:
    return ('<w:commentRangeStart w:id="%d"/>%s<w:commentRangeEnd w:id="%d"/>'
            '<w:r><w:commentReference w:id="%d"/></w:r>' % (comment_id, run(text), comment_id, comment_id))


def hyperlink(rid: str, text: str) -> str:
    return '<w:hyperlink r:id="%s">%s</w:hyperlink>' % (rid, run(text))


def field(instr: str, result: str) -> str:
    return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> %s </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>%s<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            % (instr, run(result)))


def image(rid: str, name: str = "Рисунок 1") -> str:
    return ('<w:r><w:drawing><wp:inline><wp:docPr id="1" name="%s"/><a:graphic><a:graphicData>'
            '<a:blip r:embed="%s"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>' % (name, rid))


STYLES = (
    '<w:styles %s>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Custom"><w:name w:val="Мой заголовок"/><w:basedOn w:val="2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Outlined"><w:name w:val="Раздел"/>'
    '<w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
    '</w:styles>' % NS)

NUMBERING = (
    '<w:numbering %s>'
    '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
    '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:numFmt w:val="lowerLetter"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
    '</w:numbering>' % NS)

FOOTNOTES = (
    '<w:footnotes %s>'
    '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
    '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
    '<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Пояснение PRJ-A к тексту.'
    '</w:t></w:r></w:p></w:footnote>'
    '</w:footnotes>' % NS)

COMMENTS = (
    '<w:comments %s><w:comment w:id="0" w:author="Рецензент-1"><w:p><w:r><w:t>Уточнить формулировку у владельца.'
    '</w:t></w:r></w:p></w:comment></w:comments>' % NS)

RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="x/hyperlink" Target="https://example.com/spec" TargetMode="External"/>'
    '<Relationship Id="rId2" Type="x/image" Target="media/image1.png"/>'
    '</Relationships>')

HEADER = '<w:hdr %s><w:p><w:r><w:t>КОЛОНТИТУЛ-СЕКРЕТ</w:t></w:r></w:p></w:hdr>' % NS


def docx(path, body: str, parts: dict = None, standard: bool = True) -> str:
    """Записать docx. `standard` — стили, нумерация, сноски, комментарии, связи и колонтитул из набора выше;
    `parts` — свои части (имя → текст), поверх набора."""
    files = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/document.xml": '<?xml version="1.0" encoding="UTF-8"?><w:document %s><w:body>%s</w:body></w:document>'
                             % (NS, body),
    }
    if standard:
        files.update({"word/styles.xml": STYLES, "word/numbering.xml": NUMBERING,
                      "word/footnotes.xml": FOOTNOTES, "word/comments.xml": COMMENTS,
                      "word/_rels/document.xml.rels": RELS, "word/header1.xml": HEADER,
                      "word/footer1.xml": HEADER})
    files.update(parts or {})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text if isinstance(text, bytes) else text.encode("utf-8"))
    return str(p)


# --------------------------------------------------------------------------- pdf

def pdf(path, pages: list) -> str:
    """Минимальный pdf с текстовым слоем: `pages` — список страниц, страница — список строк (латиница и цифры).
    Шрифт стандартный Helvetica: парсеры текста (pypdf, pdftotext, fitz) читают его без вложений."""
    objs: list = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    cat = add(b"")                 # 1: каталог, заполняется ниже
    tree = add(b"")                # 2: дерево страниц
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for lines in pages:
        text = "BT /F1 12 Tf 14 TL 50 760 Td " + " ".join(
            "(%s) Tj T*" % ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines) + " ET"
        stream = text.encode("latin-1")
        cont = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                    "/Resources << /Font << /F1 %d 0 R >> >> >>" % (tree, cont, font)).encode("ascii"))
        kids.append(page)
    objs[cat - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objs[tree - 1] = ("<< /Type /Pages /Kids [%s] /Count %d >>"
                      % (" ".join("%d 0 R" % k for k in kids), len(kids))).encode("ascii")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, xref)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(out))
    return str(p)
