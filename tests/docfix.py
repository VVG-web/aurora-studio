"""Фикстуры документов для проверок движка: docx, минимальный pdf и вспомогательные куски XML.

Всё собирается программно и нейтрально (PRJ-A, «Подсистема-1», example.com): ни одного настоящего
документа в репозитории, ни одного обращения в сеть. Модуль лежит рядом с harness.py и подключается
тем же `from docfix import …` из tests/cases/part_NN.py.
"""
from __future__ import annotations

import os
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


# --------------------------------------------------------------------------- Word reference fixtures

def _reference_styles(unused: bool = True) -> str:
    extra = ('<w:style w:type="paragraph" w:styleId="Unused"><w:name w:val="Unused Firm"/></w:style>'
             if unused else "")
    return (
        '<w:styles %s>'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
        '</w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
        '<w:name w:val="Default Paragraph Font"/></w:style>'
        '<w:style w:type="table" w:default="1" w:styleId="TableNormal">'
        '<w:name w:val="Normal Table"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="TitleFirm"><w:name w:val="Title Firm"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="a0"/></w:style>'
        '<w:style w:type="character" w:styleId="TitleChar"><w:name w:val="Title Char"/>'
        '<w:basedOn w:val="DefaultParagraphFont"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/>'
        '<w:pPr><w:numPr><w:ilvl w:val="2"/><w:numId w:val="2"/></w:numPr>'
        '<w:outlineLvl w:val="3"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="a0"><w:name w:val="Body Firm"/>'
        '<w:pPr><w:spacing w:after="120"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="BodyChild"><w:name w:val="Body Child"/>'
        '<w:basedOn w:val="a0"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="OutlineOnly"><w:name w:val="Outlined Section"/>'
        '<w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="NumOnly"><w:name w:val="Numbered Custom"/>'
        '<w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="2"/></w:numPr>'
        '<w:outlineLvl w:val="4"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="TableCaptionFirm"><w:name w:val="Table Caption"/>'
        '<w:basedOn w:val="CaptionFirm"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="CaptionFirm"><w:name w:val="Caption Firm"/>'
        '<w:pPr><w:jc w:val="center"/></w:pPr></w:style>'
        '<w:style w:type="character" w:styleId="HyperlinkFirm"><w:name w:val="Hyperlink Firm"/>'
        '<w:basedOn w:val="DefaultParagraphFont"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="FirstParagraphFirm"><w:name w:val="First Paragraph"/>'
        '<w:basedOn w:val="a0"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="CompactFirm"><w:name w:val="Compact"/>'
        '<w:basedOn w:val="a0"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="TOCHeading"><w:name w:val="TOC Heading"/>'
        '<w:basedOn w:val="Normal"/></w:style>'
        '<w:style w:type="table" w:styleId="TableFirm"><w:name w:val="Table Firm"/>'
        '<w:basedOn w:val="TableNormal"/></w:style>'
        '<w:style w:type="character" w:styleId="LinkedChar"><w:name w:val="Linked Char"/>'
        '<w:basedOn w:val="DefaultParagraphFont"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="LinkedParagraph"><w:name w:val="Linked Paragraph"/>'
        '<w:link w:val="LinkedChar"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="NextSource"><w:name w:val="Next Source"/>'
        '<w:next w:val="NextTarget"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="NextTarget"><w:name w:val="Next Target"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="HeaderText"><w:name w:val="Header Text"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="FooterText"><w:name w:val="Footer Text"/></w:style>'
        + extra + '</w:styles>') % NS


