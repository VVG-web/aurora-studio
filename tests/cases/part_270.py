"""Проверки W1 R5: эталон Word, стили, allowlist и детерминизм (1.231.19).

Каркас и помощники — tests/harness.py. Проверки описывают только R5 W1:
применённые стили и замыкание, карту styleId, outlineLvl, numPr, пустой
документ с sectPr, docProps/core.xml, allowlist и отсутствие колонтитулов
примера.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from docfix import (
    bad_mc_numbering_docx,
    bad_sectpr_docx,
    broken_xml_docx,
    broken_zip,
    docx_part_names,
    large_reference_docx,
    numbered_reference_docx,
    reference_base,
    reference_docx,
    replace_part,
    russian_numbering_docx,
    settings_reference_docx,
    unknown_style_docx,
    word_styles_docx,
)
from harness import KIT, SCRIPTS, test  # noqa: F401

for _p in (str(SCRIPTS), str(KIT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import docx_reference as DR  # noqa: E402

W = "{%s}" % DR.W_NS
DATE = "2026-11-24T00:00:00Z"


def _build(tmp: Path, name: str = "ex", date: str = DATE, **kwargs) -> dict:
    ex = reference_docx(tmp / (name + ".docx"))
    base = reference_base(tmp / "base.docx")
    return DR.build(ex, tmp / ("out-" + name), "opz", base=base, version="1.231.19",
                    generated_at=date, **kwargs)


def _parts(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def _styles(path) -> dict:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/styles.xml"))
    return {el.get(W + "styleId"): el for el in root if el.tag == W + "style"}


def _child(el, name: str):
    return next((ch for ch in el if ch.tag == W + name), None)


def _outline(el):
    ppr = _child(el, "pPr")
    if ppr is None:
        return None
    node = _child(ppr, "outlineLvl")
    return None if node is None else node.get(W + "val")


def _numpr(el):
    ppr = _child(el, "pPr")
    node = _child(ppr, "numPr") if ppr is not None else None
    if node is None:
        return None
    return (_child(node, "ilvl").get(W + "val") if _child(node, "ilvl") is not None else None,
            _child(node, "numId").get(W + "val") if _child(node, "numId") is not None else None)


def _chain(styles: dict, sid: str) -> list:
    out = []
    while sid and sid in styles and sid not in out:
        out.append(sid)
        based = _child(styles[sid], "basedOn")
        sid = based.get(W + "val") if based is not None else ""
    return out


def _rels(path, part="_rels/.rels") -> list:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(part))
    return [(el.get("Id"), el.get("Type"), el.get("Target")) for el in root
            if el.tag.endswith("Relationship")]


def _real_example():
    """Настоящий пример Word, если он есть рядом с китом или задан переменной."""
    candidates = []
    env = os.environ.get("AURORA_REAL_DOCX")
    if env:
        candidates.append(Path(env))
    candidates.append(KIT / "References/2.2_Как_есть.docx")
    return next((path for path in candidates if path.is_file()), None)


@test
def test_w1_reference_builds_r5_package_and_meta(tmp: Path):
    """Сборка даёт ровно разрешённые части, годный zip и метаданные R5 W1."""
    meta = _build(tmp)
    assert meta["status"] == "ready" and meta["validation"] == [], meta
    assert DR.validate(meta["path"]) == []
    names = docx_part_names(meta["path"])
    assert names == sorted([
        "[Content_Types].xml", "_rels/.rels", "docProps/core.xml",
        "word/_rels/document.xml.rels", "word/document.xml", "word/fontTable.xml",
        "word/numbering.xml", "word/settings.xml", "word/styles.xml",
        "word/theme/theme1.xml"])
    assert meta["generated_at"] == DATE and meta["source_date_epoch"] == 1795478400, meta
    assert meta["parts"] == names and meta["reference_sha"], meta
    assert Path(meta["path"]).stat().st_size <= DR.MAX_REFERENCE_BYTES


@test
def test_w1_reference_accepts_real_11mb_example(tmp: Path):
    """Реальный пример 11 МБ проходит styles/sectPr, эталон остаётся пустым и малым."""
    real = _real_example()
    if real is None:
        return
    base = reference_base(tmp / "base.docx")
    meta = DR.build(str(real), tmp / "out-real", "opz", base=base,
                    version="1.231.19", generated_at=DATE)
    assert meta["status"] == "ready", meta
    assert meta["validation"] == [], meta["validation"]
    assert DR.validate(meta["path"]) == []
    assert os.path.getsize(meta["path"]) <= 5_000_000
    names = docx_part_names(meta["path"])
    assert not [name for name in names if "/media/" in name or "customXml" in name], names
    with zipfile.ZipFile(meta["path"]) as z:
        doc = ET.fromstring(z.read("word/document.xml"))
    body = doc.find(W + "body")
    assert [ch.tag for ch in body] == [W + "sectPr"], [ch.tag for ch in body]
    assert not (doc.text or "").strip()


@test
def test_w1_reference_does_not_copy_example_header_or_media(tmp: Path):
    """Текст, логотип и картинки колонтитула примера не попадают в эталон."""
    meta = _build(tmp)
    parts = _parts(meta["path"])
    assert not [name for name in parts if "header" in name or "footer" in name
                or "media/" in name or "comments" in name or "customXml" in name], parts
    joined = b"\n".join(parts.values())
    for marker in ("КОЛОНТИТУЛ", "НИЗ", "LOGO", "PRJ-A"):
        assert marker.encode("utf-8") not in joined, marker
    doc = ET.fromstring(parts["word/document.xml"])
    assert [ch.tag for ch in doc.find(W + "body")] == [W + "sectPr"]
    assert not [el for el in doc.iter()
                if el.tag in (W + "headerReference", W + "footerReference")]


@test
def test_w1_reference_only_applied_styles_and_closure(tmp: Path):
    """Неприменённые стили не переносятся, basedOn/link/next замкнуты."""
    meta = _build(tmp)
    styles = _styles(meta["path"])
    assert "Unused" not in styles and "TableCaptionFirm" not in styles, sorted(styles)
    assert _chain(styles, "Firm_BodyChild") == ["Firm_BodyChild", "Firm_a0"]
    assert "Firm_NextTarget" in styles and "Firm_LinkedChar" in styles
    assert "HeaderText" not in styles and "FooterText" not in styles


@test
def test_w1_reference_localized_heading_outline_and_reasons(tmp: Path):
    """Имя важнее numPr, numPr важнее outlineLvl; у Heading1 outlineLvl=0."""
    meta = _build(tmp)
    mapping = meta["mapping"]
    assert mapping["Heading1"]["source"] == "1" and mapping["Heading1"]["reason"] == "name", mapping
    assert mapping["Heading2"]["source"] == "NumOnly" and mapping["Heading2"]["reason"] == "numPr", mapping
    assert mapping["Heading3"]["source"] == "OutlineOnly" and mapping["Heading3"]["reason"] == "outlineLvl", mapping
    styles = _styles(meta["path"])
    assert _outline(styles["Heading1"]) == "0"
    assert _outline(styles["Firm_1"]) == "0"
    assert _chain(styles, "Heading1") == ["Heading1", "Firm_1"]


@test
def test_w1_reference_numbered_heading_has_numpr_and_numid(tmp: Path):
    """Нумерованный заголовок сохраняет numPr/ilvl/numId, numId есть в numbering."""
    meta = _build(tmp)
    styles = _styles(meta["path"])
    assert _numpr(styles["Firm_1"]) == ("0", "2"), _numpr(styles["Firm_1"])
    assert _numpr(styles["Firm_NumOnly"]) == ("1", "2"), _numpr(styles["Firm_NumOnly"])
    assert _outline(styles["Firm_NumOnly"]) == "1"
    with zipfile.ZipFile(meta["path"]) as z:
        numbering = ET.fromstring(z.read("word/numbering.xml"))
    nums = {el.get(W + "numId") for el in numbering if el.tag == W + "num"}
    abstracts = {el.get(W + "abstractNumId") for el in numbering if el.tag == W + "abstractNum"}
    assert {"1", "2"} <= nums, nums
    assert {"0", "1"} <= abstracts, abstracts


@test
def test_w1_numbering_pstyle_map_keeps_heading_and_body(tmp: Path):
    """w:lvl/w:pStyle входит в карту: Heading1 остаётся заголовком, NumPara телом."""
    ex = numbered_reference_docx(tmp / "numbered.docx")
    meta = DR.build(ex, tmp / "out-numbered", "opz", base=reference_base(tmp / "base.docx"),
                    version="1.231.19", generated_at=DATE)
    assert DR.validate(meta["path"]) == []
    assert meta["mapping"]["Heading1"]["source"] == "1", meta["mapping"]
    assert meta["mapping"]["BodyText"]["source"] == "a0", meta["mapping"]
    assert meta["style_map"]["1"] == "Firm_1", meta["style_map"]
    assert meta["style_map"]["NumLonely"] == "Firm_NumLonely", meta["style_map"]
    styles = _styles(meta["path"])
    for sid in ("Firm_1", "Firm_NumPara", "Firm_NumLonely"):
        assert sid in styles, sid
    assert _outline(styles["Heading1"]) == "0"
    assert _numpr(styles["Heading1"]) is None
    assert _numpr(styles["Firm_NumPara"]) is None
    with zipfile.ZipFile(meta["path"]) as z:
        numbering = ET.fromstring(z.read("word/numbering.xml"))
    refs = {el.get(W + "val") for el in numbering.iter() if el.tag == W + "pStyle"}
    assert refs == {"Firm_1", "Firm_NumPara", "Firm_NumLonely"}, refs


@test
def test_w1_numbering_accepts_real_word_tabs_and_mc_ignorable(tmp: Path):
    """tabs/tab, ind hanging/firstLine, russianUpper, hint и numIdMacAtCleanup сохраняются."""
    ex = russian_numbering_docx(tmp / "real-numbering.docx")
    meta = DR.build(ex, tmp / "out-real-numbering", "opz",
                    base=reference_base(tmp / "base.docx"),
                    version="1.231.19", generated_at=DATE)
    assert DR.validate(meta["path"]) == []
    with zipfile.ZipFile(meta["path"]) as z:
        numbering = z.read("word/numbering.xml").decode("utf-8")
    for marker in ("<w:tabs>", "<w:tab ", "w:hanging=", "w:firstLine=",
                   "russianUpper", "russianLower", "numIdMacAtCleanup",
                   'w:hint="default"', 'w:asciiTheme="minorHAnsi"',
                   'w:tplc="1"', 'w:tentative="1"', "<w:caps"):
        assert marker in numbering, marker
    assert "mc:Ignorable" not in numbering


@test
def test_w1_numbering_rejects_unsafe_mc_ignorable(tmp: Path):
    """mc:Ignorable с необъявленным или небезопасным префиксом не проходит."""
    base = reference_base(tmp / "base.docx")
    cases = (
        ("undeclared", "w14 w99", ""),
        ("vml", "w14 v", 'xmlns:v="urn:schemas-microsoft-com:vml"'),
    )
    for name, mc, extra_ns in cases:
        ex = bad_mc_numbering_docx(tmp / (name + ".docx"), mc=mc, extra_ns=extra_ns)
        try:
            DR.build(ex, tmp / ("out-" + name), "opz", base=base,
                     version="1.231.19", generated_at=DATE)
        except DR.DocxRefused as e:
            assert "mc:Ignorable" in str(e), str(e)
        else:
            raise AssertionError("%s: небезопасный mc:Ignorable принят" % name)


@test
def test_w1_reference_body_and_table_roles_are_separate(tmp: Path):
    """Таблица не становится телом, тело берёт самый частый абзацный стиль."""
    meta = _build(tmp)
    assert meta["mapping"]["BodyText"]["source"] == "a0", meta["mapping"]
    assert meta["mapping"]["Table"]["source"] == "TableFirm", meta["mapping"]
    styles = _styles(meta["path"])
    assert styles["Table"].get(W + "type") == "table"
    assert _chain(styles, "BodyText")[:2] == ["BodyText", "Firm_a0"], _chain(styles, "BodyText")
    assert _chain(styles, "Table")[:2] == ["Table", "Firm_TableFirm"], _chain(styles, "Table")


@test
def test_w1_reference_title_styles_keep_original_ids(tmp: Path):
    """Стили титульного фрагмента сохраняют исходные id, а не получают Firm_."""
    meta = _build(tmp)
    styles = _styles(meta["path"])
    assert meta["title_styles"] == ["TitleChar", "TitleFirm"], meta["title_styles"]
    assert "TitleFirm" in styles and "TitleChar" in styles
    assert "Firm_TitleFirm" not in styles and "Firm_TitleChar" not in styles
    assert _chain(styles, "TitleFirm") == ["TitleFirm", "Normal"]


@test
def test_w1_reference_docdefaults_and_fonttable_are_replaced(tmp: Path):
    """docDefaults берутся из примера, fontTable содержит только используемые шрифты."""
    meta = _build(tmp)
    with zipfile.ZipFile(meta["path"]) as z:
        styles = z.read("word/styles.xml").decode("utf-8")
        fonts = z.read("word/fontTable.xml").decode("utf-8")
    assert 'w:ascii="Arial"' in styles and "Calibri" not in styles
    assert "latentStyles" not in styles
    assert 'w:name="Arial"' in fonts and "Calibri" not in fonts


@test
def test_w1_reference_core_props_and_links(tmp: Path):
    """docProps/core.xml, core-properties relationship и content type согласованы."""
    meta = _build(tmp)
    parts = _parts(meta["path"])
    core = parts["docProps/core.xml"].decode("utf-8")
    assert "Aurora Studio" in core and "<cp:revision>1</cp:revision>" in core, core
    assert DATE in core and core.count("dcterms:W3CDTF") == 2, core
    assert any(typ.endswith("/core-properties") and target == "docProps/core.xml"
               for _rid, typ, target in _rels(meta["path"])), _rels(meta["path"])
    ct = parts["[Content_Types].xml"].decode("utf-8")
    assert 'PartName="/docProps/core.xml"' in ct and "macroEnabled" not in ct, ct


@test
def test_w1_reference_cuts_unknown_settings_from_real_word(tmp: Path):
    """embedSystemFonts, stylePaneFormatFilter и useFELayout вырезаются, полезные настройки остаются."""
    ex = settings_reference_docx(tmp / "settings.docx")
    meta = DR.build(ex, tmp / "out-settings", "opz",
                    base=reference_base(tmp / "base.docx"),
                    version="1.231.19", generated_at=DATE)
    assert DR.validate(meta["path"]) == []
    with zipfile.ZipFile(meta["path"]) as z:
        settings = z.read("word/settings.xml").decode("utf-8")
    for marker in ("embedSystemFonts", "stylePaneFormatFilter", "useFELayout",
                   "Ignorable", "trackRevisions", "rsids"):
        assert marker not in settings, marker
    assert "compatSetting" in settings and "defaultTabStop" in settings, settings


@test
def test_w1_word_styles_keep_bidi_tblind_and_theme_color(tmp: Path):
    """bidi, tblInd, themeColor и взаимные link из Word сохраняются, rsid удаляется."""
    ex = word_styles_docx(tmp / "word-styles.docx")
    meta = DR.build(ex, tmp / "out-word-styles", "opz",
                    base=reference_base(tmp / "base.docx"),
                    version="1.231.19", generated_at=DATE)
    assert DR.validate(meta["path"]) == []
    styles = _styles(meta["path"])
    body = meta["mapping"]["BodyText"]["style"]
    body_el = styles[body]
    assert _child(_child(body_el, "rPr"), "lang").get(W + "bidi") == "ar-SA"
    assert _child(_child(body_el, "rPr"), "color").get(W + "themeColor") == "text1"
    table = styles[meta["mapping"]["Table"]["style"]]
    tbl_pr = _child(table, "tblPr")
    assert _child(tbl_pr, "tblInd").get(W + "w") == "120"
    assert _child(_child(tbl_pr, "tblCellMar"), "top").get(W + "w") == "20"
    assert _numpr(styles[meta["style_map"]["NoIlvl"]]) == ("0", "2")
    link_a = styles[meta["style_map"]["LinkA"]]
    link_b = styles[meta["style_map"]["LinkB"]]
    assert _child(link_a, "link").get(W + "val") == link_b.get(W + "styleId")
    assert _child(link_b, "link").get(W + "val") == link_a.get(W + "styleId")
    with zipfile.ZipFile(meta["path"]) as z:
        styles_xml = z.read("word/styles.xml").decode("utf-8")
        doc = ET.fromstring(z.read("word/document.xml"))
    assert "w:rsid" not in styles_xml
    body_el = doc.find(W + "body")
    sects = [el for el in body_el.iter() if el.tag == W + "sectPr"]
    assert len(sects) == 1, len(sects)
    assert _child(sects[0], "cols").get(W + "space") == "1440"
    assert meta["mapping"]["Caption"]["reason"] == "fallback"
    assert "Caption" in styles and "Title" in styles and "TOCHeading" in styles
    latent = ('<w:styles xmlns:w="%s"><w:latentStyles w:defLockedState="0" '
              'w:defUIPriority="99" w:count="1"><w:lsdException w:name="Normal" '
              'w:qFormat="1"/></w:latentStyles></w:styles>' % DR.W_NS).encode("utf-8")
    assert DR.validate_xml(latent, "styles") == []


@test
def test_w1_reference_sectpr_is_sanitized_and_has_a4(tmp: Path):
    """sectPr остаётся один, A4, без ссылок на колонтитулы и с полями примера."""
    meta = _build(tmp)
    with zipfile.ZipFile(meta["path"]) as z:
        doc = ET.fromstring(z.read("word/document.xml"))
    body = doc.find(W + "body")
    sects = [el for el in body.iter() if el.tag == W + "sectPr"]
    assert len(sects) == 1, len(sects)
    sect = sects[0]
    size = _child(sect, "pgSz")
    assert (size.get(W + "w"), size.get(W + "h")) == ("11906", "16838")
    assert _child(sect, "pgMar") is not None
    assert not [el for el in sect if el.tag in (W + "headerReference", W + "footerReference")]


@test
def test_w1_reference_is_deterministic_and_date_changes_hash(tmp: Path):
    """Одинаковая дата даёт тот же sha256, другая дата меняет пакет и core."""
    meta1 = _build(tmp, "a")
    meta2 = _build(tmp, "a")
    meta3 = _build(tmp, "b", date="2026-11-25T00:00:00Z")
    assert meta1["reference_sha"] == meta2["reference_sha"], meta1["reference_sha"]
    assert meta1["reference_sha"] != meta3["reference_sha"]
    with zipfile.ZipFile(meta1["path"]) as z:
        assert z.getinfo("word/document.xml").date_time == (2026, 11, 24, 0, 0, 0)
    with zipfile.ZipFile(meta3["path"]) as z:
        assert z.getinfo("word/document.xml").date_time == (2026, 11, 25, 0, 0, 0)
        assert b"2026-11-25T00:00:00Z" in z.read("docProps/core.xml")


@test
def test_w1_reference_is_hashseed_independent(tmp: Path):
    """Два процесса с разным PYTHONHASHSEED дают один и тот же sha256."""
    ex = reference_docx(tmp / "ex.docx")
    base = reference_base(tmp / "base.docx")
    hashes = []
    for seed in ("1", "2"):
        out = tmp / ("seed-" + seed)
        cmd = [sys.executable, str(SCRIPTS / "docx_reference.py"), "--example", ex,
               "--base", base, "--out", str(out), "--kind", "opz", "--date",
               "2026-11-24", "--json"]
        cp = subprocess.run(cmd, capture_output=True, text=True, cwd=str(KIT),
                            env={**os.environ, "PYTHONHASHSEED": seed})
        assert cp.returncode == 0, cp.stderr
        hashes.append(json.loads(cp.stdout)["reference_sha"])
    assert hashes[0] == hashes[1], hashes


@test
def test_w1_reference_large_example_is_read_but_output_stays_small(tmp: Path):
    """Пример 11.1 МБ читается, а готовый эталон остаётся меньше 5 МБ."""
    ex = large_reference_docx(tmp / "big.docx")
    assert Path(ex).stat().st_size > 5_000_000
    meta = DR.build(ex, tmp / "out-big", "opz", base=reference_base(tmp / "base.docx"),
                    version="1.231.19", generated_at=DATE)
    assert Path(meta["path"]).stat().st_size <= DR.MAX_REFERENCE_BYTES
    assert DR.validate(meta["path"]) == []
    assert not [name for name in docx_part_names(meta["path"]) if "media/" in name]


@test
def test_w1_reference_rejects_docm_dotm_and_large_template(tmp: Path):
    """Макроформаты и шаблон больше 5 МБ отвергаются с человекочитаемой причиной."""
    src = reference_docx(tmp / "src.docx")
    base = reference_base(tmp / "base.docx")
    for suffix in (".docm", ".dotm"):
        target = tmp / ("bad" + suffix)
        shutil.copyfile(src, target)
        try:
            DR.build(target, tmp / "out", "opz", base=base, generated_at=DATE)
        except DR.DocxRefused as e:
            assert suffix in str(e), str(e)
        else:
            raise AssertionError("формат %s принят" % suffix)
    big_template = large_reference_docx(tmp / "big-template.docx")
    try:
        DR.build(src, tmp / "out2", "opz", template=big_template, generated_at=DATE)
    except DR.DocxRefused as e:
        assert "5 МБ" in str(e), str(e)
    else:
        raise AssertionError("шаблон больше 5 МБ принят")


@test
def test_w1_reference_rejects_broken_zip_xml_and_unknown_style(tmp: Path):
    """Битый zip, битый XML и неизвестный стиль не дают traceback наружу."""
    base = reference_base(tmp / "base.docx")
    cases = [(broken_zip(tmp / "broken.docx"), "zip"),
             (broken_xml_docx(tmp / "broken-xml.docx"), "XML"),
             (unknown_style_docx(tmp / "unknown.docx"), "unknownFirm"),
             (bad_sectpr_docx(tmp / "bad-sect.docx"), "pgSz")]
    for path, marker in cases:
        try:
            DR.build(path, tmp / ("out-" + Path(path).stem), "opz", base=base,
                     generated_at=DATE)
        except DR.DocxRefused as e:
            assert marker.lower() in str(e).lower() or marker == "pgSz", str(e)
        else:
            raise AssertionError("%s принят" % Path(path).name)


@test
def test_w1_allowlist_accepts_page_break_and_rejects_unknown_xml(tmp: Path):
    """Allowlist v1: w:br type=page разрешён, неизвестное и чужой type блокируются."""
    ns = DR.W_NS
    def doc(body):
        return ('<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>'
                % (ns, body)).encode("utf-8")
    assert DR.validate_xml(doc('<w:p><w:r><w:br w:type="page"/></w:r></w:p>'), "document") == []
    assert DR.validate_xml(doc('<w:p><w:r><w:br/></w:r></w:p>'), "document") == []
    assert DR.validate_xml(doc('<w:p><w:r><w:br w:type="textWrapping"/></w:r></w:p>'),
                           "document")
    assert DR.validate_xml(doc('<w:p><w:r><w:t>ok</w:t></w:r></w:p><w:evil/>'),
                           "document")
    assert DR.validate_xml(doc('<w:p><w:pPr><w:outlineLvl w:val="9"/></w:pPr></w:p>'),
                           "document")
    assert DR.validate_xml(doc('<w:p><w:pPr><w:numPr><w:numId w:val="0"/></w:numPr></w:pPr></w:p>'),
                           "document")
    assert DR.validate_xml(doc('<w:p><w:pPr><w:pStyle w:val="Missing"/></w:pPr></w:p>'),
                           "document", {"Heading1"})


@test
def test_w1_allowlist_rejects_bad_sectpr(tmp: Path):
    """sectPr с чужим размером страницы и ссылкой на колонтитул не проходит."""
    bad = ('<w:sectPr xmlns:w="%s" xmlns:r="%s"><w:pgSz w:w="100" w:h="100"/>'
           '<w:headerReference w:type="default" r:id="rIdX"/></w:sectPr>'
           % (DR.W_NS, DR.R_NS)).encode("utf-8")
    errors = DR.validate_xml(bad, "sectPr")
    assert any("pgSz" in e for e in errors), errors
    assert any("headerReference" in e for e in errors), errors
    ok = ('<w:sectPr xmlns:w="%s"><w:pgSz w:w="11906" w:h="16838"/>'
          '<w:cols w:space="31680"/></w:sectPr>' % DR.W_NS).encode("utf-8")
    assert DR.validate_xml(ok, "sectPr") == []
    too_wide = ('<w:sectPr xmlns:w="%s"><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:cols w:space="40000"/></w:sectPr>' % DR.W_NS).encode("utf-8")
    assert any("space" in e for e in DR.validate_xml(too_wide, "sectPr"))


@test
def test_w1_reference_cli_json_status_and_missing(tmp: Path):
    """CLI собирает эталон, отдаёт JSON, затем ready/missing через --status."""
    ex = reference_docx(tmp / "ex.docx")
    base = reference_base(tmp / "base.docx")
    out = tmp / "cli"
    cmd = [sys.executable, str(SCRIPTS / "docx_reference.py"),
           "--example", ex, "--base", base, "--out", str(out), "--kind", "opz",
           "--version", "1.231.19", "--date", "2026-11-24", "--json"]
    cp = subprocess.run(cmd, capture_output=True, text=True, cwd=str(KIT))
    assert cp.returncode == 0, cp.stderr
    data = json.loads(cp.stdout)
    assert data["status"] == "ready", data
    status_cmd = [sys.executable, str(SCRIPTS / "docx_reference.py"), "--status",
                  "--out", str(out), "--kind", "opz", "--version", "1.231.19",
                  "--date", "2026-11-24", "--json"]
    cp = subprocess.run(status_cmd, capture_output=True, text=True, cwd=str(KIT))
    assert cp.returncode == 0 and json.loads(cp.stdout)["status"] == "ready", cp.stdout
    missing_cmd = [sys.executable, str(SCRIPTS / "docx_reference.py"), "--status",
                   "--out", str(tmp / "missing"), "--kind", "opz", "--json"]
    cp = subprocess.run(missing_cmd, capture_output=True, text=True, cwd=str(KIT))
    assert cp.returncode == 0 and json.loads(cp.stdout)["status"] == "missing", cp.stdout


@test
def test_w1_reference_pandoc_sees_heading1_and_firm_chain(tmp: Path):
    """Функциональная проба: pandoc сохраняет Heading1, outlineLvl=0 и Firm_1."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return
    meta = _build(tmp)
    md = tmp / "probe.md"
    md.write_text("# Заголовок\n\nТекст.\n", encoding="utf-8")
    docx = tmp / "probe.docx"
    cp = subprocess.run([pandoc, "--from=markdown", "--to=docx",
                         "--reference-doc", meta["path"], "-o", str(docx), str(md)],
                        capture_output=True, text=True,
                        env={**os.environ, "TZ": "UTC"})
    assert cp.returncode == 0, cp.stderr
    with zipfile.ZipFile(docx) as z:
        styles_root = ET.fromstring(z.read("word/styles.xml"))
        doc = ET.fromstring(z.read("word/document.xml"))
    styles = {el.get(W + "styleId"): el for el in styles_root if el.tag == W + "style"}
    used = [el.get(W + "val") for el in doc.iter() if el.tag == W + "pStyle"]
    assert "Heading1" in used, used
    assert _chain(styles, "Heading1") == ["Heading1", "Firm_1"]
    assert _outline(styles["Heading1"]) == "0"


