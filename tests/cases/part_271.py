"""part_271 — титул Word: первый раздел, подстановки, allowlist и пример.

Проверки не поднимают сервер: пример собирается программно в docx, модуль
`docx_title` работает как в экспорте — с теми же XML-пределами и allowlist.
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from harness import KIT, test

for _path in (str(KIT / "scripts"), str(KIT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from docfix import docx  # noqa: E402
import docx_text  # noqa: E402
import docx_title as DT  # noqa: E402

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W = "{%s}" % W_NS
NS = ('xmlns:w="%s" xmlns:w14="%s" xmlns:r="%s"'
      % (W_NS, W14_NS, R_NS))


def _p(text: str, style: str = "", extra: str = "", sect: str = "",
       attrs: str = "") -> str:
    ppr = ""
    if style or extra or sect:
        ppr = "<w:pPr>"
        if style:
            ppr += '<w:pStyle w:val="%s"/>' % style
        ppr += extra + sect + "</w:pPr>"
    return ('<w:p%s>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>'
            % (attrs, ppr, text))


def _document(body: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><w:document %s><w:body>%s'
            "</w:body></w:document>" % (NS, body))


STYLES = (
    '<w:styles %s>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Custom"><w:name w:val="Custom"/>'
    '<w:basedOn w:val="Heading1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Unused"><w:name w:val="Unused"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Broken"><w:name w:val="Broken"/>'
    '<w:basedOn w:val="Missing"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Foreign"><w:name w:val="Foreign"/>'
    '<w:altChunk w:val="evil"/></w:style>'
    "</w:styles>" % NS
)
NUMBERING = (
    '<w:numbering %s><w:abstractNum w:abstractNumId="0">'
    '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>' % NS
)


def _example(tmp: Path, *, title: str = "Титул", sentinel: bool = False,
             table: bool = False) -> Path:
    """Первый раздел с данными, второй — секрет; в sectPr — колонтитулы."""
    body = [
        _p(title, "Title"),
        '<w:p w14:paraId="A1" w14:textId="A2"><w:r><w:t>'
        "Иванов Иван Иванович</w:t></w:r></w:p>",
        _p("Генеральный директор"),
        _p("01.02.2025"),
        _p("№ 42-2025"),
        _p("Москва"),
        _p("2025"),
        _p("Договор поставки оборудования"),
    ]
    if sentinel:
        body.append(_p("[ПРИМЕР] служебная строка"))
    if table:
        body.append("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>ТАБЛИЦА</w:t></w:r></w:p>"
                    "</w:tc></w:tr></w:tbl>")
    body.append(_p(
        "Заголовок раздела", "Heading1",
        '<w:outlineLvl w:val="0"/><w:numPr><w:ilvl w:val="0"/>'
        '<w:numId w:val="1"/></w:numPr>',
        '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
        '<w:footerReference w:type="default" r:id="rIdFooter"/>'
        '<w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'))
    body.append(_p("СЕКРЕТ ВТОРОГО РАЗДЕЛА"))
    path = tmp / "Templates" / "Word" / "OPZ-example.docx"
    docx(path, "\n".join(body), parts={
        "word/document.xml": _document("\n".join(body)),
        "word/styles.xml": STYLES,
        "word/numbering.xml": NUMBERING,
        "word/header1.xml": '<w:hdr %s><w:p><w:r><w:t>HEADER-SENTINEL</w:t>'
                            "</w:r></w:p></w:hdr>" % NS,
        "word/footer1.xml": '<w:ftr %s><w:p><w:r><w:t>FOOTER-SENTINEL</w:t>'
                            "</w:r></w:p></w:ftr>" % NS,
    })
    return path


@test
def test_s4_title_core_uses_engine_utc_time(tmp: Path):
    """Timestamp docProps/core.xml берётся из SOURCE_DATE_EPOCH через AC.utc_stamp,
    а не через местное время машины."""
    old = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = "1795478400"
    try:
        core = DT._core_xml().decode("utf-8")
    finally:
        if old is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = old
    assert DT.AC.utc_stamp(1795478400) == "2026-11-24T00:00:00Z"
    assert "2026-11-24T00:00:00Z" in core


def _plain_title(tmp: Path) -> Path:
    path = tmp / "plain-title.docx"
    docx(path, _p("Титул без подстановок"))
    return path


def _header(tmp: Path, **values) -> Path:
    fields = {
        "fio": "Петров Пётр Петрович",
        "position": "Технический директор",
        "date": "03.03.2026",
        "number": "99-2026",
        "city": "Казань",
        "year": "2026",
        "contract": "Договор поставки",
    }
    fields.update(values)
    text = "---\n" + "".join("%s: %s\n" % item for item in fields.items() if item[1] is not None)
    path = tmp / "OPZ.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "---\n\n# Документ\n", encoding="utf-8")
    return path


@test
def test_s4_title_extracts_first_section_and_keeps_only_allowed_xml(tmp: Path):
    """Первый раздел, pPr/outlineLvl, sectPr и колонтитулы примера не утекают."""
    example = _example(tmp)
    built = DT.extract(example)
    xml = built["template_xml"].decode("utf-8")
    assert built["placeholders"] == ["fio", "position", "date", "number", "city",
                                     "year", "contract"], built
    assert built["issues"] == [], built["issues"]
    assert "СЕКРЕТ ВТОРОГО РАЗДЕЛА" not in xml
    assert "HEADER-SENTINEL" not in xml and "FOOTER-SENTINEL" not in xml
    assert "w14:" not in xml and "sectPr" not in xml
    assert "w:outlineLvl" in xml and 'w:val="Heading1"' in xml
    root = ET.fromstring(built["template_xml"])
    styles = [node.get(W + "val") for node in root.iter(W + "pStyle")]
    assert "Heading1" in styles, styles
    assert any(node.tag == W + "outlineLvl" for node in root.iter())

    out = tmp / "Templates" / "opz_title.docx"
    result = DT.save(example, out, kind="opz", apply=True)
    assert result["status"] == "written" and out.is_file(), result
    text = docx_text.convert(str(out))
    assert text and "{{fio}}" in text and "СЕКРЕТ" not in text, text
    with zipfile.ZipFile(out) as z:
        package_xml = z.read("word/document.xml").decode("utf-8")
        styles = z.read("word/styles.xml").decode("utf-8")
        names = set(z.namelist())
        content_types = z.read("[Content_Types].xml").decode("utf-8")
        package_rels = z.read("_rels/.rels").decode("utf-8")
        core = z.read("docProps/core.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "Иванов" not in package_xml and "СЕКРЕТ" not in package_xml
    assert "sectPr" not in package_xml and "w14:" not in package_xml
    assert "outlineLvl" in styles
    assert "Title" in styles and "Heading1" in styles
    assert "Unused" not in styles and "Broken" not in styles
    assert "Missing" not in styles and "altChunk" not in styles
    assert "styles.xml" in rels and "TargetMode" not in rels
    assert "docProps/core.xml" in names and "Aurora Studio" in core
    assert "core-properties" in content_types and "core-properties" in package_rels
    assert not any(name.startswith(("word/header", "word/footer")) for name in names)


@test
def test_s4_title_allowlist_blocks_unsupported_xml_and_sentinel_data(tmp: Path):
    """Таблица и sentinel-строка в титуле блокируют шаблон, а не вырезаются молча."""
    table_example = _example(tmp, table=True)
    table = DT.extract(table_example)
    assert any(item["code"] == "docx-title-unsupported" for item in table["issues"]), table
    out = tmp / "Templates" / "bad_title.docx"
    blocked = DT.save(table_example, out, kind="bad", apply=True)
    assert blocked["status"] == "blocked" and not out.exists(), blocked

    sentinel_example = _example(tmp / "sentinel", sentinel=True)
    sentinel = DT.extract(sentinel_example)
    assert any(item["code"] == "docx-example-data" for item in sentinel["issues"]), sentinel
    assert DT.save(sentinel_example, tmp / "sentinel-title.docx", apply=True)["status"] == "blocked"


@test
def test_s4_title_fills_header_and_checks_fields_and_blocks_missing(tmp: Path):
    """Подстановки берутся из шапки и title_fields; пустая блокирует проверку."""
    example = _example(tmp)
    template = tmp / "Templates" / "opz_title.docx"
    DT.save(example, template, kind="opz", apply=True)
    header = _header(tmp)
    built = DT.extract(example, header=header)
    assert built["missing"] == [] and built["filled"] == built["placeholders"], built
    filled = built["filled_xml"].decode("utf-8")
    assert "Петров Пётр Петрович" in filled and "{{" not in filled
    assert DT.check(example=example, title=template, header=header) == []

    partial = _header(tmp / "partial", city=None)
    issues_missing = DT.check(example=example, title=template, header=partial, checks={})
    assert any(item["code"] == "docx-placeholder" for item in issues_missing), issues_missing
    # city берётся из title_fields, если в шапке его нет.
    checks = {"title_fields": {"city": "Самара"}}
    built = DT.extract(example, header=partial, checks=checks)
    assert built["missing"] == [] and built["values"]["city"] == "Самара", built
    assert "Самара" in built["filled_xml"].decode("utf-8")
    escaped = DT.extract(example, header=_header(tmp / "escaped",
                                                 fio="Петров <&> Пётр"))
    escaped_xml = escaped["filled_xml"]
    assert b"<&>" not in escaped_xml and b"&lt;&amp;&gt;" in escaped_xml, escaped_xml
    assert "Петров <&> Пётр" in ET.fromstring(escaped_xml).itertext()
    issues = DT.check(example=example, title=template, header=_header(
        tmp / "missing", city=None), checks={})
    assert any(item["code"] == "docx-placeholder" for item in issues), issues


@test
def test_s4_title_ready_docx_passes_check_and_render_without_example(tmp: Path):
    """Готовый `.docx` проверяется и рендерится без примера: заглушка `example=None`
    не должна блокировать вызовы из `ship_doc`."""
    example = _example(tmp)
    title = tmp / "Templates" / "ready_title.docx"
    assert DT.save(example, title, kind="opz", apply=True)["status"] == "written"
    header = _header(tmp)
    assert DT.check(example=None, title=title, header=header) == []
    rendered = DT.render(title, header=header)
    assert rendered["filled_docx"] and rendered["docx"] == rendered["filled_docx"]
    assert "{{" not in rendered["filled_xml"].decode("utf-8")


@test
def test_s4_title_requires_title_with_example_and_warns_without_it(tmp: Path):
    """Есть пример без титула — block; примера нет — предупреждение."""
    example = _example(tmp)
    required = DT.check(example=example, title=None)
    assert any(item["level"] == "block" and item["code"] == "docx-title"
               for item in required), required
    optional = DT.check(example=None, title=None)
    assert optional and not any(item["level"] == "block" for item in optional), optional
    plain = _plain_title(tmp)
    no_placeholders = DT.check(example=example, title=plain)
    assert any(item["code"] == "docx-title" for item in no_placeholders), no_placeholders
    plain_built = DT.extract(plain)
    assert any(item["code"] == "docx-title" for item in plain_built["issues"]), plain_built
    assert DT.save(plain, tmp / "plain-title-template.docx",
                   apply=True)["status"] == "blocked"
    optional_plain = DT.check(example=None, title=plain)
    assert any(item["code"] == "docx-example-data" for item in optional_plain), optional_plain


@test
def test_s4_title_apply_is_idempotent_and_xml_is_valid(tmp: Path):
    """Повторный --apply не перезаписывает шаблон; XML-вариант разбирается."""
    example = _example(tmp)
    out = tmp / "Templates" / "opz_title.docx"
    first = DT.save(example, out, kind="opz", apply=True)
    assert first["status"] == "written" and first["sha256"]
    digest = first["sha256"]
    second = DT.save(example, out, kind="opz", apply=True)
    assert second["status"] == "exists" and not second["written"]
    assert DT._sha(out.read_bytes()) == digest
    xml_path = tmp / "Templates" / "opz_title.xml"
    xml_result = DT.save(example, xml_path, kind="opz", apply=True, xml=True)
    assert xml_result["status"] == "written"
    root = ET.fromstring(xml_path.read_bytes())
    assert root.tag == W + "document"
    assert not any(node.tag == W + "sectPr" for node in root.iter())


@test
def test_s4_title_fields_schema_and_checks_contract(tmp: Path):
    """title_fields с плохим ключом или управляющим символом отвергается."""
    example = _example(tmp)
    try:
        DT.extract(example, checks={"title_fields": {"fio-1": "X"}})
    except DT.DocxTitleRefused:
        pass
    else:
        raise AssertionError("плохой ключ title_fields принят")
    try:
        DT.extract(example, checks={"title_fields": {"fio": "bad\x01value"}})
    except DT.DocxTitleRefused:
        pass
    else:
        raise AssertionError("управляющий символ title_fields принят")


@test
def test_s4_title_cli_apply_writes_template_once(tmp: Path):
    """CLI --apply пишет шаблон, а повторный запуск не перезаписывает его."""
    example = _example(tmp)
    out = tmp / "Templates" / "opz_title.docx"
    script = KIT / "scripts" / "docx_title.py"
    first = subprocess.run([sys.executable, str(script), "--example", str(example),
                            "--kind", "opz", "--out", str(out), "--apply", "--json"],
                           capture_output=True, text=True, encoding="utf-8", check=False)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert out.is_file()
    digest = out.read_bytes()
    second = subprocess.run([sys.executable, str(script), "--example", str(example),
                             "--kind", "opz", "--out", str(out), "--apply", "--json"],
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert out.read_bytes() == digest


@test
def test_s4_title_rejects_dangling_styles_and_external_relationships(tmp: Path):
    """Стиль на несуществующий styleId и внешняя связь блокируются."""
    broken_styles = (
        '<w:styles %s><w:style w:type="paragraph" w:styleId="Broken">'
        '<w:name w:val="Broken"/><w:basedOn w:val="Missing"/></w:style>'
        "</w:styles>" % NS
    )
    title = tmp / "Broken-title.docx"
    docx(title, _p("Титул {{fio}}", "Broken"), parts={
        "word/document.xml": _document(_p("Титул {{fio}}", "Broken")),
        "word/styles.xml": broken_styles,
    })
    issues = DT.check(example=None, title=title, fields={"fio": "Иванов И.И."})
    assert any("отсутствующий" in item["message"] for item in issues), issues

    parts = {
        "word/document.xml": _document(
            '<w:p><w:hyperlink r:id="rId9"><w:r><w:t>внешняя</w:t>'
            "</w:r></w:hyperlink></w:p>").encode("utf-8"),
        "word/_rels/document.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId9" Type="x/hyperlink" '
            'Target="https://example.com" TargetMode="External"/></Relationships>'
        ).encode("utf-8"),
    }
    rel_issues = DT.relationship_issues(parts)
    assert any(item["code"] == "docx-external-rel" for item in rel_issues), rel_issues


if __name__ == "__main__":
    raise SystemExit("part_271 запускается через tests/run_tests.py")