def reference_docx(path, *, unused: bool = True, with_styles: bool = True,
                   header_logo: bool = True, body_images: int = 10,
                   large_media: int = 0) -> str:
    """Пример Word для дорожки W1: локализованные id, замыкание, таблица, титул,
    колонтитул с логотипом и «мусор», который не должен попасть в эталон."""
    doc = (
        '<w:document %s><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="TitleFirm"/></w:pPr><w:r><w:rPr><w:rStyle w:val="TitleChar"/>'
        '</w:rPr><w:t>Название примера</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="1"/></w:pPr><w:r><w:t>Первый заголовок</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="a0"/></w:pPr><w:r><w:t>Основной текст PRJ-A.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="BodyChild"/></w:pPr><w:r><w:t>Наследник тела.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="a0"/></w:pPr><w:r><w:t>Ещё основной текст.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="OutlineOnly"/></w:pPr><w:r><w:t>Заголовок по outlineLvl.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="NumOnly"/></w:pPr><w:r><w:t>Заголовок по numPr.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="TOCHeading"/></w:pPr><w:r><w:t>Оглавление</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="CaptionFirm"/></w:pPr><w:r><w:t>Подпись таблицы</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="FirstParagraphFirm"/></w:pPr><w:r><w:t>Первый абзац.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="CompactFirm"/></w:pPr><w:r><w:t>Компактный абзац.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="LinkedParagraph"/></w:pPr>'
        '<w:hyperlink r:id="rIdLink"><w:r><w:rPr><w:rStyle w:val="HyperlinkFirm"/></w:rPr>'
        '<w:t>ссылка</w:t></w:r></w:hyperlink></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="NextSource"/></w:pPr><w:r><w:t>Следующий стиль.</w:t></w:r></w:p>'
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableFirm"/></w:tblPr><w:tr><w:tc><w:p>'
        '<w:r><w:t>Ячейка таблицы</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
        '<w:footerReference w:type="default" r:id="rIdFooter"/>'
        '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" '
        'w:bottom="1134" w:left="1134"/><w:cols w:space="100"/></w:sectPr>'
        '</w:body></w:document>'
        % NS)
    if body_images:
        doc = doc.replace('<w:tbl>', '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Рисунок"/>'
                          '<a:graphic><a:graphicData><a:blip r:embed="rIdImage"/></a:graphicData>'
                          '</a:graphic></wp:inline></w:drawing></w:r></w:p><w:tbl>', 1)
    header = ('<w:hdr %s><w:p><w:pPr><w:pStyle w:val="HeaderText"/></w:pPr>'
              '<w:r><w:t>КОЛОНТИТУЛ</w:t></w:r>%s</w:p></w:hdr>'
              % (NS, ('<w:r><w:drawing><wp:inline><wp:docPr id="2" name="Логотип"/>'
                      '<a:graphic><a:graphicData><a:blip r:embed="rIdLogo"/></a:graphicData>'
                      '</a:graphic></wp:inline></w:drawing></w:r>') if header_logo else ""))
    footer = ('<w:ftr %s><w:p><w:pPr><w:pStyle w:val="FooterText"/></w:pPr>'
              '<w:r><w:t>НИЗ</w:t></w:r></w:p></w:ftr>' % NS)
    doc_rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
        '<Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
        '<Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/spec" TargetMode="External"/>'
        '<Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/body.png"/>'
        '</Relationships>')
    header_rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdLogo" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/logo.png"/>'
        '</Relationships>') if header_logo else (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    settings = ('<w:settings %s><w:zoom w:percent="100"/><w:trackRevisions/>'
                '<w:documentProtection w:edit="readOnly"/><w:attachedTemplate r:id="rIdTemplate"/>'
                '<w:rsids><w:rsidRoot w:val="00112233"/></w:rsids>'
                '<w:compat><w:compatSetting w:name="compatibilityMode" w:val="15"/></w:compat></w:settings>' % NS)
    numbering = (
        '<w:numbering %s><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0">'
        '<w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>' % NS)
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/document.xml": doc,
        "word/_rels/document.xml.rels": doc_rels,
        "word/header1.xml": header,
        "word/_rels/header1.xml.rels": header_rels,
        "word/footer1.xml": footer,
        "word/numbering.xml": numbering,
        "word/settings.xml": settings,
        "word/theme/theme1.xml": '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="PRJ-A"/>',
        "word/fontTable.xml": ('<w:fonts %s><w:font w:name="Arial"/></w:fonts>' % NS),
        "word/customXml/item1.xml": '<root>PRJ-A</root>',
        "word/comments.xml": ('<w:comments %s><w:comment w:id="0" w:author="Рецензент-1">'
                              '<w:p><w:r><w:t>Не переносить.</w:t></w:r></w:p></w:comment></w:comments>' % NS),
    }
    if with_styles:
        parts["word/styles.xml"] = _reference_styles(unused)
    for i in range(1, body_images + 1):
        parts["word/media/body%d.png" % i] = b"\x89PNG\r\n\x1a\nPRJ-A-%d" % i
    if header_logo:
        parts["word/media/logo.png"] = b"\x89PNG\r\n\x1a\nLOGO"
    if large_media:
        parts["word/media/body-large.bin"] = os.urandom(large_media)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(p)


def large_reference_docx(path, size: int = 11_100_000) -> str:
    """Пример больше 5 МБ: большой медиафайл тела, который не должен читаться."""
    return reference_docx(path, large_media=size)


def reference_base(path) -> str:
    """Нейтральная основа вместо pandoc: один default на тип и канонические имена."""
    styles = (
        '<w:styles %s>'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
        '</w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:latentStyles w:defLockedState="0" w:defUIPriority="99"/>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
        '<w:name w:val="Default Paragraph Font"/></w:style>'
        '<w:style w:type="table" w:default="1" w:styleId="TableNormal">'
        '<w:name w:val="Normal Table"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading5"><w:name w:val="heading 5"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading6"><w:name w:val="heading 6"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="Caption"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="FirstParagraph"><w:name w:val="First Paragraph"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Compact"><w:name w:val="Compact"/></w:style>'
        '<w:style w:type="table" w:styleId="Table"><w:name w:val="Table"/></w:style>'
        '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/></w:style>'
        '</w:styles>' % NS)
    return docx(path, '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>',
                parts={"word/styles.xml": styles}, standard=False)