@test
def test_w1_reference_status_ready_and_stale(tmp: Path):
    """status называет ready, затем stale по примеру, версии, файлу и метаданным."""
    meta = _build(tmp, "status")
    ex = tmp / "status.docx"
    assert DR.status(str(tmp / "out-status"), "opz", str(ex), None, "1.231.19",
                     DATE).get("status") == "ready"
    Path(ex).write_bytes(Path(ex).read_bytes() + b"x")
    assert DR.status(str(tmp / "out-status"), "opz", str(ex), None, "1.231.19",
                     DATE).get("status") == "stale"
    assert "пример" in DR.status(str(tmp / "out-status"), "opz", str(ex), None,
                                 "1.231.19", DATE)["reason"]
    meta2 = _build(tmp, "status2")
    assert DR.status(str(tmp / "out-status2"), "opz", str(tmp / "status2.docx"),
                     None, "9.9.9").get("status") == "stale"
    replace_part(meta2["path"], "docProps/core.xml", "Aurora Studio", "Other")
    assert DR.status(str(tmp / "out-status2"), "opz").get("status") == "stale"
    meta3 = _build(tmp, "status3")
    (tmp / "out-status3" / "opz.meta.json").write_text("{", encoding="utf-8")
    assert DR.status(str(tmp / "out-status3"), "opz").get("status") == "stale"
    assert "метаданные" in DR.status(str(tmp / "out-status3"), "opz")["reason"]


@test
def test_w1_validate_rejects_tampered_core_and_extra_part(tmp: Path):
    """validate ловит подмену core и лишнюю часть, даже если package читается."""
    meta = _build(tmp)
    bad = tmp / "bad.docx"
    shutil.copyfile(meta["path"], bad)
    replace_part(bad, "docProps/core.xml", "Aurora Studio", "Other")
    assert any("creator" in e for e in DR.validate(str(bad))), DR.validate(str(bad))
    with zipfile.ZipFile(bad, "a") as z:
        z.writestr("word/media/image1.png", b"not png")
    assert any("media" in e or "разрешена" in e for e in DR.validate(str(bad))), DR.validate(str(bad))


@test
def test_w1_reference_rejects_bad_kind(tmp: Path):
    """Вид с путём, пустой или слишком длинный не принимается."""
    ex = reference_docx(tmp / "ex.docx")
    base = reference_base(tmp / "base.docx")
    for kind in ("../x", "", "A", "x" * 60):
        try:
            DR.build(ex, tmp / "out", kind, base=base, generated_at=DATE)
        except DR.DocxRefused:
            pass
        else:
            raise AssertionError("вид %r принят" % kind)