def broken_zip(path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not a zip")
    return str(p)


def broken_xml_docx(path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types>")
        z.writestr("_rels/.rels", "<Relationships>")
        z.writestr("word/document.xml", "<w:document>")
    return str(p)


def docx_part_names(path) -> list:
    with zipfile.ZipFile(path) as z:
        return sorted(z.namelist())


def replace_part(path, part: str, old: str, new: str) -> str:
    """Заменить фрагмент XML-части в готовом docx, не меняя остальные байты."""
    p = Path(path)
    with zipfile.ZipFile(p) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    data = parts[part].decode("utf-8")
    if old not in data:
        raise AssertionError("в %s нет фрагмента %r" % (part, old))
    parts[part] = data.replace(old, new, 1).encode("utf-8")
    tmp = p.with_name("." + p.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in parts.items():
            z.writestr(name, value)
    os.replace(tmp, p)
    return str(p)


def set_part(path, part: str, data) -> str:
    """Заменить XML-часть целиком, сохранив остальные части пакета."""
    p = Path(path)
    with zipfile.ZipFile(p) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    parts[part] = data if isinstance(data, bytes) else data.encode("utf-8")
    tmp = p.with_name("." + p.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in parts.items():
            z.writestr(name, value)
    os.replace(tmp, p)
    return str(p)


def numbered_reference_docx(path) -> str:
    """Пример, где нумерация ссылается на стили через w:lvl/w:pStyle.

    Heading1 связан с уровнем 1, NumPara и NumLonely — с уровнями обычной
    нумерации. NumLonely в теле не встречается, поэтому попадает в карту только
    из numbering.xml.
    """
    reference_docx(path)
    replace_part(path, "word/styles.xml",
                 '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/>'
                 '<w:pPr><w:numPr><w:ilvl w:val="2"/><w:numId w:val="2"/></w:numPr>'
                 '<w:outlineLvl w:val="3"/></w:pPr></w:style>',
                 '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>')
    replace_part(path, "word/styles.xml", "</w:styles>",
                 '<w:style w:type="paragraph" w:styleId="NumPara"><w:name w:val="Numbered Para"/></w:style>'
                 '<w:style w:type="paragraph" w:styleId="NumLonely"><w:name w:val="Numbered Lonely"/></w:style>'
                 "</w:styles>")
    numbering = (
        '<w:numbering %s>'
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:pStyle w:val="1"/></w:lvl></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:pStyle w:val="NumPara"/></w:lvl></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:pStyle w:val="NumLonely"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        '<w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num>'
        '</w:numbering>' % NS)
    set_part(path, "word/numbering.xml", numbering)
    para = ('<w:p><w:pPr><w:pStyle w:val="NumPara"/></w:pPr>'
            '<w:r><w:t>Нумерованный абзац.</w:t></w:r></w:p>')
    replace_part(path, "word/document.xml", "<w:sectPr>", para + "<w:sectPr>")
    return str(path)


def _mutated_reference(path, part: str, old: str, new: str) -> str:
    reference_docx(path)
    return replace_part(path, part, old, new)


def unknown_style_docx(path) -> str:
    return _mutated_reference(path, "word/styles.xml",
                        '<w:style w:type="paragraph" w:styleId="a0">',
                        '<w:style w:type="paragraph" w:styleId="a0"><w:unknownFirm/>')


def settings_reference_docx(path) -> str:
    """Пример с настройками из реального Word: embedSystemFonts, stylePaneFormatFilter,
    useFELayout, атрибуты clrSchemeMapping и mc:Ignorable. Всё это не должно попасть
    в эталон, а нужные zoom, compatSetting и defaultTabStop должны остаться."""
    reference_docx(path)
    settings = (
        '<w:settings %s xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        'mc:Ignorable="w14">'
        '<w:zoom w:percent="100"/>'
        '<w:embedSystemFonts/>'
        '<w:stylePaneFormatFilter w:val="0004" w:allStyles="0"/>'
        '<w:defaultTabStop w:val="720"/>'
        '<w:compat><w:compatSetting w:name="compatibilityMode" w:val="15"/>'
        '<w:useFELayout/></w:compat>'
        '<w:clrSchemeMapping w:bg1="light1"/>'
        '<w:rsids><w:rsidRoot w:val="00112233"/></w:rsids>'
        '<w:trackRevisions/>'
        '</w:settings>' % NS)
    return set_part(path, "word/settings.xml", settings)


def russian_numbering_docx(path, mc="w14", extra_ns="") -> str:
    """Пример с numbering из реального Word: tabs/tab, ind с hanging и firstLine,
    numFmt russianUpper, rFonts hint/theme, numIdMacAtCleanup и mc:Ignorable."""
    reference_docx(path)
    numbering = (
        '<w:numbering %s xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        '%s mc:Ignorable="%s">'
        '<w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0" w:tplc="1" w:tentative="1">'
        '<w:start w:val="1"/><w:numFmt w:val="russianUpper"/>'
        '<w:pPr><w:tabs><w:tab w:val="num" w:pos="1492" w:leader="none"/></w:tabs>'
        '<w:ind w:left="1492" w:hanging="360" w:firstLine="0"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="Times New Roman" w:hint="default" '
        'w:asciiTheme="minorHAnsi"/><w:caps w:val="0"/><w:smallCaps w:val="0"/></w:rPr>'
        '</w:lvl></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:numFmt w:val="russianLower"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        '<w:numIdMacAtCleanup w:val="1"/>'
        '</w:numbering>' % (NS, extra_ns, mc))
    return set_part(path, "word/numbering.xml", numbering)


def bad_mc_numbering_docx(path, mc: str, extra_ns: str = "") -> str:
    """Тот же пример, но mc:Ignorable называет необъявленный или небезопасный префикс."""
    return russian_numbering_docx(path, mc=mc, extra_ns=extra_ns)


def word_styles_docx(path) -> str:
    """Пример с оформлением из реального Word: bidi, tblInd, themeColor, rsid,
    несколько sectPr с разрывом раздела, numId без ilvl и взаимные link."""
    reference_docx(path)
    replace_part(path, "word/styles.xml",
                 '<w:style w:type="paragraph" w:styleId="a0"><w:name w:val="Body Firm"/>'
                 '<w:pPr><w:spacing w:after="120"/></w:pPr></w:style>',
                 '<w:style w:type="paragraph" w:styleId="a0"><w:name w:val="Body Firm"/>'
                 '<w:rsid w:val="00112233"/>'
                 '<w:pPr><w:spacing w:after="120"/></w:pPr>'
                 '<w:rPr><w:lang w:val="en-US" w:bidi="ar-SA"/>'
                 '<w:color w:val="000000" w:themeColor="text1"/></w:rPr></w:style>')
    replace_part(path, "word/styles.xml",
                 '<w:style w:type="table" w:styleId="TableFirm"><w:name w:val="Table Firm"/>'
                 '<w:basedOn w:val="TableNormal"/></w:style>',
                 '<w:style w:type="table" w:styleId="TableFirm"><w:name w:val="Table Firm"/>'
                 '<w:basedOn w:val="TableNormal"/>'
                 '<w:tblPr><w:tblInd w:w="120" w:type="dxa"/>'
                 '<w:tblCellMar><w:top w:w="20" w:type="dxa"/>'
                 '<w:left w:w="20" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>')
    replace_part(path, "word/styles.xml", "</w:styles>",
                 '<w:style w:type="paragraph" w:styleId="NoIlvl"><w:name w:val="No Ilvl"/>'
                 '<w:pPr><w:numPr><w:numId w:val="2"/></w:numPr></w:pPr></w:style>'
                 '<w:style w:type="paragraph" w:styleId="LinkA"><w:name w:val="Link A"/>'
                 '<w:link w:val="LinkB"/></w:style>'
                 '<w:style w:type="character" w:styleId="LinkB"><w:name w:val="Link B"/>'
                 '<w:link w:val="LinkA"/></w:style>'
                 "</w:styles>")
    replace_part(path, "word/document.xml",
                 '<w:p><w:pPr><w:pStyle w:val="CaptionFirm"/></w:pPr><w:r><w:t>Подпись таблицы</w:t></w:r></w:p>',
                 '<w:p><w:pPr><w:pStyle w:val="NoIlvl"/></w:pPr><w:r><w:t>Без ilvl.</w:t></w:r></w:p>'
                 '<w:p><w:pPr><w:pStyle w:val="LinkA"/></w:pPr><w:r><w:t>Взаимная ссылка.</w:t></w:r></w:p>')
    replace_part(path, "word/document.xml", "<w:sectPr>",
                 '<w:p><w:pPr><w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                 '<w:cols w:space="1440"/></w:sectPr></w:pPr></w:p><w:sectPr>')
    replace_part(path, "word/document.xml", '<w:cols w:space="100"/>',
                 '<w:cols w:space="1440"/>')
    return str(path)


def bad_sectpr_docx(path) -> str:
    return _mutated_reference(path, "word/document.xml",
                        '<w:pgSz w:w="11906" w:h="16838"/>',
                        '<w:pgSz w:w="100" w:h="100"/>')


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
